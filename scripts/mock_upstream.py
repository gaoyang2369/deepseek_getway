"""No-cost DeepSeek fixture. Captures only MOCK credential labels, never real keys/body."""

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
received = []


@app.get("/health")
async def health():
    return {"status": "mock", "requests": len(received)}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    key = request.headers.get("authorization", "")
    if not key.startswith("Bearer MOCK-"):
        return JSONResponse({"error": {"message": "mock_credentials_only"}}, 401)
    received.append(key.removeprefix("Bearer MOCK-"))
    data = await request.json()
    mode = data["messages"][-1].get("content", "")
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": 40,
        "prompt_cache_miss_tokens": 60,
        "completion_tokens_details": {"reasoning_tokens": 10},
    }
    if mode == "error":
        return JSONResponse({"error": {"message": "mock_failure"}}, 503)
    if mode == "timeout":
        await asyncio.sleep(5)
    if not data.get("stream"):
        await asyncio.sleep(0.025)
        message = {"role": "assistant", "content": "模拟回答"}
        if mode == "function-call":
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "mock-function-1",
                        "type": "function",
                        "function": {"name": "local_calculator", "arguments": '{"expression":"1+1"}'},
                    }
                ],
            }
        return {
            "id": "mock-completion",
            "object": "chat.completion",
            "created": 0,
            "model": data["model"],
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if mode == "function-call" else "stop",
                }
            ],
            **({"usage": usage} if mode != "no-usage" else {}),
        }

    async def chunks():
        yield b": keepalive\r\n\r\n"
        for value in ("Hello", " from", " mock"):
            await asyncio.sleep(0.02)
            event = {
                "id": "mock-stream",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": data["model"],
                "choices": [{"index": 0, "delta": {"content": value}, "finish_reason": None}],
            }
            wire = ("data: " + json.dumps(event) + "\r\n\r\n").encode()
            # Split inside the data prefix and JSON, not just on event boundaries.
            for i in range(0, len(wire), 17):
                yield wire[i : i + 17]
        if mode == "slow":
            await asyncio.sleep(5)
        if mode != "no-usage":
            event = {
                "id": "mock-stream",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": data["model"],
                "choices": []
                if mode == "empty-choices"
                else [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            }
            yield ("data: " + json.dumps(event) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")
