"""Pagination must use filtered totals and replace rows instead of accumulating them."""

from conftest import PASSWORD, completion, price, student, team
from playwright.async_api import expect
from sqlalchemy import delete, select

from gateway.models import Audit, Call, Event, Rejection, uid


async def seed_records(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    await completion(admin, person["key"])
    async with app.state.db() as db, db.begin():
        original = await db.scalar(select(Call))
        values = {c.name: getattr(original, c.name) for c in Call.__table__.columns if c.name != "id"}
        for n in range(23):
            db.add(Call(id=uid(), **values))
            db.add(Rejection(id=uid(), model="lab-chat", reason="invalid_api_key", http_status=401))
            db.add(Event(dedup=f"pagination-{n}", kind="rate_limit", detail={}, resolved=False))
            db.add(Audit(actor="pagination", action="test", target=str(n), detail={}, request_id=uid()))


async def test_paginated_totals_filters_and_legacy_responses(admin, app):
    await seed_records(admin, app)
    for kind in ("requests", "rejections", "events", "audit"):
        path = "/admin/api/" + kind
        first = (await admin.get(path, params={"paginated": "true", "limit": 10})).json()
        second = (await admin.get(path, params={"paginated": "true", "limit": 10, "offset": 10})).json()
        legacy = (await admin.get(path)).json()
        all_rows = legacy["rows"] if kind == "rejections" else legacy
        assert first["total"] == second["total"] == len(all_rows)
        assert len(first["rows"]) == len(second["rows"]) == 10
        assert not ({r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]})
        last = (await admin.get(path, params={"paginated": "true", "limit": 10, "offset": 20})).json()
        assert len(last["rows"]) == first["total"] - 20
    for kind, filter_ in (
        ("requests", {"status": "pending"}),
        ("events", {"resolved": "true"}),
        ("rejections", {"reason": "budget_exhausted"}),
    ):
        empty = (await admin.get("/admin/api/" + kind, params={"paginated": "true", **filter_})).json()
        assert empty == {"rows": [], "total": 0}
    exported = await admin.get("/admin/api/export.csv", params={"paginated": "true", "limit": 10000})
    assert exported.headers["content-type"].startswith("text/csv") and len(exported.text.splitlines()) == 25


async def test_browser_all_record_tabs_page_size_and_navigation(admin, app, live_server, browser):
    await seed_records(admin, app)
    page = await browser.new_page()
    await page.goto(live_server + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator('#nav button[data-view="records"]').click()
    for kind in ("requests", "rejections", "events", "audit"):
        await page.locator(f'#record-tabs button[data-view="{kind}"]').click()
        rows = page.locator("#content tbody tr")
        await expect(rows).to_have_count(10)
        bars = page.get_by_role("group", name="日志分页")
        await expect(bars).to_have_count(2)
        await expect(bars.first.get_by_role("button", name="上一页", exact=True)).to_be_disabled()
        await bars.last.get_by_role("button", name="下一页", exact=True).click()
        await expect(bars.first).to_contain_text("第 2 /")
        await expect(rows).to_have_count(10)
        total = int((await bars.first.locator(".pagination-summary").inner_text()).split()[1])
        last_page = (total + 9) // 10
        await bars.first.get_by_role("button", name=f"第 {last_page} 页", exact=True).click()
        await expect(bars.first).to_contain_text(f"第 {last_page} / {last_page} 页")
        await expect(rows).to_have_count(total - (last_page - 1) * 10)
        await expect(bars.last.get_by_role("button", name="下一页", exact=True)).to_be_disabled()
        await bars.last.get_by_label("每页条数").select_option("20")
        await expect(bars.first).to_contain_text("第 1 / 2 页")
        await expect(rows).to_have_count(20)
        await expect(bars.first.get_by_label("每页条数")).to_have_value("20")
    await page.locator('#record-tabs button[data-view="events"]').click()
    await page.get_by_role("group", name="日志分页").first.get_by_role("button", name="下一页").click()
    await expect(page.get_by_role("group", name="日志分页").first).to_contain_text("第 2 / 2 页")
    async with app.state.db() as db, db.begin():
        await db.execute(delete(Event).where(Event.dedup.like("pagination-%")))
    await page.locator("#refresh").click()
    await expect(page.get_by_role("group", name="日志分页").first).to_contain_text("共 1 条 · 第 1 / 1 页")
    await expect(page.locator("#content tbody tr")).to_have_count(1)
    await page.locator('#content [name="resolved"]').select_option("true")
    await expect(page.locator("#content tbody tr")).to_have_count(0)
    await expect(page.get_by_role("group", name="日志分页").first).to_contain_text("共 0 条 · 第 1 / 1 页")
    await expect(
        page.get_by_role("group", name="日志分页").last.get_by_role("button", name="下一页")
    ).to_be_disabled()
    await page.close()
