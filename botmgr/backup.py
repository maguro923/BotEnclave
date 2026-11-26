"""Backup and restore (PLAN sections 5.3, 5.4).

A backup is one tar.gz holding everything needed to rebuild an
installation on the same host:

- both databases (botmgr.db, auth.db), copied through SQLite's backup
  API so the snapshot is consistent even while the manager runs;
- the key material (secret.key, web-secret) — keys and databases are
  always taken together, as the plan requires;
- the per-bot secret stores (secrets/: systemd-creds ciphertexts,
  Fernet-encrypted .env files, metadata, webhook secrets);
- the audit log;
- every bot's data/ (and logs/); releases/ and venvs/ only on request,
  since git-sourced releases can be re-fetched.

A manifest with a SHA-256 per file travels inside the archive and is
verified on restore before anything is moved into place.

Host binding caveat (also in the docs): systemd-creds ciphertexts are
bound to the host key, so a backup restored onto a *different* host
cannot decrypt them; those bots need their environment re-entered.
Fernet material restores anywhere the backup itself is trusted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

MANIFEST = "manifest.json"
FORMAT_VERSION = 1


class BackupError(Exception):
    """The backup or restore could not be completed safely."""


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_snapshot(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def create_backup(
    root: Path, dest_dir: Path, *, include_releases: bool = False
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    archive = dest_dir / f"botenclave-backup-{stamp}.tar.gz"
    files: dict[str, str] = {}
    with tempfile.TemporaryDirectory(dir=dest_dir) as tmp:
        stage = Path(tmp) / "payload"
        stage.mkdir()

        def add_file(source: Path, arcname: str) -> None:
            target = stage / arcname
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            files[arcname] = _hash_file(target)

        def add_tree(source: Path, arc_prefix: str) -> None:
            for path in sorted(source.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    add_file(path, f"{arc_prefix}/{path.relative_to(source)}")

        if (root / "botmgr.db").exists():
            _sqlite_snapshot(root / "botmgr.db", stage / "db" / "botmgr.db")
            files["db/botmgr.db"] = _hash_file(stage / "db" / "botmgr.db")
        if (root / "auth.db").exists():
            _sqlite_snapshot(root / "auth.db", stage / "db" / "auth.db")
            files["db/auth.db"] = _hash_file(stage / "db" / "auth.db")
        for name in ("secret.key", "web-secret"):
            if (root / name).exists():
                add_file(root / name, f"keys/{name}")
        if (root / "secrets").is_dir():
            add_tree(root / "secrets", "secrets")
        if (root / "audit.log").exists():
            add_file(root / "audit.log", "audit.log")
        bots_dir = root / "bots"
        if bots_dir.is_dir():
            for bot_dir in sorted(bots_dir.iterdir()):
                if not bot_dir.is_dir():
                    continue
                for sub in ("data", "logs"):
                    if (bot_dir / sub).is_dir():
                        add_tree(bot_dir / sub, f"bots/{bot_dir.name}/{sub}")
                if include_releases:
                    for sub in ("releases", "venvs"):
                        if (bot_dir / sub).is_dir():
                            add_tree(bot_dir / sub, f"bots/{bot_dir.name}/{sub}")
        manifest = {
            "format": FORMAT_VERSION,
            "created_at": time.time(),
            "hostname": os.uname().nodename,
            "include_releases": include_releases,
            "files": files,
        }
        (stage / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(stage, arcname=".")
    return archive


def _safe_member_name(name: str) -> str:
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if name.startswith("/") or ".." in parts:
        raise BackupError(f"unsafe path in archive: {name!r}")
    return "/".join(parts)


def restore_backup(archive: Path, root: Path, *, force: bool = False) -> list[str]:
    if (root / "botmgr.db").exists() and not force:
        raise BackupError(
            f"{root} already contains an installation; pass force to replace it"
        )
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "extract"
        stage.mkdir()
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                rel = _safe_member_name(member.name)
                if not rel:
                    continue
                if member.issym() or member.islnk():
                    raise BackupError(f"archive contains a link: {member.name!r}")
                if member.isdir():
                    (stage / rel).mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        raise BackupError(f"cannot read {member.name!r}")
                    target = stage / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(extracted.read())
        manifest_path = stage / MANIFEST
        if not manifest_path.exists():
            raise BackupError("archive has no manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT_VERSION:
            raise BackupError(f"unknown backup format: {manifest.get('format')!r}")
        for rel, expected in manifest["files"].items():
            target = stage / rel
            if not target.exists() or _hash_file(target) != expected:
                raise BackupError(f"backup integrity check failed for {rel}")
        restored: list[str] = []
        if force and root.exists():
            aside = root.with_name(root.name + f".pre-restore-{int(time.time())}")
            shutil.move(str(root), str(aside))
        root.mkdir(parents=True, exist_ok=True)
        mapping = {
            "db/botmgr.db": "botmgr.db",
            "db/auth.db": "auth.db",
            "keys/secret.key": "secret.key",
            "keys/web-secret": "web-secret",
        }
        for rel in manifest["files"]:
            source = stage / rel
            dest_rel = mapping.get(rel, rel)
            dest = root / dest_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            restored.append(dest_rel)
        secrets_dir = root / "secrets"
        if secrets_dir.is_dir():
            secrets_dir.chmod(0o700)
            for path in secrets_dir.iterdir():
                if path.is_file():
                    path.chmod(0o600)
        for name in ("secret.key", "web-secret"):
            if (root / name).exists():
                (root / name).chmod(0o600)
        return restored
