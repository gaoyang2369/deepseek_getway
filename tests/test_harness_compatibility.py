"""Replay Harness defaults, verify safe normalization and group budget bounds."""

import asyncio
import json
import logging
import os
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn
from conftest import price, student, team
from fastapi import FastAPI, Request
from sqlalchemy import select

from gateway.models import Call
from gateway.schemas import Chat
from scripts.mock_upstream import chat as mock_chat

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("none", "none"),
        ("low", "low"),
        ("high", "high"),
        ("max", "max"),
        ("minimal", "low"),
        ("medium", "high"),
        ("xhigh", "high"),
    ],
)
def test_official_reasoning_levels_and_aliases(effort, expected):
    request = json.loads((FIXTURES / "harness-default-request.json").read_text())
    request["reasoning_effort"] = effort
    body = Chat.model_validate(request).model_dump(exclude_none=True)
    assert body["reasoning_effort"] == expected
    assert body["messages"][0]["role"] == "system"
    assert "store" not in body


async def capture_mock_requests(app):
    requests = []
    upstream = FastAPI()

    @upstream.post("/v1/chat/completions")
    async def receive(request: Request):
        requests.append(await request.json())
        return await mock_chat(request)

    await app.state.http.aclose()
    app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream))
    return requests


async def test_harness_request_streams_and_clamps_before_budget_reservation(admin, app):
    await price(admin)
    group = await team(admin, max_output=1536)
    person, _ = await student(admin, group)
    requests = await capture_mock_requests(app)
    payload = json.loads((FIXTURES / "harness-default-request.json").read_text())
    payload["model"] = "lab-chat"
    headers = {"Authorization": "Bearer " + person["key"]}
    response = await admin.post("/v1/chat/completions", headers=headers, json=payload)
    assert response.status_code == 200 and "[DONE]" in response.text
    assert len(requests) == 1
    forwarded = requests[0]
    assert forwarded["messages"][0]["role"] == "system"
    assert forwarded["reasoning_effort"] == "max"
    assert forwarded["max_tokens"] == 1536
    assert "store" not in forwarded and "max_completion_tokens" not in forwarded
    listed = (await admin.get("/v1/models", headers=headers)).json()["data"]
    assert listed[0]["max_output_tokens"] == listed[0]["max_tokens"] == 1536
    async with app.state.db() as db:
        call = await db.scalar(select(Call))
        assert call.status == "settled" and call.cost == 188
        assert call.reservation < 20000  # At 128,000 tokens the old ceiling would reserve >384,000.


async def test_unsupported_fields_report_safe_paths_without_inputs(admin, app, caplog):
    caplog.set_level(logging.WARNING, logger="lab_gateway")
    secret = "lab_secret_from_untrusted_input_never_echo"
    payload = {
        "model": "lab-chat",
        "messages": [{"role": "bad_role_" + secret, "content": secret}],
        "store": True,
        secret: secret,
    }
    response = await admin.post("/v1/chat/completions", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["message"] == "invalid_request_parameters"
    assert {item["parameter"] for item in error["details"]} == {
        "messages.0.role",
        "store",
        "unsupported_field",
    }
    assert secret not in response.text and secret not in caplog.text
    assert error["request_id"] in caplog.text
    assert "messages.0.role" in caplog.text
    async with app.state.db() as db:
        assert (await db.scalars(select(Call))).all() == []


@pytest.mark.skipif(
    not os.getenv("PI_AI_TEST_ROOT"), reason="Optional Harness dependency check: set PI_AI_TEST_ROOT"
)
async def test_actual_harness_dependency_streaming(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin, max_output=1024))
    requests = await capture_mock_requests(app)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}/v1"
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        process = await asyncio.create_subprocess_exec(
            "node",
            str(FIXTURES / "harness-client.mjs"),
            env={
                **os.environ,
                "LAB_LLM_BASE_URL": base,
                "LAB_LLM_API_KEY": person["key"],
                "NODE_TLS_REJECT_UNAUTHORIZED": "1",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        assert [r["effort"] for r in result["checks"]] == ["max", "xhigh"]
        assert len(requests) == 2
        assert all(request["max_tokens"] == 1024 for request in requests)
        assert [request["reasoning_effort"] for request in requests] == ["max", "high"]
        assert all(
            "store" not in request and request["messages"][0]["role"] == "system" for request in requests
        )
        Path("docs/harness-client-results.json").write_text(json.dumps(result, indent=2) + "\n")
    finally:
        server.should_exit = True
        await serving
