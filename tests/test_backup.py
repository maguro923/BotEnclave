"""Backup / restore round trip and integrity checks (PLAN 5.3)."""

from __future__ import annotations

import hashlib
import hmac
import json
import tarfile
import time
from pathlib import Path

import pytest

from botmgr.audit import AuditLog
from botmgr.backup import BackupError, create_backup, restore_backup
from botmgr.envstore import SecretsService
from botmgr.launcher import LaunchConfig
from botmgr.store import StateStore
from botmgr.webhooks import WebhookSecrets

TOKEN = "backup-token-42"


def build_root(root: Path) -> str:
    store = StateStore(root / "botmgr.db")
    store.register_bot(
        "alpha", LaunchConfig(("python3", "bot.py"), str(root / "bots/alpha/data"), {})
    )
    service = SecretsService(root, store)
    service.set_env("alpha", {"DISCORD_TOKEN": TOKEN}, "fernet")
    webhook_secret = WebhookSecrets(root).rotate("alpha")
    data = root / "bots" / "alpha" / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "state.json").write_text('{"hp": 3}', encoding="utf-8")
    AuditLog(root / "audit.log").record("admin", "bot.create", "alpha")
    store.close()
    return webhook_secret


def test_backup_restore_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "live"
    root.mkdir()
    webhook_secret = build_root(root)
    archive = create_backup(root, tmp_path / "backups")
    assert archive.exists()
    assert TOKEN.encode() not in archive.read_bytes()

    restored_root = tmp_path / "restored"
    restored = restore_backup(archive, restored_root)
    assert "botmgr.db" in restored
    store = StateStore(restored_root / "botmgr.db")
    bot = store.get_bot("alpha")
    assert bot is not None
    service = SecretsService(restored_root, store)
    assert service.resolve("alpha").env == {"DISCORD_TOKEN": TOKEN}
    # The webhook secret survived: a signature made with the original
    # secret still verifies against the restored store.
    hooks = WebhookSecrets(restored_root)
    body = b"{}"
    ts = str(int(time.time()))
    sig = hmac.new(
        webhook_secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    assert hooks.verify("alpha", ts, sig, body) is True
    assert AuditLog(restored_root / "audit.log").verify_chain() is True
    data = restored_root / "bots" / "alpha" / "data" / "state.json"
    assert json.loads(data.read_text(encoding="utf-8")) == {"hp": 3}
    # Secret files land with owner-only permissions.
    enc = restored_root / "secrets" / "alpha.env.enc"
    assert enc.stat().st_mode & 0o777 == 0o600


def test_restore_refuses_existing_and_tampering(tmp_path: Path) -> None:
    root = tmp_path / "live"
    root.mkdir()
    build_root(root)
    archive = create_backup(root, tmp_path / "backups")
    with pytest.raises(BackupError):
        restore_backup(archive, root)
    restored = restore_backup(archive, root, force=True)
    assert "botmgr.db" in restored
    asides = list(tmp_path.glob("live.pre-restore-*"))
    assert len(asides) == 1 and (asides[0] / "botmgr.db").exists()
    # Tamper with one archived file: integrity check must catch it.
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(tampered, "w:gz") as dst:
        for member in src.getmembers():
            data = src.extractfile(member).read() if member.isfile() else None
            if member.name.endswith("audit.log") and data:
                data = data + b"forged line\n"
                member.size = len(data)
                from io import BytesIO

                dst.addfile(member, BytesIO(data))
            elif data is not None:
                from io import BytesIO

                dst.addfile(member, BytesIO(data))
            else:
                dst.addfile(member)
    with pytest.raises(BackupError):
        restore_backup(tampered, tmp_path / "other")


def test_restore_rejects_traversing_manifest_keys(tmp_path: Path) -> None:
    # A crafted archive whose manifest points outside the target root
    # must be rejected, even though its member names are all safe.
    import io

    archive = tmp_path / "crafted.tar.gz"
    manifest = {
        "format": 1,
        "files": {"bots/../../evil.txt": "0" * 64},
    }
    with tarfile.open(archive, "w:gz") as tar:
        payload = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        data = b"pwned"
        info = tarfile.TarInfo("evil.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    dest = tmp_path / "restored"
    with pytest.raises(BackupError):
        restore_backup(archive, dest)
    assert not (tmp_path / "evil.txt").exists()
