"""Roster pagination and filtering include all matches, not just the visible page."""

from conftest import PASSWORD, team
from playwright.async_api import expect
from sqlalchemy import select
from support import artifact

from gateway.models import Membership, PersonalKey, Student, StudentAccount, now, uid


async def seed_people(admin, app):
    a, b = await team(admin, "people-A"), await team(admin, "people-B")
    async with app.state.db() as db, db.begin():
        people = []
        for i in range(25):
            person = Student(id=uid(), name=f"同学{i:02d}", student_number=f"00{i:03d}")
            db.add(person)
            people.append(person)
        db.add(Student(name="已删除学生", student_number="deleted", deleted_at=now()))
        await db.flush()
        for i, person in enumerate(people):
            if i < 24:
                db.add(
                    StudentAccount(
                        student_id=person.id,
                        password_hash="not-a-login-hash",
                        must_change_password=i % 2 == 0,
                    )
                )
                member = Membership(
                    id=uid(),
                    student_id=person.id,
                    team_id=a["id"] if i < 23 else b["id"],
                    models=["lab-chat"],
                )
                db.add(member)
                await db.flush()
                db.add(
                    PersonalKey(
                        membership_id=member.id, prefix=f"prefix-{i:02d}", digest=uid(), models=["lab-chat"]
                    )
                )
        # Same student in two groups must not duplicate student-account search results.
        member = Membership(id=uid(), student_id=people[0].id, team_id=b["id"], models=["lab-chat"])
        db.add(member)
        await db.flush()
        db.add(PersonalKey(membership_id=member.id, prefix="prefix-other", digest=uid(), models=["lab-chat"]))
    return a, b


async def test_people_page_totals_filters_and_legacy_api(admin, app):
    a, b = await seed_people(admin, app)
    for kind, identity in (("students", "student_id"), ("memberships", "id"), ("keys", "id")):
        path = "/admin/api/" + kind
        first = (await admin.get(path, params={"paginated": "true", "limit": 10})).json()
        second = (await admin.get(path, params={"paginated": "true", "offset": 10, "limit": 10})).json()
        last = (await admin.get(path, params={"paginated": "true", "offset": 20, "limit": 10})).json()
        legacy = (await admin.get(path)).json()
        assert isinstance(legacy, list) and len(legacy) == 25
        assert first["total"] == second["total"] == last["total"] == 25
        assert len(first["rows"]) == len(second["rows"]) == 10 and len(last["rows"]) == 5
        assert len({r[identity] for page in (first, second, last) for r in page["rows"]}) == 25
        for invalid in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            assert (await admin.get(path, params={"paginated": "true", **invalid})).status_code == 422
    for kind in ("memberships", "keys"):
        result = (
            await admin.get("/admin/api/" + kind, params={"paginated": "true", "team_id": b["id"]})
        ).json()
        assert result["total"] == len(result["rows"]) == 2
        assert (
            await admin.get("/admin/api/" + kind, params={"paginated": "true", "team_id": "missing"})
        ).json() == {"total": 0, "rows": []}
        if kind == "keys":
            assert {k["member"]["name"] for k in result["rows"]} == {"同学00", "同学23"}
            assert all("digest" not in k for k in result["rows"])
    for q, count in (("同学2", 5), ("00024", 1), ("  同学23  ", 1), ("%", 0), ("_", 0), ("已删除", 0)):
        response = await admin.get("/admin/api/students", params={"q": q, "paginated": "true"})
        assert response.status_code == 200
        assert response.json()["total"] == count
        assert "password_hash" not in response.text
    orphan = (await admin.get("/admin/api/students", params={"q": "00024", "paginated": "true"})).json()[
        "rows"
    ][0]
    assert orphan["must_change_password"] is None
    async with app.state.db() as db, db.begin():
        person = await db.scalar(select(Student).where(Student.student_number == "00024"))
        person.name = "Alice_100%"
    for q in ("alice", "_100%"):
        assert (await admin.get("/admin/api/students", params={"q": q, "paginated": "true"})).json()[
            "total"
        ] == 1


