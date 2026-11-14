"""Official tariff import fails closed and preserves existing financial records."""

import asyncio
import socket
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn
from conftest import PASSWORD, completion, price, student, team
from playwright.async_api import async_playwright, expect
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from test_deletions import financial_snapshot

from gateway import admin as admin_module
from gateway.models import Audit
from gateway.official_prices import PricingFormatError, parse_prices

# Minimal synthetic HTML with the official table's merged-cell layout.
HTML = """<table><tr><th>模型</th><th>deepseek-flash<sup>(1)</sup></th><th>deepseek-v4-pro</th></tr>
<tr><td rowspan="6">价格</td><td rowspan="2">百万tokens输入<br>（缓存命中）</td>
<td>空闲时段</td><td>0.02元</td><td>0.15元</td></tr>
<tr><td>高峰时段</td><td>0.04元</td><td>0.30元</td></tr>
<tr><td rowspan="2">百万tokens输入（缓存未命中）</td><td>空闲时段</td><td>1元</td><td>4.5元</td></tr>
<tr><td>高峰时段</td><td>2元</td><td>9.0元</td></tr>
<tr><td rowspan="2">百万tokens输出</td><td>空闲时段</td><td>4元</td><td>13.5元</td></tr>
<tr><td>高峰时段</td><td>8元</td><td>27.0元</td></tr></table>"""


@pytest.fixture
def official(monkeypatch):
    async def fetch():
        return parse_prices(HTML)

    monkeypatch.setattr(admin_module, "fetch_prices", fetch)


def test_parser_maps_columns_and_bands_without_float_rounding():
    flash, pro = parse_prices(HTML)
    assert flash["peak"] == {"input_hit": 40000, "input_miss": 2000000, "output": 8000000}
    assert pro["off_peak"] == {"input_hit": 150000, "input_miss": 4500000, "output": 13500000}
    swapped = HTML.replace("deepseek-flash", "placeholder").replace("deepseek-v4-pro", "deepseek-flash")
    swapped = swapped.replace("placeholder", "deepseek-v4-pro")
    assert parse_prices(swapped)[0]["model"] == "deepseek-v4-pro"
    assert parse_prices(swapped)[0]["peak"] == flash["peak"]


@pytest.mark.parametrize(
    "html",
    [
        HTML.replace("8元", "$8"),
        HTML.replace("高峰时段", "新计价时段"),
        HTML.replace("27.0元", "0元"),
        HTML.replace("0.15元", "99元"),
        HTML.replace("deepseek-v4-pro", "invalid model"),
        HTML.replace("<td>8元</td>", ""),
    ],
)
def test_changed_or_invalid_official_table_is_rejected(html):
    with pytest.raises(PricingFormatError):
        parse_prices(html)


async def test_import_unlocks_authorized_models_and_removal_preserves_history(admin, app, official):
    await price(admin)
    legacy, _ = await student(admin, await team(admin))
    assert (await completion(admin, legacy["key"])).status_code == 200
    group = await team(admin, "real", models=["deepseek-flash", "deepseek-v4-pro"])
    person, _ = await student(admin, group, "002", models=group["models"])
    headers = {"Authorization": "Bearer " + person["key"]}
    assert (await admin.get("/v1/models", headers=headers)).json()["data"] == []
    before = await financial_snapshot(app)
    teams = (await admin.get("/admin/api/teams")).json()
    keys = (await admin.get("/admin/api/keys")).json()
    result = await admin.post("/admin/api/prices/import-official", json={})
    assert result.status_code == 200
    assert set(result.json()["created"]) == set(group["models"])
    assert result.json()["policy"] == "official_peak_estimate"
    assert [m["id"] for m in (await admin.get("/v1/models", headers=headers)).json()["data"]] == group[
        "models"
    ]
    prices = (await admin.get("/admin/api/prices")).json()
    assert all(p["estimated"] for p in prices)
    assert all(len(p["note"]) <= 500 for p in prices)
    mock_id = next(p["id"] for p in prices if p["model"] == "lab-chat")
    for _ in range(2):
        deleted = await admin.delete("/admin/api/prices/" + mock_id)
        assert deleted.status_code == 200 and deleted.json()["history_preserved"]
    assert {p["model"] for p in (await admin.get("/admin/api/prices")).json()} == set(group["models"])
    history = (await admin.get("/admin/api/prices?include_ended=true")).json()
    assert next(p for p in history if p["id"] == mock_id)["ends_at"]
    assert await financial_snapshot(app) == before
    assert (await admin.get("/admin/api/teams")).json() == teams
    assert (await admin.get("/admin/api/keys")).json() == keys
    repeat = (await admin.post("/admin/api/prices/import-official", json={})).json()
    assert repeat["created"] == [] and set(repeat["unchanged"]) == set(group["models"])
    assert len((await admin.get("/admin/api/prices?include_ended=true")).json()) == 3
    async with app.state.db() as db:
        assert (
            await db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "price_remove"))
            == 1
        )


