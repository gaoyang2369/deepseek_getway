import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError


def secret(name: str) -> str:
    path = os.environ.get(name + "_FILE")
    if not path:
        raise RuntimeError(f"{name}_FILE is required")
    p = Path(path)
    if p.stat().st_mode & 0o077:
        raise RuntimeError(f"{name} file must have mode 0600 or 0400")
    value = p.read_text().strip()
    if len(value) < 32 or value.lower().startswith(("example", "changeme")):
        raise RuntimeError(f"{name} secret is missing or unsafe")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    encryption_key: str
    hmac_key: str
    admin_hash: str
    admin_name: str = "admin"
    production: bool = True
    public_url: str = "http://127.0.0.1:18081"
    upstream_url: str = "https://api.deepseek.com/v1"
    trusted_proxies: tuple[str, ...] = ()
    session_seconds: int = 3600
    max_body: int = 262144
    max_chat_body: int = 48 * 1024 * 1024
    timeout: float = 180
    stream_heartbeat_seconds: float = 15
    retention_days: int = 90
    budget_thresholds: tuple[int, ...] = (80, 100)
    support_email: str = ""

    @classmethod
    def from_env(cls):
        mode = os.getenv("GATEWAY_ENV", "production")
        if mode not in ("production", "demo"):
            raise RuntimeError("GATEWAY_ENV must be production or demo")
        production = mode == "production"
        upstream = os.getenv("UPSTREAM_URL", "https://api.deepseek.com/v1").rstrip("/")
        public = os.getenv("PUBLIC_URL", "http://127.0.0.1:18081").rstrip("/")
        host = urlsplit(public).hostname or ""
        if production and (not host or "your-lab" in host.lower() or "example" in host.lower()):
            raise RuntimeError("PUBLIC_URL must use the actual approved domain")
        if production and (upstream != "https://api.deepseek.com/v1" or not public.startswith("https://")):
            raise RuntimeError("Production requires HTTPS PUBLIC_URL and fixed official DeepSeek upstream")
        db = os.environ["DATABASE_URL"]
        if not db.startswith("postgresql+psycopg://"):
            raise RuntimeError("PostgreSQL with psycopg is required")
        if production and any(marker in db.lower() for marker in ("set_by_init", "changeme", "example")):
            raise RuntimeError("Database credentials must not use example values")
        admin_hash = secret("ADMIN_PASSWORD_HASH")
        try:
            PasswordHasher().check_needs_rehash(admin_hash)
        except InvalidHashError:
            raise RuntimeError("ADMIN_PASSWORD_HASH must be a valid Argon2 hash") from None
        retention = int(os.getenv("RETENTION_DAYS", "90"))
        if retention < 1:
            raise RuntimeError("RETENTION_DAYS must be positive")
        thresholds = tuple(sorted(set(int(x) for x in os.getenv("BUDGET_THRESHOLDS", "80,100").split(","))))
        if not thresholds or any(x < 1 or x > 100 for x in thresholds):
            raise RuntimeError("BUDGET_THRESHOLDS must contain percentages from 1 to 100")
        return cls(
            database_url=db,
            redis_url=os.environ["REDIS_URL"],
            encryption_key=secret("ENCRYPTION_KEY"),
            hmac_key=secret("HMAC_KEY"),
            admin_hash=admin_hash,
            admin_name=os.getenv("ADMIN_NAME", "admin"),
            production=production,
            public_url=public,
            upstream_url=upstream,
            trusted_proxies=tuple(filter(None, os.getenv("TRUSTED_PROXIES", "").split(","))),
            retention_days=retention,
            budget_thresholds=thresholds,
            support_email=os.getenv("SUPPORT_EMAIL", "").strip(),
        )
