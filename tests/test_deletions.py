"""Deletion is scoped, revokes access and never erases financial history."""

import uuid

import httpx
import pytest
from conftest import PASSWORD, completion, price, student, team
from playwright.async_api import expect
from sqlalchemy import func, select
from support import artifact
from test_student_portal import change, login, roster

from gateway.billing import settle
from gateway.models import (
    Audit,
    BudgetPeriod,
    Call,
    Credential,
    Ledger,
    Membership,
    PersonalKey,
    Student,
    Team,
)


async def financial_snapshot(app):
    async with app.state.db() as db:
        return {
            table.__tablename__: [
                dict(row) for row in (await db.execute(select(table.__table__).order_by(table.id))).mappings()
            ]
            for table in (Call, BudgetPeriod, Ledger)
        }


@pytest.mark.parametrize("scope", ["teams", "memberships", "students"])
async def test_delete_retains_history_and_other_teams(admin, app, scope):
    await price(admin)
    a, b = await team(admin, "A"), await team(admin, "B")
    first, _ = await student(admin, a, "001")
    other, _ = await student(admin, b, "002")
    assert (await completion(admin, first["key"])).status_code == 200
    before = await financial_snapshot(app)
    identity = {"teams": a["id"], "memberships": first["membership_id"], "students": first["student_id"]}[
        scope
    ]
    url = f"/admin/api/{scope}/{identity}"
    assert (await admin.delete(url)).status_code == 200
    assert (await admin.delete(url)).status_code == 200
    assert await financial_snapshot(app) == before
    assert (await completion(admin, first["key"])).status_code == 401
    assert (await completion(admin, other["key"])).status_code == 200
    assert [m["id"] for m in (await admin.get("/admin/api/memberships")).json()] == [other["membership_id"]]
    listed = (await admin.get("/admin/api/keys")).json()
    assert [k["membership_id"] for k in listed] == [other["membership_id"]]
    assert all("key" not in k for k in listed)
    teams = (await admin.get("/admin/api/teams")).json()
    assert {t["id"] for t in teams} == ({b["id"]} if scope == "teams" else {a["id"], b["id"]})
    async with app.state.db() as db:
        assert (await db.get(Membership, first["membership_id"])).deleted_at is not None
        assert (await db.get(Student, other["student_id"])).deleted_at is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Audit)
                .where(
                    Audit.action
                    == {
                        "teams": "team_delete",
                        "memberships": "membership_delete",
                        "students": "student_delete",
                    }[scope]
                )
            )
            == 1
        )
    historical = (await admin.get("/admin/api/memberships?include_deleted=true")).json()
    assert len(historical) == 2
    assert next(m for m in historical if m["id"] == first["membership_id"])["deleted_at"]
    if scope == "teams":
        assert (await admin.patch(f"/admin/api/teams/{a['id']}", json={"active": True})).status_code == 404
        async with app.state.db() as db:
            credentials = (await db.scalars(select(Credential).where(Credential.team_id == a["id"]))).all()
            assert credentials and all(not c.active for c in credentials)


async def test_removed_identifiers_can_be_reused_without_reviving_old_keys(admin, app):
    a = await team(admin)
    original, _ = await student(admin, a)
    assert (await admin.delete(f"/admin/api/memberships/{original['membership_id']}")).status_code == 200
    second, _ = await student(admin, a)
    assert second["membership_id"] != original["membership_id"]
    assert second["student_id"] == original["student_id"]
    assert (await admin.delete(f"/admin/api/students/{original['student_id']}")).status_code == 200
    third, _ = await student(admin, a)
    assert third["student_id"] != original["student_id"]
    assert (await admin.delete(f"/admin/api/teams/{a['id']}")).status_code == 200
    fresh = await team(admin)
    assert fresh["id"] != a["id"]
    assert (await admin.get("/admin/api/teams")).json() == [fresh]
    async with app.state.db() as db:
        assert not (await db.get(PersonalKey, original["key_id"])).active
        assert (await db.get(Team, a["id"])).deleted_at


