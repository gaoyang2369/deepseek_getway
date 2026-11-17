"""Consumption totals, historical prices and drilldowns share one accounting scope."""

import asyncio
import csv
import io
from datetime import datetime, timezone
from decimal import Decimal

from conftest import PASSWORD, completion, price, student, team
from playwright.async_api import expect
from sqlalchemy import select
from support import artifact
from test_deletions import financial_snapshot

from gateway.models import Call, PersonalKey, Price, uid


async def seed_consumption(admin, app):
    await price(admin)
    a, b = await team(admin, "research", name="研究项目"), await team(admin, "test", name="测试项目")
    person, _ = await student(admin, a)
    other, _ = await student(admin, b)
    await completion(admin, person["key"])
    async with app.state.db() as db, db.begin():
        original = await db.scalar(select(Call))
        extra_key = PersonalKey(id=uid(), membership_id=person["membership_id"], digest=uid(), prefix="extra")
        db.add(extra_key)
        await db.flush()
        base = {c.name: getattr(original, c.name) for c in Call.__table__.columns if c.name != "id"}
        db.add(Call(id=uid(), **{**base, "key_id": extra_key.id}))
    await completion(admin, other["key"])
    await completion(admin, person["key"], "no-usage")
    return a, b, person, other


async def test_all_dimensions_lists_and_export_match_with_explicit_test_labels(admin, app):
    a, b, person, _ = await seed_consumption(admin, app)
    before = await financial_snapshot(app)
    assert (
        await admin.put(f"/admin/api/teams/{b['id']}/report-label", json={"is_test": True})
    ).status_code == 200
    assert await financial_snapshot(app) == before
    for scope, expected_cost, expected_calls in (("all", 564, 4), ("regular", 376, 3), ("test", 188, 1)):
        params = {"data_scope": scope, "student_id": person["student_id"]}
        for dimension in ("team", "member", "model", "key"):
            response = await admin.get("/admin/api/consumption", params={**params, "group_by": dimension})
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["summary"]["cost"] == sum(row["cost"] for row in data["rows"]) == expected_cost
            assert data["summary"]["calls"] == expected_calls
            assert data["summary"]["active_members"] == 1
            assert sum(row["cost"] for row in data["trend"]) == expected_cost
            assert data["summary"]["pending_cost"] == (0 if scope == "test" else None)
            if dimension == "member":
                assert len(data["rows"]) == 1  # Multiple Keys AND projects merge into one member.
        calls = (
            await admin.get(
                "/admin/api/requests", params={**params, "paginated": True, "include_summary": True}
            )
        ).json()
        assert calls["total"] == expected_calls
        assert calls["summary"]["cost"] == expected_cost
        assert sum(r["cost"] or 0 for r in calls["rows"]) == expected_cost
        export = await admin.get("/admin/api/export.csv", params=params)
        records = list(csv.DictReader(io.StringIO(export.text.lstrip("\ufeff"))))
        assert len(records) == expected_calls
        assert sum(int(r["cost"] or 0) for r in records) == expected_cost
    # Labels are explicitly reversible; a project's name alone never changes classification.
    await admin.put(f"/admin/api/teams/{b['id']}/report-label", json={"is_test": False})
    assert (await admin.get("/admin/api/consumption", params={"data_scope": "test"})).json()["summary"][
        "calls"
    ] == 0
    await admin.delete("/admin/api/teams/" + a["id"])
    data = (await admin.get("/admin/api/consumption", params={"team_id": a["id"]})).json()
    assert data["summary"]["cost"] == 376 and data["summary"]["pending"] == 1
    row = (await admin.get("/admin/api/requests", params={"team_id": a["id"]})).json()[0]
    detail = (await admin.get("/admin/api/requests/" + row["id"])).json()
    assert detail["team_deleted"] and detail["team_name"] == a["name"]
    assert "ciphertext" not in str(detail) and "digest" not in str(detail)


