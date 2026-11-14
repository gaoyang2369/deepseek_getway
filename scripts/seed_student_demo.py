"""Provision fictional, unclaimed student accounts; never reset an existing account."""

import asyncio
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


async def main():
    values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    if values.get("GATEWAY_ENV") != "demo" or values.get("UPSTREAM_URL") != "http://mock:8080/v1":
        raise SystemExit("This script is only for the isolated Compose MOCK environment")
    async with httpx.AsyncClient(base_url=values["PUBLIC_URL"], trust_env=False, timeout=30) as client:
        r = await client.post(
            "/admin/api/login",
            json={
                "username": "admin",
                "password": (ROOT / "secrets/demo_admin_password").read_text().strip(),
            },
        )
        r.raise_for_status()
        client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        teams = (await client.get("/admin/api/teams")).json()
        team = next((t for t in teams if t["code"] == "demo_A"), None)
        if not team:
            raise SystemExit("Run scripts/seed_demo.py in a fresh MOCK environment before student seeding")
        r = await client.post(
            "/admin/api/students/import",
            json={
                "operation_id": "fictional-student-roster-demo-v1",
                "team_id": team["id"],
                "students": [
                    {"student_number": str(2026001000 + n), "name": f"自助模拟同学{n}"} for n in (1, 2, 3)
                ],
                "models": ["lab-chat"],
                "budget": 10000000,
                "initial_password_mode": "student_number",
            },
        )
        r.raise_for_status()
        await client.post("/admin/api/logout", json={})
    print("Fictional accounts provisioned: 2026001001, 2026001002, 2026001003; 10 CNY/month each.")
    print("New accounts use their student number initially; existing passwords and budgets are preserved.")


if __name__ == "__main__":
    asyncio.run(main())
