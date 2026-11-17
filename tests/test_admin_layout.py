"""Browser regression evidence for menus, essential fields and consumption navigation."""

import json
import os
from pathlib import Path

import pytest
from conftest import PASSWORD, completion, price, student, team
from playwright.async_api import expect
from sqlalchemy import select
from support import artifact

from gateway.models import Call, now, uid

LONG_MODEL = "lab-historical-research-reasoning-model-" + "long-context-" * 5
LONG_TEAM = "跨学科研究与复杂系统长期协作项目组（文本与视觉推理）"


@pytest.fixture
async def layout_data(admin, app):
    await price(admin)
    response = await admin.post(
        "/admin/api/prices",
        json={
            "model": LONG_MODEL,
            "upstream_model": "mock-text",
            "version": "historical-layout-v1",
            "input_miss": 2000000,
            "input_hit": 200000,
            "output": 3000000,
            "note": "Layout fixture only",
        },
    )
    assert response.status_code == 201
    groups = []
    for i in range(12):
        groups.append(
            await team(
                admin,
                f"layout-{i:02d}",
                name=LONG_TEAM if i == 0 else f"项目 {i:02d}",
                models=["lab-chat", LONG_MODEL],
            )
        )
    people = []
    for i in range(12):
        p, _ = await student(admin, groups[i], f"{i:03d}", models=["lab-chat", LONG_MODEL])
        people.append(p)
        await completion(admin, p["key"], model=LONG_MODEL if i == 0 else "lab-chat")
    for group in groups[1:4]:
        await student(admin, group, "000", models=["lab-chat", LONG_MODEL])
    async with app.state.db() as db, db.begin():
        original = await db.scalar(select(Call).where(Call.student_id == people[0]["student_id"]))
        values = {c.name: getattr(original, c.name) for c in Call.__table__.columns if c.name != "id"}
        for _ in range(12):
            db.add(Call(id=uid(), **{**values, "started_at": now()}))
    return groups, people


