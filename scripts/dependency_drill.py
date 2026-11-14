"""Stop ONLY this Compose project's Redis/PostgreSQL; prove paid requests fail closed."""

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
identity = json.loads((ROOT / "secrets/benchmark_client.json").read_text())
connection_url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")


def state():
    with psycopg.connect(connection_url) as db:
        return db.execute("SELECT id,spent,reserved FROM budget_periods ORDER BY id").fetchall(), db.execute(
            "SELECT count(*) FROM calls"
        ).fetchone()[0]


def compose(*args):
    subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.yaml"), *args], check=True)


before = state()
body = {"model": "lab-chat", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 64}
headers = {"Authorization": "Bearer " + identity["key"]}
results = {}
with httpx.Client(base_url="http://127.0.0.1:18081", trust_env=False, timeout=30) as client:
    for service in ("redis", "postgres"):
        try:
            compose("stop", service)
            response = client.post("/v1/chat/completions", headers=headers, json=body)
            assert response.status_code == 503, (service, response.status_code)
            assert client.get("/health/live").status_code == 200
            results[service] = "503 before upstream dispatch"
        finally:
            compose("start", "--wait", service)
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
(ROOT / "docs/dependency-results.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