async def test_people_ui_filter_search_pages_and_deletion(admin, app, live_server, browser):
    a, b = await seed_people(admin, app)
    page = await browser.new_page(viewport={"width": 1400, "height": 1000})
    errors, requests = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(live_server + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator("#console").wait_for(state="visible")
    page.on("request", lambda r: requests.append(r.url))
    await page.locator('nav button[data-view="members"]').click()
    bars = page.get_by_role("group", name="成员分页", exact=True)
    keybars = page.get_by_role("group", name="密钥分页", exact=True)
    rows = page.locator("#content table").first.locator("tbody tr")
    keyrows = page.locator("#content table").last.locator("tbody tr")
    await expect(rows).to_have_count(10)
    await expect(keyrows).to_have_count(10)
    await expect(bars).to_have_count(1)
    await expect(keybars).to_have_count(1)
    await expect(page.locator('thead select[name="member_team"]')).to_have_count(1)
    await expect(page.locator("main footer")).to_have_count(0)
    await bars.last.get_by_role("button", name="下一页").click()
    await expect(bars.first).to_contain_text("第 2 / 3 页")
    await expect(keybars.first).to_contain_text("第 1 / 3 页")
    await keybars.first.get_by_role("button", name="第 3 页", exact=True).click()
    await expect(keyrows).to_have_count(5)
    await page.get_by_label("按项目组查看").select_option(b["id"])
    await expect(rows).to_have_count(2)
    await expect(keyrows).to_have_count(2)
    await expect(bars.first).to_contain_text("共 2 条 · 第 1 / 1 页")
    await expect(keybars.first).to_contain_text("共 2 条 · 第 1 / 1 页")
    await expect(bars.first.get_by_role("button", name="下一页")).to_be_disabled()
    await page.get_by_role("button", name="＋ 批量添加成员", exact=True).click()
    await expect(page.locator('#modal-fields [name="team_id"]')).to_have_value(b["id"])
    await page.locator("#close-modal").click()
    await page.get_by_label("按项目组查看").select_option(a["id"])
    await bars.first.get_by_label("每页条数").select_option("20")
    await expect(rows).to_have_count(20)
    await expect(keyrows).to_have_count(10)
    await bars.last.get_by_role("button", name="下一页").click()
    await expect(rows).to_have_count(3)
    await page.screenshot(path=str(artifact("admin-people-paging.png")))

    await page.locator('nav button[data-view="students"]').click()
    studentbars = page.get_by_role("group", name="学生账号分页", exact=True)
    await expect(studentbars).to_have_count(1)
    studentrows = page.locator("#content tbody tr")
    await expect(studentrows).to_have_count(10)
    await studentbars.last.get_by_role("button", name="第 3 页", exact=True).click()
    await expect(studentrows).to_have_count(5)
    search = page.get_by_label("学号 / 姓名", exact=True)
    await search.fill("同学2")
    await page.get_by_role("button", name="查询", exact=True).click()
    await expect(studentrows).to_have_count(5)
    await expect(studentbars.first).to_contain_text("第 1 / 1 页")
    await search.fill("00024")
    await search.press("Enter")
    await expect(studentrows).to_have_count(1)
    await expect(studentrows).to_contain_text("待设置初始密码")
    await page.screenshot(path=str(artifact("admin-student-search.png")))
    await search.fill("不存在")
    await page.get_by_role("button", name="查询", exact=True).click()
    await expect(studentrows).to_have_count(0)
    await expect(studentbars.first).to_contain_text("共 0 条")
    await page.get_by_role("button", name="清空搜索", exact=True).click()
    await expect(studentrows).to_have_count(10)
    await studentbars.first.get_by_label("每页条数").select_option("20")
    await expect(studentrows).to_have_count(20)
    await studentbars.last.get_by_role("button", name="下一页").click()
    await expect(studentrows).to_have_count(5)
    # Removing the last page via another admin action clamps to the remaining page.
    for row in (await admin.get("/admin/api/students", params={"q": "同学2"})).json():
        assert (await admin.delete("/admin/api/students/" + row["student_id"])).status_code == 200
    await page.locator("#refresh").click()
    await expect(studentbars.first).to_contain_text("共 20 条 · 第 1 / 1 页")
    await expect(studentrows).to_have_count(20)
    assert not errors
    assert not any(
        "/memberships?include_deleted=true" in url or "/students/accounts" in url for url in requests
    )
    await page.close()
