import asyncio
import io
import os
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import completion, price, student, team
from cryptography.exceptions import InvalidTag

from gateway.admission import admit
from gateway.billing import settle
from gateway.models import BudgetPeriod, Call, Price
from scripts.backup import decrypt, encrypt
from scripts.mock_upstream import received


async def test_zero_budget_and_routing_disabled(admin):
    await price(admin)
    t = await team(admin)
    s, _ = await student(admin, t, budget=0)
    assert (await completion(admin, s["key"])).status_code == 402
    assert not received

    await admin.post(
        f"/admin/api/budgets/membership/{s['membership_id']}", json={"amount": 1000000, "reason": "enable"}
    )
    await admin.patch(f"/admin/api/teams/{t['id']}", json={"models": []})
    # Empty list is rejected by schema. Explicit team stop must still prevent dispatch.
    await admin.patch(f"/admin/api/teams/{t['id']}", json={"active": False})
    assert (await completion(admin, s["key"])).status_code == 403
    assert not received


async def test_overlapping_admissions_and_settlements_avoid_fk_deadlock(admin, app):
    await price(admin)
    s, _ = await student(admin, await team(admin), concurrency=100, rpm=10000)
    async with asyncio.timeout(10):
        results = await asyncio.gather(*(completion(admin, s["key"], stream=True) for _ in range(30)))
    assert all(r.status_code == 200 for r in results), [r.status_code for r in results]
    rows = (await admin.get("/admin/api/requests")).json()
    assert len(rows) == 30 and all(r["status"] == "settled" and r["cost"] == 188 for r in rows)
    assert app.state.metrics["database_failures"] == 0


async def test_rate_and_concurrency_survive_redis_flush(admin, app):
    await price(admin)
    s, _ = await student(admin, await team(admin, rpm=1, concurrency=1))
    body = {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}]}
    call, _, _ = await admit(
        app, "Bearer " + s["key"], dict(body), str(uuid.uuid4()), "::1", time.perf_counter()
    )
    await app.state.redis.flushdb()
    assert (await completion(admin, s["key"])).status_code == 429
    await settle(app.state.db, call.id, manual_cost=0, reason="not_sent")
    await app.state.redis.flushdb()
    assert (await completion(admin, s["key"])).status_code == 429
    assert not received


async def test_upstream_error_and_timeout_do_not_retry_or_refund(admin, app):
    await price(admin)
    s, _ = await student(admin, await team(admin), concurrency=100)
    assert (await completion(admin, s["key"], "error")).status_code == 502
    assert received == ["A-credential"]
    await app.state.http.aclose()
    calls = []

    async def timeout(request):
        calls.append(1)
        raise httpx.ReadTimeout("mock timeout", request=request)

    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    assert (await completion(admin, s["key"])).status_code == 502
    assert len(calls) == 1
    rows = (await admin.get("/admin/api/requests")).json()
    assert len(rows) == 2 and all(x["status"] == "pending" and x["cost"] is None for x in rows)


async def test_concurrent_settlement_and_actual_over_reserve(admin, app):
    await price(admin)
    s, _ = await student(admin, await team(admin))
    call, _, _ = await admit(
        app,
        "Bearer " + s["key"],
        {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}]},
        str(uuid.uuid4()),
        "::1",
        time.perf_counter(),
    )
    costs = await asyncio.gather(
        *(settle(app.state.db, call.id, manual_cost=99999999, reason="fault_injection") for _ in range(10))
    )
    assert sum(costs) == 1
    assert (await completion(admin, s["key"])).status_code == 402
    async with app.state.db() as db:
        p = await db.get(BudgetPeriod, call.member_period_id)
        assert p.spent == 99999999 and p.reserved == 0 and p.limit - p.spent < 0


async def test_old_price_and_period_are_frozen(admin, app):
    await price(admin)
    s, _ = await student(admin, await team(admin))
    call, _, _ = await admit(
        app,
        "Bearer " + s["key"],
        {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}]},
        str(uuid.uuid4()),
        "::1",
        time.perf_counter(),
    )
    async with app.state.db() as db, db.begin():
        p = await db.get(BudgetPeriod, call.member_period_id)
        p.starts_at = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
        p.ends_at = p.starts_at + timedelta(days=31)
        old = await db.get(Price, call.price_id)
        old.ends_at = datetime.now(timezone.utc)
    await settle(
        app.state.db,
        call.id,
        usage={"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 40},
    )
    async with app.state.db() as db:
        c = await db.get(Call, call.id)
        p = await db.get(BudgetPeriod, c.member_period_id)
        assert c.price_id == call.price_id and c.cost == 188
        assert p.starts_at.month == 9 and p.spent == 188


def test_authenticated_backup_rejects_tampering_and_truncation():
    plain, key = os.urandom(2100000), os.urandom(32)
    encrypted = io.BytesIO()
    encrypt(io.BytesIO(plain), encrypted, key)
    restored = io.BytesIO()
    decrypt(io.BytesIO(encrypted.getvalue()), restored, key)
    assert restored.getvalue() == plain
    wire = encrypted.getvalue()
    # Always alter a bit; replacing a random byte with 'x' occasionally changes nothing.
    for data in (wire[:-20], wire[:-1] + bytes([wire[-1] ^ 1])):
        with pytest.raises((ValueError, InvalidTag)):
            decrypt(io.BytesIO(data), io.BytesIO(), key)


async def test_production_configuration_requires_real_https_and_secrets(settings, monkeypatch):
    from gateway.config import Settings

    monkeypatch.setenv("GATEWAY_ENV", "production")
    monkeypatch.setenv("PUBLIC_URL", "http://127.0.0.1:18081")
    with pytest.raises(RuntimeError):
        Settings.from_env()
    app_settings = replace(settings, encryption_key="invalid")
    from gateway.main import create_app

    with pytest.raises(ValueError):
        create_app(app_settings)
