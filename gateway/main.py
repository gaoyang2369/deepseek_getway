import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from openai import APIError, APIStatusError, AsyncOpenAI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import admin, student, student_admin
from .accounts import backfill_demo_accounts
from .admission import AdmissionBatcher
from .billing import admit, authenticate, event, parse_usage, recover, settle
from .config import Settings
from .db import database
from .models import Price, now, uid
from .schemas import Chat, chat_validation_details
from .security import client_ip, fail
from .streaming import with_heartbeats

log = logging.getLogger("lab_gateway")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)


class SafeFastAPI(FastAPI):
    async def __call__(self, scope, receive, send):
        started = False

        async def tracked(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await super().__call__(scope, receive, tracked)
        except Exception as exc:
            if scope["type"] != "http":
                raise
            # Starlette re-raises handled 500s after sending them. Suppress the
            # server's default traceback, which can expose third-party response bodies.
            request_id = scope.get("state", {}).get("request_id", "unknown")
            log.error("request_failed request_id=%s exception_type=%s", request_id, type(exc).__name__)
            if not started:
                await JSONResponse({"error": {"message": "internal_error", "request_id": request_id}}, 500)(
                    scope, receive, send
                )


class Guard:
    """Bound memory before JSON parsing; attach server request IDs and safe headers."""

    def __init__(self, app, settings, metrics):
        self.app, self.settings, self.metrics = app, settings, metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uid()
        scope.setdefault("state", {})["request_id"] = request_id
        scope["state"]["request_started"] = time.perf_counter()
        self.metrics["requests"] += 1

        async def safe_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                        ),
                    ]
                )
                message["headers"] = headers
            await send(message)

        if scope["method"] in ("POST", "PATCH", "PUT"):
            body = bytearray()
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                body.extend(msg.get("body", b""))
                if len(body) > self.settings.max_body:
                    return await JSONResponse(
                        {
                            "error": {
                                "message": "request_too_large",
                                "type": "gateway_error",
                                "request_id": request_id,
                            }
                        },
                        413,
                    )(scope, receive, safe_send)
                if not msg.get("more_body"):
                    break
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            return await self.app(scope, replay, safe_send)
        return await self.app(scope, receive, safe_send)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    engine, factory = database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        # Single process only. PostgreSQL prevents a second worker from marking live
        # requests in the first worker as interrupted. No TTL safety assumptions.
        async with engine.connect() as owner:
            acquired = await owner.scalar(text("SELECT pg_try_advisory_lock(70711013)"))
            await owner.commit()
            if not acquired:
                raise RuntimeError("Only one gateway process is supported; stop the other instance first")
            app.state.redis = Redis.from_url(
                settings.redis_url, socket_connect_timeout=3, socket_timeout=3, decode_responses=True
            )
            app.state.http = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.timeout, connect=10),
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=30),
                follow_redirects=False,
                trust_env=False,
            )
            try:
                await app.state.redis.ping()
                await recover(factory)
                await backfill_demo_accounts(factory, settings)
                yield
            finally:
                await app.state.admission.close()
                await app.state.http.aclose()
                await app.state.redis.aclose()
                await owner.execute(text("SELECT pg_advisory_unlock(70711013)"))
                await owner.commit()
        await engine.dispose()

    app = SafeFastAPI(
        title="实验室大模型 API 网关",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings, app.state.db, app.state.engine = settings, factory, engine
    app.state.admission = AdmissionBatcher(lambda: app.state.db)
    app.state.cipher = Fernet(settings.encryption_key.encode())
    app.state.metrics = {
        "requests": 0,
        "rejections": {},
        "upstream_errors": 0,
        "database_failures": 0,
        "settlement_failures": 0,
    }
    app.add_middleware(Guard, settings=settings, metrics=app.state.metrics)

    def error(request, status, message, details=None):
        counts = app.state.metrics["rejections"]
        counts[message] = counts.get(message, 0) + 1
        payload = {"message": message, "type": "gateway_error", "request_id": request.state.request_id}
        if details is not None:
            payload["details"] = details
        return JSONResponse(
            {"error": payload},
            status,
            headers={"X-Request-ID": request.state.request_id},
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if exc.status_code in (402, 429):
            try:
                async with factory() as db, db.begin():
                    key, member, _, team = await authenticate(
                        db, request.headers.get("authorization"), settings
                    )
                    await event(db, str(exc.detail), team.id, member.id)
            except Exception:
                pass
        return error(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default response echoes inputs, including passwords and API keys.
        if request.url.path == "/v1/chat/completions":
            details = chat_validation_details(exc.errors())
            log.warning(
                "request_validation_failed request_id=%s details=%s", request.state.request_id, details
            )
            return error(request, 422, "invalid_request_parameters", details)
        return error(request, 422, "invalid_request_parameters")

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return error(request, 409, "record_conflict")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        app.state.metrics["database_failures"] += 1
        return error(request, 503, "database_unavailable")

    @app.exception_handler(RedisError)
    async def redis_error(request, exc):
        return error(request, 503, "rate_service_unavailable")

    @app.exception_handler(Exception)
    async def unhandled(request, exc):
        # Only type and request ID are safe. Third-party exception messages can contain credentials.
        log.error(
            "internal_error request_id=%s exception_type=%s", request.state.request_id, type(exc).__name__
        )
        return error(request, 500, "internal_error")

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        await app.state.redis.ping()
        async with factory() as db:
            active_prices = await db.scalar(
                select(func.count())
                .select_from(Price)
                .where(Price.starts_at <= now(), (Price.ends_at.is_(None)) | (Price.ends_at > now()))
            )
            if not active_prices:
                fail(503, "price_not_configured")
        return {"status": "ready", "upstream": "not_probed"}

    @app.get("/v1/models")
    async def models(request: Request):
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

    async def finalize(call_id, team_id=None, **kwargs):
        # Shield client cancellation, then bounded retries. If DB remains down, the
        # already committed reservation survives and restart recovery makes it visible.
        for attempt in range(3):
            try:
                if team_id is not None:
                    async with app.state.admission.turn(team_id).enter():
                        return await settle(factory, call_id, thresholds=settings.budget_thresholds, **kwargs)
                return await settle(factory, call_id, thresholds=settings.budget_thresholds, **kwargs)
            except SQLAlchemyError:
                app.state.metrics["settlement_failures"] += 1
                log.error("settlement_deferred request_id=%s", call_id)
                if attempt < 2:
                    await asyncio.sleep(0.1 * (attempt + 1))

    async def finish_safely(call_id, **kwargs):
        task = asyncio.create_task(finalize(call_id, **kwargs))
        # Starlette disconnects use AnyIO cancellation scopes. Shield that scope
        # as well as edge-triggered asyncio cancellation so finalization can commit.
        with anyio.CancelScope(shield=True):
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task

    @app.post("/v1/chat/completions")
    async def chat(data: Chat, request: Request):
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
            await finish_safely(call.id, team_id=call.team_id, admission_ms=call.admission_ms, **kwargs)

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
            done, _ = await asyncio.wait(
                (upstream_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED
            )
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
                            if (
                                delta.get("content")
                                or delta.get("reasoning_content")
                                or delta.get("tool_calls")
                            ):
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

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    app.include_router(admin.auth_router)
    app.include_router(admin.router)
    app.include_router(student.router)
    app.include_router(student_admin.router)

    @app.get("/")
    async def home():
        return RedirectResponse("/student", status_code=302)

    @app.get("/student/api/config")
    async def student_config():
        return {"demo": not settings.production, "support_email": settings.support_email}

    @app.get("/student")
    @app.get("/student/")
    async def student_page():
        return FileResponse(Path(__file__).parent / "static" / "student.html")

    @app.get("/student/app.js")
    async def student_script():
        return FileResponse(Path(__file__).parent / "static" / "student.js", media_type="text/javascript")

    @app.get("/student/guide.js")
    async def student_guide_script():
        return FileResponse(
            Path(__file__).parent / "static" / "student-guide.js", media_type="text/javascript"
        )

    @app.get("/student/style.css")
    async def student_style():
        return FileResponse(Path(__file__).parent / "static" / "style.css", media_type="text/css")

    @app.get("/admin")
    @app.get("/admin/")
    async def admin_page():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/admin/app.js")
    async def admin_script():
        return FileResponse(Path(__file__).parent / "static" / "app.js", media_type="text/javascript")

    @app.get("/admin/students.js")
    async def admin_students_script():
        return FileResponse(
            Path(__file__).parent / "static" / "admin-students.js", media_type="text/javascript"
        )

    @app.get("/admin/style.css")
    async def admin_css():
        return FileResponse(Path(__file__).parent / "static" / "style.css", media_type="text/css")

    return app
