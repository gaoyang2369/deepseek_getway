"""No-cost network benchmark: comparable direct MOCK vs gateway at 1/10/30 concurrency."""

import asyncio
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]


def percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 2) if ordered else None


async def main():
    password = (ROOT / "secrets/demo_admin_password").read_text().strip()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:18081", timeout=30, trust_env=False) as admin:
        r = await admin.post("/admin/api/login", json={"username": "admin", "password": password})
        r.raise_for_status()
        admin.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        teams = (await admin.get("/admin/api/teams")).json()
        team = next(t for t in teams if t["code"] == "demo_A")
        key_file = ROOT / "secrets/benchmark_client.json"
        if key_file.exists():
            identity = json.loads(key_file.read_text())
        else:
            r = await admin.post(
                "/admin/api/enroll",
                json={
                    "operation_id": str(uuid.uuid4()),
                    "name": "模拟性能测试",
                    "student_number": "demo_benchmark",
                    "kind": "service",
                    "owner": "本机模拟验收",
                    "team_id": team["id"],
                    "models": ["lab-chat"],
                    "budget": 10000000,
                    "rpm": 10000,
                    "concurrency": 100,
                },
            )
            r.raise_for_status()
            identity = r.json()
            os.umask(0o077)
            key_file.write_text(json.dumps(identity))
        results = []
        run_start = datetime.now(timezone.utc).isoformat()
        for concurrency in (1, 10, 30):
            for target, base, key, model in (
                ("direct_mock", "http://127.0.0.1:18082/v1", "MOCK-A-credential", "mock-text"),
                ("gateway", "http://127.0.0.1:18081/v1", identity["key"], "lab-chat"),
            ):
                async with AsyncOpenAI(
                    base_url=base,
                    api_key=key,
                    max_retries=0,
                    timeout=30,
                    http_client=httpx.AsyncClient(trust_env=False),
                ) as sdk:

                    async def run():
                        start, first = time.perf_counter(), None
                        try:
                            response = await sdk.chat.completions.create(
                                model=model,
                                messages=[{"role": "user", "content": "hello"}],
                                stream=True,
                                max_tokens=128,
                                stream_options={"include_usage": True},
                            )
                            async for chunk in response:
                                if first is None and any(c.delta.content for c in chunk.choices):
                                    first = (time.perf_counter() - start) * 1000
                            await response.close()
                            return first, (time.perf_counter() - start) * 1000, False
                        except Exception:
                            return None, None, True

                    outcomes = await asyncio.gather(*(run() for _ in range(concurrency)))
                firsts = [x[0] for x in outcomes if x[0] is not None]
                totals = [x[1] for x in outcomes if x[1] is not None]
                results.append(
                    {
                        "target": target,
                        "concurrency": concurrency,
                        "requests": concurrency,
                        "errors": sum(x[2] for x in outcomes),
                        "ttft_p50_ms": percentile(firsts, 0.5),
                        "ttft_p95_ms": percentile(firsts, 0.95),
                        "total_p50_ms": percentile(totals, 0.5),
                        "total_p95_ms": percentile(totals, 0.95),
                    }
                )
        for _ in range(100):
            calls = (
                await admin.get(
                    "/admin/api/requests",
                    params={"student_id": identity["student_id"], "limit": 10000, "start": run_start},
                )
            ).json()
            if len(calls) == 41 and all(c["status"] == "settled" for c in calls):
                break
            await asyncio.sleep(0.05)
        admission = [c["admission_ms"] for c in calls if c["admission_ms"] is not None]
        settled = sum(c["status"] == "settled" and c["cost"] == 188 for c in calls)
        assert len(calls) == 41 and settled == 41, f"Benchmark accounting incomplete: {settled}/41 settled"
        report = {
            "mode": "mock only; no paid calls",
            "input": "one user message: hello",
            "max_output_tokens": 128,
            "network": "localhost",
            "results": results,
            "settled_gateway_requests": settled,
            "admission_p50_ms": percentile(admission, 0.5),
            "admission_p95_ms": percentile(admission, 0.95),
        }
        (ROOT / "docs/performance-results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
