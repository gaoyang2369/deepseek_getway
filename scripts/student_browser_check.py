"""Exercise roster import, mandatory password change, keys and reset over real IP HTTP; MOCK only."""

import asyncio
import json
import os
import secrets
import time

import httpx
from playwright.async_api import async_playwright
from support import admin_password, artifact, mock_identity, test_gateway_url


async def main():
    base = test_gateway_url()
    async with httpx.AsyncClient(base_url=base, trust_env=False) as client:
        identity = await mock_identity(client)
    number = "99" + str(time.time_ns())[-14:]
    new_password = secrets.token_urlsafe(24)
    page_errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=os.getenv("GATEWAY_CHROMIUM_PATH"),
            headless=True,
            args=["--no-sandbox", "--no-proxy-server"],
        )
        admin_context = await browser.new_context()
        admin = await admin_context.new_page()
        admin.on("pageerror", lambda e: page_errors.append(type(e).__name__))
        admin.on("dialog", lambda d: d.accept())
        await admin.goto(base + "/admin")
        await admin.locator('input[name="password"]').fill(admin_password())
        await admin.locator("#login button").click()
        await admin.locator("#console").wait_for(state="visible")
        await admin.locator('nav button[data-view="students"]').click()
        await admin.get_by_role("button", name="＋ 批量导入学号").click()
        await admin.locator('select[name="team_id"]').select_option(identity["team_id"])
        await admin.locator('textarea[name="roster"]').fill(number + ",浏览器模拟同学")
        await admin.locator('select[name="initial_password_mode"]').select_option("student_number")
        async with admin.expect_response(lambda r: r.url.endswith("/students/import")) as imported:
            await admin.locator("#modal-submit").click()
        assert (await imported.value).status == 201
        await admin.locator("#modal").wait_for(state="hidden")
        await admin.get_by_role("cell", name=number, exact=True).wait_for()
        context = await browser.new_context(viewport={"width": 1280, "height": 1000})
        page = await context.new_page()
        page.on("pageerror", lambda e: page_errors.append(type(e).__name__))
        page.on("dialog", lambda d: d.accept())
        await page.goto(base + "/")
        await page.locator('#student-login-form input[name="username"]').fill(number)
        await page.locator('#student-login-form input[name="password"]').fill(number)
        await page.locator("#student-login-form button").click()
        await page.locator("#student-password").wait_for(state="visible")
        assert not await page.locator("#student-profile").is_visible()
        await page.locator('input[name="current_password"]').fill(number)
        await page.locator('input[name="new_password"]').fill(new_password)
        await page.locator('input[name="confirmation"]').fill(new_password)
        await page.locator("#student-password-form button").first.click()
        await page.locator("#student-profile").wait_for(state="visible")
        await page.get_by_role("button", name="领取个人 Key").click()
        await page.locator("#student-key-dialog").wait_for(state="visible")
        old_key = await page.locator("#student-key-value").inner_text()
        assert old_key.startswith("lab_")
        await page.locator("#student-key-close").click()
        assert await page.locator("#student-key-value").inner_text() == ""
        await page.get_by_role("button", name="轮换 Key（旧 Key 将失效）").click()
        await page.locator("#student-confirm-submit").click()
        await page.locator("#student-key-dialog").wait_for(state="visible")
        new_key = await page.locator("#student-key-value").inner_text()
        assert new_key != old_key
        await page.locator("#student-key-close").click()
        await page.screenshot(path=str(artifact("student-preview.png")), full_page=True)
        async with httpx.AsyncClient(base_url=base, trust_env=False) as client:
            assert (
                await client.get("/v1/models", headers={"Authorization": "Bearer " + old_key})
            ).status_code == 401
            assert (
                await client.get("/v1/models", headers={"Authorization": "Bearer " + new_key})
            ).status_code == 200
            row = admin.get_by_role("row").filter(has_text=number)
            await row.get_by_role("button", name="重置密码").click()
            await admin.locator('textarea[name="reason"]').fill("浏览器模拟：已核实身份后重置")
            async with admin.expect_response(lambda r: r.url.endswith("/reset-password")) as reset:
                await admin.locator("#modal-submit").click()
            response = await reset.value
            assert response.status == 200
            temporary = (await response.json())["temporary_password"]
            await admin.locator("#close-modal").click()
            await page.reload()
            await page.locator("#student-login").wait_for(state="visible")
            assert (
                await client.get("/v1/models", headers={"Authorization": "Bearer " + new_key})
            ).status_code == 401
            await page.locator('#student-login-form input[name="username"]').fill(number)
            await page.locator('#student-login-form input[name="password"]').fill(temporary)
            await page.locator("#student-login-form button").click()
            await page.locator("#student-password").wait_for(state="visible")
        assert not page_errors, page_errors
        await browser.close()
    result = {
        "base_url": base,
        "browser": "Chromium",
        "roster_form": "passed",
        "forced_password_change": "passed",
        "key_issue_once_and_rotation": "passed",
        "admin_reset_and_session_key_revocation": "passed",
        "page_errors": page_errors,
        "paid_calls": 0,
    }
    artifact("student-browser-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
