import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import completion, price, student, team
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from gateway.admission import admit
from gateway.billing import parse_usage, period_bounds, recover, settle
from gateway.models import Audit, BudgetPeriod, Call, Credential, Ledger, Membership, PersonalKey
from gateway.security import client_ip
from scripts.mock_upstream import received


async def test_A01_A02_A03_binding_and_statistics(admin, app):
    await price(admin)
    a, b = await team(admin, "A"), await team(admin, "B")
    s1, _ = await student(admin, a, "001")
    s2, _ = await student(admin, a, "002")
    s3, _ = await student(admin, b, "003")
    for s in (s1, s2, s3):
        r = await completion(admin, s["key"])
        assert r.status_code == 200, r.text
    assert received == ["A-credential", "A-credential", "B-credential"]
    assert (await admin.get("/v1/models", headers={"Authorization": "Bearer " + s1["key"]})).json()["data"][
        0
    ]["id"] == "lab-chat"
    for field in ("team_id", "channel", "api_base", "user", "api_key"):
        assert (await completion(admin, s1["key"], **{field: b["id"]})).status_code == 422
    rows = (await admin.get("/admin/api/statistics")).json()
    assert len(rows) == 3 and sum(r["cost"] for r in rows) == 564
    assert sum(r["input_tokens"] for r in rows) == 300
    assert sum(r["output_tokens"] for r in rows) == 60
    async with app.state.db() as db:
        assert await db.scalar(select(func.count()).select_from(Credential)) == 2


async def test_A04_A05_A06_key_lifecycle(admin, app):
    await price(admin)
    t = await team(admin)
    s, _ = await student(admin, t, kind="service", owner="test-owner")
    assert (await completion(admin, s["key"])).status_code == 200
    assert (
        await admin.post(f"/admin/api/teams/{t['id']}/credential", json={"upstream_key": "MOCK-A-new"})
    ).status_code == 200
    assert (await completion(admin, s["key"])).status_code == 200
    assert received == ["A-credential", "A-new"]
    new = (
        await admin.post(
            f"/admin/api/memberships/{s['membership_id']}/keys",
            json={"operation_id": str(uuid.uuid4()), "models": ["lab-chat"], "revoke_key_id": s["key_id"]},
        )
    ).json()
    assert (await completion(admin, s["key"])).status_code == 401
    assert (await completion(admin, new["key"])).status_code == 200
    async with app.state.db() as db:
        periods = (
            await db.scalars(select(BudgetPeriod).where(BudgetPeriod.owner_type == "membership"))
        ).all()
        assert len(periods) == 1 and periods[0].spent == 188 * 3
        k = await db.get(PersonalKey, new["key_id"])
        k.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    assert (await completion(admin, new["key"])).status_code == 401
    assert (await admin.patch(f"/admin/api/teams/{t['id']}", json={"active": False})).status_code == 200


async def test_A07_protocol_tools_and_input_limits(admin):
    await price(admin)
    t = await team(admin)
    s, _ = await student(admin, t)
    assert (await completion(admin, s["key"], model="forbidden")).status_code == 403
    for path in ("/v1/responses", "/v1/embeddings", "/v1/images/generations", "/api/token"):
        assert (await admin.post(path, json={})).status_code == 404
    assert (await completion(admin, s["key"], tools=[{"type": "web_search"}])).status_code == 422
    assert (
        await completion(admin, s["key"], content=[{"type": "image_url", "image_url": {"url": "http://x"}}])
    ).status_code == 422
    assert (await completion(admin, s["key"], max_tokens=0)).status_code == 422
    assert (await completion(admin, s["key"], "x" * 300000)).status_code == 413
    assert len(received) == 0


