"""Chinese composition must not submit or interrupt administrative forms."""

import asyncio

from conftest import PASSWORD, completion, price, student, team
from playwright.async_api import expect
from support import artifact


async def test_budget_short_reason_and_ime_only_save_on_explicit_click(admin, app, live_server, browser):
    group = await team(admin, "ime", name="中文输入测试组")
    base = live_server
    page = await browser.new_page(viewport={"width": 1200, "height": 900})
    changes, errors = [], []
    page.on(
        "request",
        lambda request: changes.append(request.url)
        if "/budgets/" in request.url and request.method == "POST"
        else None,
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(base + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator("#console").wait_for(state="visible")
    await page.locator('nav button[data-view="teams"]').click()
    await (
        page.get_by_role("row").filter(has_text=group["name"]).get_by_role("button", name="调整预算").click()
    )
    form, reason = page.locator("#modal-form"), page.locator('textarea[name="reason"]')
    await page.evaluate("""() => {
      window.inputEvents = {invalid: 0, compositionstart: 0, compositionend: 0};
      for (const name of Object.keys(window.inputEvents)) {
        document.querySelector('#modal-form').addEventListener(name, () => window.inputEvents[name]++, true);
      }
    }""")
    assert await form.evaluate("f => f.noValidate")
    assert not await reason.get_attribute("minlength")
    await reason.focus()
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Input.imeSetComposition", {"text": "ceshi", "selectionStart": 5, "selectionEnd": 5})
    await reason.press("Space")
    await form.evaluate("f => f.requestSubmit()")
    await cdp.send("Input.insertText", {"text": "测试"})
    assert not changes
    await expect(page.locator("#modal-error")).to_be_hidden()
    await reason.fill("测试")
    await reason.press("Space")
    await reason.press("Enter")
    assert "\n" in await reason.input_value()
    assert await reason.evaluate("e => document.activeElement === e")
    assert not changes
    await expect(page.locator("#modal-error")).to_be_hidden()
    await reason.fill("   ")
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal-error")).to_have_text("请填写调整原因。")
    assert not changes
    await page.locator('input[name="amount"]').fill("101")
    await reason.fill("测试")
    await page.screenshot(path=str(artifact("admin-input-preview.png")))
    async with page.expect_response(lambda response: "/budgets/team/" in response.url) as saved:
        await page.locator("#modal-submit").click()
    assert (await saved.value).status == 200
    await page.locator("#modal").wait_for(state="hidden")
    assert len(changes) == 1
    events = await page.evaluate("window.inputEvents")
    assert events["invalid"] == 0
    assert events["compositionstart"] >= 1 and events["compositionend"] >= 1
    assert not errors
    audit = (await admin.get("/admin/api/audit")).json()
    updated = next(row for row in audit if row["action"] == "budget_update")
    assert updated["detail"]["reason"] == "测试"
    assert updated["detail"]["after"] == 101000000


async def test_stale_refresh_cannot_replace_visible_form_data(admin, live_server, browser):
    group = await team(admin, "race", name="原项目组")
    page = await browser.new_page()
    await page.goto(live_server + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator('nav button[data-view="members"]').click()
    await page.get_by_role("button", name="＋ 添加人员").wait_for()
    first_started, release_first, first_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = 0

    async def teams_response(route):
        nonlocal requests
        requests += 1
        old = requests == 1
        if old:
            first_started.set()
            await release_first.wait()
        await route.fulfill(json=[{**group, "name": "过期项目组" if old else "最新项目组"}])
        if old:
            first_finished.set()

    await page.route("**/admin/api/teams?include_deleted=true", teams_response)
    try:
        await page.locator("#refresh").click()
        await asyncio.wait_for(first_started.wait(), 5)
        await page.locator("#refresh").click()
        await page.get_by_role("button", name="＋ 添加人员").wait_for()
        release_first.set()
        await asyncio.wait_for(first_finished.wait(), 5)
        # Drain the fulfilled response and both loads before using the visible action.
        await page.wait_for_timeout(100)
        await page.get_by_role("button", name="＋ 添加人员").click()
        await expect(page.locator('#modal-fields select[name="team_id"] option')).to_have_text("最新项目组")
    finally:
        release_first.set()
        await page.close()


async def test_every_admin_module_loads_with_populated_data(admin, live_server, browser):
    await price(admin)
    person, _ = await student(admin, await team(admin, "module-smoke"))
    assert (await completion(admin, person["key"])).status_code == 200
    page = await browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(live_server + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    for view in (
        "overview",
        "teams",
        "members",
        "students",
        "budgets",
        "requests",
        "prices",
        "events",
        "audit",
    ):
        if view in ("requests", "rejections", "events", "audit"):
            await page.locator('#nav button[data-view="records"]').click()
        await page.locator(f'nav button[data-view="{view}"]').click()
        await page.locator("#content table").first.wait_for()
        await expect(page.locator("#notice")).to_be_empty()
    assert not errors
    await page.close()


async def test_form_retry_reuses_operation_and_key_result_is_readonly(admin, live_server, browser):
    await team(admin, "form-retry")
    page = await browser.new_page()
    await page.goto(live_server + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await page.locator('nav button[data-view="members"]').click()
    await page.get_by_role("button", name="＋ 添加人员").click()
    for name, value in (("name", "验证服务"), ("student_number", "retry-service"), ("owner", "负责人")):
        await page.locator(f'#modal-fields [name="{name}"]').fill(value)
    await page.locator('#modal-fields [name="kind"]').select_option("service")
    submissions = []

    async def fail_first(route):
        submissions.append(route.request.post_data_json)
        if len(submissions) == 1:
            await route.fulfill(status=503, json={"error": {"message": "database_unavailable"}})
        else:
            await route.continue_()

    await page.route("**/admin/api/enroll", fail_first)
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal-error")).to_be_visible()
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal-title")).to_have_text("服务 Key 已签发")
    await expect(page.locator("#modal-submit")).to_be_hidden()
    assert submissions[0] == submissions[1]
    assert len(submissions[0]["operation_id"]) == 32
    await page.locator("#close-modal").click()
    await page.get_by_role("button", name="＋ 添加人员").click()
    await expect(page.locator("#modal-submit")).to_be_visible()
    operation = await page.locator("#modal-form").get_attribute("data-operation")
    assert operation != submissions[0]["operation_id"]
    await page.close()
