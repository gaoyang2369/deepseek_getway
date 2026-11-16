"""Load local secrets and run a command against isolated Compose dependencies."""

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
env = os.environ.copy()
for line in (root / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k] = v
env["DATABASE_URL"] = env["DATABASE_URL"].replace("@postgres:5432/", "@127.0.0.1:18543/")
env["REDIS_URL"] = env["REDIS_URL"].replace("@redis:6379/", "@127.0.0.1:18379/")
env["GATEWAY_COMPOSE_PROJECT"] = os.getenv("GATEWAY_COMPOSE_PROJECT", "lab-gateway-test")
env["GATEWAY_ENV"] = "demo"
env["UPSTREAM_URL"] = "http://127.0.0.1:18082/v1"
for variable, filename in (
    ("ENCRYPTION_KEY", "encryption_key"),
    ("HMAC_KEY", "hmac_key"),
    ("ADMIN_PASSWORD_HASH", "admin_password_hash"),
):
    env[variable + "_FILE"] = str(root / "secrets" / filename)
raise SystemExit(subprocess.call(sys.argv[1:], env=env, cwd=root))
