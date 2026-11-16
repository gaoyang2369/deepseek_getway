"""First-login password handoff stays in memory and visibility controls never submit."""

from conftest import team
from playwright.async_api import expect
from support import artifact
from test_student_portal import roster


async def test_first_login_password_handoff_and_visibility(admin, live_server, browser):
    imported, _ = await roster(
        admin, await team(admin), numbers=("password-ui",), initial_password_mode="random"
    )
    temporary = imported["initial_passwords"][0]["password"]
    page = await browser.new_page()
    await page.goto(live_server + "/student")
    await page.locator('[name="username"]').fill("password-ui")
    login_password = page.locator('#student-login-form [name="password"]')
    await login_password.fill(temporary)
    await page.get_by_role("button", name="显示登录密码", exact=True).click()
    await expect(login_password).to_have_attribute("type", "text")
    await page.get_by_role("button", name="隐藏登录密码", exact=True).click()
    await expect(login_password).to_have_attribute("type", "password")
    await page.get_by_role("button", name="登录 →", exact=True).click()
    await expect(page.locator("#student-password")).to_be_visible()
    current = page.locator('[name="current_password"]')
    await expect(current).to_have_value(temporary)
    await expect(current).to_have_attribute("readonly", "")
    await expect(login_password).to_have_value("")
    assert temporary not in await page.evaluate("JSON.stringify({...localStorage, ...sessionStorage})")
    for name, label in (
        ("current_password", "当前密码"),
        ("new_password", "新密码"),
        ("confirmation", "确认新密码"),
    ):
        if name != "current_password":
            await page.locator(f'[name="{name}"]').fill("Lab2026!")
        await page.get_by_role("button", name="显示" + label, exact=True).click()
        await expect(page.locator(f'[name="{name}"]')).to_have_attribute("type", "text")
        await page.get_by_role("button", name="隐藏" + label, exact=True).click()
        await expect(page.locator(f'[name="{name}"]')).to_have_attribute("type", "password")
    await page.locator("#student-password").screenshot(path=str(artifact("student-password-controls.png")))
    # Simulate a password manager replacing even a read-only field after login.
    await current.evaluate("input => input.value = 'wrong-autofill-value'")
    async with page.expect_request(lambda r: r.url.endswith("/student/api/password")) as saving:
        await page.get_by_role("button", name="保存新密码", exact=True).click()
    assert (await saving.value).post_data_json["current_password"] == temporary
    await expect(page.locator("#student-profile")).to_be_visible()
    for name in ("current_password", "new_password", "confirmation"):
        await expect(page.locator(f'[name="{name}"]')).to_have_value("")
        await expect(page.locator(f'[name="{name}"]')).to_have_attribute("type", "password")
    await page.get_by_role("button", name="修改密码", exact=True).click()
    await expect(current).to_have_value("")
    await expect(current).to_be_editable()
    await current.fill("wrong-current")
    await page.locator('[name="new_password"]').fill("Next2026")
    await page.locator('[name="confirmation"]').fill("Next2026")
    await page.get_by_role("button", name="保存新密码", exact=True).click()
    await expect(page.locator("#student-notice")).to_have_text("当前密码不正确。")
    await current.fill("Lab2026!")
    await page.get_by_role("button", name="显示当前密码", exact=True).click()
    await page.get_by_role("button", name="返回", exact=True).click()
    await expect(page.locator("#student-profile")).to_be_visible()
    await expect(current).to_have_value("")
    await expect(current).to_have_attribute("type", "password")
    await page.get_by_role("button", name="退出登录", exact=True).click()
    await page.locator('[name="username"]').fill("password-ui")
    await login_password.fill("Lab2026!")
    await page.get_by_role("button", name="登录 →", exact=True).click()
    await expect(page.locator("#student-profile")).to_be_visible()
    await page.close()


async def test_reload_requires_current_password_without_persisting_initial_secret(
    admin, live_server, browser
):
    await roster(admin, await team(admin), numbers=("reload-ui",))
    page = await browser.new_page()
    await page.goto(live_server + "/student")
    await page.locator('[name="username"]').fill("reload-ui")
    await page.locator('#student-login-form [name="password"]').fill("reload-ui")
    await page.get_by_role("button", name="登录 →", exact=True).click()
    current = page.locator('[name="current_password"]')
    await expect(current).to_have_value("reload-ui")
    await page.reload()
    await expect(page.locator("#student-password")).to_be_visible()
    await expect(current).to_have_value("")
    await expect(current).to_be_editable()
    await current.fill("reload-ui")
    await page.locator('[name="new_password"]').fill("Fresh123")
    await page.locator('[name="confirmation"]').fill("Fresh123")
    await page.get_by_role("button", name="保存新密码", exact=True).click()
    await expect(page.locator("#student-profile")).to_be_visible()
    await page.close()
