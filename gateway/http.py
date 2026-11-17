import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .models import uid
from .observability import record_rejection
from .schemas import chat_validation_details

log = logging.getLogger("lab_gateway")


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

    def __init__(self, app, settings, metrics, factory):
        self.app, self.settings, self.metrics, self.factory = app, settings, metrics, factory

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uid()
        scope.setdefault("state", {})["request_id"] = request_id
        scope["state"]["request_started"] = time.perf_counter()
        self.metrics["requests"] += 1

        async def safe_send(message):
            if message["type"] == "http.response.start":
                if (
                    scope["path"] == "/v1/chat/completions"
                    and scope["method"] == "POST"
                    and message["status"] >= 400
                    and not scope["state"].get("admitted")
                ):
                    await record_rejection(Request(scope), self.settings, self.factory, message["status"])
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
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
            limit = (
                self.settings.max_chat_body
                if scope["path"] == "/v1/chat/completions"
                else self.settings.max_body
            )
            body = bytearray()
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                body.extend(msg.get("body", b""))
                if len(body) > limit:
                    scope["state"]["rejection_reason"] = "request_too_large"
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


def install_error_handlers(app, settings, factory):
    def error(request, status, message, details=None):
        request.state.rejection_reason = message
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
        if request.url.path == "/admin/api/login":
            # Only fixed reason codes; never log credentials, bodies or cookies.
            reason = (
                exc.detail
                if exc.detail in ("invalid_origin", "invalid_login", "login_rate_limit")
                else "login_rejected"
            )
            log.warning(
                "admin_login_failed request_id=%s status=%s reason=%s",
                request.state.request_id,
                exc.status_code,
                reason,
            )
        if isinstance(exc.detail, dict) and "message" in exc.detail:
            return error(request, exc.status_code, exc.detail["message"], exc.detail.get("details"))
        return error(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default response echoes inputs, including passwords and API keys.
        if request.url.path == "/v1/chat/completions":
            if isinstance(exc.body, dict):
                model = exc.body.get("model")
                if isinstance(model, str) and 0 < len(model) <= 128:
                    request.state.requested_model = model
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
        response = error(request, 500, "internal_error")
        # Starlette's generic 500 handler sits outside Guard's response wrapper.
        if (
            request.url.path == "/v1/chat/completions"
            and request.method == "POST"
            and not getattr(request.state, "admitted", False)
        ):
            await record_rejection(request, settings, factory, 500)
        return response
