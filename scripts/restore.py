"""Restore ONLY to a new isolated database; production replacement is deliberately manual."""

import argparse
import os
import re
import subprocess
import tempfile
from pathlib import Path

from backup import ROOT, compose, decrypt, key_bytes

p = argparse.ArgumentParser()
p.add_argument("backup")
p.add_argument("--target", required=True, help="New database named lab_gateway_restore_<suffix>")
p.add_argument("--key-file", default=str(ROOT / "secrets/backup_key"))
args = p.parse_args()
if not re.fullmatch(r"lab_gateway_restore_[a-z0-9_]{1,30}", args.target):
    raise SystemExit("Restore target must be a NEW lab_gateway_restore_<suffix> database")
os.umask(0o077)
# Authenticate every frame, including the final frame, before touching PostgreSQL.
with tempfile.TemporaryFile() as plain, Path(args.backup).open("rb") as encrypted:
    decrypt(encrypted, plain, key_bytes(args.key_file))
    plain.seek(0)
    subprocess.run(
        compose("exec", "-T", "postgres", "createdb", "-U", "lab_gateway", args.target), check=True
    )
    subprocess.run(
        compose(
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "-U",
            "lab_gateway",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "-d",
            args.target,
        ),
        stdin=plain,
        check=True,
    )
print(f"Restored isolated database: {args.target}. Validate before any deliberate production switch.")
