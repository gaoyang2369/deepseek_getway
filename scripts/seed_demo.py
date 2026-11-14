"""Create two groups and three students on a local MOCK gateway. Store keys only in a protected file."""

import json
import os
import secrets
import uuid
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
output = root / "secrets/demo_clients.json"
if output.exists():
    raise SystemExit("Demo clients already exist; refusing to duplicate or overwrite keys")
env = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines() if "=" in line)
if env.get("GATEWAY_ENV") != "demo":
    raise SystemExit("Demo seeding is allowed only in demo mode")
os.umask(0o077)
with httpx.Client(base_url="http://127.0.0.1:18081", trust_env=False) as c:
    login = c.post(
        "/admin/api/login",
        json={"username": "admin", "password": (root / "secrets/demo_admin_password").read_text().strip()},
    )
    login.raise_for_status()
    c.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    c.post(
        "/admin/api/prices",
        json={
            "model": "lab-chat",
            "upstream_model": "mock-text",
            "version": "mock-demo-v1",
            "input_miss": 2000000,
            "input_hit": 200000,
            "output": 3000000,
            "estimated": True,
            "note": "仅模拟验收，2/0.2/3 元每百万 Token，不是官方价格",
        },
    ).raise_for_status()
    clients = []
    for code, numbers in (("A", ("demo001", "demo002")), ("B", ("demo003",))):
        response = c.post(
            "/admin/api/teams",
            json={
                "name": code + " 模拟项目组",
                "code": "demo_" + code,
                "upstream_key": "MOCK-" + code + "-credential",
                "models": ["lab-chat"],
                "budget": 100000000,
                "concurrency": 100,
                "rpm": 10000,
            },
        )
        response.raise_for_status()
        team = response.json()
        for number in numbers:
            response = c.post(
                "/admin/api/enroll",
                json={
                    "operation_id": str(uuid.uuid4()),
                    "name": "模拟同学 " + number,
                    "student_number": number,
                    "team_id": team["id"],
                    "models": ["lab-chat"],
                    "budget": 10000000,
                },
            )
            response.raise_for_status()
            person = response.json()
            with httpx.Client(base_url="http://127.0.0.1:18081", trust_env=False) as portal:
                signed_in = portal.post("/student/api/login", json={"username": number, "password": number})
                signed_in.raise_for_status()
                portal.headers["X-CSRF-Token"] = signed_in.json()["csrf_token"]
                password = secrets.token_urlsafe(24)
                changed = portal.post(
                    "/student/api/password", json={"current_password": number, "new_password": password}
                )
                changed.raise_for_status()
                portal.headers["X-CSRF-Token"] = changed.json()["csrf_token"]
                issued = portal.post(
                    "/student/api/keys",
                    json={"operation_id": str(uuid.uuid4()), "membership_id": person["membership_id"]},
                )
                issued.raise_for_status()
                person.update({k: issued.json()[k] for k in ("key", "key_id", "prefix")})
                person["student_password"] = password
            clients.append(person)
    output.write_text(json.dumps(clients, ensure_ascii=False, indent=2))
print(
    "Created two MOCK teams and three students. One-time personal keys saved to secrets/demo_clients.json (0600)."
)
