"""Check deliverable artifacts against local runtime secrets without printing secret values."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
values = []
for filename in ("database_password", "encryption_key", "hmac_key", "backup_key", "demo_admin_password"):
    p = ROOT / "secrets" / filename
    if p.exists():
        values.append(p.read_bytes().strip())
for filename in ("demo_clients.json", "benchmark_client.json"):
    p = ROOT / "secrets" / filename
    if p.exists():
        entries = json.loads(p.read_text())
        for entry in entries if isinstance(entries, list) else [entries]:
            values.append(entry["key"].encode())
excluded = {".venv", "secrets", "backups", "research", ".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
bad = []
for p in ROOT.rglob("*"):
    rel = p.relative_to(ROOT)
    if not p.is_file() or any(x in excluded for x in rel.parts) or p.name == ".env":
        continue
    if any(value and value in p.read_bytes() for value in values):
        bad.append(str(rel))
if bad:
    raise SystemExit("Runtime secret found in deliverable files: " + ", ".join(bad))
print("No local runtime credentials or personal keys found in deliverable artifacts.")
