"""Student self-service, optional team codes, and scoped demo ledger cleanup."""

import uuid
from dataclasses import replace

import httpx
from argon2 import PasswordHasher
from conftest import PASSWORD, completion, price, student, team
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from support import artifact
from test_deletions import financial_snapshot
from test_student_portal import change, login

from gateway.accounts import backfill_demo_accounts
from gateway.admission import admit
from gateway.models import Audit, Membership, Operation, PersonalKey, Student, StudentAccount


async def test_team_name_only_creates_internal_identifier(admin):
    data = {"name": "中文项目组", "upstream_key": "MOCK-test-key", "models": ["lab-chat"]}
    first = await admin.post("/admin/api/teams", json=data)
    second = await admin.post("/admin/api/teams", json=data)
    assert first.status_code == second.status_code == 201
    assert first.json()["name"] == data["name"]
    assert first.json()["code"] != second.json()["code"]


async def test_enrollment_auto_creates_login_without_admin_key(admin, app):
    t = await team(admin)
    body = {
        "operation_id": str(uuid.uuid4()),
        "name": "新学生",
        "student_number": "000123",
        "team_id": t["id"],
        "models": ["lab-chat"],
        "budget": 1000000,
    }
    response = await admin.post("/admin/api/enroll", json=body)
    assert response.status_code == 201
    result = response.json()
    assert result["account_created"] and result["key"] is None and result["key_id"] is None
    async with app.state.db() as db:
        assert await db.get(StudentAccount, result["student_id"])
        assert await db.scalar(select(func.count()).select_from(PersonalKey)) == 0
    replay = (await admin.post("/admin/api/enroll", json=body)).json()
    assert replay["replayed"] and replay["temporary_password"] is None
    assert (
        await admin.post(
            f"/admin/api/memberships/{result['membership_id']}/keys",
            json={"operation_id": str(uuid.uuid4()), "models": ["lab-chat"]},
        )
    ).status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as portal:
        await login(portal, "000123")
        await change(portal, "000123")
        issued = await portal.post(
            "/student/api/keys",
            json={"operation_id": str(uuid.uuid4()), "membership_id": result["membership_id"]},
        )
        assert issued.status_code == 201
        old_key = issued.json()["key"]
        reset = await admin.post(
            f"/admin/api/students/{result['student_id']}/reset-password",
            json={"reason": "已核实学生身份后重置"},
        )
        assert reset.status_code == 200
        assert (
            await portal.get("/v1/models", headers={"Authorization": "Bearer " + old_key})
        ).status_code == 401
        await login(portal, "000123", reset.json()["temporary_password"])
        await change(portal, reset.json()["temporary_password"], new="student-new-long-passphrase-2026")
        renewed = await portal.post(
            "/student/api/keys",
            json={"operation_id": str(uuid.uuid4()), "membership_id": result["membership_id"]},
        )
        assert renewed.status_code == 201 and renewed.json()["key"] != old_key


async def test_production_enrollment_generates_initial_password_once(admin, app):
    t = await team(admin)
    app.state.settings = replace(app.state.settings, production=True)
    body = {
        "operation_id": str(uuid.uuid4()),
        "name": "新学生",
        "student_number": "000123",
        "team_id": t["id"],
        "models": ["lab-chat"],
    }
    response = await admin.post("/admin/api/enroll", json=body)
    assert response.status_code == 201
    result = response.json()
    assert result["initial_password_mode"] == "random"
    password = result["temporary_password"]
    assert len(password) >= 24 and password != body["student_number"] and result["key"] is None
    assert password not in (await admin.get("/admin/api/audit")).text
    assert (await admin.post("/admin/api/enroll", json=body)).json()["temporary_password"] is None
    async with app.state.db() as db:
        operation = await db.get(Operation, body["operation_id"])
        assert password not in str(operation.result)
    body["operation_id"] = str(uuid.uuid4())
    body["initial_password_mode"] = "student_number"
    assert (await admin.post("/admin/api/enroll", json=body)).status_code == 400


