"""Authenticated streaming backup. Credentials and encryption keys are never printed."""

import argparse
import base64
import io
import os
import struct
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from support import compose

MAGIC = b"LABBACKUP1"
ROOT = Path(__file__).resolve().parents[1]


def key_bytes(path):
    p = Path(path)
    if p.stat().st_mode & 0o077:
        raise ValueError("Backup key must be mode 0600/0400")
    key = base64.urlsafe_b64decode(p.read_bytes().strip())
    if len(key) != 32:
        raise ValueError("Backup key must encode 32 bytes")
    return key


def encrypt(source, target, key):
    cipher, prefix = AESGCM(key), os.urandom(8)
    header = MAGIC + prefix
    target.write(header)
    index = 0
    while True:
        chunk = source.read(1024 * 1024)
        counter = struct.pack(">I", index)
        encrypted = cipher.encrypt(prefix + counter, chunk, header + counter)
        target.write(struct.pack(">I", len(encrypted)) + encrypted)
        if not chunk:
            break
        index += 1


def decrypt(source, target, key):
    header = source.read(len(MAGIC) + 8)
    if not header.startswith(MAGIC) or len(header) != len(MAGIC) + 8:
        raise ValueError("Invalid backup header")
    cipher, prefix, index = AESGCM(key), header[-8:], 0
    while True:
        size = source.read(4)
        if len(size) != 4:
            raise ValueError("Truncated backup")
        size = struct.unpack(">I", size)[0]
        if not 16 <= size <= 1024 * 1024 + 16:
            raise ValueError("Invalid backup frame")
        data = source.read(size)
        counter = struct.pack(">I", index)
        chunk = cipher.decrypt(prefix + counter, data, header + counter)
        if not chunk:
            if source.read(1):
                raise ValueError("Trailing backup data")
            return
        target.write(chunk)
        index += 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key-file", default=str(ROOT / "secrets/backup_key"))
    p.add_argument("--directory", default=str(ROOT / "backups"))
    p.add_argument("--secrets", action="store_true", help="Create a separate encrypted secrets archive")
    p.add_argument("--snapshot", help="Exported PostgreSQL snapshot held open by a restore drill")
    args = p.parse_args()
    output = create_backup(
        key_file=args.key_file, directory=args.directory, include_secrets=args.secrets, snapshot=args.snapshot
    )
    print(f"Encrypted backup written: {output}")


def create_backup(*, key_file, directory, include_secrets=False, snapshot=None, database="lab_gateway"):
    os.umask(0o077)
    key = key_bytes(key_file)
    destination = Path(directory)
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = destination / (at + ("-secrets.enc" if include_secrets else "-database.enc"))
    target = output.open("xb")
    try:
        with target:
            if include_secrets:
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    archive.add(ROOT / ".env", arcname=".env")
                    for file in (ROOT / "secrets").iterdir():
                        if (
                            file.is_file()
                            and file.resolve() != Path(key_file).resolve()
                            and file.name != "demo_admin_password"
                        ):
                            archive.add(file, arcname="secrets/" + file.name)
                buffer.seek(0)
                encrypt(buffer, target, key)
            else:
                command = compose(
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "lab_gateway",
                    "-d",
                    database,
                    "-Fc",
                    "--no-owner",
                )
                if snapshot:
                    command.extend(["--snapshot", snapshot])
                proc = subprocess.Popen(command, stdout=subprocess.PIPE)
                try:
                    encrypt(proc.stdout, target, key)
                finally:
                    proc.stdout.close()
                if proc.wait() != 0:
                    raise RuntimeError("pg_dump failed")
        return output
    except BaseException:
        output.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
