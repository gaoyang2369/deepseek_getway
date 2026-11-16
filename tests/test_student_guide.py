"""Verify student onboarding examples against the isolated mock gateway."""

import asyncio
import json
import os
import sys

import httpx
from conftest import price, team
from playwright.async_api import expect
from support import artifact
from test_student_portal import roster


async def run_example(*command, env):
    process = await asyncio.create_subprocess_exec(
        *command, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(process.communicate(), 20)
    assert process.returncode == 0, err.decode()
    return out.decode()


async def test_guide_metadata_respects_initial_password_and_group_limits(admin, app):
    group = await team(admin, max_output=1024)
    await roster(admin, group, numbers=("001",))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as portal:
        signed_in = await portal.post("/student/api/login", json={"username": "001", "password": "001"})
        assert signed_in.status_code == 200
        portal.headers["X-CSRF-Token"] = signed_in.json()["csrf_token"]
        initial = (await portal.get("/student/api/me")).json()
        assert initial["must_change_password"] and "memberships" not in initial
        changed = await portal.post(
            "/student/api/password",
            json={"current_password": "001", "new_password": "student-guide-test-passphrase-2026"},
        )
        assert changed.status_code == 200
        profile = (await portal.get("/student/api/me")).json()
        assert profile["memberships"][0]["max_output"] == 1024
        assert profile["memberships"][0]["models"] == ["lab-chat"]
        assert "password_hash" not in str(profile)
    script = await admin.get("/student/guide.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]


async def test_browser_guide_copy_switching_and_runnable_examples(admin, app, live_server, browser):
    await price(admin)
    first = await team(admin, "A", name="示例项目组 A", max_output=4096)
    second = await team(admin, "B", name="示例项目组 B", models=["lab-other"], max_output=512)
    await roster(admin, first, numbers=("001",))
    await roster(admin, second, numbers=("001",), models=["lab-other"])
    assert (
        await admin.post(
            "/admin/api/prices",
            json={
                "model": "lab-other",
                "upstream_model": "mock-text",
                "version": "mock-guide-other",
                "input_miss": 2000000,
                "input_hit": 200000,
                "output": 3000000,
                "estimated": True,
                "note": "MOCK ONLY guide test",
            },
        )
    ).status_code == 201
    base = live_server
    context = await browser.new_context(
        permissions=["clipboard-read", "clipboard-write"], viewport={"width": 1440, "height": 1000}
    )
    page = await context.new_page()
    errors, browser_model_requests = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on(
        "request",
        lambda request: browser_model_requests.append(request.url) if "/v1/" in request.url else None,
    )
    await page.goto(base + "/student")
    await page.locator('#student-login-form input[name="username"]').fill("001")
    await page.locator('#student-login-form input[name="password"]').fill("001")
    await page.locator('#student-login-form button[type="submit"]').click()
    await page.locator("#student-password").wait_for(state="visible")
    assert not await page.locator("#student-guide").is_visible()
    await expect(page.locator('input[name="current_password"]')).to_have_value("001")
    await page.locator('input[name="new_password"]').fill("student-guide-test-passphrase-2026")
    await page.locator('input[name="confirmation"]').fill("student-guide-test-passphrase-2026")
    await page.locator('#student-password-form button[type="submit"]').click()
    await page.locator("#guide-title").wait_for(state="visible")
    await expect(page.locator("#student-memberships")).to_contain_text("本月已用：¥0.000000（已结算）")
    for hidden_label in ("个人预算", "；预留", "；可用"):
        await expect(page.locator("#student-memberships")).not_to_contain_text(hidden_label)
    assert await page.locator("[role=tab]").count() == 6
    member = page.locator(".student-membership").filter(has_text="示例项目组 A")
    async with page.expect_response(lambda r: r.url.endswith("/student/api/keys")) as issued:
        await member.get_by_role("button", name="领取个人 Key").click()
    key = (await (await issued.value).json())["key"]
    await page.locator("#student-key-dialog").wait_for(state="visible")
    await page.locator("#student-key-copy").click()
    await expect(page.locator("#student-key-copy")).to_have_text("已复制 ✓")
    await page.locator("#student-key-guide").click()
    await page.locator("#student-key-dialog").wait_for(state="hidden")
    assert key not in await page.locator("body").inner_text()
    await page.locator("#guide-tab-harness").click()
    await expect(page.locator("#guide-pane-harness > ol > li")).to_have_count(4)
    await expect(page.locator("#guide-harness")).not_to_be_visible()
    await expect(page.locator("#guide-environment")).not_to_be_visible()
    await expect(page.locator("#guide-pane-harness")).to_contain_text("完成后即可使用")
    await page.locator("#student-guide").screenshot(path=str(artifact("student-harness-simple.png")))
    await page.get_by_text("高级配置（可选，界面接入无需操作）", exact=True).click()
    await expect(page.locator("#guide-harness")).to_be_visible()
    harness = await page.locator("#guide-harness").inner_text()
    assert f'baseURL: "{base}/v1"' in harness
    assert "supportsStore: false" in harness and "supportsDeveloperRole: false" in harness
    assert "maxTokens: 4096" in harness
    assert "max: max" not in harness and key not in harness
    await page.locator("#guide-group").select_option(label="示例项目组 B")
    await page.get_by_text("高级配置（可选，界面接入无需操作）", exact=True).click()
    harness = await page.locator("#guide-harness").inner_text()
    assert 'id: "lab-other"' in harness and "maxTokens: 512" in harness
    assert "示例项目组 B" in await page.locator("#guide-scope").inner_text()
    await page.locator("#guide-group").select_option(label="示例项目组 A")
    await page.locator("#guide-tab-node").click()
    node_code = await page.locator("#guide-node").inner_text()
    checked = await asyncio.create_subprocess_exec(
        "node",
        "--input-type=module",
        "--check",
        stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, check_error = await checked.communicate(node_code.encode())
    assert checked.returncode == 0, check_error.decode()
    await page.locator("#guide-tab-node").press("ArrowLeft")
    assert await page.locator("#guide-tab-python").get_attribute("aria-selected") == "true"
    python_code = await page.locator("#guide-python").inner_text()
    await page.evaluate("Object.defineProperty(window,'isSecureContext',{value:false,configurable:true})")
    await page.get_by_role("button", name="复制保存为 demo.py · python demo.py", exact=True).click()
    assert (
        await page.get_by_role("button", name="复制保存为 demo.py · python demo.py", exact=True).inner_text()
        == "已复制 ✓"
    )
    assert await page.evaluate("navigator.clipboard.readText()") == python_code
    env = {
        **os.environ,
        "LAB_LLM_API_KEY": key,
        "LAB_LLM_BASE_URL": base + "/v1",
        "LAB_LLM_MODEL": "lab-chat",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    assert "模拟回答" in await run_example(sys.executable, "-c", python_code, env=env)
    await page.locator("#guide-tab-http").click()
    models_command = await page.locator("#guide-model-check").inner_text()
    result = json.loads(await run_example("bash", "-c", models_command, env=env))
    assert result["data"][0]["id"] == "lab-chat"
    chat_command = await page.locator("#guide-http").inner_text()
    assert (
        json.loads(await run_example("bash", "-c", chat_command, env=env))["choices"][0]["message"]["content"]
        == "模拟回答"
    )
    await page.locator("#guide-shell").select_option("powershell")
    assert "Invoke-RestMethod" in await page.locator("#guide-model-check").inner_text()
    assert "-AsSecureString" in await page.locator("#guide-environment").inner_text()
    await page.locator("#guide-tab-agent").click()
    await page.get_by_text("最小 Agent 工具调用循环（Python）", exact=True).click()
    agent_code = await page.locator("#guide-agent").inner_text()
    calls = []

    async def upstream(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "status-1",
                        "type": "function",
                        "function": {"name": "get_project_status", "arguments": "{}"},
                    }
                ],
            }
            reason = "tool_calls"
        else:
            assert payload["messages"][-1]["role"] == "tool"
            assert payload["messages"][-1]["tool_call_id"] == "status-1"
            assert json.loads(payload["messages"][-1]["content"])["status"] == "开发中"
            message = {"role": "assistant", "content": "工具调用成功"}
            reason = "stop"
        return httpx.Response(
            200,
            json={
                "id": "mock-guide",
                "object": "chat.completion",
                "created": 0,
                "model": "lab-chat",
                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    await app.state.http.aclose()
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    assert "工具调用成功" in await run_example(
        sys.executable, "-c", python_code.split("reply = ")[0] + agent_code, env=env
    )
    assert len(calls) == 2
    assert not browser_model_requests
    await page.locator("#guide-tab-client").click()
    await page.locator("#student-guide").screenshot(path=str(artifact("student-guide-preview.png")))
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.locator("#student-guide").scroll_into_view_if_needed()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await page.locator("#student-guide").screenshot(path=str(artifact("student-guide-mobile-preview.png")))
    assert not errors, errors
