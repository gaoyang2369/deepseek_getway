import logging
from contextlib import asynccontextmanager

import httpx
from cryptography.fernet import Fernet
from redis.asyncio import Redis
from sqlalchemy import func, select, text

from . import admin, chat, student
from .accounts import backfill_demo_accounts
from .admission import AdmissionBatcher
from .billing import recover
from .config import Settings
from .db import database
from .http import Guard, SafeFastAPI, install_error_handlers
from .models import Price, now
from .pages import install_pages
from .security import fail

log = logging.getLogger("lab_gateway")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)


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

    install_error_handlers(app, settings, factory)
    install_pages(app, settings)

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

    app.include_router(chat.router)
    app.include_router(admin.auth_router)
    app.include_router(admin.router)
    app.include_router(student.router)
    return app
