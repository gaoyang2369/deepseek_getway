"""Exercise the real admin page in Chromium without recording credentials or personal keys."""

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from playwright.async_api import async_playwright
from support import admin_password, artifact, mock_identity, test_gateway_url

ROOT = Path(__file__).resolve().parents[1]


async def main(base_url):
    base_url = test_gateway_url(base_url)
    async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30) as client:
        identity = await mock_identity(client)
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=os.getenv("GATEWAY_CHROMIUM_PATH"),
            headless=True,
            args=["--no-sandbox", "--no-proxy-server"],
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(type(e).__name__))
        await page.goto(base_url.rstrip("/") + "/admin")
        await page.locator('input[name="password"]').fill(admin_password())
        await page.locator("#login button").click()
        await page.locator("#console").wait_for(state="visible")
        await page.get_by_text("筛选范围 · 已入账费用", exact=True).wait_for()
        await page.screenshot(path=str(artifact("admin-preview.png")), full_page=True)
        visited = []
        for name in ("teams", "members", "consumption", "budgets", "requests", "prices", "events", "audit", "overview"):
            if name in ("requests", "rejections", "events", "audit"):
                await page.locator('#nav button[data-view="records"]').click()
            await page.locator(f'nav button[data-view="{name}"]').click()
            await page.locator("#content > p.muted").wait_for(state="hidden")
            assert not await page.locator("#notice").inner_text(), f"View {name} displayed an error"
            visited.append(name)
        # Test a real low-risk form update on the MOCK project, then close it.
        await page.locator('nav button[data-view="teams"]').click()
        await page.get_by_role("row").filter(has_text=identity["team_name"]).get_by_role("button", name="更多", exact=True).click()
        await (
            page.get_by_role("menuitem", name="调整预算")
            .click()
        )
        await page.locator('#modal input[name="amount"]').fill("100")
        await page.locator('#modal textarea[name="reason"]').fill("浏览器模拟验收：保持原组上限")
        await page.locator("#modal-submit").click()
        await page.locator("#modal").wait_for(state="hidden")
        assert not errors, errors
        await page.locator("#logout").click()
        await page.locator("#login-screen").wait_for(state="visible")
        await browser.close()
    result = {
        "browser": "Chromium",
        "base_url": base_url,
        "visited": visited,
        "form": "budget update",
        "logout": "passed",
        "page_errors": errors,
    }
    artifact("browser-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=None)
    asyncio.run(main(parser.parse_args().base_url))
