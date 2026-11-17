"""Regression tests for batching, cancellation and streaming under real DB transactions."""

import asyncio
import time
import uuid
from datetime import datetime

import pytest
from conftest import completion, price, student, team
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from gateway.admission import admit
from gateway.billing import period_bounds
from gateway.models import BudgetPeriod, Call, Ledger
from gateway.security import digest
from gateway.streaming import with_heartbeats
from scripts.mock_upstream import received


@pytest.mark.parametrize("period", ["day", "month"])
@pytest.mark.parametrize("timestamp", ["2028-02-28T16:00:00+00:00", "2028-02-29T16:00:00+00:00"])
async def test_sql_admission_and_python_budget_calendar_agree(admin, app, period, timestamp):
    await price(admin)
    person, _ = await student(admin, await team(admin, period=period), period=period)
    at = datetime.fromisoformat(timestamp)
    async with app.state.db() as db:
        result = await db.scalar(
            text("SELECT gateway_admit(:key, 'lab-chat', 64, 1000, :id, '127.0.0.1', 0, 0, :at)"),
            {"key": digest(person["key"], app.state.settings.hmac_key), "id": str(uuid.uuid4()), "at": at},
        )
        assert "error" not in result, result
        for field in ("team_period_id", "member_period_id"):
            budget = await db.get(BudgetPeriod, result["call"][field])
            assert (budget.starts_at, budget.ends_at) == period_bounds(at, period)
        # Roll back synthetic future admissions; no upstream request is sent.
        await db.rollback()


def admission(app, key):
    return admit(
        app,
        "Bearer " + key,
        {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}]},
        str(uuid.uuid4()),
        "127.0.0.1",
        time.perf_counter(),
    )


async def test_mixed_batch_keeps_individual_budget_rejections(admin, app):
    await price(admin)
    group = await team(admin)
    people = [await student(admin, group, f"mixed-{i}", budget=0 if i % 2 else 1000000) for i in range(10)]
    responses = await asyncio.gather(*(completion(admin, person[0]["key"]) for person in people))
    assert [r.status_code for r in responses] == [200 if i % 2 == 0 else 402 for i in range(10)]
    async with app.state.db() as db:
        assert await db.scalar(select(func.count()).select_from(Call)) == 5
        assert await db.scalar(select(func.count()).select_from(Ledger)) == 10
        assert await db.scalar(select(func.sum(BudgetPeriod.reserved))) == 0
    assert len(received) == 5


async def test_cancel_after_batch_commit_refunds_only_never_dispatched_request(admin, app, monkeypatch):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    committed, release = asyncio.Event(), asyncio.Event()
    original = app.state.admission._execute

    async def delayed(jobs):
        result = await original(jobs)
        committed.set()
        await release.wait()
        return result

    monkeypatch.setattr(app.state.admission, "_execute", delayed)
    task = asyncio.create_task(admission(app, person["key"]))
    await asyncio.wait_for(committed.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await app.state.admission.close()
    async with app.state.db() as db:
        call = await db.scalar(select(Call))
        assert call.status == "settled" and call.cost == 0 and call.reason == "not_sent"
        assert await db.scalar(select(func.sum(BudgetPeriod.reserved))) == 0
    assert not received


async def test_failed_batch_commit_rolls_back_every_admission(admin, app, monkeypatch):
    import json

    await price(admin)
    person, _ = await student(admin, await team(admin), concurrency=100, rpm=10000)

    async def fail_commit(jobs):
        async with app.state.db() as db, db.begin():
            await db.scalar(
                text("SELECT gateway_admit_batch(CAST(:items AS jsonb))"),
                {"items": json.dumps([job.params for job in jobs])},
            )
            raise OperationalError("simulated commit failure", {}, Exception("test"))

    monkeypatch.setattr(app.state.admission, "_execute", fail_commit)
    responses = await asyncio.gather(*(completion(admin, person["key"]) for _ in range(10)))
    assert all(r.status_code == 503 for r in responses)
    async with app.state.db() as db:
        assert await db.scalar(select(func.count()).select_from(Call)) == 0
        assert await db.scalar(select(func.count()).select_from(Ledger)) == 0
    assert not received


async def test_queued_request_rechecks_disabled_team(admin, app, monkeypatch):
    await price(admin)
    group = await team(admin)
    person, _ = await student(admin, group)
    queued, release = asyncio.Event(), asyncio.Event()
    original = app.state.admission._execute

    async def delayed(jobs):
        queued.set()
        await release.wait()
        return await original(jobs)

    monkeypatch.setattr(app.state.admission, "_execute", delayed)
    task = asyncio.create_task(completion(admin, person["key"]))
    await asyncio.wait_for(queued.wait(), 3)
    response = await admin.patch(f"/admin/api/teams/{group['id']}", json={"active": False})
    assert response.status_code == 200
    release.set()
    assert (await task).status_code == 403
    assert not received


async def test_admission_timer_includes_commit_acknowledgement(admin, app, monkeypatch):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    original = app.state.admission._execute

    async def slow_ack(jobs):
        result = await original(jobs)
        await asyncio.sleep(0.06)
        return result

    monkeypatch.setattr(app.state.admission, "_execute", slow_ack)
    response = await completion(admin, person["key"])
    assert response.status_code == 200
    async with app.state.db() as db:
        call = await db.scalar(select(Call))
        assert call.admission_ms >= 60


async def test_heartbeats_do_not_cancel_slow_read_and_close_cancels_pending_read():
    closed = asyncio.Event()

    async def upstream():
        try:
            await asyncio.sleep(0.04)
            yield "content"
            await asyncio.sleep(60)
        finally:
            closed.set()

    stream = with_heartbeats(upstream(), 0.005)
    assert await anext(stream) is None
    while await anext(stream) != "content":
        pass
    assert await anext(stream) is None
    await stream.aclose()
    assert closed.is_set()


async def test_budget_turn_prioritizes_admission_but_does_not_starve_settlement():
    from gateway.admission import BudgetTurn

    turn, order = BudgetTurn(), []

    async def work(admission):
        async with turn.enter(admission=admission):
            order.append("admit" if admission else "settle")
            await asyncio.sleep(0)

    async with turn.enter():
        tasks = [asyncio.create_task(work(False))]
        tasks.extend(asyncio.create_task(work(True)) for _ in range(8))
        await asyncio.sleep(0)
    await asyncio.wait_for(asyncio.gather(*tasks), 2)
    assert order[:4] == ["admit"] * 4
    assert order[4] == "settle"


async def test_roster_password_hashing_does_not_hold_team_lock(admin, app, monkeypatch):
    import threading

    from argon2 import PasswordHasher

    await price(admin)
    group = await team(admin)
    person, _ = await student(admin, group)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = PasswordHasher.hash

    def blocked_hash(self, password, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(self, password, **kwargs)

    monkeypatch.setattr(PasswordHasher, "hash", blocked_hash)
    task = asyncio.create_task(
        admin.post(
            "/admin/api/students/import",
            json={
                "operation_id": str(uuid.uuid4()),
                "team_id": group["id"],
                "students": [{"student_number": "hashing-001"}],
                "models": ["lab-chat"],
                "budget": 1000000,
            },
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await asyncio.wait_for(completion(admin, person["key"]), 2)
        assert response.status_code == 200
    finally:
        release.set()
        response = await task
    assert response.status_code == 201
