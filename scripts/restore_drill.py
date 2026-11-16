"""Snapshot, encrypt, restore to a new DB and compare every persisted application row."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
from backup import create_backup
from psycopg import sql
from support import artifact, require_isolated_dependencies

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


require_isolated_dependencies()
url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(url) as source:
    source.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    snapshot_id = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
    before = snapshot(source)
    backup = create_backup(
        key_file=ROOT / "secrets/backup_key",
        directory=artifact("restore"),
        snapshot=snapshot_id,
        database=source.info.dbname,
    )
target = "lab_gateway_restore_" + uuid.uuid4().hex[:24]
start = time.perf_counter()
restored_successfully = False
try:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/restore.py"), str(backup), "--target", target], check=True
    )
    restored_successfully = True
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
    artifact("restore-results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
finally:
    # A failed createdb must never cause deletion of a pre-existing restore target.
    # Failed imports are left for diagnosis; only a successful restore is ours to remove.
    if restored_successfully:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(target)))