async def test_student_delete_revokes_all_groups_and_sessions_and_allows_reimport(admin, app):
    a, b = await team(admin, "A"), await team(admin, "B")
    data, _ = await roster(admin, a, numbers=("001",))
    first = data["students"][0]
    await roster(admin, b, numbers=("001",))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as portal:
        await login(portal, "001")
        await change(portal, "001")
        members = (await portal.get("/student/api/me")).json()["memberships"]
        assert len(members) == 2
        keys = []
        for member in members:
            response = await portal.post(
                "/student/api/keys", json={"operation_id": str(uuid.uuid4()), "membership_id": member["id"]}
            )
            assert response.status_code == 201
            keys.append(response.json()["key"])
        assert (await admin.delete(f"/admin/api/students/{first['student_id']}")).status_code == 200
        assert (await portal.get("/student/api/me")).status_code == 401
        assert (
            await portal.post("/student/api/login", json={"username": "001", "password": "001"})
        ).status_code == 401
        for key in keys:
            assert (
                await portal.get("/v1/models", headers={"Authorization": "Bearer " + key})
            ).status_code == 401
        assert (await admin.get("/admin/api/students")).json() == []
        assert (await admin.get("/admin/api/students/accounts")).json() == []
        again, _ = await roster(admin, a, numbers=("001",))
        assert again["students"][0]["student_id"] != first["student_id"]
        await login(portal, "001")
    assert len((await admin.get("/admin/api/teams")).json()) == 2


async def test_member_and_team_delete_preserve_global_student_account(admin, app):
    a, b = await team(admin, "A"), await team(admin, "B")
    data, _ = await roster(admin, a, numbers=("001",))
    await roster(admin, b, numbers=("001",))
    member = data["students"][0]
    assert (await admin.delete(f"/admin/api/memberships/{member['membership_id']}")).status_code == 200
    assert len((await admin.get("/admin/api/students")).json()) == 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as portal:
        await login(portal, "001")
        await change(portal, "001")
        assert len((await portal.get("/student/api/me")).json()["memberships"]) == 1
        assert (await admin.delete(f"/admin/api/teams/{b['id']}")).status_code == 200
        assert (await portal.get("/student/api/me")).json()["memberships"] == []
    assert len((await admin.get("/admin/api/students/accounts")).json()) == 1


async def test_pending_call_can_be_reconciled_after_team_deletion(admin, app):
    await price(admin)
    a = await team(admin)
    first, _ = await student(admin, a)
    assert (await completion(admin, first["key"], content="no-usage")).status_code == 200
    async with app.state.db() as db:
        call = await db.scalar(select(Call))
        call_id = call.id
        assert call.status == "pending"
    before = await financial_snapshot(app)
    assert (await admin.delete(f"/admin/api/teams/{a['id']}")).status_code == 200
    assert await financial_snapshot(app) == before
    assert await settle(app.state.db, call_id, manual_cost=0, reason="verified mock only")
    async with app.state.db() as db:
        assert (await db.get(Call, call_id)).status == "settled"


