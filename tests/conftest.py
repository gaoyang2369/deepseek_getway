import os
import subprocess
from dataclasses import replace

import httpx
import psycopg
import pytest
from argon2 import PasswordHasher
from sqlalchemy import delete

from gateway.config import Settings
from gateway.db import database
from gateway.main import create_app
from gateway.models import Base
from scripts.mock_upstream import app as mock_app
from scripts.mock_upstream import received

PASSWORD = "test-only-password-1234"


@pytest.fixture(scope="session")
def settings():
    base = Settings.from_env()
    assert "127.0.0.1:18543/lab_gateway" in base.database_url, (
        "Run via scripts/local.py against isolated dependencies"
    )
    connection_url = base.database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(connection_url, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='lab_gateway_test'").fetchone():
            conn.execute("CREATE DATABASE lab_gateway_test")
    url = base.database_url.rsplit("/", 1)[0] + "/lab_gateway_test"
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([".venv/bin/alembic", "upgrade", "head"], env=env, check=True)
    return replace(
        base,
        database_url=url,
        redis_url=base.redis_url.rsplit("/", 1)[0] + "/15",
        production=False,
        admin_hash=PasswordHasher().hash(PASSWORD),
        timeout=0.5,
    )


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
