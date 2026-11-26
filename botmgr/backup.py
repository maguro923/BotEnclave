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


def restore_backup(
    archive: Path,
    root: Path,
    *,
    force: bool = False,
    warnings: list[str] | None = None,
) -> list[str]:
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
            # Manifest keys are archive-controlled input, exactly like
            # member names: sanitize them before building any path.
            target = stage / _safe_member_name(rel)
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
            safe_rel = _safe_member_name(rel)
            source = stage / safe_rel
            dest_rel = mapping.get(safe_rel, safe_rel)
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
        _reapply_layout(root, warnings if warnings is not None else [])
        return restored


def _reapply_layout(root: Path, warnings: list[str]) -> None:
    """Re-apply the OS-side layout a backup cannot carry: per-bot OS
    users, ownership and the permission model (PLAN 2.4).

    Restored files land owned by whoever ran the restore, with
    umask-default modes — bots/<bot>/data would come back world-
    readable instead of 0700, and on a fresh host the bm-<bot> users
    do not exist at all, so no bot could start. Running as root, this
    recreates the users through the helper's own executor (the same
    code that builds the layout at bot creation) and hands the file
    contents back to them. Anything that cannot be re-applied is
    reported as a warning, never silently skipped.
    """
    bots_dir = root / "bots"
    bots = (
        sorted(p.name for p in bots_dir.iterdir() if p.is_dir())
        if bots_dir.is_dir()
        else []
    )
    if os.geteuid() != 0:
        if bots:
            warnings.append(
                "OS layout not re-applied (restore is not running as"
                " root): per-bot users, ownership and permissions must"
                " be restored manually"
            )
        return
    import grp
    import pwd

    from botmgr.helperd import SystemExecutor

    root_stat = root.stat()
    manager_group: str | None = None
    if root_stat.st_uid != 0:
        # Same-host restore: the data root still belongs to the
        # manager user, so restored top-level files go back to that
        # owner and the manager's group is the layout's group.
        try:
            manager_group = grp.getgrgid(root_stat.st_gid).gr_name
        except KeyError:
            manager_group = None
        for entry in root.iterdir():
            if entry.name == "bots":
                continue
            _chown_tree(entry, root_stat.st_uid, root_stat.st_gid)
    elif bots:
        warnings.append(
            "data root is owned by root: chown it to the manager user"
            " before starting the panel"
        )
    executor = SystemExecutor(root, manager_group=manager_group)
    for bot in bots:
        try:
            executor.create_user(bot)
            bot_entry = pwd.getpwnam(f"bm-{bot}")
            bot_dir = bots_dir / bot
            _fix_contents(
                bot_dir / "data", bot_entry.pw_uid, bot_entry.pw_gid, 0o700, 0o600
            )
            logs = bot_dir / "logs"
            logs_gid = logs.stat().st_gid if logs.exists() else bot_entry.pw_gid
            _fix_contents(logs, bot_entry.pw_uid, logs_gid, 0o750, 0o640)
            releases = bot_dir / "releases"
            if releases.is_dir():
                for release in sorted(p.name for p in releases.iterdir()):
                    try:
                        executor.publish_release(bot, release)
                    except Exception as exc:  # noqa: BLE001 - warn, continue
                        warnings.append(
                            f"release {release} of bot {bot} was not"
                            f" re-published: {exc}"
                        )
        except Exception as exc:  # noqa: BLE001 - warn, continue
            warnings.append(f"layout for bot {bot} was not re-applied: {exc}")


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    if path.is_symlink():
        return
    os.chown(path, uid, gid, follow_symlinks=False)
    if path.is_dir():
        for child in path.iterdir():
            _chown_tree(child, uid, gid)


def _fix_contents(
    base: Path, uid: int, gid: int, dir_mode: int, file_mode: int
) -> None:
    if not base.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        if current != base:
            os.chown(current, uid, gid, follow_symlinks=False)
            os.chmod(current, dir_mode, follow_symlinks=False)
        for name in filenames:
            target = current / name
            if target.is_symlink():
                continue
            os.chown(target, uid, gid, follow_symlinks=False)
            os.chmod(target, file_mode, follow_symlinks=False)
