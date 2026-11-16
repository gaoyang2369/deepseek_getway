import asyncio
import os
import secrets
import socket
import subprocess
import sys
import uuid
from dataclasses import replace

import httpx
import psycopg
import pytest
import uvicorn
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from playwright.async_api import async_playwright
from psycopg import sql
from sqlalchemy import delete
from support import require_isolated_dependencies

from gateway.config import Settings
from gateway.db import database
from gateway.main import create_app
from gateway.models import Base
from scripts.mock_upstream import app as mock_app
from scripts.mock_upstream import received

PASSWORD = "test-only-password-1234"


@pytest.fixture
async def live_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app.state.settings = replace(app.state.settings, public_url=base)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("Test HTTP server exited before startup")
                await asyncio.sleep(0.01)
        yield base
    finally:
        server.should_exit = True
        await serving
        sock.close()


@pytest.fixture
async def browser():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.getenv("GATEWAY_CHROMIUM_PATH"), args=["--no-sandbox"]
        )
        try:
            yield browser
        finally:
            await browser.close()


@pytest.fixture(scope="session")
def settings():
    require_isolated_dependencies()
    name = "lab_gateway_test_" + uuid.uuid4().hex
    connection_url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(connection_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/" + name
    try:
        env = {**os.environ, "DATABASE_URL": url}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True)
        yield Settings(
            database_url=url,
            redis_url=os.environ["REDIS_URL"].rsplit("/", 1)[0] + "/15",
            encryption_key=Fernet.generate_key().decode(),
            hmac_key=secrets.token_urlsafe(32),
            production=False,
            public_url="http://gateway",
            upstream_url="http://mock:8080/v1",
            admin_hash=PasswordHasher().hash(PASSWORD),
            timeout=0.5,
        )
    finally:
        with psycopg.connect(connection_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
async def app(settings):
    engine, db = database(settings.database_url)
    async with db() as session, session.begin():
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(delete(table))
    await engine.dispose()
    received.clear()
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await app.state.redis.flushdb()
        await app.state.http.aclose()
        app.state.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock_app), base_url="http://mock"
        )
        yield app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as c:
        yield c


@pytest.fixture
async def admin(client):
    response = await client.post("/admin/api/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


async def team(admin, code="A", **kwargs):
    r = await admin.post(
        "/admin/api/teams",
        json={
            "name": code,
            "code": code,
            "upstream_key": f"MOCK-{code}-credential",
            "models": ["lab-chat"],
            "budget": 100000000,
            "rpm": 10000,
            "concurrency": 100,
            **kwargs,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def price(admin):
    r = await admin.post(
        "/admin/api/prices",
        json={
            "model": "lab-chat",
            "upstream_model": "mock-text",
            "version": "mock-v1",
            "input_miss": 2000000,
            "input_hit": 200000,
            "output": 3000000,
            "estimated": True,
            "note": "MOCK ONLY: 2/0.2/3 CNY per million",
        },
    )
    assert r.status_code == 201, r.text


async def student(admin, t, number="001", **kwargs):
    import uuid

    data = {
        "operation_id": str(uuid.uuid4()),
        "name": "同学" + number,
        "student_number": number,
        "team_id": t["id"],
        "budget": 10000000,
        "models": ["lab-chat"],
        **kwargs,
    }
    r = await admin.post("/admin/api/enroll", json=data)
    assert r.status_code == 201, r.text
    result = r.json()
    if data.get("kind", "student") == "student":
        async with httpx.AsyncClient(transport=admin._transport, base_url="http://gateway") as portal:
            password = result.get("temporary_password") or (
                number if result["account_created"] else "student-test-passphrase-2026"
            )
            login = await portal.post("/student/api/login", json={"username": number, "password": password})
            assert login.status_code == 200, login.text
            portal.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            if login.json()["must_change_password"]:
                changed = await portal.post(
                    "/student/api/password",
                    json={"current_password": password, "new_password": "student-test-passphrase-2026"},
                )
                assert changed.status_code == 200, changed.text
                portal.headers["X-CSRF-Token"] = changed.json()["csrf_token"]
            issued = await portal.post(
                "/student/api/keys",
                json={"operation_id": str(uuid.uuid4()), "membership_id": result["membership_id"]},
            )
            assert issued.status_code == 201, issued.text
            result.update({k: issued.json()[k] for k in ("key", "key_id", "prefix")})
    return result, data


async def completion(client, key, content="hello", **kwargs):
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + key},
        json={"model": "lab-chat", "messages": [{"role": "user", "content": content}], **kwargs},
    )