async def test_deletion_requires_admin_and_csrf(admin, app):
    a = await team(admin)
    first, _ = await student(admin, a)
    for path in (
        f"teams/{a['id']}",
        f"memberships/{first['membership_id']}",
        f"students/{first['student_id']}",
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as stranger:
            assert (await stranger.delete("/admin/api/" + path)).status_code == 401
        assert (await admin.delete("/admin/api/" + path, headers={"X-CSRF-Token": ""})).status_code == 403
        assert (
            await admin.delete("/admin/api/" + path, headers={"Origin": "https://untrusted.example"})
        ).status_code == 403
    assert len((await admin.get("/admin/api/teams")).json()) == 1
    assert len((await admin.get("/admin/api/students")).json()) == 1
    for scope in ("teams", "memberships", "students"):
        assert (await admin.delete(f"/admin/api/{scope}/{uuid.uuid4()}")).status_code == 404


async def test_browser_delete_buttons_and_cancellation(admin, app, live_server, browser):
    a, b = await team(admin, "DeleteMe"), await team(admin, "KeepMe")
    first, _ = await student(admin, a, "browser001")
    await student(admin, b, "browser002")
    base = live_server
    page = await browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(base + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator("#console").wait_for(state="visible")
    await page.locator('nav button[data-view="teams"]').click()
    row = page.get_by_role("row").filter(has_text="DeleteMe")
    await row.get_by_role("button", name="更多", exact=True).click()
    await page.get_by_role("menuitem", name="删除项目组", exact=True).wait_for()
    await page.get_by_role("menuitem", name="删除项目组", exact=True).click()
    await page.locator("#confirm-dialog").wait_for(state="visible")
    await page.screenshot(path=str(artifact("admin-confirm-preview.png")))
    await page.locator("#confirm-cancel").click()
    assert len((await admin.get("/admin/api/teams")).json()) == 2
    await row.get_by_role("button", name="更多", exact=True).click()
    async with page.expect_response(lambda r: r.request.method == "DELETE") as deleted:
        await page.get_by_role("menuitem", name="删除项目组", exact=True).click()
        await page.locator("#confirm-submit").click()
    assert (await deleted.value).status == 200
    await row.wait_for(state="hidden")
    await page.get_by_role("row").filter(has_text="KeepMe").wait_for()
    await page.locator('nav button[data-view="members"]').click()
    row = page.get_by_role("row").filter(has_text="browser002")
    await row.get_by_role("button", name="管理", exact=True).click()
    await page.locator(".member-project").get_by_role("button", name="更多", exact=True).click()
    await page.get_by_role("menuitem", name="删除成员", exact=True).click()
    await page.locator("#confirm-submit").click()
    await expect(page.locator("#modal")).not_to_be_visible()
    await expect(row).to_contain_text("未分配项目")
    row = page.get_by_role("row").filter(has_text="browser001")
    await row.get_by_role("button", name="管理", exact=True).click()
    await page.locator("#modal-fields > .row-actions").get_by_role("button", name="更多", exact=True).click()
    await page.get_by_role("menuitem", name="删除学生").click()
    await page.locator("#confirm-submit").click()
    await row.wait_for(state="hidden")
    await page.get_by_role("row").filter(has_text="browser002").wait_for()
    assert errors == []
    assert (await admin.get("/admin/api/teams")).json()[0]["id"] == b["id"]
    assert (await admin.get("/admin/api/students")).json()[0]["student_id"] != first["student_id"]


def test_upgrade_preserves_existing_roster_values(settings):
    """Exercise the incremental upgrade on populated old-schema tables."""
    import os
    import subprocess

    import psycopg

    target = "lab_gateway_deletion_migration_" + uuid.uuid4().hex[:8]
    source_url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    target_url = settings.database_url.rsplit("/", 1)[0] + "/" + target
    with psycopg.connect(source_url, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(target)))
    try:
        env = {**os.environ, "DATABASE_URL": target_url}
        subprocess.run(
            [".venv/bin/alembic", "upgrade", "9d31b47fa820"], env=env, check=True, capture_output=True
        )
        with psycopg.connect(target_url.replace("postgresql+psycopg://", "postgresql://")) as conn:
            conn.execute("""INSERT INTO teams
                (id,name,code,active,models,budget,period,policy_version,rpm,concurrency,max_output)
                VALUES ('existing-team','用户已创建项目组','existing',true,'["lab-chat"]',12345678,'month',4,300,40,32768)""")
            conn.execute("""INSERT INTO students (id,name,student_number,kind,active,created_at)
                VALUES ('existing-student','原有学生','001','student',true,now())""")
            conn.execute("""INSERT INTO memberships
                (id,student_id,team_id,active,budget,period,policy_version,rpm,concurrency,models)
                VALUES ('existing-member','existing-student','existing-team',true,12345,'month',2,60,10,'["lab-chat"]')""")
            before = {
                table: conn.execute(f"SELECT to_jsonb(t) FROM {table} t").fetchone()[0]
                for table in ("teams", "students", "memberships")
            }
        subprocess.run([".venv/bin/alembic", "upgrade", "head"], env=env, check=True, capture_output=True)
        with psycopg.connect(target_url.replace("postgresql+psycopg://", "postgresql://")) as conn:
            for table, expected in before.items():
                row = conn.execute(f"SELECT to_jsonb(t) FROM {table} t").fetchone()[0]
                assert row.pop("deleted_at") is None
                assert row == expected
    finally:
        with psycopg.connect(source_url, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP DATABASE {}").format(psycopg.sql.Identifier(target)))
