import asyncio
import json
import logging
import time

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import APIError, APIStatusError, AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .admission import admit
from .billing import parse_usage, settle
from .keys import authenticate
from .models import Price, now
from .schemas import Chat
from .security import client_ip, fail
from .streaming import with_heartbeats

log = logging.getLogger("lab_gateway")

router = APIRouter()


@router.get("/v1/models")
async def models(request: Request):
    app = request.app
    settings, factory = app.state.settings, app.state.db
    if request.query_params:
        fail(400, "query_parameters_not_supported")
    async with factory() as db:
        key, member, _, team = await authenticate(db, request.headers.get("authorization"), settings)
        allowed = set(key.models) & set(member.models) & set(team.models)
        priced = set(
            (
                await db.scalars(
                    select(Price.model).where(
                        Price.starts_at <= now(), (Price.ends_at.is_(None)) | (Price.ends_at > now())
                    )
                )
            ).all()
        )
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": 0,
                "owned_by": "lab",
                "max_tokens": team.max_output,
                "max_output_tokens": team.max_output,
            }
            for m in sorted(allowed & priced)
        ],
    }


async def finalize(app, call_id, team_id, **kwargs):
    settings, factory = app.state.settings, app.state.db
    # Shield client cancellation, then bounded retries. If DB remains down, the
    # already committed reservation survives and restart recovery makes it visible.
    for attempt in range(3):
        try:
            async with app.state.admission.turn(team_id).enter():
                return await settle(factory, call_id, thresholds=settings.budget_thresholds, **kwargs)
        except SQLAlchemyError:
            app.state.metrics["settlement_failures"] += 1
            log.error("settlement_deferred request_id=%s", call_id)
            if attempt < 2:
                await asyncio.sleep(0.1 * (attempt + 1))


async def finish_safely(app, call_id, **kwargs):
    task = asyncio.create_task(finalize(app, call_id, **kwargs))
    # Starlette disconnects use AnyIO cancellation scopes. Shield that scope
    # as well as edge-triggered asyncio cancellation so finalization can commit.
    with anyio.CancelScope(shield=True):
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task


@router.post("/v1/chat/completions")
async def chat(data: Chat, request: Request):
    app = request.app
    settings = app.state.settings
    started = request.state.request_started
    if request.query_params:
        fail(400, "query_parameters_not_supported")
    body = data.model_dump(exclude_none=True)
    if len(json.dumps(body, ensure_ascii=False).encode()) > settings.max_input_bytes:
        fail(413, "input_too_large")
    if "max_completion_tokens" in body:
        if "max_tokens" in body:
            fail(400, "ambiguous_output_limit")
        body["max_tokens"] = body.pop("max_completion_tokens")
    is_stream = body.pop("stream", False)
    body.pop("stream_options", None)
    # All routing and credentials are selected by server-side key binding.
    call, encrypted, upstream_model = await admit(
        app,
        request.headers.get("authorization"),
        body,
        request.state.request_id,
        client_ip(request, settings),
        started,
    )

    async def finish_call(**kwargs):
        await finish_safely(app, call.id, team_id=call.team_id, admission_ms=call.admission_ms, **kwargs)

    try:
        upstream_key = app.state.cipher.decrypt(encrypted.encode()).decode()
    except Exception:
        await finish_call(manual_cost=0, reason="not_sent")
        fail(503, "upstream_configuration_error")
    client = AsyncOpenAI(
        api_key=upstream_key,
        base_url=settings.upstream_url,
        http_client=app.state.http,
        max_retries=0,
        timeout=settings.timeout,
    )
    body["model"] = upstream_model
    # SDK extra_body is populated only with our validated DeepSeek parameters.
    extra = {k: body.pop(k) for k in ("thinking", "reasoning_effort") if k in body}

    async def wait_disconnect():
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return

    upstream_task = asyncio.create_task(
        client.chat.completions.create(
            **body,
            extra_body=extra or None,
            stream=is_stream,
            **({"stream_options": {"include_usage": True}} if is_stream else {}),
        )
    )
    disconnect_task = asyncio.create_task(wait_disconnect())
    try:
        done, _ = await asyncio.wait((upstream_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED)
        if upstream_task in done:
            response = upstream_task.result()
        else:
            upstream_task.cancel()
            await asyncio.gather(upstream_task, return_exceptions=True)
            await finish_call(reason="client_disconnected")
            fail(499, "client_disconnected")
    except (APIError, httpx.HTTPError) as exc:
        app.state.metrics["upstream_errors"] += 1
        status = exc.status_code if isinstance(exc, APIStatusError) else None
        await finish_call(
            reason="upstream_error",
            upstream_status=status,
            total_ms=round((time.perf_counter() - started) * 1000),
        )
        fail(502, "upstream_error_pending_reconciliation")
    except asyncio.CancelledError:
        upstream_task.cancel()
        await asyncio.gather(upstream_task, return_exceptions=True)
        await finish_call(reason="client_disconnected")
        raise
    except HTTPException:
        raise
    except Exception:
        await finish_call(reason="upstream_protocol_error")
        fail(502, "upstream_protocol_error")
    finally:
        disconnect_task.cancel()
        await asyncio.gather(disconnect_task, return_exceptions=True)
    if not is_stream:
        result = response.model_dump(exclude_none=True)
        await finish_call(
            usage=result.get("usage"),
            upstream_id=result.get("id"),
            upstream_status=200,
            total_ms=round((time.perf_counter() - started) * 1000),
        )
        return JSONResponse(result)

    async def generate():
        usage, upstream_id, first = None, None, None
        reason = "missing_usage"
        try:
            async for chunk in with_heartbeats(response, settings.stream_heartbeat_seconds):
                if chunk is None:
                    yield ": keep-alive\n\n"
                    continue
                payload = chunk.model_dump(exclude_none=True)
                upstream_id = payload.get("id", upstream_id)
                final_layout = not payload.get("choices") or any(
                    choice.get("finish_reason") is not None for choice in payload.get("choices", [])
                )
                if final_layout and parse_usage(payload.get("usage")) is not None:
                    usage = payload["usage"]
                if first is None:
                    for choice in payload.get("choices", []):
                        delta = choice.get("delta", {})
                        if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
                            first = round((time.perf_counter() - started) * 1000)
                            break
                yield "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            reason = "client_disconnected"
            raise
        except Exception:
            reason = "stream_interrupted"
            app.state.metrics["upstream_errors"] += 1
            yield "data: " + json.dumps({"error": {"message": reason, "request_id": call.id}}) + "\n\n"
        finally:
            try:
                await response.close()
            finally:
                await finish_call(
                    usage=usage,
                    reason=None if usage else reason,
                    upstream_id=upstream_id,
                    upstream_status=200,
                    first_delta_ms=first,
                    total_ms=round((time.perf_counter() - started) * 1000),
                )

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
