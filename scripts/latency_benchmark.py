"""Disposable real-socket MOCK benchmark; run via scripts/local.py.

Compares SDK direct/gateway against the SAME mock process. Creates and removes
one unique database, isolated identities and Redis DB 13 keys. Never uses the
running demo gateway or an external provider. --source-root can test a snapshot.
"""

import argparse
import asyncio
import json
import math
import os
import socket
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
import psycopg
from openai import AsyncOpenAI
from psycopg import sql
from redis.asyncio import Redis
from support import artifact, require_isolated_dependencies

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def percentile(values, p):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * p) - 1)], 2) if values else None


async def ready(url, process):
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(200):
            if process.poll() is not None:
                raise RuntimeError("Isolated process exited before readiness")
            try:
                if (await client.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise RuntimeError("Isolated process readiness timeout")


async def benchmark(args, env, gateway_port, mock_port):
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    async with httpx.AsyncClient(base_url=gateway_url, trust_env=False, timeout=30) as admin:

        async def post(path, data):
            response = await admin.post(path, json=data)
            response.raise_for_status()
            return response.json()

        login = await post(
            "/admin/api/login",
            {
                "username": "admin",
                "password": (ROOT / "secrets/demo_admin_password").read_text().strip(),
            },
        )
        admin.headers["X-CSRF-Token"] = login["csrf_token"]
        await post(
            "/admin/api/prices",
            {
                "model": "lab-chat",
                "upstream_model": "mock-text",
                "version": "latency-mock-v1",
                "input_miss": 2000000,
                "input_hit": 200000,
                "output": 3000000,
                "estimated": True,
                "note": "Isolated latency benchmark MOCK ONLY",
            },
        )
        people = []
        for group in range(3):
            team = await post(
                "/admin/api/teams",
                {
                    "name": "latency",
                    "code": f"latency-{group}",
                    "upstream_key": "MOCK-latency-credential",
                    "models": ["lab-chat"],
                    "budget": 10**12,
                    "rpm": 10000,
                    "concurrency": 100,
                },
            )
            members = []
            for number in range(30 if group == 0 else 10):
                members.append(
                    await post(
                        "/admin/api/enroll",
                        {
                            "operation_id": str(uuid.uuid4()),
                            "name": "latency mock",
                            "kind": "service",
                            "owner": "isolated latency verification",
                            "student_number": f"latency-{group}-{number}",
                            "team_id": team["id"],
                            "models": ["lab-chat"],
                            "budget": 10**10,
                            "rpm": 10000,
                            "concurrency": 100,
                        },
                    )
                )
            people.append(members)
        scenarios = {
            "same_member": [people[0][0]] * 30,
            "same_team": people[0],
            "three_teams": people[0][:10] + people[1] + people[2],
        }
        results, calls_seen = [], []
        async with (
            AsyncOpenAI(
                base_url=gateway_url + "/v1",
                api_key="MOCK-placeholder",
                max_retries=0,
                timeout=30,
                http_client=httpx.AsyncClient(trust_env=False),
            ) as gateway,
            AsyncOpenAI(
                base_url=f"http://127.0.0.1:{mock_port}/v1",
                api_key="MOCK-latency-credential",
                max_retries=0,
                timeout=30,
                http_client=httpx.AsyncClient(trust_env=False),
            ) as direct,
        ):

            async def run(target, person):
                started, first = time.perf_counter(), None
                sdk = gateway if target == "gateway" else direct
                try:
                    response = await sdk.chat.completions.create(
                        model="lab-chat" if target == "gateway" else "mock-text",
                        messages=[{"role": "user", "content": args.content}],
                        max_tokens=128,
                        stream=True,
                        stream_options={"include_usage": True},
                        extra_headers={"Authorization": "Bearer " + person["key"]}
                        if target == "gateway"
                        else {},
                    )
                    if target == "gateway":
                        calls_seen.append(response.response.headers["x-request-id"])
                    async for chunk in response:
                        if first is None and any(
                            c.delta.content
                            or getattr(c.delta, "reasoning_content", None)
                            or c.delta.tool_calls
                            for c in chunk.choices
                        ):
                            first = (time.perf_counter() - started) * 1000
                    await response.close()
                    if first is None:
                        return {"error": "missing_effective_delta"}
                    return {"first_ms": first, "total_ms": (time.perf_counter() - started) * 1000}
                except Exception as exc:
                    return {"error": f"{type(exc).__name__}:{getattr(exc, 'status_code', '')}"}

            async def wait_settled(ids):
                for _ in range(400):
                    with psycopg.connect(
                        env["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
                    ) as db:
                        rows = db.execute(
                            "SELECT status,admission_ms FROM calls WHERE id=ANY(%s)", (ids,)
                        ).fetchall()
                    if len(rows) == len(ids) and all(r[0] == "settled" for r in rows):
                        return rows
                    await asyncio.sleep(0.025)
                raise AssertionError("Settlement backlog did not drain in 10 seconds")

            def summarize(scenario, concurrency, target, rows):
                good = [r for r in rows if "error" not in r]
                return {
                    "scenario": scenario,
                    "concurrency": concurrency,
                    "target": target,
                    "requests": len(rows),
                    "errors": len(rows) - len(good),
                    "error_types": dict(Counter(r["error"] for r in rows if "error" in r)),
                    "ttft_p50_ms": percentile([r["first_ms"] for r in good], 0.5),
                    "ttft_p95_ms": percentile([r["first_ms"] for r in good], 0.95),
                    "total_p95_ms": percentile([r["total_ms"] for r in good], 0.95),
                }

            # Warm imports, SDK, HTTP pools and budget periods; excluded from samples.
            for target in ("direct", "gateway"):
                outcomes = await asyncio.gather(
                    *(run(target, person) for group in people for person in group)
                )
                assert all("error" not in r for r in outcomes), "Warmup failed"
            await wait_settled(calls_seen)
            for scenario, persons in scenarios.items():
                for concurrency in (1, 10, 30):
                    samples = {"direct": [], "gateway": []}
                    first_call = len(calls_seen)
                    drain_times = []
                    for repeat in range(args.rounds):
                        # Alternate ordering so warm/cache/time drift does not always favor one path.
                        targets = ("direct", "gateway") if repeat % 2 == 0 else ("gateway", "direct")
                        for target in targets:
                            samples[target].extend(
                                await asyncio.gather(
                                    *(run(target, person) for person in persons[:concurrency])
                                )
                            )
                        if not args.continuous:
                            drain_started = time.perf_counter()
                            await wait_settled(calls_seen[first_call:])
                            drain_times.append((time.perf_counter() - drain_started) * 1000)
                    for target, rows in samples.items():
                        results.append(summarize(scenario, concurrency, target, rows))
                    # Wait for streaming finalization, then read only this case's timings.
                    ids = calls_seen[first_call:]
                    timings = await wait_settled(ids)
                    admission = [r[1] for r in timings if r[1] is not None]
                    results[-1]["admission_p95_ms"] = percentile(admission, 0.95)
                    results[-1]["settlement_drain_p95_ms"] = percentile(drain_times, 0.95)
            if args.steady:
                for target in ("direct", "gateway"):
                    tasks, started = [], time.perf_counter()
                    first_call = len(calls_seen)
                    for i in range(100):
                        await asyncio.sleep(max(0, started + i / 10 - time.perf_counter()))
                        tasks.append(asyncio.create_task(run(target, people[0][i % 30])))
                    rows = await asyncio.gather(*tasks)
                    results.append(summarize("steady_10_rps", None, target, rows))
                    if target == "gateway":
                        timings = await wait_settled(calls_seen[first_call:])
                        results[-1]["admission_p95_ms"] = percentile(
                            [r[1] for r in timings if r[1] is not None], 0.95
                        )
            with psycopg.connect(env["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")) as db:
                counts = db.execute(
                    "SELECT count(*),count(*) FILTER(WHERE status='settled' AND cost=188) FROM calls"
                ).fetchone()
                reserved = db.execute("SELECT coalesce(sum(reserved),0) FROM budget_periods").fetchone()[0]
                ledger = db.execute("SELECT count(*) FROM ledger").fetchone()[0]
            assert counts[0] == counts[1] == len(calls_seen) and reserved == 0 and ledger == 2 * counts[0]
            return {
                "mode": "isolated same-mock real-socket SDK benchmark; no paid calls",
                "rounds": args.rounds,
                "workload": "back-to-back rounds, settlements overlap"
                if args.continuous
                else "isolated bursts; settlement drain between rounds",
                "steady_10_rps": args.steady,
                "warmup_requests_per_path": 50,
                "content_bytes": len(args.content.encode()),
                "source_root": str(args.source_root),
                "results": results,
                "gateway_metrics": (await admin.get("/admin/api/metrics")).json(),
                "accounting": {
                    "calls": counts[0],
                    "settled": counts[1],
                    "reserved": int(reserved),
                    "ledger": ledger,
                },
                "notes": [
                    "Warm samples only; each concurrency=1 case has rounds samples.",
                    "Difference of p95 values is not a paired per-request overhead quantile.",
                    "Original code admission_ms excludes commit; optimized code includes it.",
                ],
            }


async def main(args):
    require_isolated_dependencies()
    env = os.environ.copy()
    assert "@127.0.0.1:18543/" in env["DATABASE_URL"], "Use scripts/local.py with isolated test containers"
    assert env.get("GATEWAY_ENV") == "demo", "MOCK benchmark requires demo mode"
    name = "latency_bench_" + uuid.uuid4().hex[:12]
    base_url = env["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(base_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env["DATABASE_URL"] = env["DATABASE_URL"].rsplit("/", 1)[0] + "/" + name
    env["REDIS_URL"] = env["REDIS_URL"].rsplit("/", 1)[0] + "/13"
    gp, mp = port(), port()
    env["UPSTREAM_URL"] = f"http://127.0.0.1:{mp}/v1"
    env["PUBLIC_URL"] = f"http://127.0.0.1:{gp}"
    processes = []
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=args.source_root,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        for module, bind_port, cwd in (
            ("scripts.mock_upstream:app", mp, ROOT),
            ("gateway.main:create_app", gp, args.source_root),
        ):
            command = [
                sys.executable,
                "-m",
                "uvicorn",
                module,
                "--host",
                "127.0.0.1",
                "--port",
                str(bind_port),
                "--no-access-log",
            ]
            if module.endswith("create_app"):
                command.append("--factory")
            processes.append(
                subprocess.Popen(
                    command, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            )
        await ready(f"http://127.0.0.1:{mp}/health", processes[0])
        await ready(f"http://127.0.0.1:{gp}/health/live", processes[1])
        report = await benchmark(args, env, gp, mp)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        # Only this benchmark's unique team IDs appear in this temporary DB.
        with psycopg.connect(base_url.rsplit("/", 1)[0] + "/" + name) as db:
            exists = db.execute("SELECT to_regclass('teams')").fetchone()[0]
            teams = db.execute("SELECT id FROM teams").fetchall() if exists else []
        async with Redis.from_url(env["REDIS_URL"]) as redis:
            for (tid,) in teams:
                keys = [key async for key in redis.scan_iter(match="rate:{" + tid + "}:*")]
                if keys:
                    await redis.delete(*keys)
        with psycopg.connect(base_url, autocommit=True) as db:
            db.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
    report["temporary_database_removed"] = True
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if any(r["errors"] for r in report["results"]):
        raise SystemExit("Request errors recorded in report; this run is not a zero-error pass")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--content", default="hello")
    parser.add_argument("--continuous", action="store_true", help="Do not drain settlements between bursts")
    parser.add_argument(
        "--steady", action="store_true", help="Also test 100 requests at a fixed 10 requests/s"
    )
    parser.add_argument("--output", type=Path, default=artifact("latency-optimized-results.json"))
    args = parser.parse_args()
    assert args.rounds > 0
    asyncio.run(main(args))
