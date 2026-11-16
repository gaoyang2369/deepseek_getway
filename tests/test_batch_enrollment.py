"""Batch membership entry, atomic conflicts and once-only credentials."""

import uuid

from conftest import PASSWORD, team
from playwright.async_api import expect
from sqlalchemy import func, select
from support import artifact

from gateway.models import Audit, Membership, Operation, PersonalKey, Student, StudentAccount


async def test_batch_results_replay_existing_members_and_atomic_conflict(admin, app):
    group = await team(admin)
    body = {
        "operation_id": str(uuid.uuid4()),
        "team_id": group["id"],
        "models": ["lab-chat"],
        "budget": 23000000,
        "initial_password_mode": "random",
        "students": [
            {"student_number": "002", "name": "李四"},
            {"student_number": "001", "name": "张三"},
        ],
    }
    first = await admin.post("/admin/api/students/import", json=body)
    assert first.status_code == 201
    result = first.json()
    assert len(result["initial_passwords"]) == 2
    assert all(s["membership_created"] and s["account_created"] for s in result["students"])
    assert [s["name"] for s in result["students"]] == ["张三", "李四"]
    replay = (await admin.post("/admin/api/students/import", json=body)).json()
    assert replay["replayed"] and replay["initial_passwords"] == []
    async with app.state.db() as db:
        hashes = {s.student_id: s.password_hash for s in (await db.scalars(select(StudentAccount))).all()}
        audit = await db.scalar(select(Audit).where(Audit.action == "student_roster_import"))
        assert audit.detail["new_memberships"] == audit.detail["new_accounts"] == 2
        stored = str((await db.get(Operation, body["operation_id"])).result) + str(audit.detail)
        assert all(p["password"] not in stored for p in result["initial_passwords"])
    repeated = await admin.post(
        "/admin/api/students/import", json={**body, "operation_id": str(uuid.uuid4()), "budget": 1}
    )
    assert repeated.status_code == 201
    assert all(not s["membership_created"] and not s["account_created"] for s in repeated.json()["students"])
    assert repeated.json()["initial_passwords"] == []
    async with app.state.db() as db:
        assert all(m.budget == body["budget"] for m in (await db.scalars(select(Membership))).all())
        assert hashes == {
            s.student_id: s.password_hash for s in (await db.scalars(select(StudentAccount))).all()
        }
        assert await db.scalar(select(func.count()).select_from(PersonalKey)) == 0

    # Conflict after a newly inserted row must roll back the entire batch.
    conflict_body = {
        **body,
        "operation_id": str(uuid.uuid4()),
        "students": [
            {"student_number": "002", "name": "错误姓名"},
            {"student_number": "000", "name": "新成员"},
        ],
    }
    conflict = await admin.post("/admin/api/students/import", json=conflict_body)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["message"] == "student_identity_conflict"
    assert conflict.json()["error"]["details"] == {"line": 1, "student_number": "002"}
    async with app.state.db() as db:
        assert await db.scalar(select(func.count()).select_from(Student)) == 2
        assert await db.get(Operation, conflict_body["operation_id"]) is None
    # The same operation may be corrected after a transaction that never committed.
    conflict_body["students"][0]["name"] = "李四"
    corrected = await admin.post("/admin/api/students/import", json=conflict_body)
    assert corrected.status_code == 201
    assert sum(s["membership_created"] for s in corrected.json()["students"]) == 1

    other_group = await team(admin, "other")
    moved = await admin.post(
        "/admin/api/students/import",
        json={**body, "operation_id": str(uuid.uuid4()), "team_id": other_group["id"]},
    )
    assert moved.status_code == 201
    assert all(s["membership_created"] and not s["account_created"] for s in moved.json()["students"])
    assert moved.json()["initial_passwords"] == []
    for invalid in ([body["students"][0]] * 2, [{"student_number": str(i)} for i in range(101)]):
        response = await admin.post(
            "/admin/api/students/import",
            json={**body, "operation_id": str(uuid.uuid4()), "students": invalid},
        )
        assert response.status_code == 422


