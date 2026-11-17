"""Management reports must distinguish admissions, denials, delivery and billing."""

import asyncio
import time
import uuid

import httpx
import pytest
from conftest import PASSWORD, completion, price, student, team
from playwright.async_api import expect
from support import artifact

from gateway.admission import admit
from gateway.billing import recover, settle
from scripts.mock_upstream import received


@pytest.mark.parametrize("owner", ["team", "membership"])
async def test_budget_rejections_are_durable_counted_and_attributed(admin, app, owner):
    await price(admin)
    t = await team(admin, budget=0 if owner == "team" else 100000000)
    s, _ = await student(admin, t, budget=0 if owner == "membership" else 10000000)
    responses = await asyncio.gather(*(completion(admin, s["key"], "private-prompt") for _ in range(4)))
    assert all(r.status_code == 402 for r in responses)
    app.state.metrics["rejections"].clear()  # Persisted reports do not depend on memory counters.
    payload = (await admin.get("/admin/api/rejections", params={"team_id": t["id"], "limit": 2})).json()
    assert payload["total"] == 4 and len(payload["rows"]) == 2
    rows = (await admin.get("/admin/api/rejections")).json()["rows"]
    assert {r["id"] for r in rows} == {r.headers["x-request-id"] for r in responses}
    assert all(
        r["limit_scope"] == owner and r["key_id"] == s["key_id"] and r["model"] == "lab-chat" for r in rows
    )
    assert "private-prompt" not in str(rows) and s["key"] not in str(rows)
    assert (await admin.get("/admin/api/requests")).json() == []
    events = (await admin.get("/admin/api/events")).json()
    event = next(e for e in events if e["kind"] == "budget_exhausted")
    assert event["occurrences"] == 4 and event["last_seen_at"] >= event["created_at"]
    assert (
        await admin.post(f"/admin/api/events/{event['id']}/resolve", json={"reason": "已通知成员"})
    ).status_code == 200
    assert (await completion(admin, s["key"])).status_code == 402
    refreshed = (await admin.get("/admin/api/events", params={"resolved": "false"})).json()
    assert refreshed[0]["occurrences"] == 5 and refreshed[0]["resolved_by"] is None
    audit = (await admin.get("/admin/api/audit")).json()
    assert any(a["action"] == "event_resolve" and a["detail"]["reason"] == "已通知成员" for a in audit)
    assert not received


async def test_invalid_and_revoked_keys_validation_and_large_bodies_are_recorded(admin, app):
    await price(admin)
    s, _ = await student(admin, await team(admin))
    assert (await completion(admin, "unknown-secret", "private-content")).status_code == 401
    assert (await completion(admin, s["key"], max_tokens=0)).status_code == 422
    assert (await completion(admin, s["key"], "x" * app.state.settings.max_chat_body)).status_code == 413
    assert (await admin.post(f"/admin/api/keys/{s['key_id']}/revoke", json={})).status_code == 200
    assert (await completion(admin, s["key"])).status_code == 401
    data = (await admin.get("/admin/api/rejections")).json()
    assert data["total"] == 4
    assert sum(r["key_id"] == s["key_id"] for r in data["rows"]) == 3
    assert {r["http_status"] for r in data["rows"]} == {401, 413, 422}
    assert "unknown-secret" not in str(data) and "private-content" not in str(data)
    assert (await admin.get("/admin/api/rejections", params={"start": "invalid"})).status_code == 400
    assert (await admin.get("/admin/api/rejections", params={"offset": -1})).status_code == 400


