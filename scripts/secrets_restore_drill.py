"""Restore the encrypted secrets archive into a temporary restricted directory and compare bytes."""

import io
import json
import os
import tarfile
import tempfile
from pathlib import Path

from backup import ROOT, create_backup, decrypt, key_bytes
from support import artifact

backup = create_backup(
    key_file=ROOT / "secrets/backup_key", directory=artifact("restore"), include_secrets=True
)
plain = io.BytesIO()
with backup.open("rb") as source:
    decrypt(source, plain, key_bytes(ROOT / "secrets/backup_key"))
plain.seek(0)
os.umask(0o077)
restored = []
with tempfile.TemporaryDirectory(prefix="lab-secrets-restore-") as directory:
    with tarfile.open(fileobj=plain, mode="r") as archive:
        for member in archive.getmembers():
            name = Path(member.name)
            if (
                not member.isfile()
                or name.is_absolute()
                or ".." in name.parts
                or (name.parts[0] != "secrets" and str(name) != ".env")
            ):
                raise RuntimeError("Unexpected archive member")
            target = Path(directory) / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
            assert target.read_bytes() == (ROOT / name).read_bytes()
            assert not target.stat().st_mode & 0o077
            restored.append(str(name))
assert "secrets/encryption_key" in restored and "secrets/hmac_key" in restored
assert "secrets/backup_key" not in restored
report = {
    "secret_files_match": True,
    "restored_file_count": len(restored),
    "mode_0600": True,
    "backup_decryption_key_excluded": True,
    "destination": "temporary restricted directory, removed after check",
}
artifact("secrets-restore-results.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