async def login(page, base):
    if os.getenv("ADMIN_LAYOUT_CAPTURE") == "before":
        from urllib.parse import urlsplit

        from gateway.pages import ASSETS

        async def baseline(route):
            asset = ASSETS.get(urlsplit(route.request.url).path)
            path = Path("artifacts/admin-layout-review/baseline/gateway/static") / asset[0] if asset else None
            if path and path.exists():
                await route.fulfill(path=path)
            else:
                await route.continue_()

        await page.route("**/admin/**", baseline)
    await page.goto(base + "/admin")
    await page.locator('#login input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator(".management-cards").wait_for()


async def go(page, view):
    await page.locator(f'#nav [data-view="{view}"]').click()
    await page.locator("#content table:visible").first.wait_for()


async def test_layout_evidence(layout_data, live_server, browser):
    phase = os.getenv("ADMIN_LAYOUT_CAPTURE", "after")
    folder = artifact("admin-layout-review") / phase
    folder.mkdir(parents=True, exist_ok=True)
    page = await browser.new_page(viewport={"width": 1180, "height": 920})
    await login(page, live_server)
    await go(page, "teams")
    row = page.locator(".team-table tbody tr").first
    before = await row.bounding_box()
    trigger = (
        row.locator("summary") if phase == "before" else row.get_by_role("button", name="更多", exact=True)
    )
    await trigger.click()
    after = await row.bounding_box()
    await page.screenshot(path=str(folder / "teams-menu-1180.png"))
    measurements = {"row_closed_height": before["height"], "row_open_height": after["height"]}
    await trigger.click()
    await page.locator('#nav [data-view="records"]').click()
    await page.locator(".request-table").wait_for()
    await page.screenshot(path=str(folder / "requests-1180.png"))
    await go(page, "members")
    await page.screenshot(path=str(folder / "members-1180.png"))
    await page.locator(".members-table").get_by_role("button", name="管理", exact=True).first.click()
    trigger = (
        page.locator("#modal-fields > .row-actions summary")
        if phase == "before"
        else page.locator("#modal-fields > .row-actions").get_by_role("button", name="更多", exact=True)
    )
    await trigger.click()
    await page.screenshot(path=str(folder / "member-menu-1180.png"))
    (folder / "measurements.json").write_text(json.dumps(measurements, indent=2) + "\n")
    if phase != "before":
        assert abs(before["height"] - after["height"]) < 1
    await page.close()


async def assert_floating_visible(menu, page):
    box = await menu.bounding_box()
    viewport = page.viewport_size
    assert box and box["x"] >= 0 and box["y"] >= 0
    assert box["x"] + box["width"] <= viewport["width"] + 1
    assert box["y"] + box["height"] <= viewport["height"] + 1
    assert await menu.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    # Actual hit testing catches overflow and dialog-layer occlusion, not just CSS visibility.
    assert await menu.evaluate(
        "e => { const r=e.getBoundingClientRect(); return e.contains(document.elementFromPoint(r.x+12,r.y+12)); }"
    )


async def test_action_menu_layout_keyboard_clipping_and_dialog(layout_data, live_server, browser):
    page = await browser.new_page(viewport={"width": 980, "height": 760})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await login(page, live_server)
    await go(page, "teams")
    rows = page.locator(".team-table tbody tr")
    trigger = rows.first.get_by_role("button", name="更多", exact=True)
    geometry = await rows.evaluate_all(
        "rs => rs.slice(0,2).map(r => ({height:r.getBoundingClientRect().height,top:r.getBoundingClientRect().top}))"
    )
    await trigger.click()
    menu = page.get_by_role("menu", name="更多操作")
    await assert_floating_visible(menu, page)
    assert (
        await rows.evaluate_all(
            "rs => rs.slice(0,2).map(r => ({height:r.getBoundingClientRect().height,top:r.getBoundingClientRect().top}))"
        )
        == geometry
    )
    await expect(trigger).to_have_attribute("aria-expanded", "true")
    await trigger.click()
    await expect(menu).to_have_count(0)
    await trigger.focus()
    await page.keyboard.press("ArrowDown")
    await expect(menu.get_by_role("menuitem").first).to_be_focused()
    await page.keyboard.press("End")
    await expect(menu.get_by_role("menuitem").last).to_be_focused()
    await page.keyboard.press("Home")
    await page.keyboard.press("ArrowDown")
    await expect(menu.get_by_role("menuitem").nth(1)).to_be_focused()
    await page.keyboard.press("Escape")
    await expect(trigger).to_be_focused()
    await expect(trigger).to_have_attribute("aria-expanded", "false")
    # Keyboard selection still invokes the original dangerous-action confirmation.
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("End")
    await page.keyboard.press("Enter")
    await expect(page.locator("#confirm-dialog")).to_be_visible()
    await page.locator("#confirm-cancel").click()
    await expect(rows).to_have_count(12)
    await trigger.click()
    await page.locator("#view-title").click()
    await expect(menu).to_have_count(0)
    await trigger.click()
    await page.evaluate("window.scrollBy(0, 40)")
    await page.wait_for_timeout(80)
    await assert_floating_visible(menu, page)
    await page.locator("#toggle-sidebar").click()
    await expect(menu).to_have_count(0)
    await trigger.click()
    await assert_floating_visible(menu, page)
    await page.keyboard.press("Escape")
    await page.locator("#toggle-sidebar").click()
    await trigger.click()
    await rows.nth(1).get_by_role("button", name="更多", exact=True).click()
    await expect(page.get_by_role("menu")).to_have_count(1)
    await expect(trigger).to_have_attribute("aria-expanded", "false")
    await page.keyboard.press("Escape")
    # Last row opens upward near the viewport edge, without changing its height.
    last = rows.last
    await last.scroll_into_view_if_needed()
    height = (await last.bounding_box())["height"]
    await last.get_by_role("button", name="更多", exact=True).click()
    await expect(menu).to_have_attribute("data-placement", "top")
    await assert_floating_visible(menu, page)
    assert (await last.bounding_box())["height"] == height
    await page.set_viewport_size({"width": 920, "height": 720})
    await page.wait_for_timeout(80)
    await assert_floating_visible(menu, page)
    await page.keyboard.press("Escape")
    # An overflow-scrolling table must not clip its top-layer menu.
    await page.locator(".team-table").evaluate('e => {e.style.maxHeight="280px"; e.style.overflow="auto";}')
    await page.evaluate("window.scrollTo(0,0)")
    await rows.first.scroll_into_view_if_needed()
    await trigger.click()
    await assert_floating_visible(menu, page)
    await page.locator(".team-table").evaluate("e => e.scrollTop=300")
    await expect(menu).to_have_count(0)  # Trigger left the table's visible area.
    await go(page, "members")
    await page.locator(".members-table").get_by_role("button", name="管理", exact=True).first.click()
    modal = page.locator("#modal")
    trigger = page.locator("#modal-fields > .row-actions").get_by_role("button", name="更多", exact=True)
    project_top = (await page.locator(".member-project").first.bounding_box())["y"]
    await trigger.click()
    await assert_floating_visible(menu, page)
    assert await menu.evaluate("e => e.parentElement.id") == "modal"
    assert (await page.locator(".member-project").first.bounding_box())["y"] == project_top
    await page.keyboard.press("Escape")
    await expect(modal).to_be_visible()
    await expect(trigger).to_be_focused()
    await trigger.click()
    await menu.get_by_role("menuitem", name="删除学生", exact=True).click()
    await expect(page.locator("#confirm-dialog")).to_be_visible()
    await expect(menu).to_have_count(0)
    await page.locator("#confirm-cancel").click()
    await expect(modal).to_be_visible()
    await page.locator("#close-modal").click()
    assert not errors


async def test_essential_fields_at_all_layout_widths(layout_data, live_server, browser):
    page = await browser.new_page(viewport={"width": 980, "height": 900})
    await login(page, live_server)
    for width in (980, 1180, 1280, 1440):
        await page.set_viewport_size({"width": width, "height": 900})
        for collapsed in (False, True):
            actual = await page.locator("body").evaluate('e => e.classList.contains("sidebar-collapsed")')
            if actual != collapsed:
                await page.locator("#toggle-sidebar").click()
            await page.locator('#nav [data-view="records"]').evaluate("e => e.click()")
            await page.locator(".request-table tbody tr").first.wait_for()
            row = page.locator(".request-table tbody tr").filter(has_text=LONG_MODEL).first
            await expect(row.locator("td").nth(2)).to_be_visible()
            assert "/" in await row.locator("td").nth(2).inner_text()  # Beijing call time.
            for index in range(6):
                await expect(row.locator("td").nth(index)).to_be_visible()
            assert await page.locator(".request-table").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
            await page.screenshot(
                path=str(
                    artifact(
                        f"admin-layout-review/after/requests-{width}-{'collapsed' if collapsed else 'expanded'}.png"
                    )
                )
            )
            await page.locator('#nav [data-view="members"]').evaluate("e => e.click()")
            await page.locator(".members-table").wait_for()
            row = page.locator(".members-table tbody tr").filter(has_text="同学000")
            await expect(row.locator(".project-summary")).to_be_visible()
            await expect(row.locator(".project-summary")).to_contain_text(LONG_TEAM)
            await row.locator(".project-expansion summary").click()
            await expect(row.locator(".project-expansion")).to_contain_text("项目 03")
            await expect(row.get_by_role("button", name="管理", exact=True)).to_be_visible()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert await page.locator(".members-table").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
            await page.screenshot(
                path=str(
                    artifact(
                        f"admin-layout-review/after/members-{width}-{'collapsed' if collapsed else 'expanded'}.png"
                    )
                )
            )
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.locator('#nav [data-view="records"]').evaluate("e => e.click()")
    await page.locator(".request-table").wait_for()
    await expect(page.locator(".request-table tbody tr").first.locator("td").nth(2)).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.screenshot(path=str(artifact("admin-layout-review/after/requests-390.png")))
    await page.locator(".request-table").evaluate("e => e.scrollIntoView({block:'start'})")
    await page.screenshot(path=str(artifact("admin-layout-review/after/requests-390-data.png")))
    await page.locator('#nav [data-view="overview"]').evaluate("e => e.click()")
    await page.locator(".management-cards").wait_for()
    for width, columns in ((1440, 4), (980, 2), (390, 1)):
        await page.set_viewport_size({"width": width, "height": 900})
        assert (
            await page.locator(".management-cards").evaluate(
                'e => getComputedStyle(e).gridTemplateColumns.split(" ").length'
            )
            == columns
        )
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(artifact(f"admin-layout-review/after/overview-{width}.png")))
    await page.locator('#nav [data-view="teams"]').evaluate("e => e.click()")
    await page.locator(".team-table tbody tr").first.get_by_role("button", name="更多", exact=True).click()
    await assert_floating_visible(page.get_by_role("menu"), page)
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.close()