async def test_import_failure_and_transaction_rollback_leave_prices_unchanged(
    admin, app, official, monkeypatch
):
    await price(admin)
    before = (await admin.get("/admin/api/prices?include_ended=true")).json()

    def break_second_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO prices") and parameters.get("model") == "deepseek-v4-pro":
            raise OperationalError("fault injection", {}, None)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", break_second_insert)
    try:
        assert (await admin.post("/admin/api/prices/import-official", json={})).status_code == 503
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", break_second_insert)
    assert (await admin.get("/admin/api/prices?include_ended=true")).json() == before

    for exception, code in (
        (PricingFormatError("changed"), "official_pricing_format_changed"),
        (httpx.ConnectError("offline"), "official_pricing_unavailable"),
    ):

        async def fail_fetch():
            raise exception

        monkeypatch.setattr(admin_module, "fetch_prices", fail_fetch)
        response = await admin.post("/admin/api/prices/import-official", json={})
        assert response.status_code == 502 and response.json()["error"]["message"] == code
        assert (await admin.get("/admin/api/prices?include_ended=true")).json() == before


async def test_import_and_remove_require_admin_and_csrf(admin, app, official):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as anonymous:
        assert (await anonymous.post("/admin/api/prices/import-official", json={})).status_code == 401
        assert (await anonymous.delete("/admin/api/prices/unknown")).status_code == 401
    admin.headers.pop("X-CSRF-Token")
    assert (await admin.post("/admin/api/prices/import-official", json={})).status_code == 403
    assert (await admin.delete("/admin/api/prices/unknown")).status_code == 403


async def test_browser_import_remove_and_history(admin, app, official):
    await price(admin)
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
            page = await browser.new_page(viewport={"width": 1550, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base + "/admin")
            await page.locator('input[name="password"]').fill(PASSWORD)
            await page.locator("#login button").click()
            await page.locator("#console").wait_for(state="visible")
            await page.locator('nav button[data-view="prices"]').click()
            await page.get_by_role("button", name="从 DeepSeek 官网更新", exact=True).click()
            await expect(page.get_by_role("cell", name="deepseek-flash", exact=True)).to_have_count(2)
            row = page.get_by_role("row").filter(has=page.get_by_role("cell", name="lab-chat", exact=True))
            await row.get_by_role("button", name="删除模型", exact=True).click()
            await page.locator("#confirm-submit").click()
            await expect(page.get_by_role("cell", name="deepseek-flash", exact=True)).to_have_count(2)
            await expect(page.get_by_role("cell", name="lab-chat", exact=True)).to_have_count(0)
            await page.screenshot(path="docs/official-prices-preview.png", full_page=True)
            await page.get_by_role("button", name="查看历史计价", exact=True).click()
            await expect(page.get_by_role("cell", name="lab-chat", exact=True)).to_have_count(1)
            assert "已结束" in await page.locator("#content").inner_text()
            assert not errors
            await browser.close()
    finally:
        server.should_exit = True
        await serving
