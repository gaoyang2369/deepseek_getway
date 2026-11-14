"""SIGKILL an isolated gateway mid-stream, restart, and verify durable reservation recovery."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


async def main():
    env = os.environ.copy()
    base_url = env["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    name = "lab_gateway_crash_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    with psycopg.connect(base_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env["DATABASE_URL"] = env["DATABASE_URL"].rsplit("/", 1)[0] + "/" + name
    env["REDIS_URL"] = env["REDIS_URL"].rsplit("/", 1)[0] + "/14"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True)

    def start():
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "gateway.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "19081",
                "--no-access-log",
                "--no-proxy-headers",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def wait_ready(client, process):
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Isolated gateway exited before readiness")
            try:
                if (await client.get("/health/live")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
        raise RuntimeError("Isolated gateway did not start")

    process = start()
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:19081", trust_env=False, timeout=10
        ) as client:
            await wait_ready(client, process)
            r = await client.post(
                "/admin/api/login",
                json={
                    "username": "admin",
                    "password": (ROOT / "secrets/demo_admin_password").read_text().strip(),
                },
            )
            r.raise_for_status()
            client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
            r = await client.post(
                "/admin/api/prices",
                json={
                    "model": "lab-chat",
                    "upstream_model": "mock-text",
                    "version": "crash-mock-v1",
                    "input_miss": 2000000,
                    "input_hit": 200000,
                    "output": 3000000,
                    "estimated": True,
                    "note": "MOCK crash drill only",
                },
            )
            r.raise_for_status()
            r = await client.post(
                "/admin/api/teams",
                json={
                    "name": "crash mock",
                    "code": "crash",
                    "upstream_key": "MOCK-crash-credential",
                    "models": ["lab-chat"],
                    "budget": 10000000,
                },
            )
            r.raise_for_status()
            team_id = r.json()["id"]
            r = await client.post(
                "/admin/api/enroll",
                json={
                    "operation_id": str(uuid.uuid4()),
                    "name": "崩溃模拟",
                    "student_number": "mock-crash-001",
                    "team_id": team_id,
                    "models": ["lab-chat"],
                    "budget": 1000000,
                },
            )
            r.raise_for_status()
            identity = r.json()
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + identity["key"]},
                json={"model": "lab-chat", "stream": True, "messages": [{"role": "user", "content": "slow"}]},
            ) as response:
                response.raise_for_status()
                request_id = response.headers["x-request-id"]
                async for line in response.aiter_lines():
                    if '"content":' in line:
                        process.send_signal(signal.SIGKILL)
                        process.wait(timeout=5)
                        break
            with psycopg.connect(base_url.rsplit("/", 1)[0] + "/" + name) as db:
                before = db.execute(
                    "SELECT status,reservation,member_period_id FROM calls WHERE id=%s", (request_id,)
                ).fetchone()
            assert before[0] == "reserved" and before[1] > 0
            at = time.perf_counter()
            process = start()
            await wait_ready(client, process)
            with psycopg.connect(base_url.rsplit("/", 1)[0] + "/" + name) as db:
                after = db.execute(
                    "SELECT status,reservation,cost FROM calls WHERE id=%s", (request_id,)
                ).fetchone()
                budget = db.execute(
                    "SELECT spent,reserved FROM budget_periods WHERE id=%s", (before[2],)
                ).fetchone()
                ledgers = db.execute("SELECT kind,count(*) FROM ledger GROUP BY kind").fetchall()
            assert after == ("pending", before[1], None) and budget == (0, before[1])
            assert ledgers == [("reserve", 2)]
            report = {
                "signal": "SIGKILL",
                "database": name,
                "recovered_status": after[0],
                "reservation_preserved": True,
                "no_automatic_refund": True,
                "no_double_settlement": True,
                "restart_seconds": round(time.perf_counter() - at, 3),
                "paid_calls": 0,
            }
            (ROOT / "docs/crash-results.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)


if __name__ == "__main__":
    asyncio.run(main())