@pytest.mark.parametrize("owner_type", ["team", "membership"])
async def test_A08_atomic_reservations(admin, app, owner_type):
    await price(admin)
    t = await team(admin)
    s, _ = await student(admin, t)
    owner_id = t["id"] if owner_type == "team" else s["membership_id"]
    # Determine the exact conservative reservation through real admission, then
    # configure one-and-a-half reservations and race 30 independent transactions.
    body = {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}]}
    first, _, _ = await admit(
        app, "Bearer " + s["key"], dict(body), str(uuid.uuid4()), "127.0.0.1", time.perf_counter()
    )
    await settle(app.state.db, first.id, manual_cost=0, reason="not_sent_test")
    r = await admin.post(
        f"/admin/api/budgets/{owner_type}/{owner_id}",
        json={"amount": first.reservation * 3 // 2, "reason": "race test"},
    )
    assert r.status_code == 200, r.text

    async def reserve():
        try:
            return await admit(
                app, "Bearer " + s["key"], dict(body), str(uuid.uuid4()), "127.0.0.1", time.perf_counter()
            )
        except Exception as e:
            return e

    results = await asyncio.gather(*(reserve() for _ in range(30)))
    successes = [x for x in results if isinstance(x, tuple)]
    assert len(successes) == 1, [type(x).__name__ for x in results]
    async with app.state.db() as db:
        p = await db.scalar(
            select(BudgetPeriod).where(
                BudgetPeriod.owner_type == owner_type, BudgetPeriod.owner_id == owner_id
            )
        )
        assert p.reserved == first.reservation and p.spent == 0 and p.reserved <= p.limit


def test_A09_beijing_calendar():
    start, end = period_bounds(datetime(2026, 11, 30, 16, tzinfo=timezone.utc), "month")
    assert start == datetime(2026, 11, 30, 16, tzinfo=timezone.utc)
    assert end == datetime(2026, 12, 31, 16, tzinfo=timezone.utc)
    leap = period_bounds(datetime(2028, 2, 15, tzinfo=timezone.utc), "month")
    assert leap[1] - leap[0] == timedelta(days=29)
    day = period_bounds(datetime(2026, 11, 13, 15, 59, 59, tzinfo=timezone.utc), "day")
    assert day[1] == datetime(2026, 11, 13, 16, tzinfo=timezone.utc)


async def test_A10_A11_usage_and_sse_layouts(admin, app):
    await price(admin)
    t = await team(admin)
    s, _ = await student(admin, t)
    for mode in ("hello", "empty-choices"):
        r = await completion(admin, s["key"], mode, stream=True)
        assert r.status_code == 200, r.text
        assert "Hello" in r.text and r.text.endswith("data: [DONE]\n\n")
    calls = (await admin.get("/admin/api/requests")).json()
    assert all(
        c["cost"] == 188 and c["usage"]["completion_tokens_details"]["reasoning_tokens"] == 10 for c in calls
    )
    assert all(c["first_delta_ms"] is not None for c in calls)
    assert parse_usage(None) is None
    assert parse_usage({"prompt_tokens": 0, "completion_tokens": 0}) is not None
    assert parse_usage({"prompt_tokens": 3, "completion_tokens": 2, "prompt_cache_hit_tokens": 4}) is None


async def test_A12_A13_pending_and_idempotent_reconciliation(admin, app):
    await price(admin)
    t = await team(admin)
    s, _ = await student(admin, t)
    assert (await completion(admin, s["key"], "no-usage")).status_code == 200
    c = (await admin.get("/admin/api/requests")).json()[0]
    assert c["status"] == "pending" and c["cost"] is None
    p = (await admin.get("/admin/api/budgets")).json()
    assert all(x["reserved"] == c["reservation"] and x["spent"] == 0 for x in p)
    r = await admin.post(
        f"/admin/api/requests/{c['id']}/reconcile", json={"cost": 200, "reason": "模拟账单核实为 200 微元"}
    )
    assert r.status_code == 200
    assert await settle(app.state.db, c["id"], manual_cost=999) is False
    async with app.state.db() as db:
        assert (await db.get(Call, c["id"])).cost == 200
        assert (
            await db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "reconcile")) == 1
        )
        assert await db.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "settle")) == 2


async def test_A14_recovery_preserves_reservations(admin, app):
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
    await recover(app.state.db)
    await recover(app.state.db)
    async with app.state.db() as db:
        c = await db.get(Call, call.id)
        p = await db.get(BudgetPeriod, c.member_period_id)
        assert c.status == "pending" and p.reserved == c.reservation and p.spent == 0


async def test_A15_redis_and_database_fail_closed(admin, app, monkeypatch):
    await price(admin)
    s, _ = await student(admin, await team(admin))
    from redis.exceptions import ConnectionError

    async def broken(*args, **kwargs):
        raise ConnectionError("test failure")

    monkeypatch.setattr(app.state.redis, "eval", broken)
    assert (await completion(admin, s["key"])).status_code == 503
    assert not received
    monkeypatch.undo()
    original = app.state.db

    class Broken:
        async def __aenter__(self):
            raise OperationalError("test", {}, Exception("offline"))

        async def __aexit__(self, *args):
            pass

        def begin(self):
            return self

    app.state.db = lambda: Broken()
    assert (await completion(admin, s["key"])).status_code == 503
    assert not received
    app.state.db = original


