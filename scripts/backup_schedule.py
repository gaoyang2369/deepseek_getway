"""Daily backup entrypoint; retain seven daily and four weekly successful backup pairs."""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in ([], ["--secrets"]):
    subprocess.run([sys.executable, str(ROOT / "scripts/backup.py"), *extra], check=True)
for kind in ("database", "secrets"):
    files = sorted((ROOT / "backups").glob(f"*-{kind}.enc"), reverse=True)
    daily, weekly = {}, {}
    for p in files:
        at = datetime.strptime(p.name[:16], "%Y%m%dT%H%M%SZ")
        daily.setdefault(at.date(), p)
        weekly.setdefault(at.isocalendar()[:2], p)
    keep = set(list(daily.values())[:7] + list(weekly.values())[:4])
    for p in files:
        if p not in keep:
            p.unlink()
print("Backup pairs complete; retained seven daily/four weekly snapshots. Copy encrypted files off-host.")
