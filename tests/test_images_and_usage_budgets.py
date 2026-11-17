import base64
import json

import httpx
import pytest
from conftest import completion, price, student, team
from sqlalchemy import func, select

from gateway.models import BudgetPeriod, Call, Event, Price
from tests.test_harness_compatibility import capture_mock_requests


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("role", ["user", "system", "tool"])
async def test_images_and_long_agent_context_are_forwarded(admin, app, stream, role):
    await price(admin)
    person, _ = await student(admin, await team(admin), budget=1)
    requests = await capture_mock_requests(app)
    # The gateway validates the encoding; the provider validates the image bytes.
    data_url = "data:image/png;base64," + base64.b64encode(b"mock-image" * 40000).decode()
    parts = [
        {"type": "text", "text": "Describe these images"},
        {"type": "image_url", "image_url": {"url": data_url, "detail": "original"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg", "detail": "low"}},
    ]
    messages = [{"role": "user", "content": "历史内容" * 120} for _ in range(501)]
    messages.append(
        {"role": role, "content": parts, **({"tool_call_id": "tool-1"} if role == "tool" else {})}
    )
    response = await admin.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + person["key"]},
        json={"model": "lab-chat", "messages": messages, "stream": stream},
    )
    assert response.status_code == 200, response.text
    assert requests[0]["messages"] == messages
    if stream:
        assert response.text.endswith("data: [DONE]\n\n")
    async with app.state.db() as db:
        call = await db.scalar(select(Call))
        assert call.status == "settled" and call.cost == 188 and call.reservation == 0
        assert data_url not in json.dumps(call.usage)
        assert await db.scalar(select(func.sum(BudgetPeriod.reserved))) == 0
        assert (
            await db.scalar(
                select(func.count()).select_from(Event).where(Event.kind == "reservation_exceeded")
            )
            == 0
        )
    assert (await completion(admin, person["key"])).status_code == 402


@pytest.mark.parametrize(
    "image",
    [
        {"url": "file:///private/file.png"},
        {"url": "data:image/png;base64,not-base64"},
        {"url": "data:text/plain;base64,aGVsbG8="},
        {"url": "https://example.com/photo.png", "detail": "unsupported"},
        {"url": "https://example.com/photo.png", "extra": "secret"},
        {"url": 123},
    ],
)
async def test_invalid_image_parameters_do_not_dispatch_or_echo(admin, app, image):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    requests = await capture_mock_requests(app)
    response = await completion(admin, person["key"], [{"type": "image_url", "image_url": image}])
    assert response.status_code == 422
    assert not requests and "private/file" not in response.text and "not-base64" not in response.text


async def test_pro_images_are_rejected_without_upstream_cost(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    async with app.state.db() as db, db.begin():
        (await db.scalar(select(Price))).upstream_model = "deepseek-v4-pro"
    requests = await capture_mock_requests(app)
    response = await completion(
        admin, person["key"], [{"type": "image_url", "image_url": {"url": "https://example.com/photo.png"}}]
    )
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "model_does_not_support_images"
    assert not requests
    async with app.state.db() as db:
        call = await db.scalar(select(Call))
        assert call.cost == 0 and call.status == "settled"


async def test_missing_usage_does_not_hold_budget_or_finished_concurrency(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin, concurrency=1), concurrency=1, budget=1)
    assert (await completion(admin, person["key"], "no-usage")).status_code == 200
    budgets = (await admin.get("/admin/api/budgets")).json()
    assert all(p["reserved"] == 0 and p["available"] == p["limit"] for p in budgets)
    assert (await completion(admin, person["key"])).status_code == 200
    assert (await completion(admin, person["key"])).status_code == 402


async def test_budget_boundary_is_inclusive(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin), budget=188)
    assert (await completion(admin, person["key"])).status_code == 200
    assert (await completion(admin, person["key"])).status_code == 402


async def test_admin_request_limit_stays_small(admin, app):
    response = await admin.post("/admin/api/teams", content=b"x" * (app.state.settings.max_body + 1))
    assert response.status_code == 413


async def test_python_sdk_can_send_inline_image(admin, app):
    from openai import AsyncOpenAI

    await price(admin)
    person, _ = await student(admin, await team(admin))
    requests = await capture_mock_requests(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        sdk = AsyncOpenAI(api_key=person["key"], base_url="http://gateway/v1", http_client=http)
        response = await sdk.chat.completions.create(
            model="lab-chat",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
        )
        assert response.usage.prompt_tokens == 100
    assert requests[0]["messages"][0]["content"][1]["type"] == "image_url"


async def test_upgrade_releases_legacy_holds_without_losing_charges(admin, app):
    import asyncio
    import os
    import subprocess
    import sys
    import time
    import uuid

    from gateway.admission import admit
    from gateway.billing import settle
    from gateway.models import Ledger

    await price(admin)
    person, _ = await student(admin, await team(admin), concurrency=10)
    url = app.state.settings.database_url
    assert url.rsplit("/", 1)[1].startswith("lab_gateway_test_")

    async def migrate(direction, revision):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", direction, revision],
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    await migrate("downgrade", "c42e8a910d65")
    try:
        legacy = []
        for _ in range(3):
            call, _, _ = await admit(
                app,
                "Bearer " + person["key"],
                {"model": "lab-chat", "messages": [{"role": "user", "content": "legacy"}]},
                str(uuid.uuid4()),
                "127.0.0.1",
                time.perf_counter(),
            )
            assert call.reservation > 0
            legacy.append(call)
        await settle(app.state.db, legacy[0].id, manual_cost=188)
        await settle(app.state.db, legacy[1].id, reason="missing_usage")
    finally:
        await migrate("upgrade", "head")

    async with app.state.db() as db:
        assert (await db.get(Call, legacy[0].id)).cost == 188
        assert (await db.get(Call, legacy[0].id)).reservation == legacy[0].reservation
        for old in legacy[1:]:
            call = await db.get(Call, old.id)
            assert call.reservation == 0 and call.cost is None
        assert await db.scalar(select(func.sum(BudgetPeriod.spent))) == 376
        assert await db.scalar(select(func.sum(BudgetPeriod.reserved))) == 0
        assert await db.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "reserve")) == 6
        releases = (await db.scalars(select(Ledger).where(Ledger.kind == "reserve_release"))).all()
        assert len(releases) == 2 and all(
            r.amount == -sum(c.reservation for c in legacy[1:]) for r in releases
        )
    for old in legacy[1:]:
        assert await settle(app.state.db, old.id, manual_cost=100)
        assert not await settle(app.state.db, old.id, manual_cost=999)
    assert (await completion(admin, person["key"])).status_code == 200


@pytest.mark.parametrize(
    "upstream, inputs", [("deepseek-flash", ["text", "image"]), ("deepseek-v4-pro", ["text"])]
)
async def test_model_capabilities_use_upstream_mapping(admin, app, upstream, inputs):
    await price(admin)
    person, _ = await student(admin, await team(admin, max_output=32768))
    async with app.state.db() as db, db.begin():
        (await db.scalar(select(Price))).upstream_model = upstream
    response = await admin.get("/v1/models", headers={"Authorization": "Bearer " + person["key"]})
    model = response.json()["data"][0]
    assert model["input"] == inputs and model["context_window"] == 1_000_000
    assert model["max_output_tokens"] == 32768