async def test_batch_members_ui_preview_validation_and_group_entry(admin, live_server, browser):
    await team(admin, "first")
    group = await team(admin, "second", name="批量测试组", models=["other-model"])
    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
    errors, submissions = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "request",
        lambda r: submissions.append(r.post_data_json) if r.url.endswith("/students/import") else None,
    )
    await page.goto(live_server + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator('nav button[data-view="members"]').click()
    await page.get_by_role("button", name="＋ 批量添加成员", exact=True).click()
    roster = page.locator('textarea[name="roster"]')
    await page.locator('select[name="team_id"]').select_option(group["id"])
    await expect(page.locator('input[name="models"]')).to_have_value("other-model")
    await roster.fill("001,张三\n001,李四")
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal-error")).to_contain_text("第 2 行：学号 001 与第 1 行重复")
    assert not submissions
    await roster.fill("001")
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal-error")).to_contain_text("第 1 行")
    await roster.fill("\n".join(f"{i},学生{i}" for i in range(101)))
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal-error")).to_contain_text("每批最多添加 100 人")
    assert not submissions
    await roster.fill("001\t张三\n002，李四")
    await page.get_by_role("button", name="预览名单", exact=True).click()
    await expect(page.locator("#roster-preview")).to_contain_text("共 2 人")
    await expect(page.locator("#roster-preview tbody tr")).to_have_count(2)
    await page.locator('input[name="budget"]').fill("12")
    await page.locator('select[name="initial_password_mode"]').select_option("random")
    await page.screenshot(path=str(artifact("admin-batch-preview.png")))
    async with page.expect_response(lambda r: r.url.endswith("/students/import")) as response:
        await page.locator("#modal-submit").click()
    result = await (await response.value).json()
    assert (await response.value).status == 201
    await expect(page.locator("#modal-title")).to_have_text("批量添加结果")
    await expect(page.locator("#modal-fields")).to_contain_text("新增 2 位成员，已有 0 位成员")
    for password in result["initial_passwords"]:
        await expect(page.locator("#modal-fields")).to_contain_text(password["password"])
    assert submissions[0]["students"][0] == {"student_number": "001", "name": "张三"}
    members = (await admin.get("/admin/api/memberships")).json()
    assert len(members) == 2
    assert all(m["budget"] == 12000000 and m["team_id"] == group["id"] for m in members)
    await page.locator("#close-modal").click()
    await expect(page.locator("#modal-fields")).to_be_empty()

    # Group entry selects the right group. Existing members are skipped and conflicts are actionable.
    await page.locator('nav button[data-view="teams"]').click()
    await (
        page.get_by_role("row")
        .filter(has_text=group["name"])
        .get_by_role("button", name="批量添加成员")
        .click()
    )
    await expect(page.locator('select[name="team_id"]')).to_have_value(group["id"])
    await roster.fill("001,错误姓名\n003,王五")
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal-error")).to_contain_text("名单第 1 人（学号 001）")
    await expect(page.locator("#modal-error")).to_contain_text("本批未添加任何成员")
    await roster.fill("001,张三\n003,王五")
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal-title")).to_have_text("批量添加结果")
    await expect(page.locator("#modal-fields")).to_contain_text("新增 1 位成员，已有 1 位成员")
    await expect(page.locator("#modal-fields")).to_contain_text("使用原密码")
    assert submissions[1]["operation_id"] == submissions[2]["operation_id"]
    await page.locator("#close-modal").click()

    # The existing simplified student-account import remains available.
    await page.locator('nav button[data-view="students"]').click()
    await page.get_by_role("button", name="＋ 批量导入学号").click()
    await roster.fill("004")
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal-title")).to_have_text("批量添加结果")
    assert not errors
    await page.close()
