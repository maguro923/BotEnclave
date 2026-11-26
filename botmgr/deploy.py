"""Deploy pipeline (PLAN sections 2.6, 3.4): fetch, verify, build a
per-release venv, switch ``current``, garbage-collect old generations.

The heavy work runs in a worker subprocess (``python -m botmgr.deploy``)
so a hung clone or pip install cannot stall the manager; the worker
streams its output to the release's deploy log and reports back through
a result JSON. The manager side only does bookkeeping: store records,
the ``current`` symlink, the supervisor's launch config, restarts, GC.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from botmgr.helper import HelperClient
from botmgr.launcher import LaunchConfig
from botmgr.store import ReleaseRecord, StateStore
from botmgr.supervisor import Supervisor

ENTRY_CANDIDATES = ("main.py", "bot.py", "app.py", "run.py")
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024
WORKER_TIMEOUT_SECONDS = 1200
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class DeployError(Exception):
    """A deploy or rollback failed; the previous release stays active."""


def validate_git_url(url: str) -> str:
    """Scheme allowlist + option-injection guard (PLAN 3.4). Credentials
    embedded in https URLs are accepted here and are the caller's job to
    store encrypted and keep out of logs and audit details."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise DeployError("invalid git URL")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
        raise DeployError("invalid git URL")
    if url.startswith("-"):
        raise DeployError("invalid git URL")
    if url.startswith(("https://", "ssh://")):
        return url
    if url.startswith("git@") and ":" in url[4:]:
        return url
    raise DeployError("git URL scheme must be https, ssh, or git@")


def validate_branch(branch: str | None) -> str | None:
    if branch is None:
        return None
    if (
        not _BRANCH_RE.match(branch)
        or ".." in branch
        or branch.endswith(("/", ".lock"))
    ):
        raise DeployError(f"invalid branch name: {branch!r}")
    return branch


def new_release_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{os.urandom(2).hex()}"


def detect_entry(release_dir: Path) -> str:
    for candidate in ENTRY_CANDIDATES:
        if (release_dir / candidate).is_file():
            return candidate
    scripts = sorted(p.name for p in release_dir.glob("*.py") if p.is_file())
    if len(scripts) == 1:
        return scripts[0]
    raise DeployError(
        "no entry file found (looked for main.py, bot.py, app.py, run.py)"
    )


# ---------------------------------------------------------------------------
# Worker side (runs as a subprocess; functions take an open log stream)
# ---------------------------------------------------------------------------


def _redact_url(url: str) -> str:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=host))
    return url


def _run_logged(
    argv: list[str],
    log: TextIO,
    timeout: int,
    cwd: Path | None = None,
    display: list[str] | None = None,
) -> None:
    log.write(f"$ {' '.join(display or argv)}\n")
    log.flush()
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise DeployError(f"command timed out after {timeout}s: {argv[0]}") from None
    if proc.returncode != 0:
        raise DeployError(f"command failed ({proc.returncode}): {argv[0]}")


def fetch_git(url: str, branch: str | None, dest: Path, log: TextIO) -> None:
    argv = ["git", "-c", "protocol.file.allow=never", "clone", "--depth", "1"]
    if branch:
        argv += ["--branch", branch]
    argv += [url, str(dest)]
    # The deploy log must never carry URL credentials (PLAN 3.4).
    display = [_redact_url(part) if part == url else part for part in argv]
    _run_logged(argv, log, timeout=600, display=display)


def fetch_zip(zip_path: Path, dest: Path, log: TextIO) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise DeployError("zip has too many members")
        for info in infos:
            name = info.filename
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if name.startswith("/") or ".." in parts:
                raise DeployError(f"unsafe path in zip: {name!r}")
            if not parts:
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise DeployError(f"zip contains a symlink: {name!r}")
            if info.is_dir():
                continue
            total += info.file_size
            if total > MAX_ZIP_TOTAL_BYTES:
                raise DeployError("zip expands beyond the size limit")
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=65536)
    log.write(f"extracted zip into {dest}\n")


