"""Snapshot, encrypt, restore to a new DB and compare every persisted application row."""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


def snapshot(connection):
    result = {}
    tables = [
        r[0]
        for r in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )
    ]
    for table in tables:
        rows = connection.execute(
            sql.SQL("SELECT row_to_json(t)::text FROM {} t ORDER BY row_to_json(t)::text").format(
                sql.Identifier(table)
            )
        ).fetchall()
        result[table] = [r[0] for r in rows]
    return result


url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(url) as source:
    source.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    snapshot_id = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
    before = snapshot(source)
    subprocess.run([sys.executable, str(ROOT / "scripts/backup.py"), "--snapshot", snapshot_id], check=True)
backup = max((ROOT / "backups").glob("*-database.enc"), key=lambda p: p.stat().st_mtime)
target = "lab_gateway_restore_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
start = time.perf_counter()
subprocess.run(
    [sys.executable, str(ROOT / "scripts/restore.py"), str(backup), "--target", target], check=True
)
with psycopg.connect(url.rsplit("/", 1)[0] + "/" + target) as connection:
    restored = snapshot(connection)
assert before == restored, "Restored database differs from source snapshot"
report = {
    "target": target,
    "all_rows_match": True,
    "restore_seconds": round(time.perf_counter() - start, 3),
    "tables": {name: len(rows) for name, rows in before.items()},
    "scope": "identities, key digests, encrypted credentials, budgets, pending requests, ledger, audit; all rows",
}
(ROOT / "docs/restore-results.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
