"""Create secrets once, without printing any plaintext credentials."""

import argparse
import getpass
import os
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from cryptography.fernet import Fernet

p = argparse.ArgumentParser()
p.add_argument("--demo", action="store_true")
args = p.parse_args()
root = Path(__file__).resolve().parents[1]
secret_dir = root / "secrets"
if (root / ".env").exists() or secret_dir.exists():
    raise SystemExit("Refusing to overwrite existing .env or secrets; back up and rotate deliberately.")
os.umask(0o077)
secret_dir.mkdir(mode=0o700)
password = (
    secrets.token_urlsafe(24)
    if args.demo
    else getpass.getpass("New administrator password (16+ characters): ")
)
if len(password) < 16:
    raise SystemExit("Administrator password must have at least 16 characters")
db_password, redis_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
for name, value in {
    "database_password": db_password,
    "encryption_key": Fernet.generate_key().decode(),
    "hmac_key": secrets.token_urlsafe(48),
    "backup_key": Fernet.generate_key().decode(),
    "admin_password_hash": PasswordHasher().hash(password),
    "redis.conf": f"bind 0.0.0.0\nprotected-mode yes\nrequirepass {redis_password}\nappendonly yes\ndir /data\n",
}.items():
    (secret_dir / name).write_text(value + "\n")
if args.demo:
    (secret_dir / "demo_admin_password").write_text(password + "\n")
env = f"""GATEWAY_ENV={"demo" if args.demo else "production"}
PUBLIC_URL={"http://127.0.0.1:18081" if args.demo else "https://YOUR-LAB-DOMAIN"}
UPSTREAM_URL={"http://mock:8080/v1" if args.demo else "https://api.deepseek.com/v1"}
DATABASE_URL=postgresql+psycopg://lab_gateway:{db_password}@postgres:5432/lab_gateway
REDIS_URL=redis://:{redis_password}@redis:6379/0
ADMIN_NAME=admin
SUPPORT_EMAIL=
GATEWAY_BIND_HOST=127.0.0.1
GATEWAY_PORT=18081
GATEWAY_UID={os.getuid()}
GATEWAY_GID={os.getgid()}
TRUSTED_PROXIES=
RETENTION_DAYS=90
"""
(root / ".env").write_text(env)
print("Created mode-0600 secrets and .env. No credentials printed.")
if args.demo:
    print("Demo admin password is in secrets/demo_admin_password; read it locally.")
else:
    print("Set PUBLIC_URL and actual proxy addresses in .env before deployment.")