def _unwrap_single_dir(dest: Path) -> None:
    """Bot zips often wrap everything in one top folder; if the release
    root has no entry file but exactly one directory, promote its
    contents so entry detection sees the real root."""
    entries = list(dest.iterdir())
    if any((dest / name).is_file() for name in ENTRY_CANDIDATES):
        return
    dirs = [e for e in entries if e.is_dir()]
    files = [e for e in entries if e.is_file()]
    if len(dirs) == 1 and not files:
        inner = dirs[0]
        for child in list(inner.iterdir()):
            shutil.move(str(child), str(dest / child.name))
        inner.rmdir()


def build_release(job: dict[str, object]) -> dict[str, object]:
    root = Path(str(job["root"]))
    bot = str(job["bot"])
    release_id = str(job["release_id"])
    bot_dir = root / "bots" / bot
    release_dir = bot_dir / "releases" / release_id
    venv_dir = bot_dir / "venvs" / release_id
    log_path = bot_dir / "releases" / f"{release_id}.deploy.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        kind = job["kind"]
        if kind == "git":
            branch = job.get("branch")
            fetch_git(str(job["url"]), branch if isinstance(branch, str) else None, release_dir, log)
        elif kind == "zip":
            fetch_zip(Path(str(job["zip_path"])), release_dir, log)
        else:
            raise DeployError(f"unknown deploy kind: {kind!r}")
        _unwrap_single_dir(release_dir)
        entry = detect_entry(release_dir)
        log.write(f"entry: {entry}\n")
        # Syntax check in its own process, per PLAN 2.6 step 2.
        _run_logged(
            [sys.executable, "-m", "py_compile", str(release_dir / entry)],
            log,
            timeout=60,
        )
        _run_logged(
            [sys.executable, "-m", "venv", str(venv_dir)], log, timeout=300
        )
        venv_python = venv_dir / "bin" / "python"
        requirements = release_dir / "requirements.txt"
        if requirements.is_file():
            _run_logged(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-r",
                    str(requirements),
                ],
                log,
                timeout=900,
            )
        else:
            log.write("no requirements.txt; venv left bare\n")
    return {"release_id": release_id, "entry": entry}


def worker_main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m botmgr.deploy <job.json>", file=sys.stderr)
        return 2
    job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    result_path = Path(str(job["result_path"]))
    try:
        result = build_release(job)
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - report, never crash silently
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return 0 if result["ok"] else 1


# ---------------------------------------------------------------------------
# Manager side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployResult:
    release_id: str
    entry: str
    state: str


