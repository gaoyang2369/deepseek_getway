from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import backup
from scripts.support import require_isolated_dependencies
from scripts.support import test_gateway_url as gateway_target


@pytest.fixture
def backup_files(tmp_path, monkeypatch):
    (tmp_path / "secrets").mkdir()
    (tmp_path / ".env").write_text("TEST=synthetic\n")
    monkeypatch.setattr(backup, "ROOT", tmp_path)
    monkeypatch.setattr(backup, "key_bytes", lambda _: b"x" * 32)
    monkeypatch.setattr(
        backup, "datetime", SimpleNamespace(now=lambda _: datetime(2020, 1, 1, tzinfo=timezone.utc))
    )
    monkeypatch.setattr("sys.argv", ["backup.py", "--secrets", "--directory", str(tmp_path)])
    return tmp_path / "20200101T000000Z-secrets.enc"


def test_backup_collision_preserves_existing_file(backup_files):
    backup_files.write_bytes(b"existing authenticated backup")
    with pytest.raises(FileExistsError):
        backup.main()
    assert backup_files.read_bytes() == b"existing authenticated backup"


def test_failed_backup_removes_only_its_partial_file(backup_files, monkeypatch):
    def broken(source, target, key):
        target.write(b"partial")
        raise OSError("synthetic write failure")

    monkeypatch.setattr(backup, "encrypt", broken)
    with pytest.raises(OSError):
        backup.main()
    assert not backup_files.exists()


@pytest.mark.parametrize(
    "url", ["", "http://127.0.0.1:18081", "http://10.108.13.250:19081", "https://api.deepseek.com/v1"]
)
def test_verification_rejects_live_or_implicit_gateway(url, monkeypatch):
    monkeypatch.delenv("GATEWAY_TEST_URL", raising=False)
    with pytest.raises(SystemExit):
        gateway_target(url)


def test_destructive_drill_requires_explicit_project(monkeypatch):
    monkeypatch.delenv("GATEWAY_COMPOSE_PROJECT", raising=False)
    with pytest.raises(SystemExit):
        require_isolated_dependencies()


@pytest.mark.parametrize("published_port", [18543, 15432])
def test_drill_checks_compose_ports_against_connection_targets(monkeypatch, published_port):
    monkeypatch.setenv("GATEWAY_COMPOSE_PROJECT", "isolated-test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@127.0.0.1:18543/lab_gateway")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:18379/0")

    def published(command, **kwargs):
        assert command[:4] == ["docker", "compose", "-p", "isolated-test"]
        port = published_port if "postgres" in command else 18379
        return f"127.0.0.1:{port}\n"

    monkeypatch.setattr("scripts.support.subprocess.check_output", published)
    if published_port == 18543:
        require_isolated_dependencies()
    else:
        with pytest.raises(SystemExit, match="target differ"):
            require_isolated_dependencies()