async def test_A16_spoofed_forwarding_header(admin, app):
    await price(admin)
    s, _ = await student(admin, await team(admin))
    admin.headers["X-Forwarded-For"] = "1.2.3.4"
    assert (await completion(admin, s["key"])).status_code == 200
    row = (await admin.get("/admin/api/requests")).json()[0]
    assert row["ip"] == "127.0.0.1"
    from dataclasses import replace

    from starlette.requests import Request

    req = Request(
        {"type": "http", "client": ("::1", 1), "headers": [(b"x-forwarded-for", b"2001:db8::9, 10.0.0.1")]}
    )
    assert (
        client_ip(req, replace(app.state.settings, trusted_proxies=("::1/128", "10.0.0.0/8")))
        == "2001:db8::9"
    )


async def test_A17_A18_admin_security_and_secret_redaction(client, app):
    assert (await client.get("/admin/api/keys")).status_code == 401
    bad = await client.post(
        "/admin/api/login", json={"username": "admin", "password": "SENSITIVE", "unexpected": "MOCK-SECRET"}
    )
    assert "SENSITIVE" not in bad.text and "MOCK-SECRET" not in bad.text
    from conftest import PASSWORD

    result = await client.post("/admin/api/login", json={"username": "admin", "password": PASSWORD})
    csrf = result.json()["csrf_token"]
    assert "HttpOnly" in result.headers["set-cookie"] and "SameSite=strict" in result.headers["set-cookie"]
    assert (await client.post("/admin/api/teams", json={})).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    await price(client)
    t = await team(client)
    s, _ = await student(client, t)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as outsider:
        assert (
            await outsider.get("/admin/api/teams", headers={"Authorization": "Bearer " + s["key"]})
        ).status_code == 401
    for path in ("/keys", "/teams", "/audit", "/export.csv"):
        text = (await client.get("/admin/api" + path)).text
        assert s["key"] not in text and "MOCK-A-credential" not in text
    async with app.state.db() as db:
        key = await db.get(PersonalKey, s["key_id"])
        cred = await db.scalar(select(Credential))
        assert key.digest != s["key"] and cred.ciphertext != "MOCK-A-credential"


async def test_A19_csv_filters_and_formula_injection(admin):
    await price(admin)
    s, _ = await student(admin, await team(admin), name="=HYPERLINK(1)")
    await completion(admin, s["key"], "PRIVATE_PROMPT_MARKER")
    csv = await admin.get("/admin/api/export.csv")
    assert "'=HYPERLINK(1)" in csv.text and "PRIVATE_PROMPT_MARKER" not in csv.text
    assert (await admin.get("/admin/api/requests?start=2099-01-01")).json() == []
    assert (await admin.get("/admin/api/requests?start=bad")).status_code == 400


async def test_A22_enrollment_idempotency(admin, app):
    await price(admin)
    t = await team(admin)
    s, data = await student(admin, t)
    results = await asyncio.gather(*(admin.post("/admin/api/enroll", json=data) for _ in range(10)))
    assert all(r.status_code == 201 and r.json()["key"] is None and r.json()["replayed"] for r in results)
    changed = {**data, "budget": 9999}
    assert (await admin.post("/admin/api/enroll", json=changed)).status_code == 409
    async with app.state.db() as db:
        assert await db.scalar(select(func.count()).select_from(Membership)) == 1
        assert await db.scalar(select(func.count()).select_from(PersonalKey)) == 1


async def test_client_function_call_round_trip(admin):
    await price(admin)
    s, _ = await student(admin, await team(admin))
    r = await completion(
        admin,
        s["key"],
        "function-call",
        tools=[
            {"type": "function", "function": {"name": "local_calculator", "parameters": {"type": "object"}}}
        ],
    )
    assert r.status_code == 200
    message = r.json()["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "local_calculator"
    r = await admin.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + s["key"]},
        json={
            "model": "lab-chat",
            "messages": [
                {"role": "user", "content": "function-call"},
                message,
                {"role": "tool", "tool_call_id": "mock-function-1", "content": "2"},
            ],
        },
    )
    assert r.status_code == 200