async def test_outcomes_survive_manual_reconciliation_and_statistics_are_disjoint(admin, app):
    await price(admin)
    s, _ = await student(admin, await team(admin), concurrency=100)
    assert (await completion(admin, s["key"])).status_code == 200
    assert (await completion(admin, s["key"], "no-usage")).status_code == 200
    assert (await completion(admin, s["key"], "error")).status_code == 502
    active, _, _ = await admit(
        app,
        "Bearer " + s["key"],
        {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}]},
        str(uuid.uuid4()),
        "127.0.0.1",
        time.perf_counter(),
    )
    rows = (await admin.get("/admin/api/requests")).json()
    pending_success = next(r for r in rows if r["outcome"] == "success" and r["status"] == "pending")
    failed = next(r for r in rows if r["outcome"] == "upstream_failure")
    stats = (await admin.get("/admin/api/statistics")).json()[0]
    assert (stats["calls"], stats["success"], stats["failures"], stats["in_progress"], stats["pending"]) == (
        4,
        2,
        1,
        1,
        2,
    )
    events = (await admin.get("/admin/api/events")).json()
    alert = next(
        e for e in events if e["kind"] == "pending_usage" and e["detail"]["request_id"] == failed["id"]
    )
    assert (
        await admin.post(f"/admin/api/events/{alert['id']}/resolve", json={"reason": "忽略"})
    ).status_code == 409
    assert (
        await admin.post(
            f"/admin/api/requests/{failed['id']}/reconcile", json={"cost": 0, "reason": "已核对未收费"}
        )
    ).status_code == 200
    after = (await admin.get("/admin/api/requests", params={"request_id": failed["id"]})).json()[0]
    assert after["outcome"] == "upstream_failure" and after["status"] == "settled"
    assert after["outcome_reason"] == "upstream_error" and after["ended_at"] == failed["ended_at"]
    alert = next(e for e in (await admin.get("/admin/api/events")).json() if e["id"] == alert["id"])
    assert alert["resolved"] and alert["resolved_by"] == "admin" and alert["resolution"] == "已核对未收费"
    assert (await admin.get("/admin/api/rejections")).json()["total"] == 0
    await settle(app.state.db, active.id, manual_cost=0, reason="not_sent")
    await admin.post(
        f"/admin/api/requests/{pending_success['id']}/reconcile", json={"cost": 188, "reason": "核对账单"}
    )
    stats = (await admin.get("/admin/api/statistics")).json()[0]
    assert (
        stats["success"] == 2 and stats["failures"] == 1 and stats["not_sent"] == 1 and stats["pending"] == 0
    )
    exported = await admin.get("/admin/api/export.csv")
    assert "outcome" in exported.text and "cache_hit_tokens" in exported.text


async def test_transport_failure_and_recovery_remain_unknown(admin, app):
    await price(admin)
    s, _ = await student(admin, await team(admin), concurrency=100)

    async def timeout(request):
        raise httpx.ReadTimeout("private-upstream-error", request=request)

    await app.state.http.aclose()
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    assert (await completion(admin, s["key"])).status_code == 502
    call, _, _ = await admit(
        app,
        "Bearer " + s["key"],
        {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}]},
        str(uuid.uuid4()),
        "127.0.0.1",
        time.perf_counter(),
    )
    await recover(app.state.db)
    rows = (await admin.get("/admin/api/requests")).json()
    assert all(r["outcome"] == "unknown" and r["status"] == "pending" for r in rows)
    stats = (await admin.get("/admin/api/statistics")).json()[0]
    assert stats["unknown"] == 2 and stats["failures"] == 0
    assert any(r["id"] == call.id for r in rows)


async def test_rate_and_concurrency_scope_and_team_audit(admin, app):
    await price(admin)
    t = await team(admin, concurrency=1, rpm=1)
    s, _ = await student(admin, t, concurrency=1, rpm=1)
    call, _, _ = await admit(
        app,
        "Bearer " + s["key"],
        {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}]},
        str(uuid.uuid4()),
        "127.0.0.1",
        time.perf_counter(),
    )
    await app.state.redis.flushdb()
    assert (await completion(admin, s["key"])).status_code == 429
    await settle(app.state.db, call.id, manual_cost=0, reason="not_sent")
    await app.state.redis.flushdb()
    assert (await completion(admin, s["key"])).status_code == 429
    assert (await completion(admin, s["key"])).status_code == 429
    rows = (await admin.get("/admin/api/rejections")).json()["rows"]
    assert len(rows) == 3 and all(r["limit_scope"] == "both" for r in rows)
    assert {r["reason"] for r in rows} == {"rate_limit", "concurrency_limit"}
    assert (await admin.patch(f"/admin/api/teams/{t['id']}", json={"rpm": 5})).status_code == 200
    audit = next(r for r in (await admin.get("/admin/api/audit")).json() if r["action"] == "team_update")
    assert audit["detail"] == {"before": {"rpm": 1}, "after": {"rpm": 5}}


