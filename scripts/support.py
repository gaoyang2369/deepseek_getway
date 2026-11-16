"""Explicit targets and disposable output for local verification commands."""

import os
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def artifact(name):
    directory = Path(os.getenv("GATEWAY_ARTIFACT_DIR", ROOT / "artifacts"))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def compose(*args):
    project = os.getenv("GATEWAY_COMPOSE_PROJECT", "lab-llm-gateway")
    command = ["docker", "compose", "-p", project, "-f", str(ROOT / "compose.yaml")]
    if project != "lab-llm-gateway":
        command += ["-f", str(ROOT / "compose.test.yaml")]
    return [*command, *args]


def require_isolated_dependencies():
    project = os.getenv("GATEWAY_COMPOSE_PROJECT")
    if not project or project == "lab-llm-gateway":
        raise SystemExit("An explicit isolated GATEWAY_COMPOSE_PROJECT is required")
    for service, port, variable in (("postgres", 5432, "DATABASE_URL"), ("redis", 6379, "REDIS_URL")):
        url = urlsplit(os.environ[variable])
        if url.hostname != "127.0.0.1":
            raise SystemExit("Verification dependencies must use loopback addresses")
        published = subprocess.check_output(compose("port", service, str(port)), text=True).strip()
        if published != f"127.0.0.1:{url.port}":
            raise SystemExit("Compose service and verification connection target differ")


def test_gateway_url(value=None):
    value = value or os.getenv("GATEWAY_TEST_URL", "")
    url = urlsplit(value)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.port == 18081:
        raise SystemExit("Set GATEWAY_TEST_URL to a separate loopback MOCK gateway (not port 18081)")
    return value.rstrip("/")


def admin_password():
    return os.getenv("GATEWAY_TEST_PASSWORD") or (ROOT / "secrets/demo_admin_password").read_text().strip()


async def login_mock(client):
    test_gateway_url(str(client.base_url))
    result = await client.post("/admin/api/login", json={"username": "admin", "password": admin_password()})
    result.raise_for_status()
    client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    config = await client.get("/admin/api/config")
    config.raise_for_status()
    if not config.json()["test_cleanup_enabled"]:
        raise SystemExit("The target gateway must use the isolated MOCK upstream")


async def mock_identity(client):
    """Create a fresh MOCK team/service; never select a pre-existing team's records."""
    await login_mock(client)
    suffix = uuid.uuid4().hex
    response = await client.post(
        "/admin/api/prices",
        json={
            "model": "lab-chat",
            "upstream_model": "mock-text",
            "version": "verify-" + suffix,
            "input_miss": 2000000,
            "input_hit": 200000,
            "output": 3000000,
            "estimated": True,
            "note": "Isolated verification MOCK ONLY",
        },
    )
    response.raise_for_status()
    response = await client.post(
        "/admin/api/teams",
        json={
            "name": "verification " + suffix,
            "code": "verify_" + suffix,
            "upstream_key": "MOCK-verification-credential",
            "models": ["lab-chat"],
            "budget": 100000000,
            "rpm": 10000,
            "concurrency": 100,
        },
    )
    response.raise_for_status()
    team = response.json()
    response = await client.post(
        "/admin/api/enroll",
        json={
            "operation_id": str(uuid.uuid4()),
            "name": "verification service",
            "student_number": "verify_" + suffix,
            "kind": "service",
            "owner": "isolated verification",
            "team_id": team["id"],
            "models": ["lab-chat"],
            "budget": 10**10,
            "rpm": 10000,
            "concurrency": 100,
        },
    )
    response.raise_for_status()
    identity = response.json()
    return {**identity, "team_id": team["id"], "team_name": team["name"]}