class DeployService:
    def __init__(
        self,
        root: Path,
        store: StateStore,
        supervisor: Supervisor,
        client: HelperClient,
    ) -> None:
        self._root = root
        self._store = store
        self._supervisor = supervisor
        self._client = client
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, bot: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(bot, threading.Lock())

    def _bot_dir(self, bot: str) -> Path:
        return self._root / "bots" / bot

    def deploy_git(
        self,
        bot: str,
        url: str,
        branch: str | None,
        *,
        url_enc: str | None,
        restart: bool = True,
    ) -> DeployResult:
        validate_git_url(url)
        branch = validate_branch(branch)
        result = self._deploy(
            bot,
            {"kind": "git", "url": url, "branch": branch},
            source_ref=f"git:{branch or 'HEAD'}",
            restart=restart,
        )
        self._store.set_source(bot, "git", url_enc, branch)
        return result

    def deploy_zip(
        self, bot: str, zip_path: Path, filename: str, *, restart: bool = True
    ) -> DeployResult:
        result = self._deploy(
            bot,
            {"kind": "zip", "zip_path": str(zip_path)},
            source_ref=f"upload:{filename}",
            restart=restart,
        )
        self._store.set_source(bot, "upload", None, None)
        return result

    def _deploy(
        self, bot: str, job_spec: dict[str, object], *, source_ref: str, restart: bool
    ) -> DeployResult:
        lock = self._lock_for(bot)
        if not lock.acquire(blocking=False):
            raise DeployError(f"a deploy is already in progress for {bot}")
        try:
            return self._deploy_locked(bot, job_spec, source_ref, restart)
        finally:
            lock.release()

    def _deploy_locked(
        self, bot: str, job_spec: dict[str, object], source_ref: str, restart: bool
    ) -> DeployResult:
        self._client.ensure_layout(bot)
        release_id = new_release_id()
        bot_dir = self._bot_dir(bot)
        tmp_dir = self._root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        job: dict[str, object] = {
            **job_spec,
            "root": str(self._root),
            "bot": bot,
            "release_id": release_id,
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=tmp_dir, suffix=".json", delete=False
        ) as fh:
            job["result_path"] = fh.name + ".result"
            json.dump(job, fh)
            job_path = Path(fh.name)
        result_path = Path(str(job["result_path"]))
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "botmgr.deploy", str(job_path)],
                capture_output=True,
                text=True,
                timeout=WORKER_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise DeployError("deploy worker timed out") from None
        finally:
            job_path.unlink(missing_ok=True)
        result: dict[str, object] = {}
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result_path.unlink(missing_ok=True)
        if proc.returncode != 0 or not result.get("ok"):
            error = str(result.get("error") or proc.stderr.strip() or "unknown")
            # A failed release never becomes current; drop its remains.
            shutil.rmtree(bot_dir / "releases" / release_id, ignore_errors=True)
            shutil.rmtree(bot_dir / "venvs" / release_id, ignore_errors=True)
            raise DeployError(f"deploy failed: {error}")
        entry = str(result["entry"])
        self._store.record_release(bot, release_id, source_ref)
        state = self._activate(bot, release_id, entry, restart)
        self._gc(bot)
        return DeployResult(release_id=release_id, entry=entry, state=state)

    def _activate(
        self, bot: str, release_id: str, entry: str, restart: bool
    ) -> str:
        bot_dir = self._bot_dir(bot)
        was_running = self._supervisor.state(bot).value not in (
            "stopped",
            "attention",
            "secret-pending",
        )
        if was_running:
            self._supervisor.stop(bot)
        tmp_link = bot_dir / f".current.tmp-{os.urandom(3).hex()}"
        os.symlink(f"releases/{release_id}", tmp_link)
        os.replace(tmp_link, bot_dir / "current")
        self._store.set_active_release(bot, release_id)
        data_dir = bot_dir / "data"
        config = LaunchConfig(
            command=(
                str(bot_dir / "venvs" / release_id / "bin" / "python"),
                str(bot_dir / "current" / entry),
            ),
            cwd=str(data_dir),
            env={"HOME": str(data_dir)},
        )
        self._supervisor.set_config(bot, config)
        if was_running and restart:
            self._supervisor.start(bot)
        return self._supervisor.state(bot).value

    def rollback(self, bot: str, *, restart: bool = True) -> DeployResult:
        lock = self._lock_for(bot)
        if not lock.acquire(blocking=False):
            raise DeployError(f"a deploy is already in progress for {bot}")
        try:
            records = self._store.list_releases(bot)
            target = next((r for r in records if not r.active), None)
            if target is None:
                raise DeployError("no previous release to roll back to")
            release_dir = self._bot_dir(bot) / "releases" / target.release_id
            if not release_dir.is_dir():
                raise DeployError("previous release files are gone")
            entry = detect_entry(release_dir)
            state = self._activate(bot, target.release_id, entry, restart)
            return DeployResult(
                release_id=target.release_id, entry=entry, state=state
            )
        finally:
            lock.release()

    def _gc(self, bot: str) -> None:
        """Keep the active release and the newest other one (PLAN 2.6)."""
        bot_dir = self._bot_dir(bot)
        records = self._store.list_releases(bot)
        # Invariant: the active release first, then the newest others,
        # capped at two generations in total.
        ordered = sorted(records, key=lambda r: (not r.active, -r.created_at))
        keep_set = {r.release_id for r in ordered[:2]}
        for record in records:
            if record.release_id in keep_set:
                continue
            shutil.rmtree(bot_dir / "releases" / record.release_id, ignore_errors=True)
            shutil.rmtree(bot_dir / "venvs" / record.release_id, ignore_errors=True)
            (bot_dir / "releases" / f"{record.release_id}.deploy.log").unlink(
                missing_ok=True
            )
            self._store.delete_release(bot, record.release_id)

    def releases(self, bot: str) -> list[ReleaseRecord]:
        return self._store.list_releases(bot)

    def deploy_log(self, bot: str, release_id: str, max_lines: int = 200) -> str:
        path = self._bot_dir(bot) / "releases" / f"{release_id}.deploy.log"
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])


if __name__ == "__main__":
    raise SystemExit(worker_main(sys.argv))