async def test_browser_alert_to_reconciliation_and_rejections(admin, live_server, browser):
    await price(admin)
    t = await team(admin)
    s, _ = await student(admin, t, concurrency=100)
    assert (await completion(admin, s["key"], "no-usage")).status_code == 200
    assert (await completion(admin, s["key"], model="forbidden")).status_code == 403
    page = await browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(live_server + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator('#nav button[data-view="records"]').click()
    await page.locator('#record-tabs button[data-view="events"]').click()
    row = page.get_by_role("row").filter(has_text="费用待核对")
    await row.get_by_role("button", name="查看请求 / 核对").click()
    await expect(page.locator("#view-title")).to_have_text("记录与追溯 · 请求记录")
    await page.get_by_role("button", name="人工核对").click()
    await page.locator('#modal [name="cost"]').fill("0.000188")
    await page.locator('#modal [name="reason"]').fill("模拟账单核对")
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal")).not_to_be_visible()
    await expect(page.locator("#content table")).to_contain_text("已结算")
    await expect(page.locator("#content table")).to_contain_text("成功")
    await page.screenshot(path=str(artifact("records-requests.png")), full_page=True)
    await page.locator('#record-tabs button[data-view="events"]').click()
    await page.locator('#content [name="resolved"]').select_option("true")
    await expect(page.locator("#content table")).to_contain_text("模拟账单核对")
    await page.locator('#record-tabs button[data-view="rejections"]').click()
    await expect(page.locator("#content table")).to_contain_text("模型无权限")
    await page.locator('#record-tabs button[data-view="audit"]').click()
    await expect(page.locator("#content table")).to_contain_text("reconcile")
    assert not errors
    await page.close()


@pytest.mark.parametrize("mode", ["complete", "missing_usage", "truncated", "error_after_usage"])
async def test_stream_outcome_is_independent_of_usage(admin, app, mode):
    import json

    await price(admin)
    s, _ = await student(admin, await team(admin))

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            payload = {
                "id": "mock-stream",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "mock-text",
                "choices": [{"index": 0, "delta": {"content": "mock"}, "finish_reason": None}],
            }
            yield ("data: " + json.dumps(payload) + "\n\n").encode()
            if mode != "truncated":
                payload["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                if mode != "missing_usage":
                    payload["usage"] = {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "prompt_cache_hit_tokens": 40,
                    }
                yield ("data: " + json.dumps(payload) + "\n\n").encode()
            if mode == "error_after_usage":
                raise httpx.ReadError("private-stream-error")
            if mode != "truncated":
                yield b"data: [DONE]\n\n"

    await app.state.http.aclose()
    app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Stream()
            )
        )
    )
    response = await completion(admin, s["key"], stream=True)
    assert response.status_code == 200 and "private-stream-error" not in response.text
    row = (await admin.get("/admin/api/requests")).json()[0]
    expected = {
        "complete": ("success", "settled"),
        "missing_usage": ("success", "pending"),
        "truncated": ("unknown", "pending"),
        "error_after_usage": ("upstream_failure", "settled"),
    }
    assert (row["outcome"], row["status"]) == expected[mode]
    if mode == "error_after_usage":
        assert row["cost"] == 188 and row["outcome_reason"] == "stream_interrupted"
    assert (await admin.get("/admin/api/rejections")).json()["total"] == 0


async def test_recording_failure_preserves_original_denial(admin, app, monkeypatch, caplog):
    from gateway import observability

    def unavailable(*args, **kwargs):
        raise RuntimeError("private-database-details")

    monkeypatch.setattr(observability, "insert", unavailable)
    response = await completion(admin, "unknown-secret")
    assert response.status_code == 401
    assert app.state.metrics["rejection_record_failures"] == 1
    assert "rejection_record_failed" in caplog.text
    assert "private-database-details" not in caplog.text and "unknown-secret" not in caplog.text


async def test_upgrade_of_populated_database_preserves_accounting(admin, app):
    import os
    import subprocess
    import sys

    from sqlalchemy import select

    from gateway.db import database
    from gateway.models import BudgetPeriod, Call, Event

    await price(admin)
    s, _ = await student(admin, await team(admin))
    assert (await completion(admin, s["key"])).status_code == 200
    async with app.state.db() as db:
        before = await db.scalar(select(Call))
        budget_before = [
            (p.id, p.spent, p.reserved)
            for p in (await db.scalars(select(BudgetPeriod).order_by(BudgetPeriod.id))).all()
        ]
    # conftest creates and drops a uniquely named disposable database.
    url = app.state.settings.database_url
    assert url.rsplit("/", 1)[1].startswith("lab_gateway_test_")
    env = {**os.environ, "DATABASE_URL": url}
    await app.state.engine.dispose()
    for direction, revision in (("downgrade", "b7e092dc41af"), ("upgrade", "head")):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", direction, revision],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    engine, factory = database(url)
    try:
        async with factory() as db:
            after = await db.get(Call, before.id)
            assert (after.cost, after.usage, after.status) == (before.cost, before.usage, before.status)
            assert after.outcome == "unknown"
            budget_after = [
                (p.id, p.spent, p.reserved)
                for p in (await db.scalars(select(BudgetPeriod).order_by(BudgetPeriod.id))).all()
            ]
            assert budget_after == budget_before
            events = (await db.scalars(select(Event))).all()
            assert events and all(e.occurrences == 1 and e.last_seen_at == e.created_at for e in events)
    finally:
        await engine.dispose()
