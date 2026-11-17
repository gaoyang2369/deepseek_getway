"""Real socket checks for incremental SSE, cancellation and official SDK compatibility, MOCK only."""

import asyncio
import json
import time

import httpx
from openai import AsyncOpenAI
from support import artifact, mock_identity, test_gateway_url


async def main():
    base_url = test_gateway_url()
    async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=10) as client:
        identity = await mock_identity(client)
        headers = {"Authorization": "Bearer " + identity["key"]}
        body = {"model": "lab-chat", "messages": [{"role": "user", "content": "slow"}], "stream": True}
        start = time.perf_counter()
        async with client.stream("POST", "/v1/chat/completions", headers=headers, json=body) as response:
            response.raise_for_status()
            request_id = response.headers["x-request-id"]
            async for line in response.aiter_lines():
                if '"content":' in line:
                    first = time.perf_counter() - start
                    break
        assert first < 2, "SSE output was buffered until the slow upstream ended"
        # Nonstream disconnect before upstream responds must cancel its HTTP call too.
        try:
            await client.post(
                "/v1/chat/completions",
                headers=headers,
                json={**body, "stream": False, "messages": [{"role": "user", "content": "timeout"}]},
                timeout=0.2,
            )
            raise AssertionError("Expected client read timeout")
        except httpx.ReadTimeout:
            pass
        await asyncio.sleep(0.6)
        rows = (
            await client.get(
                "/admin/api/requests", params={"student_id": identity["student_id"], "limit": 1000}
            )
        ).json()
        interrupted = next(row for row in rows if row["id"] == request_id)
        assert interrupted["status"] == "pending" and interrupted["cost"] is None, interrupted["status"]
        assert interrupted["reservation"] == 0
        assert rows[0]["status"] == "pending" and rows[0]["reason"] == "client_disconnected"
    async with AsyncOpenAI(
        base_url=base_url + "/v1",
        api_key=identity["key"],
        max_retries=0,
        http_client=httpx.AsyncClient(trust_env=False),
    ) as sdk:
        response = await sdk.chat.completions.create(
            model="lab-chat",
            messages=[{"role": "user", "content": "function-call"}],
            max_tokens=64,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "local_calculator",
                        "description": "Client function",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )
        assert response.usage.prompt_tokens == 100
        message = response.choices[0].message
        assert message.tool_calls[0].function.name == "local_calculator"
        followup = await sdk.chat.completions.create(
            model="lab-chat",
            max_tokens=64,
            messages=[
                {"role": "user", "content": "function-call"},
                message.model_dump(exclude_none=True),
                {"role": "tool", "tool_call_id": "mock-function-1", "content": "2"},
            ],
        )
        assert followup.choices[0].message.content
    result = {
        "network": "real localhost sockets",
        "incremental_sse": "passed",
        "first_delta_seconds": round(first, 3),
        "stream_disconnect": "pending, cost unknown, no budget hold",
        "nonstream_disconnect": "pending, cost unknown, no budget hold",
        "official_python_sdk": "nonstream, stream and client function round trip passed",
        "paid_calls": 0,
    }
    artifact("network-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
