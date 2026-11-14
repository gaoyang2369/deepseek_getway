"""Chinese composition must not submit or interrupt administrative forms."""

import asyncio
import socket
from dataclasses import replace
from pathlib import Path

import uvicorn
from conftest import PASSWORD, team
from playwright.async_api import async_playwright, expect


async def test_budget_short_reason_and_ime_only_save_on_explicit_click(admin, app):
    group = await team(admin, "ime", name="中文输入测试组")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app.state.settings = replace(app.state.settings, public_url=base)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with async_playwright() as playwright:
            binary = next(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
            browser = await playwright.chromium.launch(executable_path=str(binary), args=["--no-sandbox"])
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
                page.get_by_role("row")
                .filter(has_text=group["name"])
                .get_by_role("button", name="调整预算")
                .click()
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
            await cdp.send(
                "Input.imeSetComposition", {"text": "ceshi", "selectionStart": 5, "selectionEnd": 5}
            )
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
            await page.screenshot(path="docs/admin-input-preview.png")
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
            await browser.close()
    finally:
        server.should_exit = True
        await serving
