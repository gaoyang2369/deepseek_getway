"""Stop ONLY this Compose project's Redis/PostgreSQL; prove paid requests fail closed."""

import asyncio
import json
import os
import subprocess
import time

import httpx
import psycopg
from support import artifact, compose, mock_identity, require_isolated_dependencies, test_gateway_url

require_isolated_dependencies()
base_url = test_gateway_url()


async def prepare():
    async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30) as client:
        return await mock_identity(client)


identity = asyncio.run(prepare())
connection_url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")


def state():
    with psycopg.connect(connection_url) as db:
        return db.execute("SELECT id,spent,reserved FROM budget_periods ORDER BY id").fetchall(), db.execute(
            "SELECT count(*) FROM calls"
        ).fetchone()[0]


with psycopg.connect(connection_url) as db:
    assert db.execute("SELECT 1 FROM teams WHERE id=%s", (identity["team_id"],)).fetchone(), (
        "Gateway and drill must use the same isolated database"
    )
before = state()
body = {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 64}
headers = {"Authorization": "Bearer " + identity["key"]}
results = {}
with httpx.Client(base_url=base_url, trust_env=False, timeout=30) as client:
    for service in ("redis", "postgres"):
        try:
            subprocess.run(compose("stop", service), check=True)
            response = client.post("/v1/chat/completions", headers=headers, json=body)
            assert response.status_code == 503, (service, response.status_code)
            assert client.get("/health/live").status_code == 200
            results[service] = "503 before upstream dispatch"
        finally:
            subprocess.run(compose("start", "--wait", service), check=True)
        for _ in range(30):
            if client.get("/health/ready").status_code == 200:
                break
            time.sleep(0.2)
        assert state() == before, "Rejected requests must not change budgets or create paid calls"
    response = client.post("/v1/chat/completions", headers=headers, json=body)
    response.raise_for_status()
    assert state()[1] == before[1] + 1
report = {
    "services": results,
    "budgets_unchanged_during_failures": True,
    "recovered_paid_path": "MOCK response and persisted request passed",
    "paid_calls": 0,
}
artifact("dependency-results.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