async def test_legacy_demo_account_backfill_keeps_existing_password_and_key(admin, app):
    t = await team(admin)
    person, _ = await student(admin, t)
    async with app.state.db() as db, db.begin():
        existing_hash = (await db.get(StudentAccount, person["student_id"])).password_hash
        legacy = Student(name="原有学生", student_number="legacy001")
        db.add(legacy)
        await db.flush()
        legacy_id = legacy.id
    assert await backfill_demo_accounts(app.state.db, app.state.settings) == 1
    assert await backfill_demo_accounts(app.state.db, app.state.settings) == 0
    async with app.state.db() as db:
        assert (await db.get(StudentAccount, person["student_id"])).password_hash == existing_hash
        assert (await db.get(PersonalKey, person["key_id"])).active
        new_account = await db.get(StudentAccount, legacy_id)
        assert PasswordHasher().verify(new_account.password_hash, "legacy001")
        assert new_account.must_change_password


async def test_demo_cleanup_deletes_usage_but_preserves_all_configuration(admin, app):
    await price(admin)
    a, b = await team(admin, "A"), await team(admin, "B")
    first, _ = await student(admin, a, "001")
    other, _ = await student(admin, b, "002")
    assert (await completion(admin, first["key"])).status_code == 200
    assert (await completion(admin, first["key"], "no-usage")).status_code == 200
    assert (await completion(admin, other["key"])).status_code == 200
    assert {p["team_id"] for p in (await admin.get("/admin/api/budgets")).json()} == {a["id"], b["id"]}
    before = await financial_snapshot(app)
    wrong = await admin.post(f"/admin/api/teams/{a['id']}/clear-test-usage", json={"confirm_name": "wrong"})
    assert wrong.status_code == 400 and await financial_snapshot(app) == before
    result = await admin.post(
        f"/admin/api/teams/{a['id']}/clear-test-usage", json={"confirm_name": a["name"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["removed"]["calls"] == 2
    assert all(r["team_id"] == b["id"] for r in (await admin.get("/admin/api/requests")).json())
    assert all(p["team_id"] == b["id"] for p in (await admin.get("/admin/api/budgets")).json())
    assert [r["team_id"] for r in (await admin.get("/admin/api/statistics")).json()] == [b["id"]]
    assert (await admin.get("/admin/api/metrics")).json()["pending_requests"] == 0
    after = await financial_snapshot(app)
    for table in ("calls", "budget_periods", "ledger"):
        remaining = {row["id"]: row for row in after[table]}
        assert all(row == remaining[row["id"]] for row in before[table] if row["id"] in remaining)
    assert len((await admin.get("/admin/api/teams")).json()) == 2
    assert len((await admin.get("/admin/api/memberships")).json()) == 2
    assert (await completion(admin, first["key"])).status_code == 200
    async with app.state.db() as db:
        assert (await db.get(Membership, first["membership_id"])).budget == 10000000
        assert (
            await db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "test_usage_clear"))
            == 1
        )


async def test_cleanup_requires_mock_idle_team_and_csrf(admin, app):
    import time

    await price(admin)
    a = await team(admin)
    person, _ = await student(admin, a)
    path = f"/admin/api/teams/{a['id']}/clear-test-usage"
    body = {"confirm_name": a["name"]}
    assert (await admin.post(path, json=body, headers={"X-CSRF-Token": ""})).status_code == 403
    original = app.state.settings
    for settings in (
        replace(original, production=True),
        replace(original, upstream_url="https://api.deepseek.com/v1"),
    ):
        app.state.settings = settings
        assert not (await admin.get("/admin/api/config")).json()["test_cleanup_enabled"]
        assert (await admin.post(path, json=body)).status_code == 403
    app.state.settings = original
    await admit(
        app,
        "Bearer " + person["key"],
        {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}]},
        str(uuid.uuid4()),
        "127.0.0.1",
        time.perf_counter(),
    )
    before = await financial_snapshot(app)
    assert (await admin.post(path, json=body)).status_code == 409
    assert await financial_snapshot(app) == before


