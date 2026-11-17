import asyncio
import time
import uuid
from dataclasses import replace

import pytest
from conftest import completion, price, student, team
from fastapi import HTTPException

from gateway.admission import admit
from gateway.billing import settle
from gateway.models import Membership


async def begin(app, key, request=None):
    return await admit(
        app,
        "Bearer " + key,
        {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}]},
        str(uuid.uuid4()),
        "127.0.0.1",
        time.perf_counter(),
        request=request,
    )


async def wait_for_queue(app):
    async with asyncio.timeout(2):
        while not app.state.admission.capacity_waiters:
            await asyncio.sleep(0.005)


async def test_default_member_can_exceed_old_concurrency_and_rpm(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin, concurrency=0, rpm=0))
    async with app.state.db() as db:
        member = await db.get(Membership, person["membership_id"])
        assert member.concurrency == member.rpm == 0
    calls = await asyncio.gather(*(begin(app, person["key"]) for _ in range(30)))
    assert len(calls) == 30 and all(c.reservation == 0 for c, _, _ in calls)
    await asyncio.gather(*(settle(app.state.db, c.id, manual_cost=0) for c, _, _ in calls))


async def test_explicit_personal_limits_are_visible_and_can_be_disabled(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    path = f"/admin/api/memberships/{person['membership_id']}/limits"
    assert (await admin.patch(path, json={"concurrency": 1, "rpm": 0})).status_code == 200
    row = (await admin.get("/admin/api/memberships")).json()[0]
    assert row["concurrency"] == 1 and row["rpm"] == 0
    call, _, _ = await begin(app, person["key"])
    response = await completion(admin, person["key"])
    assert response.status_code == 429 and response.headers["retry-after"] == "1"
    detail = response.json()["error"]["details"]
    assert detail["membership"] == {"current": 1, "limit": 1}
    assert detail["team"]["current"] == 1
    rejected = (await admin.get("/admin/api/rejections")).json()["rows"][0]
    assert rejected["details"] == detail and rejected["limit_scope"] == "membership"
    assert (await admin.patch(path, json={"concurrency": 0, "rpm": 0})).status_code == 200
    assert (await completion(admin, person["key"])).status_code == 200
    await settle(app.state.db, call.id, manual_cost=0)


@pytest.mark.parametrize("finish", ["release", "budget", "disable"])
async def test_queue_rechecks_budget_and_identity_before_dispatch(admin, app, finish):
    await price(admin)
    app.state.settings = replace(app.state.settings, capacity_wait_seconds=2)
    person, _ = await student(admin, await team(admin, concurrency=1), budget=1)
    first, _, _ = await begin(app, person["key"])
    task = asyncio.create_task(completion(admin, person["key"]))
    await wait_for_queue(app)
    if finish == "disable":
        assert (
            await admin.post(f"/admin/api/memberships/{person['membership_id']}/disable", json={})
        ).status_code == 200
    await settle(app.state.db, first.id, manual_cost=1 if finish == "budget" else 0)
    response = await task
    assert response.status_code == {"release": 200, "budget": 402, "disable": 401}[finish]
    assert app.state.admission.capacity_waiters == 0


async def test_queue_is_bounded_and_cancelled_waiter_leaves_no_call(admin, app):
    await price(admin)
    app.state.settings = replace(app.state.settings, capacity_wait_seconds=2, capacity_queue_size=1)
    person, _ = await student(admin, await team(admin, concurrency=1))
    first, _, _ = await begin(app, person["key"])
    waiting = asyncio.create_task(begin(app, person["key"]))
    await wait_for_queue(app)
    with pytest.raises(HTTPException) as rejected:
        await begin(app, person["key"])
    assert rejected.value.detail["details"]["queue_full"]
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert app.state.admission.capacity_waiters == 0
    assert len((await admin.get("/admin/api/requests")).json()) == 1
    await settle(app.state.db, first.id, manual_cost=0)


async def test_queue_timeout_does_not_multiply_rate_attempts(admin, app):
    await price(admin)
    app.state.settings = replace(app.state.settings, capacity_wait_seconds=0.15)
    group = await team(admin, concurrency=1)
    person, _ = await student(admin, group)
    first, _, _ = await begin(app, person["key"])
    response = await completion(admin, person["key"])
    assert response.status_code == 429
    details = response.json()["error"]["details"]
    assert details["queue_timeout"] and details["waited_ms"] >= 140
    assert int(await app.state.redis.get(f"rate:{{{group['id']}}}:{person['membership_id']}")) == 2
    await settle(app.state.db, first.id, manual_cost=0)


async def test_migration_clears_legacy_defaults_but_preserves_custom_limits(admin, app):
    import os
    import subprocess
    import sys

    await price(admin)
    group = await team(admin, concurrency=40, rpm=300)
    default, _ = await student(admin, group)
    custom, _ = await student(admin, group, "002", concurrency=7, rpm=99)
    url = app.state.settings.database_url
    assert url.rsplit("/", 1)[1].startswith("lab_gateway_test_")
    for direction, revision in (("downgrade", "d58b6f2a901e"), ("upgrade", "head")):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", direction, revision],
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    async with app.state.db() as db:
        current = await db.get(Membership, default["membership_id"])
        explicit = await db.get(Membership, custom["membership_id"])
        assert (current.rpm, current.concurrency) == (0, 0)
        assert (explicit.rpm, explicit.concurrency) == (99, 7)
        assert current.budget == explicit.budget == 10000000
    assert (await completion(admin, default["key"])).status_code == 200


async def test_disconnected_capacity_waiter_never_dispatches(admin, app):
    await price(admin)
    app.state.settings = replace(app.state.settings, capacity_wait_seconds=2)
    person, _ = await student(admin, await team(admin, concurrency=1))
    first, _, _ = await begin(app, person["key"])

    class Connection:
        disconnected = False

        async def is_disconnected(self):
            return self.disconnected

    connection = Connection()
    waiting = asyncio.create_task(begin(app, person["key"], request=connection))
    await wait_for_queue(app)
    connection.disconnected = True
    with pytest.raises(HTTPException) as error:
        await waiting
    assert error.value.status_code == 499 and app.state.admission.capacity_waiters == 0
    assert len((await admin.get("/admin/api/requests")).json()) == 1
    await settle(app.state.db, first.id, manual_cost=0)
