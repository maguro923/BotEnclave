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
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from botmgr.helper import HelperClient
from botmgr.launcher import LaunchConfig
from botmgr.pythons import managed_dir, resolve_interpreter
from botmgr.store import ReleaseRecord, StateStore
from botmgr.supervisor import Supervisor

ENTRY_CANDIDATES = ("main.py", "bot.py", "app.py", "run.py")
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024
# Longer than the sum of the worker's stage budgets (clone 600s +
# compile 60s + venv 300s + pip 900s), or a slow-but-legitimate deploy
# would be killed mid-build.
WORKER_TIMEOUT_SECONDS = 2000
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


def _child_env() -> dict[str, str]:
    # Children (venv creation, pip) must not inherit the manager's
    # PYTHONPATH: pip would treat the manager's own packages as
    # already installed in the bot's venv and skip them, leaving the
    # venv silently incomplete (see botmgr.packages.pip_env).
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


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
            env=_child_env(),
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
    redacted = _redact_url(url)
    if redacted != url:
        # git clone stores the full URL as remote.origin.url in
        # .git/config, and the release tree becomes readable by the
        # bot's group once published — the bot must never see the
        # owner's credentials. Rewrite the stored remote without them.
        _run_logged(
            ["git", "-C", str(dest), "remote", "set-url", "origin", redacted],
            log,
            timeout=60,
        )


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
        elif kind == "tree":
            # A release edit session: the tree was staged by the helper
            # from the previous release — or empty, for a first release
            # written by hand (link-free by construction either way) —
            # and edited through the panel's file operations, which
            # cannot create links either.
            tree = Path(str(job["tree_path"]))
            if not tree.is_dir():
                raise DeployError(f"edit tree missing: {tree}")
            shutil.copytree(tree, release_dir)
            log.write(f"copied edit tree from {tree}\n")
        else:
            raise DeployError(f"unknown deploy kind: {kind!r}")
        _unwrap_single_dir(release_dir)
        entry = detect_entry(release_dir)
        log.write(f"entry: {entry}\n")
        # The bot's pinned interpreter (resolved by the manager from
        # the stored version) compiles the entry and builds the venv;
        # without a pin both use the manager's own Python.
        builder = str(job.get("python_path") or sys.executable)
        # Syntax check in its own process, per PLAN 2.6 step 2.
        _run_logged(
            [builder, "-m", "py_compile", str(release_dir / entry)],
            log,
            timeout=60,
        )
        _run_logged([builder, "-m", "venv", str(venv_dir)], log, timeout=300)
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
        extras = job.get("extra_packages")
        if isinstance(extras, list) and extras:
            from botmgr.packages import validate_spec

            specs = [validate_spec(str(spec)) for spec in extras]
            _run_logged(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    *specs,
                ],
                log,
                timeout=900,
            )
            log.write(f"extra packages installed: {', '.join(specs)}\n")
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

    def _sweep_stale_jobs(self, tmp_dir: Path) -> None:
        """Remove job files left behind by a manager that died mid-deploy
        (a git job file carries the credentialed URL in plaintext). A live
        deploy's job file is never older than the worker timeout."""
        cutoff = time.time() - WORKER_TIMEOUT_SECONDS
        for path in tmp_dir.iterdir():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _deploy_locked(
        self, bot: str, job_spec: dict[str, object], source_ref: str, restart: bool
    ) -> DeployResult:
        self._client.ensure_layout(bot)
        release_id = new_release_id()
        bot_dir = self._bot_dir(bot)
        tmp_dir = self._root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_stale_jobs(tmp_dir)
        job: dict[str, object] = {
            **job_spec,
            "root": str(self._root),
            "bot": bot,
            "release_id": release_id,
            "extra_packages": self._store.list_packages(bot),
        }
        stored = self._store.get_bot(bot)
        if stored is not None and stored.python_version:
            try:
                job["python_path"] = resolve_interpreter(
                    stored.python_version, managed_dir(self._root)
                )
            except ValueError as exc:
                raise DeployError(str(exc)) from None
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=tmp_dir, suffix=".json", delete=False
        ) as fh:
            job["result_path"] = fh.name + ".result"
            json.dump(job, fh)
            job_path = Path(fh.name)
        result_path = Path(str(job["result_path"]))
        # The worker runs in its own session so a timeout can kill the
        # whole tree (clone/pip children included), not just the worker.
        proc = subprocess.Popen(
            [sys.executable, "-m", "botmgr.deploy", str(job_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            _stdout, stderr = proc.communicate(timeout=WORKER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            # A timed-out deploy is a failed deploy: drop its remains.
            shutil.rmtree(bot_dir / "releases" / release_id, ignore_errors=True)
            shutil.rmtree(bot_dir / "venvs" / release_id, ignore_errors=True)
            raise DeployError("deploy worker timed out") from None
        finally:
            job_path.unlink(missing_ok=True)
        result: dict[str, object] = {}
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result_path.unlink(missing_ok=True)
        if proc.returncode != 0 or not result.get("ok"):
            error = str(result.get("error") or stderr.strip() or "unknown")
            # A failed release never becomes current; drop its remains.
            shutil.rmtree(bot_dir / "releases" / release_id, ignore_errors=True)
            shutil.rmtree(bot_dir / "venvs" / release_id, ignore_errors=True)
            raise DeployError(f"deploy failed: {error}")
        try:
            # The worker builds as the manager; handing the trees to
            # the bot's group needs root (a non-owner chown is EPERM),
            # so publishing goes through the helper like every other
            # privileged operation.
            self._client.publish_release(bot, release_id)
        except Exception as exc:  # noqa: BLE001 - surfaced as a deploy failure
            shutil.rmtree(bot_dir / "releases" / release_id, ignore_errors=True)
            shutil.rmtree(bot_dir / "venvs" / release_id, ignore_errors=True)
            raise DeployError(f"deploy failed: publish: {exc}") from None
        entry = str(result["entry"])
        self._store.record_release(bot, release_id, source_ref)
        state = self._activate(bot, release_id, entry, restart)
        self._gc(bot)
        return DeployResult(release_id=release_id, entry=entry, state=state)

    def _launch_config(
        self, bot: str, release_id: str, entry: str
    ) -> LaunchConfig:
        """The config a release runs with: the bot's pinned command
        override when one is set, else the release's venv python."""
        bot_dir = self._bot_dir(bot)
        stored = self._store.get_bot(bot)
        override = stored.command_override if stored is not None else None
        command = tuple(override) if override else (
            str(bot_dir / "venvs" / release_id / "bin" / "python"),
            str(bot_dir / "current" / entry),
        )
        data_dir = bot_dir / "data"
        return LaunchConfig(
            command=command,
            cwd=str(data_dir),
            env={"HOME": str(data_dir)},
        )

    def command_info(self, bot: str) -> dict[str, object]:
        """Effective, pinned and default launch commands for the UI."""
        stored = self._store.get_bot(bot)
        override = stored.command_override if stored is not None else None
        default: list[str] | None = None
        active = self._store.active_release(bot)
        if active is not None:
            release_dir = self._bot_dir(bot) / "releases" / active.release_id
            if release_dir.is_dir():
                entry = detect_entry(release_dir)
                bot_dir = self._bot_dir(bot)
                default = [
                    str(bot_dir / "venvs" / active.release_id / "bin" / "python"),
                    str(bot_dir / "current" / entry),
                ]
        if override:
            effective: list[str] = list(override)
        elif default is not None:
            effective = default
        elif stored is not None:
            effective = list(stored.config.command)
        else:
            effective = []
        return {
            "effective": effective,
            "override": list(override) if override else None,
            "default": default,
        }

    def apply_command(self, bot: str) -> None:
        """Re-derive the stored launch config after an override change.

        With an active release the config is rebuilt for it (override
        or default). Without one there is nothing to re-derive: the
        next deploy builds the config, honoring the override.
        """
        active = self._store.active_release(bot)
        if active is None:
            return
        release_dir = self._bot_dir(bot) / "releases" / active.release_id
        if not release_dir.is_dir():
            return
        entry = detect_entry(release_dir)
        self._supervisor.set_config(
            bot, self._launch_config(bot, active.release_id, entry)
        )

    def _point_at(self, bot: str, release_id: str, entry: str) -> None:
        """Switch ``current``, the store's active record and the
        supervisor's launch config to a release (bot already stopped)."""
        # The bot folder is bot-owned and the manager may only
        # traverse it, so the symlink swap goes through the helper.
        self._client.point_current(bot, release_id)
        self._store.set_active_release(bot, release_id)
        self._supervisor.set_config(
            bot, self._launch_config(bot, release_id, entry)
        )

    def _activate(
        self, bot: str, release_id: str, entry: str, restart: bool
    ) -> str:
        bot_dir = self._bot_dir(bot)
        was_running = self._supervisor.state(bot).value not in (
            "stopped",
            "attention",
            "secret-pending",
        )
        previous = self._store.active_release(bot)
        if was_running:
            self._supervisor.stop(bot)
        try:
            self._point_at(bot, release_id, entry)
            if was_running and restart:
                self._supervisor.start(bot)
        except Exception as exc:
            if previous is None or previous.release_id == release_id:
                raise DeployError(f"activation failed: {exc}") from exc
            # DeployError's contract: a failed deploy leaves the previous
            # release active. Point everything back and restart it.
            try:
                prev_entry = detect_entry(bot_dir / "releases" / previous.release_id)
                self._point_at(bot, previous.release_id, prev_entry)
                if was_running and restart:
                    self._supervisor.start(bot)
            except Exception:  # noqa: BLE001, S110 - report the original failure
                pass
            raise DeployError(
                f"activation failed; previous release restored: {exc}"
            ) from exc
        return self._supervisor.state(bot).value

    def deploy_edit(self, bot: str, *, restart: bool = True) -> DeployResult:
        """Commit the bot's edit session as a new release.

        The session staged a copy of the base release under edit/; the
        commit is refused when the active release has moved on since
        editing began, so an edit can never silently build on top of a
        deploy the operator did not see. A failed commit keeps the
        session (and its files) so the operator can fix and retry."""
        base = self._store.edit_session_base(bot)
        if base is None:
            raise DeployError("no edit session in progress for this bot")
        active = self._store.active_release(bot)
        if base == "":
            # Scratch session: it began with no release at all, so a
            # release that appeared in the meantime is just as much a
            # base change as a moved-on active release.
            if active is not None:
                raise DeployError(
                    "a release was deployed since editing began;"
                    " discard the edit session and start again"
                )
        elif active is None or active.release_id != base:
            raise DeployError(
                "the active release changed since editing began;"
                " discard the edit session and start again"
            )
        edit_dir = self._bot_dir(bot) / "edit"
        if not edit_dir.is_dir():
            raise DeployError("the edit staging area is missing")
        job_spec: dict[str, object] = {"kind": "tree", "tree_path": str(edit_dir)}
        result = self._deploy(bot, job_spec, source_ref="ui-edit", restart=restart)
        self._store.clear_edit_session(bot)
        self._client.edit_discard(bot)
        return result

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