async def test_drill_return_search_filters_and_detail_context(layout_data, live_server, browser):
    groups, people = layout_data
    page = await browser.new_page(viewport={"width": 1280, "height": 680})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await login(page, live_server)
    await go(page, "consumption")
    bar = page.get_by_role("group", name="消费汇总分页")
    await bar.get_by_label("每页条数").select_option("10")
    await bar.get_by_role("button", name="下一页").click()
    await expect(bar).to_contain_text("第 2 / 2 页")
    original_rows = await page.locator(".consumption-table tbody").inner_text()
    drill = page.locator(".consumption-table").get_by_role("button", name="查看构成").first
    await drill.scroll_into_view_if_needed()
    original_scroll = await page.evaluate("scrollY")
    await drill.click()
    await expect(page.locator(".drill-path")).to_contain_text("项目组→成员")
    await expect(page.locator('.segment-tabs button[aria-pressed="true"]')).to_have_text("按成员")
    await page.locator(".consumption-table").get_by_role("button", name="查看构成").first.click()
    await expect(page.locator(".drill-path")).to_contain_text("项目组→成员→模型")
    await page.screenshot(path=str(artifact("admin-layout-review/after/drill-path-1280.png")), full_page=True)
    await page.get_by_role("button", name="查看单次调用 →").click()
    await page.locator(".request-table").get_by_role("button", name="详情").first.click()
    pricing = page.locator("#modal details").filter(has=page.get_by_text("计价依据", exact=True))
    await expect(pricing).not_to_have_attribute("open", "")
    await page.screenshot(path=str(artifact("admin-layout-review/after/detail-1280.png")))
    await pricing.locator("summary").click()
    await expect(pricing).to_contain_text("当时的计价版本")
    await page.screenshot(path=str(artifact("admin-layout-review/after/detail-pricing-1280.png")))
    await page.locator("#close-modal").click()
    await page.get_by_role("button", name="返回消费汇总").click()
    await expect(page.locator(".drill-path")).to_contain_text("项目组→成员→模型")
    await page.get_by_role("button", name="← 返回上一级").click()
    await expect(page.locator('.segment-tabs button[aria-pressed="true"]')).to_have_text("按成员")
    await page.get_by_role("button", name="← 返回上一级").click()
    await expect(bar).to_contain_text("第 2 / 2 页")
    assert await page.locator(".consumption-table tbody").inner_text() == original_rows
    await page.wait_for_timeout(80)
    assert abs(await page.evaluate("scrollY") - original_scroll) < 3
    # Search candidates follow the selected project, including multi-project members.
    await page.locator('select[name="team_id"]').select_option(groups[0]["id"])
    await page.get_by_role("combobox", name="成员", exact=True).click()
    await page.get_by_role("searchbox", name="搜索姓名或学号").fill("000")
    await expect(page.get_by_role("listbox").get_by_role("option")).to_have_count(2)
    await page.get_by_role("option", name="同学000 · 000", exact=True).click()
    await expect(page.locator('input[name="student_id"]')).to_have_value(people[0]["student_id"])
    await page.locator('select[name="team_id"]').select_option(groups[5]["id"])
    await expect(page.locator('input[name="student_id"]')).to_have_value("")
    await expect(page.locator(".filter-selection-note")).to_contain_text("已改为全部成员")
    await page.get_by_role("combobox", name="成员", exact=True).click()
    await expect(page.get_by_role("listbox").get_by_role("option")).to_have_count(2)
    await expect(page.get_by_role("listbox")).to_contain_text("同学005")
    await page.keyboard.press("Escape")
    await page.get_by_role("combobox", name="模型", exact=True).click()
    await page.get_by_role("searchbox", name="搜索模型名称").fill("historical")
    # IME candidate confirmation must not select a model or close the popup.
    await page.get_by_role("searchbox", name="搜索模型名称").dispatch_event(
        "keydown", {"key": "Enter", "isComposing": True}
    )
    await expect(page.locator('input[name="model"]')).to_have_value("")
    await page.get_by_role("searchbox", name="搜索模型名称").dispatch_event(
        "keydown", {"key": "Escape", "isComposing": True}
    )
    await expect(page.get_by_role("listbox")).to_be_visible()
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")
    await expect(page.locator('input[name="model"]')).to_have_value(LONG_MODEL)
    await page.get_by_role("button", name="应用筛选", exact=True).click()
    await expect(page.get_by_text("当前筛选范围没有调用记录，可调整日期、项目或成员后重试。")).to_be_visible()
    await expect(bar).to_contain_text("共 0 条")
    await expect(bar.get_by_role("button")).to_have_count(0)
    # Project action describes exactly where it goes; one batch budget read supplies all rows.
    reads = []
    page.on("request", lambda r: reads.append(r.url) if "/budget-status" in r.url else None)
    await go(page, "teams")
    assert len(reads) == 1
    project_row = page.locator(".team-table tbody tr").filter(has_text=LONG_TEAM)
    await expect(project_row.locator("td").nth(1)).to_contain_text("上限")
    await project_row.get_by_role("button", name="更多", exact=True).click()
    await page.get_by_role("menuitem", name="管理", exact=True).click()
    await expect(page.locator('#modal [name="name"]')).to_have_value(LONG_TEAM)
    await page.locator("#close-modal").click()
    await project_row.get_by_role("button", name="查看消费", exact=True).click()
    await expect(page.locator('select[name="team_id"]')).to_have_value(groups[0]["id"])
    await expect(page.locator('.segment-tabs button[aria-pressed="true"]')).to_have_text("按成员")
    await page.locator(".consumption-table").get_by_role("button", name="查看构成").first.click()
    await expect(page.locator('.segment-tabs button[aria-pressed="true"]')).to_have_text("按模型")
    await expect(page.locator(".drill-path")).to_contain_text("成员→模型")
    await page.evaluate("window.scrollTo(0,0)")
    await page.screenshot(path=str(artifact("admin-layout-review/after/drilldown-1280.png")), full_page=True)
    assert not errors
    await page.close()


async def test_report_options_historical_models_and_monthly_project_totals(layout_data, admin, app):
    from test_deletions import financial_snapshot

    groups, _ = layout_data
    async with app.state.db() as db, db.begin():
        call = await db.scalar(select(Call).limit(1))
        call.model = "retired-model-only-in-call-history"
    before = await financial_snapshot(app)
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as stranger:
        assert (await stranger.get("/admin/api/consumption-options")).status_code == 401
        assert (await stranger.get("/admin/api/budget-status?include_month_cost=true")).status_code == 401
    options = (await admin.get("/admin/api/consumption-options")).json()
    assert LONG_MODEL in options["models"]
    assert "retired-model-only-in-call-history" in options["models"]
    from gateway.billing import period_bounds

    start, end = period_bounds(now(), "month")
    params = {"start": start.isoformat(), "end": end.isoformat(), "group_by": "team"}
    report = (await admin.get("/admin/api/consumption", params=params)).json()
    costs = {r["id"]: r["cost"] for r in report["rows"]}
    budgets = (await admin.get("/admin/api/budget-status?include_month_cost=true")).json()
    assert len(budgets) == len(groups)
    assert all(b["month_cost"] == costs[b["owner_id"]] for b in budgets)
    assert await financial_snapshot(app) == before
