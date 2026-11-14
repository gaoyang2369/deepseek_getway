import asyncio
import json
import uuid
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from argon2 import PasswordHasher
from conftest import completion, price, team
from sqlalchemy import func, select

from gateway.models import Audit, BudgetPeriod, Membership, Operation, PersonalKey, StudentAccount, now

NEW_PASSWORD = "student-test-long-passphrase-2026"


@pytest.fixture
async def portal(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway",
        headers={"Origin": app.state.settings.public_url},
    ) as client:
        yield client


async def roster(admin, t, numbers=("00020260001", "00020260002"), **kwargs):
    body = {
        "operation_id": str(uuid.uuid4()),
        "team_id": t["id"],
        "students": [{"student_number": n, "name": "模拟同学" + n} for n in numbers],
        "models": ["lab-chat"],
        "budget": 10000000,
        **kwargs,
    }
    response = await admin.post("/admin/api/students/import", json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


async def login(portal, number, password=None):
    r = await portal.post("/student/api/login", json={"username": number, "password": password or number})
    assert r.status_code == 200, r.text
    portal.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return r


async def change(portal, current, new=NEW_PASSWORD):
    r = await portal.post("/student/api/password", json={"current_password": current, "new_password": new})
    assert r.status_code == 200, r.text
    portal.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return r


async def test_student_first_login_key_rotation_and_shared_budget(admin, app, portal):
    await price(admin)
    t = await team(admin)
    data, body = await roster(admin, t)
    first = data["students"][0]
    number, member = first["student_number"], first["membership_id"]
    assert (await portal.get("/")).status_code == 302
    assert (await portal.get("/student")).status_code == 200
    await login(portal, number)
    original_cookie = portal.cookies.get("lab_student_session")
    me = (await portal.get("/student/api/me")).json()
    assert me["must_change_password"] and "memberships" not in me
    key_body = {"operation_id": str(uuid.uuid4()), "membership_id": member}
    assert (await portal.post("/student/api/keys", json=key_body)).status_code == 403
    assert (
        await portal.post("/student/api/password", json={"current_password": number, "new_password": "short"})
    ).status_code == 422
    await change(portal, number)
    assert original_cookie != portal.cookies.get("lab_student_session")
    r = await portal.post("/student/api/keys", json=key_body)
    assert r.status_code == 201, r.text
    raw = r.json()["key"]
    assert (await completion(portal, raw)).status_code == 200
    replay = (await portal.post("/student/api/keys", json=key_body)).json()
    assert replay["replayed"] and replay["key"] is None
    key_body["operation_id"] = str(uuid.uuid4())
    assert (await portal.post("/student/api/keys", json=key_body)).status_code == 409
    key_body.update(operation_id=str(uuid.uuid4()), rotate=True)
    rotated = await portal.post("/student/api/keys", json=key_body)
    assert rotated.status_code == 201, rotated.text
    assert (await completion(portal, raw)).status_code == 401
    assert (await completion(portal, rotated.json()["key"])).status_code == 200
    me_response = await portal.get("/student/api/me")
    assert me_response.json()["memberships"][0]["spent"] == 376
    assert me_response.json()["student_number"] == number
    assert raw not in me_response.text and rotated.json()["key"] not in me_response.text
    async with app.state.db() as db:
        account = await db.get(StudentAccount, first["student_id"])
        assert PasswordHasher().verify(account.password_hash, NEW_PASSWORD)
        periods = (
            await db.scalars(
                select(BudgetPeriod).where(
                    BudgetPeriod.owner_type == "membership", BudgetPeriod.owner_id == member
                )
            )
        ).all()
        assert len(periods) == 1 and periods[0].spent == 376
        records = json.dumps([(a.action, a.detail) for a in (await db.scalars(select(Audit))).all()])
        operations = json.dumps([o.result for o in (await db.scalars(select(Operation))).all()])
        assert raw not in records + operations and NEW_PASSWORD not in records + operations
    await admin.post(
        "/admin/api/students/import", json={**body, "operation_id": str(uuid.uuid4()), "budget": 999}
    )
    assert (await portal.get("/student/api/me")).json()["memberships"][0]["spent"] == 376
    assert (await portal.get("/student/api/me")).json()["memberships"][0]["limit"] == 10000000


async def test_student_permissions_csrf_and_ownership(admin, app, portal):
    t = await team(admin)
    data, _ = await roster(admin, t)
    first, other = data["students"]
    await login(portal, first["student_number"])
    await change(portal, first["student_number"])
    assert (await portal.get("/admin/api/teams")).status_code == 401
    body = {"operation_id": str(uuid.uuid4()), "membership_id": other["membership_id"]}
    assert (await portal.post("/student/api/keys", json=body)).status_code == 404
    body["membership_id"] = first["membership_id"]
    csrf = portal.headers.pop("X-CSRF-Token")
    assert (await portal.post("/student/api/keys", json=body)).status_code == 403
    portal.headers["X-CSRF-Token"] = csrf
    assert (
        await portal.post("/student/api/keys", json=body, headers={"Origin": "https://evil.example"})
    ).status_code == 403
    assert (await portal.post("/student/api/keys", json={**body, "budget": 99999999})).status_code == 422
    assert (await portal.post("/student/api/register", json={})).status_code == 404
    assert (
        await portal.post(
            "/student/api/password",
            json={"current_password": "wrong", "new_password": "another-new-passphrase-1234"},
        )
    ).status_code == 401
    assert (
        await portal.post(
            "/student/api/password", json={"current_password": NEW_PASSWORD, "new_password": "x" * 20}
        )
    ).status_code == 400
    response = await portal.post("/student/api/login", json={"username": "unknown", "password": "unknown"})
    assert response.status_code == 401 and response.json()["error"]["message"] == "invalid_login"
    async with app.state.db() as db, db.begin():
        m = await db.get(Membership, first["membership_id"])
        m.active = False
    assert (await portal.post("/student/api/keys", json=body)).status_code == 403


async def test_roster_import_concurrent_idempotency_and_no_reset(admin, app, portal):
    t = await team(admin)
    data, body = await roster(admin, t)
    responses = await asyncio.gather(*(admin.post("/admin/api/students/import", json=body) for _ in range(5)))
    assert all(r.status_code == 201 and r.json()["replayed"] for r in responses)
    assert all(r.json()["initial_passwords"] == [] for r in responses)
    await login(portal, data["students"][0]["student_number"])
    await change(portal, data["students"][0]["student_number"])
    again, _ = await roster(admin, t)
    assert all(not s["account_created"] for s in again["students"])
    assert not (await portal.get("/student/api/me")).json()["must_change_password"]
    async with app.state.db() as db:
        assert await db.scalar(select(func.count()).select_from(StudentAccount)) == 2
        assert await db.scalar(select(func.count()).select_from(Membership)) == 2
        assert await db.scalar(select(func.count()).select_from(PersonalKey)) == 0
    invalid = {
        **body,
        "operation_id": str(uuid.uuid4()),
        "students": [
            {"student_number": "00000000000"},
            {"student_number": data["students"][0]["student_number"], "name": "冲突姓名"},
        ],
    }
    assert (await admin.post("/admin/api/students/import", json=invalid)).status_code == 409
    async with app.state.db() as db:
        assert await db.scalar(select(func.count()).select_from(StudentAccount)) == 2


async def test_admin_reset_revokes_sessions_keys_and_forces_change(admin, app, portal):
    await price(admin)
    t = await team(admin)
    data, _ = await roster(admin, t, numbers=("00020260001",))
    first = data["students"][0]
    await login(portal, first["student_number"])
    await change(portal, first["student_number"])
    raw = (
        await portal.post(
            "/student/api/keys",
            json={"operation_id": str(uuid.uuid4()), "membership_id": first["membership_id"]},
        )
    ).json()["key"]
    assert (await completion(portal, raw)).status_code == 200
    response = await admin.post(
        f"/admin/api/students/{first['student_id']}/reset-password", json={"reason": "已线下核实学生身份"}
    )
    assert response.status_code == 200, response.text
    temporary = response.json()["temporary_password"]
    assert temporary != first["student_number"]
    assert (await portal.get("/student/api/me")).status_code == 401
    assert (await completion(portal, raw)).status_code == 401
    assert (
        await portal.post(
            "/student/api/login", json={"username": first["student_number"], "password": NEW_PASSWORD}
        )
    ).status_code == 401
    await login(portal, first["student_number"], temporary)
    assert (
        await portal.post(
            "/student/api/keys",
            json={"operation_id": str(uuid.uuid4()), "membership_id": first["membership_id"]},
        )
    ).status_code == 403
    await change(portal, temporary)
    assert (await portal.get("/student/api/me")).json()["memberships"][0]["spent"] == 188
    assert temporary not in (await admin.get("/admin/api/audit")).text


async def test_random_initial_password_production_policy_and_rate_limit(admin, app, portal):
    t = await team(admin)
    data, body = await roster(admin, t, numbers=("00020260001",), initial_password_mode="random")
    temporary = data["initial_passwords"][0]["password"]
    await login(portal, "00020260001", temporary)
    replay = await admin.post("/admin/api/students/import", json=body)
    assert replay.json()["initial_passwords"] == []
    assert temporary not in (await admin.get("/admin/api/audit")).text
    app.state.settings = replace(app.state.settings, production=True)
    assert (
        await admin.post(
            "/admin/api/students/import",
            json={**body, "operation_id": str(uuid.uuid4()), "initial_password_mode": "student_number"},
        )
    ).status_code == 400
    app.state.settings = replace(app.state.settings, production=False)
    statuses = []
    for _ in range(11):
        r = await portal.post("/student/api/login", json={"username": "unknown", "password": "bad"})
        statuses.append(r.status_code)
    assert statuses[:10] == [401] * 10 and statuses[-1] == 429


async def test_concurrent_student_issue_and_expiry_cap(admin, app, portal):
    t = await team(admin)
    deadline = now() + timedelta(days=2)
    data, _ = await roster(admin, t, numbers=("00020260001",), expires_at=deadline.isoformat())
    first = data["students"][0]
    await login(portal, first["student_number"])
    await change(portal, first["student_number"])
    results = await asyncio.gather(
        *(
            portal.post(
                "/student/api/keys",
                json={"operation_id": str(uuid.uuid4()), "membership_id": first["membership_id"]},
            )
            for _ in range(5)
        )
    )
    assert sorted(r.status_code for r in results) == [201, 409, 409, 409, 409]
    async with app.state.db() as db, db.begin():
        key = await db.scalar(select(PersonalKey).where(PersonalKey.membership_id == first["membership_id"]))
        assert key.expires_at == deadline
        member = await db.get(Membership, first["membership_id"])
        member.key_expires_at = now() - timedelta(seconds=1)
    assert (
        await portal.post(
            "/student/api/keys",
            json={"operation_id": str(uuid.uuid4()), "membership_id": first["membership_id"], "rotate": True},
        )
    ).status_code == 400
