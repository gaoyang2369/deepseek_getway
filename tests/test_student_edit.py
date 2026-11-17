"""Identity corrections preserve account linkage, credentials and accounting."""

import asyncio

import httpx
from conftest import PASSWORD, completion, price, student, team
from playwright.async_api import expect
from sqlalchemy import select
from support import artifact
from test_deletions import financial_snapshot

from gateway.models import Audit, StudentAccount


async def test_edit_identity_keeps_login_keys_memberships_and_ledger(admin, app):
    await price(admin)
    group = await team(admin, "edit-A")
    person, _ = await student(admin, group, "000123")
    await student(admin, await team(admin, "edit-B"), "000123")
    assert (await completion(admin, person["key"])).status_code == 200
    ledger = await financial_snapshot(app)
    keys = (await admin.get("/admin/api/keys")).json()
    members = (await admin.get("/admin/api/memberships")).json()
    async with app.state.db() as db:
        original_hash = (await db.get(StudentAccount, person["student_id"])).password_hash
    url = "/admin/api/students/" + person["student_id"]
    body = {"name": "  正确姓名  ", "student_number": " 000124 "}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as portal:
        assert (await portal.patch(url, json=body)).status_code == 401
        assert (await admin.patch(url, json=body, headers={"X-CSRF-Token": ""})).status_code == 403
        assert (
            await admin.patch(url, json=body, headers={"Origin": "https://wrong.example"})
        ).status_code == 403
        response = await admin.patch(url, json=body)
        assert response.status_code == 200
        assert response.json() == {
            "student_id": person["student_id"],
            "name": "正确姓名",
            "student_number": "000124",
        }
        assert (
            await portal.post(
                "/student/api/login", json={"username": "000123", "password": "student-test-passphrase-2026"}
            )
        ).status_code == 401
        assert (
            await portal.post(
                "/student/api/login", json={"username": "000124", "password": "student-test-passphrase-2026"}
            )
        ).status_code == 200
        assert (await portal.get("/student/api/me")).json()["name"] == "正确姓名"
    assert await financial_snapshot(app) == ledger
    assert (await admin.get("/admin/api/keys")).json() == keys
    updated = (await admin.get("/admin/api/memberships")).json()
    assert {m["id"] for m in updated} == {m["id"] for m in members}
    for member in updated:
        previous = next(m for m in members if m["id"] == member["id"])
        assert member == {**previous, "name": "正确姓名", "student_number": "000124"}
    assert (await completion(admin, person["key"])).status_code == 200
    assert (await admin.patch(url, json=body)).status_code == 200
    async with app.state.db() as db:
        assert (await db.get(StudentAccount, person["student_id"])).password_hash == original_hash
        changes = (await db.scalars(select(Audit).where(Audit.action == "student_update"))).all()
        assert len(changes) == 1
        assert changes[0].detail == {
            "before": {"name": "同学000123", "student_number": "000123"},
            "after": {"name": "正确姓名", "student_number": "000124"},
        }
    for invalid in ({**body, "name": "   "}, {**body, "student_number": ""}, {**body, "budget": 1}):
        assert (await admin.patch(url, json=invalid)).status_code == 422
    assert (await admin.delete(url)).status_code == 200
    assert (await admin.patch(url, json=body)).status_code == 404


async def test_edit_duplicate_number_and_concurrent_claim(admin):
    group = await team(admin)
    people = []
    for number in ("001", "002"):
        response = await admin.post(
            "/admin/api/enroll",
            json={
                "operation_id": "edit-initial-credential-" + number,
                "name": "同学" + number,
                "student_number": number,
                "team_id": group["id"],
                "models": ["lab-chat"],
            },
        )
        assert response.status_code == 201
        people.append(response.json())
    path = "/admin/api/students/" + people[0]["student_id"]
    duplicate = await admin.patch(path, json={"name": "改错", "student_number": "002"})
    assert duplicate.status_code == 409 and duplicate.json()["error"]["message"] == "student_number_exists"
    results = await asyncio.gather(
        *(
            admin.patch(
                "/admin/api/students/" + p["student_id"], json={"name": "更正", "student_number": "003"}
            )
            for p in people
        )
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    winner = next(i for i, r in enumerate(results) if r.status_code == 200)
    async with httpx.AsyncClient(transport=admin._transport, base_url="http://gateway") as portal:
        # Changing the login number never changes an initial password silently.
        response = await portal.post(
            "/student/api/login", json={"username": "003", "password": ("001", "002")[winner]}
        )
        assert response.status_code == 200 and response.json()["must_change_password"]


async def test_edit_name_and_account_from_unified_member_detail(admin, live_server, browser):
    person, _ = await student(admin, await team(admin), "edit-ui")
    page = await browser.new_page(viewport={"width": 1400, "height": 1000})
    await page.goto(live_server + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator('nav button[data-view="members"]').click()
    row = page.get_by_role("row").filter(has_text="edit-ui")
    await row.get_by_role("button", name="管理", exact=True).click()
    await page.locator("#modal").get_by_role("button", name="编辑信息", exact=True).click()
    await expect(page.locator('#modal-fields input[name="name"]')).to_have_value("同学edit-ui")
    await page.locator('#modal-fields input[name="name"]').fill("更正后的姓名")
    await page.screenshot(path=str(artifact("admin-student-edit.png")))
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal")).not_to_be_visible()
    await expect(row).to_contain_text("更正后的姓名")
    await page.locator('nav button[data-view="members"]').click()
    await row.get_by_role("button", name="管理", exact=True).click()
    await page.locator("#modal").get_by_role("button", name="编辑信息", exact=True).click()
    await expect(page.locator('#modal-fields input[name="name"]')).to_have_value("更正后的姓名")
    await page.locator('#modal-fields input[name="student_number"]').fill("000987")
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal")).not_to_be_visible()
    await expect(page.get_by_role("row").filter(has_text="000987")).to_contain_text("更正后的姓名")
    record = next(
        s for s in (await admin.get("/admin/api/students")).json() if s["student_id"] == person["student_id"]
    )
    assert record["student_number"] == "000987"
    await page.close()
