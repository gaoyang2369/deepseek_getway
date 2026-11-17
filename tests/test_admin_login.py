import logging

from conftest import PASSWORD
from playwright.async_api import expect


async def test_failed_login_is_visible_and_can_be_corrected(client, live_server, browser, caplog):
    caplog.set_level(logging.WARNING, logger="lab_gateway")
    page = await browser.new_page()
    await page.goto(live_server + "/admin")
    wrong = "wrong-password-never-log-this"
    await page.locator('#login input[name="password"]').fill(wrong)
    async with page.expect_response(lambda r: r.url.endswith("/admin/api/login")) as response:
        await page.locator("#login button").click()
    assert (await response.value).status == 401
    await expect(page.locator("#login-screen")).to_be_visible()
    await expect(page.locator("#console")).to_be_hidden()
    await expect(page.locator("#notice")).to_be_visible()
    await expect(page.locator("#notice")).to_contain_text("管理员账号或密码不正确")
    assert "admin_login_failed" in caplog.text and "reason=invalid_login" in caplog.text
    assert wrong not in caplog.text
    await page.locator('#login input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await expect(page.locator("#console")).to_be_visible()
    await expect(page.locator("#notice")).to_be_empty()


async def test_origin_and_rate_errors_have_safe_diagnostics(client, app, caplog):
    caplog.set_level(logging.WARNING, logger="lab_gateway")
    response = await client.post(
        "/admin/api/login",
        headers={"Origin": "http://wrong-origin.example"},
        json={"username": "admin", "password": PASSWORD},
    )
    assert response.status_code == 403 and "reason=invalid_origin" in caplog.text
    from gateway.security import digest

    key = "login:" + digest("127.0.0.1", app.state.settings.hmac_key)
    await app.state.redis.set(key, 10, ex=300)
    response = await client.post("/admin/api/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 429 and "reason=login_rate_limit" in caplog.text
    assert PASSWORD not in caplog.text