async def test_cleanup_rollback_retains_both_sides_of_budget_accounting(admin, app):
    await price(admin)
    a = await team(admin)
    person, _ = await student(admin, a)
    await completion(admin, person["key"])
    before = await financial_snapshot(app)

    def fail_call_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM calls"):
            raise OperationalError("fault injection", {}, None)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", fail_call_delete)
    try:
        response = await admin.post(
            f"/admin/api/teams/{a['id']}/clear-test-usage", json={"confirm_name": a["name"]}
        )
        assert response.status_code == 503
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", fail_call_delete)
    assert await financial_snapshot(app) == before


async def test_browser_name_student_reset_and_test_cleanup(admin, app, live_server, browser):
    await price(admin)
    group = await team(admin, "browser_cleanup", name="测试清理组")
    person, _ = await student(admin, group)
    await completion(admin, person["key"])
    base = live_server
    page = await browser.new_page(viewport={"width": 1440, "height": 1000})
    errors, native_dialogs = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: (native_dialogs.append(d.type), d.dismiss()))
    await page.goto(base + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator("#console").wait_for(state="visible")
    await page.locator('nav button[data-view="teams"]').click()
    await page.get_by_role("button", name="＋ 创建项目组").click()
    assert await page.locator('input[name="code"]').count() == 0
    await page.locator('input[name="name"]').fill("只填名称的新组")
    await page.locator('input[name="upstream_key"]').fill("MOCK-browser-key")
    await page.locator('input[name="models"]').fill("lab-chat")
    await page.locator("#modal-submit").click()
    await page.locator("#modal").wait_for(state="hidden")
    await page.get_by_role("cell", name="只填名称的新组", exact=True).wait_for()
    assert await page.get_by_role("columnheader", name="标识", exact=True).count() == 0
    await page.locator('nav button[data-view="members"]').click()
    await page.get_by_role("button", name="＋ 添加人员").click()
    await page.locator('input[name="name"]').fill("浏览器学生")
    await page.locator('input[name="student_number"]').fill("browser-new")
    await page.locator('select[name="team_id"]').select_option(group["id"])
    await page.locator("#modal-submit").click()
    await page.get_by_role("heading", name="学生账号已开通").wait_for()
    assert "网页登录已自动开通" in await page.locator("#modal-fields").inner_text()
    assert "lab_" not in await page.locator("#modal-fields").inner_text()
    await page.locator("#close-modal").click()
    await page.locator('nav button[data-view="students"]').click()
    row = page.get_by_role("row").filter(has_text="browser-new")
    await row.get_by_role("button", name="重置密码").wait_for()
    assert await page.get_by_role("button", name="启用网页登录").count() == 0
    await row.get_by_role("button", name="重置密码").click()
    await page.locator('textarea[name="reason"]').fill("测试")
    assert await page.locator("#modal-form").evaluate("(f)=>f.checkValidity()")
    await page.route(
        "**/reset-password",
        lambda route: route.fulfill(status=422, json={"error": {"message": "invalid_request_parameters"}}),
    )
    await page.locator("#modal-submit").click()
    await page.locator("#modal-error").wait_for(state="visible")
    assert "invalid_request_parameters" not in await page.locator("#modal-error").inner_text()
    await page.screenshot(path=str(artifact("admin-form-error-preview.png")))
    await page.unroute("**/reset-password")
    await page.locator("#modal-submit").click()
    await page.get_by_role("heading", name="临时密码（仅显示一次）").wait_for()
    await page.locator("#close-modal").click()
    await page.locator('nav button[data-view="budgets"]').click()
    await page.get_by_role("button", name="清理测试记录", exact=True).click()
    await page.locator('select[name="team_id"]').select_option(group["id"])
    await page.locator('input[name="confirm_name"]').fill("wrong")
    await page.locator("#modal-submit").click()
    await page.locator("#modal-error").wait_for(state="visible")
    assert "名称不一致" in await page.locator("#modal-error").inner_text()
    await page.locator('input[name="confirm_name"]').fill(group["name"])
    await page.screenshot(path=str(artifact("admin-cleanup-preview.png")))
    await page.locator("#modal-submit").click()
    await page.locator("#modal").wait_for(state="hidden")
    await page.locator("#notice.success").wait_for(state="visible")
    assert not (await admin.get("/admin/api/budgets")).json()
    assert len((await admin.get("/admin/api/teams")).json()) == 2
    assert not errors and not native_dialogs