async def test_time_boundaries_unknown_results_and_combined_running_filter(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    await completion(admin, person["key"])
    async with app.state.db() as db, db.begin():
        original = await db.scalar(select(Call))
        original.started_at = datetime(2026, 10, 31, 16, tzinfo=timezone.utc)
        base = {c.name: getattr(original, c.name) for c in Call.__table__.columns if c.name != "id"}
        for status, outcome in (
            ("pending", "unknown"),
            ("reserved", "in_progress"),
            ("running", "in_progress"),
        ):
            db.add(
                Call(id=uid(), **{**base, "cost": None, "status": status, "outcome": outcome, "usage": None})
            )
        db.add(Call(id=uid(), **{**base, "started_at": datetime(2026, 11, 1, 16, tzinfo=timezone.utc)}))
    params = {"start": "2026-11-01T00:00", "end": "2026-11-02T00:00"}
    result = (await admin.get("/admin/api/consumption", params=params)).json()
    assert result["summary"]["calls"] == 4 and result["summary"]["cost"] == 188
    assert result["summary"]["unknown"] == 1 and result["summary"]["failures"] == 0
    assert result["trend"] == [{"day": "2026-11-01", "cost": 188, "pending": 1}]
    for path in ("/consumption", "/requests", "/export.csv"):
        invalid = await admin.get("/admin/api" + path, params={"start": "invalid"})
        assert invalid.status_code == 400
        invalid = await admin.get(
            "/admin/api" + path, params={"start": params["end"], "end": params["start"]}
        )
        assert invalid.status_code == 400
    running = (await admin.get("/admin/api/requests", params={**params, "status": "in_progress"})).json()
    assert len(running) == 2 and {r["status"] for r in running} == {"reserved", "running"}
    pending = (await admin.get("/admin/api/consumption", params={**params, "status": "pending"})).json()[
        "summary"
    ]
    assert pending["cost"] == 0 and pending["pending_cost"] is None and pending["unknown"] == 1
    assert (await admin.get("/admin/api/consumption", params={"group_by": "wrong"})).status_code == 422
    assert (await admin.get("/admin/api/consumption", params={"data_scope": "wrong"})).status_code == 400


async def test_detail_preserves_price_and_rounds_once_without_claiming_bill_verification(admin, app):
    await price(admin)
    async with app.state.db() as db, db.begin():
        p = await db.scalar(select(Price))
        p.input_miss += 1
        p.input_hit += 1
        p.output += 1
        p.estimated = False
    person, _ = await student(admin, await team(admin))
    await completion(admin, person["key"])
    row = (await admin.get("/admin/api/requests")).json()[0]
    result = (await admin.get("/admin/api/requests/" + row["id"])).json()
    assert result["cost"] == result["calculated_cost"] == 189
    assert sum(Decimal(p["amount_yuan"]) for p in result["components"]) + Decimal(
        result["rounding_yuan"]
    ) == Decimal("0.000189")
    assert [p["tokens"] for p in result["components"]] == [60, 40, 20]
    assert result["cost_basis"] == "rate_estimate" and result["upstream_verified"] is False
    await admin.post(
        "/admin/api/prices",
        json={
            "model": "lab-chat",
            "upstream_model": "mock-text",
            "version": "new",
            "input_miss": 9000000,
            "input_hit": 1000000,
            "output": 9000000,
            "note": "new",
        },
    )
    old = (await admin.get("/admin/api/requests/" + row["id"])).json()
    assert old["price"]["version"] == "mock-v1" and old["components"] == result["components"]
    await completion(admin, person["key"], "no-usage")
    pending = next(r for r in (await admin.get("/admin/api/requests")).json() if r["status"] == "pending")
    unknown = (await admin.get("/admin/api/requests/" + pending["id"])).json()
    assert unknown["cost"] is None and unknown["components"] == []
    await admin.post(
        f"/admin/api/requests/{pending['id']}/reconcile",
        json={"cost": 123, "reason": "根据用量人工核对，尚无上游账单"},
    )
    manual = (await admin.get("/admin/api/requests/" + pending["id"])).json()
    assert manual["cost"] == 123 and manual["cost_basis"] == "manual_reconciliation"
    assert manual["outcome"] == "success" and not manual["upstream_verified"]
    assert manual["reconciliation"]["detail"]["reason"] == "根据用量人工核对，尚无上游账单"
    before = await financial_snapshot(app)
    path = f"/admin/api/requests/{pending['id']}/bill-verification"
    assert (await admin.post(path, json={"cost": 124, "reason": "账单金额不同"})).status_code == 409
    assert (await admin.post(path, json={"cost": 123, "reason": " "})).status_code == 422
    assert (
        await admin.post(
            path, json={"cost": 123, "reason": "账单记录 #test-001"}, headers={"X-CSRF-Token": ""}
        )
    ).status_code == 403
    assert (await admin.post(path, json={"cost": 123, "reason": "账单记录 #test-001"})).status_code == 200
    verified = (await admin.get("/admin/api/requests/" + pending["id"])).json()
    assert (
        verified["upstream_verified"]
        and verified["bill_verification"]["detail"]["reason"] == "账单记录 #test-001"
    )
    assert await financial_snapshot(app) == before


async def test_reporting_reads_leave_budget_and_student_api_unchanged(admin, app):
    await price(admin)
    unused = await team(admin, "unused", budget=200000000)
    person, _ = await student(admin, await team(admin))
    await completion(admin, person["key"])
    snapshot = await financial_snapshot(app)
    for path in ("consumption", "people", "budget-status", "report-labels"):
        response = await admin.get("/admin/api/" + path)
        assert response.status_code == 200, response.text
    budgets = (await admin.get("/admin/api/budget-status")).json()
    assert next(b for b in budgets if b["team_id"] == unused["id"])["limit"] == 200000000
    assert await financial_snapshot(app) == snapshot
    people = (await admin.get("/admin/api/people")).json()
    assert people["total"] == 1 and people["rows"][0]["month_cost"] == 188
    assert "password_hash" not in str(people)
    assert (await completion(admin, person["key"])).status_code == 200


async def test_admin_drilldown_narrow_layout_details_and_pagination(admin, app, live_server, browser):
    a, b, person, _ = await seed_consumption(admin, app)
    # Enough calls to exercise list pagination, while keeping a unique member across Keys.
    async with app.state.db() as db, db.begin():
        original = await db.scalar(select(Call).where(Call.status == "settled").limit(1))
        values = {c.name: getattr(original, c.name) for c in Call.__table__.columns if c.name != "id"}
        for _ in range(15):
            db.add(Call(id=uid(), **values))
    page = await browser.new_page(viewport={"width": 985, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(live_server + "/admin")
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator("#login button").click()
    await expect(page.get_by_text("筛选范围 · 已入账费用", exact=True)).to_be_visible()
    await expect(page.locator(".consumption-table")).to_be_visible()
    assert await page.locator(".consumption-table").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    await page.screenshot(path=str(artifact("admin-overview-985.png")), full_page=True)
    await page.locator('#nav [data-view="consumption"]').click()
    await expect(page.locator(".summary-bar")).to_be_visible()
    cost = await page.locator(".summary-bar").get_attribute("data-cost")
    await page.get_by_role("button", name="按成员", exact=True).click()
    await expect(page.locator("#content tbody tr")).to_have_count(1)
    assert await page.locator(".summary-bar").get_attribute("data-cost") == cost
    await page.locator("#content").get_by_role("button", name="查看构成").click()
    await expect(page.get_by_role("button", name="按模型", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    await page.get_by_role("button", name="查看单次调用 →").click()
    await expect(page.locator(".request-table tbody tr")).to_have_count(10)
    await page.locator(".advanced-filters summary").click()
    labels = await page.locator('select[name="status"] option').all_text_contents()
    assert labels.count("进行中") == 1 and "已入账" in labels
    await page.locator(".advanced-filters summary").click()
    bars = page.get_by_role("group", name="日志分页")
    await bars.first.get_by_role("button", name="下一页").click()
    await expect(bars.first).to_contain_text("第 2 / 2 页")
    first = await page.locator(".request-table tbody").inner_text()
    await page.locator(".request-table").get_by_role("button", name="详情").first.click()
    await expect(page.locator("#modal-title")).to_have_text("单次调用明细")
    await expect(page.locator("#modal-fields")).to_contain_text("上游账单尚未核验")
    await expect(page.locator("#modal-fields pre")).to_be_hidden()
    await page.screenshot(path=str(artifact("admin-call-detail-985.png")), full_page=True)
    await page.get_by_role("button", name="记录上游账单核对").click()
    await page.locator('#modal [name="reason"]').fill("测试账单记录 B-001")
    await page.locator("#modal-submit").click()
    await expect(page.locator("#modal-title")).to_have_text("单次调用明细")
    await expect(page.locator(".detail-tags")).to_contain_text("已与上游账单核对")
    await page.locator("#close-modal").click()
    assert await page.locator(".request-table tbody").inner_text() == first
    await expect(bars.first).to_contain_text("第 2 / 2 页")
    assert await page.locator(".summary-bar").get_attribute("data-cost") == cost
    assert await page.locator(".request-table").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    await page.screenshot(path=str(artifact("admin-requests-985.png")), full_page=True)
    await page.locator('#nav [data-view="members"]').click()
    await expect(page.locator("#content tbody tr")).to_have_count(1)
    await page.get_by_role("button", name="管理", exact=True).click()
    await expect(page.locator("#modal-fields")).to_contain_text("项目归属与个人额度")
    await page.get_by_role("button", name="查看消费构成 →").click()
    await expect(page.get_by_role("button", name="按模型", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    await expect(page.locator('input[name="student_id"]')).to_have_value(person["student_id"])
    await page.get_by_role("button", name="收起侧栏").click()
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert await page.locator(".consumption-table").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    await page.screenshot(path=str(artifact("admin-analysis-mobile.png")), full_page=True)
    assert not errors


async def test_reports_during_student_calls_preserve_admission_and_settlement(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin), concurrency=100)
    responses = await asyncio.gather(
        *(completion(admin, person["key"], stream=i % 2 == 0) for i in range(6)),
        *(admin.get("/admin/api/consumption", params={"include_trend": False}) for _ in range(6)),
    )
    assert all(response.status_code == 200 for response in responses)
    for response in responses[:6:2]:
        assert "data: [DONE]" in response.text
    records = (
        await admin.get("/admin/api/requests", params={"paginated": True, "include_summary": True})
    ).json()
    assert records["total"] == records["summary"]["calls"] == 6
    assert records["summary"]["pending"] == records["summary"]["running"] == 0
    assert records["summary"]["cost"] == sum(r["cost"] for r in records["rows"]) == 6 * 188
    assert all(r["status"] == "settled" and r["outcome"] == "success" for r in records["rows"])
