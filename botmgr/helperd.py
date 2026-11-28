"""Privileged helper: the small root-owned program that performs the few
operations the (unprivileged) manager cannot do itself.

Protocol: one JSON request object on stdin, one JSON response on stdout.
The operation set is a fixed enumeration; every argument is validated here,
paths are derived from the bot name by the helper itself, and external
commands are executed with fixed argv and no shell (PLAN section 1.9).

Response shape: {"ok": true, "result": {...}} or
{"ok": false, "error": "<code>", "message": "<detail>"}.
"""

from __future__ import annotations

import contextlib
import dataclasses
import grp
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Protocol

from botmgr import fileops, procident
from botmgr.launcher import config_from_json, format_env_text, parse_env_text
from botmgr.procident import ProcessIdentity

DEFAULT_ROOT = Path("/srv/botenclave")
BOT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
USER_PREFIX = "bm-"
MAX_GRACE_SECONDS = 60.0
TMUX_SOCKET_PREFIX = "be-"


class RequestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def username_for(bot: str) -> str:
    return USER_PREFIX + bot


def socket_name_for(bot: str) -> str:
    return TMUX_SOCKET_PREFIX + bot


_RELEASE_ID_PATTERN = re.compile(r"^\d{8}T\d{6}-[0-9a-f]+$")


def validate_release_id(value: object) -> str:
    if not isinstance(value, str) or not _RELEASE_ID_PATTERN.match(value):
        raise RequestError("bad_request", f"invalid release id: {value!r}")
    return value


def validate_bot_name(value: object) -> str:
    if not isinstance(value, str) or not BOT_NAME_RE.match(value):
        raise RequestError("bad_request", "invalid bot name")
    return value


def validate_identity(value: object) -> ProcessIdentity:
    if not isinstance(value, dict):
        raise RequestError("bad_request", "identity must be an object")
    pid = value.get("pid")
    start_time = value.get("start_time")
    boot_id = value.get("boot_id")
    if not isinstance(pid, int) or not 1 <= pid <= 4_194_304:
        raise RequestError("bad_request", "identity.pid out of range")
    if not isinstance(start_time, int) or start_time <= 0:
        raise RequestError("bad_request", "identity.start_time must be positive")
    if not isinstance(boot_id, str) or not 1 <= len(boot_id) <= 64:
        raise RequestError("bad_request", "identity.boot_id must be a short string")
    return ProcessIdentity(pid=pid, start_time=start_time, boot_id=boot_id)


def identity_to_json(identity: ProcessIdentity) -> dict[str, object]:
    return {
        "pid": identity.pid,
        "start_time": identity.start_time,
        "boot_id": identity.boot_id,
    }


def _reject_unknown_keys(request: dict[str, object], allowed: set[str]) -> None:
    unknown = set(request) - allowed
    if unknown:
        raise RequestError("bad_request", f"unknown fields: {sorted(unknown)}")


class Executor(Protocol):
    def create_user(self, bot: str) -> None: ...
    def delete_user(self, bot: str) -> None: ...
    def delete_bot_files(self, bot: str) -> None: ...
    def store_credential(self, bot: str, text: str) -> None: ...
    def remove_credential_key(self, bot: str, key: str) -> list[str]: ...
    def start_bot(self, bot: str, launch: dict[str, object]) -> ProcessIdentity: ...
    def stop_bot(self, bot: str, identity: ProcessIdentity, grace: float) -> int: ...
    def bot_status(
        self, bot: str, identity: ProcessIdentity
    ) -> tuple[bool, int | None]: ...
    def set_capture(self, bot: str, enabled: bool) -> None: ...
    def capture_state(self, bot: str) -> bool: ...
    def console_snapshot(self, bot: str) -> str: ...
    def console_send(self, bot: str, text: str, enter: bool) -> None: ...
    def console_resize(self, bot: str, cols: int, rows: int) -> None: ...
    def console_clear_history(self, bot: str) -> None: ...
    def ensure_layout(self, bot: str) -> None: ...
    def publish_release(self, bot: str, release_id: str) -> None: ...
    def point_current(self, bot: str, release_id: str) -> None: ...
    def edit_begin(self, bot: str) -> str: ...
    def edit_discard(self, bot: str) -> None: ...
    def disk_usage(self, bot: str) -> int: ...
    def file_list(self, bot: str, zone: str, rel: str) -> list[dict[str, object]]: ...
    def file_read(self, bot: str, zone: str, rel: str) -> dict[str, object]: ...
    def file_write(self, bot: str, zone: str, rel: str, content: str) -> None: ...
    def file_mkdir(self, bot: str, zone: str, rel: str) -> None: ...
    def file_delete(self, bot: str, zone: str, rel: str) -> None: ...
    def file_rename(self, bot: str, zone: str, src: str, dst: str) -> None: ...


@dataclasses.dataclass
class Helper:
    root: Path
    executor: Executor

    def bot_dir(self, bot: str) -> Path:
        return self.root / "bots" / bot

    def validate_launch(self, bot: str, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise RequestError("bad_request", "launch must be an object")
        try:
            config = config_from_json(json.dumps(value).encode())
        except (ValueError, TypeError) as exc:
            raise RequestError("bad_request", f"invalid launch: {exc}") from exc
        base = self.bot_dir(bot).resolve()
        cwd = Path(config.cwd).resolve()
        if cwd != base and base not in cwd.parents:
            raise RequestError(
                "bad_request", "launch.cwd must be inside the bot's own tree"
            )
        return value

    def handle(self, request: object) -> dict[str, object]:
        try:
            result = self._dispatch(request)
        except RequestError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message}
        except Exception as exc:  # noqa: BLE001 - the helper must answer, not crash
            return {"ok": False, "error": "internal", "message": str(exc)}
        return {"ok": True, "result": result}

    def _dispatch(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict):
            raise RequestError("bad_request", "request must be a JSON object")
        op = request.get("op")
        if op == "create_user":
            _reject_unknown_keys(request, {"op", "bot"})
            bot = validate_bot_name(request.get("bot"))
            self.executor.create_user(bot)
            return {"user": username_for(bot)}
        if op == "delete_user":
            _reject_unknown_keys(request, {"op", "bot"})
            bot = validate_bot_name(request.get("bot"))
            self.executor.delete_user(bot)
            return {"user": username_for(bot)}
        if op == "delete_bot_files":
            _reject_unknown_keys(request, {"op", "bot"})
            bot = validate_bot_name(request.get("bot"))
            self.executor.delete_bot_files(bot)
            return {"removed": bot}
        if op == "store_credential":
            _reject_unknown_keys(request, {"op", "bot", "text"})
            bot = validate_bot_name(request.get("bot"))
            text = request.get("text")
            if not isinstance(text, str) or len(text) > 65536 or "\x00" in text:
                raise RequestError("bad_request", "invalid credential text")
            self.executor.store_credential(bot, text)
            return {"stored": bot}
        if op == "remove_credential_key":
            _reject_unknown_keys(request, {"op", "bot", "key"})
            bot = validate_bot_name(request.get("bot"))
            key = request.get("key")
            if not isinstance(key, str) or not key or len(key) > 128 or "\x00" in key:
                raise RequestError("bad_request", "invalid key")
            keys = self.executor.remove_credential_key(bot, key)
            return {"keys": keys}
        if op == "start_bot":
            _reject_unknown_keys(request, {"op", "bot", "launch"})
            bot = validate_bot_name(request.get("bot"))
            launch = self.validate_launch(bot, request.get("launch"))
            identity = self.executor.start_bot(bot, launch)
            return {"identity": identity_to_json(identity)}
        if op == "stop_bot":
            _reject_unknown_keys(request, {"op", "bot", "identity", "grace_seconds"})
            bot = validate_bot_name(request.get("bot"))
            identity = validate_identity(request.get("identity"))
            grace = request.get("grace_seconds", 10.0)
            if not isinstance(grace, (int, float)) or not 0 <= grace <= MAX_GRACE_SECONDS:
                raise RequestError("bad_request", "grace_seconds out of range")
            exit_code = self.executor.stop_bot(bot, identity, float(grace))
            return {"exit_code": exit_code}
        if op == "bot_status":
            _reject_unknown_keys(request, {"op", "bot", "identity"})
            bot = validate_bot_name(request.get("bot"))
            identity = validate_identity(request.get("identity"))
            alive, status_exit_code = self.executor.bot_status(bot, identity)
            return {"alive": alive, "exit_code": status_exit_code}
        if op == "set_capture":
            _reject_unknown_keys(request, {"op", "bot", "enabled"})
            bot = validate_bot_name(request.get("bot"))
            enabled = request.get("enabled")
            if not isinstance(enabled, bool):
                raise RequestError("bad_request", "enabled must be a boolean")
            self.executor.set_capture(bot, enabled)
            return {"enabled": enabled}
        if op == "capture_state":
            _reject_unknown_keys(request, {"op", "bot"})
            bot = validate_bot_name(request.get("bot"))
            return {"enabled": self.executor.capture_state(bot)}
        if op == "console_snapshot":
            _reject_unknown_keys(request, {"op", "bot"})
            bot = validate_bot_name(request.get("bot"))
            return {"text": self.executor.console_snapshot(bot)}
        if op == "console_send":
            _reject_unknown_keys(request, {"op", "bot", "text", "enter"})
            bot = validate_bot_name(request.get("bot"))
            text = request.get("text")
            if not isinstance(text, str) or len(text) > 4096 or "\x00" in text:
                raise RequestError("bad_request", "text must be a string of at most 4096 chars")
            enter = request.get("enter", True)
            if not isinstance(enter, bool):
                raise RequestError("bad_request", "enter must be a boolean")
            self.executor.console_send(bot, text, enter)
            return {"sent": len(text)}
        if op == "ensure_layout":
            _reject_unknown_keys(request, {"op", "bot"})
            bot = validate_bot_name(request.get("bot"))
            self.executor.ensure_layout(bot)
            return {"ready": True}
        if op == "publish_release":
            _reject_unknown_keys(request, {"op", "bot", "release_id"})
            bot = validate_bot_name(request.get("bot"))
            release_id = validate_release_id(request.get("release_id"))
            self.executor.publish_release(bot, release_id)
            return {"published": release_id}
        if op == "point_current":
            _reject_unknown_keys(request, {"op", "bot", "release_id"})
            bot = validate_bot_name(request.get("bot"))
            release_id = validate_release_id(request.get("release_id"))
            self.executor.point_current(bot, release_id)
            return {"current": release_id}
        if op == "edit_begin":
            _reject_unknown_keys(request, {"op", "bot"})
            bot = validate_bot_name(request.get("bot"))
            return {"base_release_id": self.executor.edit_begin(bot)}
        if op == "edit_discard":
            _reject_unknown_keys(request, {"op", "bot"})
            bot = validate_bot_name(request.get("bot"))
            self.executor.edit_discard(bot)
            return {"discarded": bot}
        if op == "disk_usage":
            _reject_unknown_keys(request, {"op", "bot"})
            bot = validate_bot_name(request.get("bot"))
            return {"bytes": self.executor.disk_usage(bot)}
        if op in (
            "file_list",
            "file_read",
            "file_write",
            "file_mkdir",
            "file_delete",
            "file_rename",
        ):
            return self._dispatch_file_op(op, request)
        if op == "console_clear_history":
            _reject_unknown_keys(request, {"op", "bot"})
            bot = validate_bot_name(request.get("bot"))
            self.executor.console_clear_history(bot)
            return {"cleared": True}
        if op == "console_resize":
            _reject_unknown_keys(request, {"op", "bot", "cols", "rows"})
            bot = validate_bot_name(request.get("bot"))
            cols = request.get("cols")
            rows = request.get("rows")
            if not isinstance(cols, int) or not 20 <= cols <= 500:
                raise RequestError("bad_request", "cols out of range")
            if not isinstance(rows, int) or not 5 <= rows <= 200:
                raise RequestError("bad_request", "rows out of range")
            self.executor.console_resize(bot, cols, rows)
            return {"cols": cols, "rows": rows}
        raise RequestError("bad_request", f"unknown op: {op!r}")

    def _dispatch_file_op(
        self, op: str, request: dict[str, object]
    ) -> dict[str, object]:
        allowed = {"op", "bot", "zone", "path"}
        if op == "file_write":
            allowed.add("content")
        if op == "file_rename":
            allowed.add("to")
        _reject_unknown_keys(request, allowed)
        bot = validate_bot_name(request.get("bot"))
        zone = request.get("zone")
        if zone not in fileops.ZONES_READABLE:
            raise RequestError("bad_request", "invalid zone")
        rel = request.get("path")
        if not isinstance(rel, str):
            raise RequestError("bad_request", "path must be a string")
        try:
            if op == "file_list":
                return {"entries": self.executor.file_list(bot, zone, rel)}
            if op == "file_read":
                return self.executor.file_read(bot, zone, rel)
            if op == "file_write":
                content = request.get("content")
                if not isinstance(content, str):
                    raise RequestError("bad_request", "content must be a string")
                self.executor.file_write(bot, zone, rel, content)
                return {"written": len(content)}
            if op == "file_mkdir":
                self.executor.file_mkdir(bot, zone, rel)
                return {"created": rel}
            if op == "file_delete":
                self.executor.file_delete(bot, zone, rel)
                return {"deleted": rel}
            to = request.get("to")
            if not isinstance(to, str):
                raise RequestError("bad_request", "to must be a string")
            self.executor.file_rename(bot, zone, rel, to)
            return {"renamed": to}
        except fileops.FileOpError as exc:
            raise RequestError(exc.code, exc.message) from exc


# ---------------------------------------------------------------------------
# Real executor (runs as root on the target host)
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    # Fixed argv built from validated inputs only; never a shell.
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)


def _user_exists(user: str) -> bool:
    return _run(["id", "-u", user]).returncode == 0


def _user_pids(uid: int) -> list[int]:
    pids: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        with contextlib.suppress(OSError):
            if os.stat(f"/proc/{entry}").st_uid == uid:
                pids.append(int(entry))
    return pids


def _kill_user_processes(user: str) -> None:
    """SIGTERM, then SIGKILL, every process owned by a bot user.

    Deletion must not be held hostage by a process that escaped the
    supervisor's stop (a double-forked daemon, a lingering tmux
    server): userdel refuses while the user owns any process.
    """
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return
    for pid in _user_pids(uid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _user_pids(uid):
        time.sleep(0.1)
    for pid in _user_pids(uid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _make_removable(target: Path) -> None:
    for dirpath, _dirnames, filenames in os.walk(target):
        with contextlib.suppress(OSError):
            os.chmod(dirpath, 0o700)
        for name in filenames:
            with contextlib.suppress(OSError):
                os.chmod(Path(dirpath) / name, 0o600)


class SystemExecutor:
    """Executes validated requests against the real system.

    Only methods on this class touch the outside world, and every command is
    built from the bot name alone (never from raw request strings).
    """

    def __init__(
        self,
        root: Path,
        manager_group: str | None = None,
        package_dir: Path | None = None,
    ) -> None:
        self.root = root
        self.manager_group = manager_group
        # Where the botmgr package lives on the target host. It must be
        # readable by the bot users (the tmux config and the launcher shim
        # are loaded from here), so production installs it system-wide.
        self.package_dir = package_dir or Path(__file__).resolve().parent

    # -- users -------------------------------------------------------------
    def create_user(self, bot: str) -> None:
        user = username_for(bot)
        if not _user_exists(user):
            proc = _run(
                [
                    "useradd",
                    "--system",
                    "--no-create-home",
                    "--shell",
                    "/usr/sbin/nologin",
                    user,
                ]
            )
            if proc.returncode != 0:
                raise RuntimeError(f"useradd failed: {proc.stderr.strip()}")
        bot_dir = self.root / "bots" / bot
        group = self.manager_group or user
        for sub, mode in (("data", 0o700), ("logs", 0o750)):
            path = bot_dir / sub
            path.mkdir(parents=True, exist_ok=True)
            _run(["chown", f"{user}:{group}", str(path)])
            path.chmod(mode)
        self.ensure_layout(bot)
        # The bot folder itself belongs to the bot user (PLAN section 2.4):
        # without that, the bot cannot even traverse into its own tree.
        _run(["chown", f"{user}:{group}", str(bot_dir)])
        bot_dir.chmod(0o710)
        # Bot users must be able to traverse the shared parent to reach
        # their own folder, without being able to list the others.
        (self.root / "bots").chmod(0o711)

    def ensure_layout(self, bot: str) -> None:
        """Create the manager-owned release areas (PLAN section 2.4):
        releases/ and venvs/ belong to the manager (the owner of the
        deployment root) with the bot's group allowed to read/execute, so
        the bot can run its code but never rewrite a release."""
        user = username_for(bot)
        manager_uid = os.stat(self.root).st_uid
        bot_dir = self.root / "bots" / bot
        for sub in ("releases", "venvs"):
            path = bot_dir / sub
            path.mkdir(parents=True, exist_ok=True)
            _run(["chown", f"{manager_uid}:{user}", str(path)])
            path.chmod(0o750)

    def publish_release(self, bot: str, release_id: str) -> None:
        """Hand a built release to the bot's group (PLAN 2.4).

        The deploy worker runs as the manager user and builds the
        release as manager-owned files; only root may change their
        group to the bot's group (the manager is not a member of it,
        and a non-owner chown is EPERM), so publishing lives here.
        Directories become 0750, files 0750 when executable else
        0640; symlinks are never followed or modified, so a release
        cannot redirect the chown/chmod outside its own tree.
        """
        validate_release_id(release_id)
        try:
            gid = pwd.getpwnam(username_for(bot)).pw_gid
        except KeyError:
            raise RuntimeError(f"no OS user for bot {bot!r}") from None
        bot_dir = self.root / "bots" / bot
        for sub in ("releases", "venvs"):
            base = bot_dir / sub / release_id
            if base.is_symlink() or not base.is_dir():
                raise RuntimeError(f"release tree missing: {base}")
            for dirpath, _dirnames, filenames in os.walk(base):
                dir_path = Path(dirpath)
                os.chown(dir_path, -1, gid, follow_symlinks=False)
                os.chmod(dir_path, 0o750, follow_symlinks=False)
                for name in filenames:
                    file_path = dir_path / name
                    st = os.lstat(file_path)
                    if stat.S_ISLNK(st.st_mode):
                        continue
                    os.chown(file_path, -1, gid, follow_symlinks=False)
                    os.chmod(
                        file_path,
                        0o750 if st.st_mode & 0o111 else 0o640,
                        follow_symlinks=False,
                    )

    def point_current(self, bot: str, release_id: str) -> None:
        """Switch the bot's ``current`` symlink to a release.

        The bot folder belongs to the bot user and the manager has
        traverse-only access to it, so the manager cannot create the
        symlink itself (EACCES); the swap is done here, atomically
        (temporary link + rename), like every other write into
        bot-owned territory.
        """
        validate_release_id(release_id)
        bot_dir = self.root / "bots" / bot
        release_dir = bot_dir / "releases" / release_id
        if release_dir.is_symlink() or not release_dir.is_dir():
            raise RuntimeError(f"release tree missing: {release_dir}")
        tmp_link = bot_dir / f".current.tmp-{os.urandom(3).hex()}"
        os.symlink(f"releases/{release_id}", tmp_link)
        os.replace(tmp_link, bot_dir / "current")

    def edit_begin(self, bot: str) -> str:
        """Stage an editable copy of the active release under edit/.

        Releases themselves stay immutable; the panel edits this copy
        and a commit turns it into a brand-new release. The tree is
        owned like a release (manager-owned, the bot's group may read)
        so the deploy worker can build from it, while the bot itself
        never sees the staging area. Symlinks are refused, mirroring
        the release rules, and stale bytecode caches are not carried
        into the next release.

        A bot with no release yet gets an empty staging area (a
        scratch session, base "") so its first release can be written
        by hand in the panel; the commit path refuses if a release
        has appeared in the meantime."""
        bot_dir = self.root / "bots" / bot
        current = bot_dir / "current"
        source: Path | None = None
        base = ""
        if current.is_symlink():
            base = Path(os.readlink(current)).name
            validate_release_id(base)
            source = bot_dir / "releases" / base
            if source.is_symlink() or not source.is_dir():
                raise RuntimeError(f"active release tree missing: {source}")
        target = bot_dir / "edit"
        if target.is_symlink():
            raise RuntimeError(f"refusing a symlinked edit directory: {target}")
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir()
        if source is not None:
            _copy_tree_no_links(source, target)
        manager_uid = os.stat(self.root).st_uid
        try:
            gid = pwd.getpwnam(username_for(bot)).pw_gid
        except KeyError:
            raise RuntimeError(f"no OS user for bot {bot!r}") from None
        for dirpath, _dirnames, filenames in os.walk(target):
            dir_path = Path(dirpath)
            os.chown(dir_path, manager_uid, gid, follow_symlinks=False)
            os.chmod(dir_path, 0o750, follow_symlinks=False)
            for name in filenames:
                file_path = dir_path / name
                os.chown(file_path, manager_uid, gid, follow_symlinks=False)
                os.chmod(file_path, 0o640, follow_symlinks=False)
        return base

    def edit_discard(self, bot: str) -> None:
        target = self.root / "bots" / bot / "edit"
        if target.is_symlink():
            raise RuntimeError(f"refusing a symlinked edit directory: {target}")
        shutil.rmtree(target, ignore_errors=True)

    def delete_user(self, bot: str) -> None:
        user = username_for(bot)
        if _user_exists(user):
            _kill_user_processes(user)
            proc = _run(["userdel", user])
            if proc.returncode != 0:
                _kill_user_processes(user)
                proc = _run(["userdel", user])
            if proc.returncode != 0:
                raise RuntimeError(f"userdel failed: {proc.stderr.strip()}")

    def store_credential(self, bot: str, text: str) -> None:
        """Encrypt a bot's .env text with systemd-creds, as root.

        The manager process cannot do this itself: encrypting with the
        host key demands interactive authentication from an
        unprivileged caller (io.systemd.InteractiveAuthenticationRequired),
        so the privileged helper performs it. The credential name must
        match the one the bot's unit passes to LoadCredentialEncrypted.
        """
        binary = shutil.which("systemd-creds")
        if binary is None:
            raise RuntimeError("systemd-creds is not available on this host")
        secrets_dir = self.root / "secrets"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        dest = secrets_dir / f"{bot}.cred"
        fd, tmp_name = tempfile.mkstemp(dir=secrets_dir, prefix=".cred-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            tmp.chmod(0o600)
            proc = _run(
                [binary, "encrypt", f"--name=botmgr-{bot}", str(tmp), str(dest)]
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"systemd-creds encrypt failed: {proc.stderr.strip()}"
                )
            dest.chmod(0o600)
        finally:
            tmp.unlink(missing_ok=True)

    def delete_bot_files(self, bot: str) -> None:
        bots_root = (self.root / "bots").resolve()
        target = bots_root / bot
        if target.parent != bots_root:
            raise RuntimeError(f"bot directory escapes the bots tree: {target}")
        if target.is_symlink():
            # Never follow or remove through a link planted in the tree.
            raise RuntimeError(f"refusing to remove symlinked bot directory: {target}")
        if target.exists():
            # Nothing running as the bot user may survive into the
            # removal: a live process can recreate files mid-delete,
            # and userdel refuses while the user owns any process —
            # either one leaves the tree behind for the next bot that
            # reuses the name.
            with contextlib.suppress(Exception):
                self._tmux(bot, "kill-server")
            _kill_user_processes(username_for(bot))
            try:
                shutil.rmtree(target)
            except OSError:
                _make_removable(target)
                shutil.rmtree(target)

    # -- tmux / process control ---------------------------------------------
    def _tmux(self, bot: str, *args: str) -> subprocess.CompletedProcess[str]:
        user = username_for(bot)
        # Drop privileges with su, not runuser: on this platform a tmux
        # server started under runuser is killed when runuser's session
        # ends, while under su it persists (verified in the dev VM).
        # Arguments go through shlex.join, so the pane command string and
        # paths reach tmux as single words.
        command = shlex.join(
            [
                "tmux",
                # -u forces UTF-8 mode regardless of the (often unset)
                # locale under systemd; without it, older tmux builds
                # replace non-ASCII output (arrows, Japanese, …) in
                # panes, which then lands garbled in the console log.
                "-u",
                "-L",
                socket_name_for(bot),
                "-f",
                str(self.package_dir / "tmux.conf"),
                *args,
            ]
        )
        argv = ["su", "-s", "/bin/sh", user, "-c", command]
        # tmux daemonizes its server, which keeps inherited pipe FDs open;
        # capturing through pipes would block until the server exits.
        # Temporary files have no EOF semantics, so the call returns as soon
        # as the tmux client itself is done. They are read back as UTF-8
        # explicitly: the locale here is not guaranteed to be one.
        with (
            tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out,
            tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err,
        ):
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=120,
                check=False,
            )
            out.seek(0)
            err.seek(0)
            return subprocess.CompletedProcess(
                argv, proc.returncode, out.read(), err.read()
            )

    def _pane_pid(self, bot: str) -> int | None:
        proc = self._tmux(bot, "display-message", "-p", "-t", "main", "#{pane_pid}")
        if proc.returncode != 0:
            return None
        try:
            return int(proc.stdout.strip())
        except ValueError:
            return None

    def _pane_exit_code(self, bot: str) -> int | None:
        proc = self._tmux(
            bot, "display-message", "-p", "-t", "main", "#{pane_dead_status}"
        )
        if proc.returncode != 0:
            return None
        text = proc.stdout.strip()
        try:
            return int(text)
        except ValueError:
            return None

    def remove_credential_key(self, bot: str, key: str) -> list[str]:
        """Remove one key from a bot's credential, as root, and return
        the remaining key names. The manager never sees the values:
        decryption, editing and re-encryption all happen here. Removing
        the last key removes the credential itself."""
        env = self._credential_env(bot)
        env.pop(key, None)
        if env:
            self.store_credential(bot, format_env_text(env))
        else:
            (self.root / "secrets" / f"{bot}.cred").unlink(missing_ok=True)
        return sorted(env)

    def _credential_env(self, bot: str) -> dict[str, str]:
        """Decrypt this bot's systemd-creds credential (root only) and
        parse its .env content. The credential name is bound to the bot,
        so another bot's file cannot be substituted (PLAN 3.3)."""
        cred = self.root / "secrets" / f"{bot}.cred"
        if not cred.exists():
            raise RuntimeError(f"no credential stored for bot {bot}")
        proc = _run(
            [
                "systemd-creds",
                "decrypt",
                f"--name=botmgr-{bot}",
                str(cred),
                "-",
            ]
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"systemd-creds decrypt failed: {proc.stderr.strip()}"
            )
        return parse_env_text(proc.stdout)

    def _unit_name(self, bot: str) -> str:
        return f"botenclave-bot-{bot}.service"

    def _start_via_unit(
        self, bot: str, launch: dict[str, object]
    ) -> ProcessIdentity:
        """systemd mode (PLAN 3.3): write the secret-free launch.json and
        start the bot's unit. PID 1 decrypts the credential; the helper
        never sees plaintext here, and refuses to write one to disk."""
        if launch.get("secret_keys"):
            raise RuntimeError(
                "this bot's secret backend cannot be delivered through a"
                " systemd unit; switch the bot to the systemd-creds backend"
            )
        unit = self._unit_name(bot)
        unit_path = Path("/etc/systemd/system") / unit
        if not unit_path.exists():
            raise RuntimeError(
                f"unit {unit} is not installed; run"
                f" `botmgr systemd install-bot {bot}` first"
            )
        bot_dir = self.root / "bots" / bot
        payload: dict[str, object] = {
            key: launch[key]
            for key in ("command", "cwd", "env", "env_text", "rlimits")
            if key in launch
        }
        if launch.get("dotenv_mode"):
            payload["dotenv_mode"] = True
        target = bot_dir / "launch.json"
        tmp = bot_dir / f".launch.json.tmp-{os.urandom(4).hex()}"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        _run(["chown", f"root:{username_for(bot)}", str(tmp)])
        tmp.chmod(0o640)
        os.replace(tmp, target)
        proc = _run(["systemctl", "start", unit])
        if proc.returncode != 0:
            raise RuntimeError(f"systemctl start failed: {proc.stderr.strip()}")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            pid = self._pane_pid(bot)
            if pid is not None:
                identity = procident.read_identity(pid)
                if identity is not None:
                    return identity
            time.sleep(0.2)
        raise RuntimeError("bot unit started but no pane appeared")

    def start_bot(self, bot: str, launch: dict[str, object]) -> ProcessIdentity:
        user = username_for(bot)
        data_dir = self.root / "bots" / bot / "data"
        launch = dict(launch)
        if launch.pop("via_unit", False):
            return self._start_via_unit(bot, launch)
        env_obj = launch.get("env")
        env: dict[str, str] = dict(env_obj) if isinstance(env_obj, dict) else {}
        if launch.pop("use_credential", False):
            # Secret values win over the plain launch env and never pass
            # through the manager process.
            env.update(self._credential_env(bot))
        if launch.pop("dotenv_mode", False):
            # dotenv compatibility mode (PLAN 2.8): the merged env also
            # lands as data/.env, bot-owned 0600. Plaintext on disk is the
            # documented weakness of this mode.
            dotenv = data_dir / ".env"
            tmp = data_dir / f".env.tmp-{os.urandom(4).hex()}"
            tmp.write_text(format_env_text(env), encoding="utf-8")
            _run(["chown", f"{user}:{user}", str(tmp)])
            tmp.chmod(0o600)
            os.replace(tmp, dotenv)
        launch["env"] = env
        fifo = data_dir / f".launch-{os.urandom(6).hex()}.fifo"
        os.mkfifo(fifo, 0o600)
        package_parent = str(self.package_dir.parent)
        # The pane command runs through /bin/sh (tmux default-shell):
        # build it with shlex.join so every path is quoted as a single
        # word — an install path containing spaces or shell characters
        # must neither break the start nor inject into the pane shell.
        pane_command = "exec " + shlex.join(
            [
                "env",
                f"PYTHONPATH={package_parent}",
                "/usr/bin/python3",
                "-m",
                "botmgr.launcher",
                str(fifo),
            ]
        )
        try:
            _run(["chown", f"{user}:{user}", str(fifo)])
            # A leftover session can only be a zombie: the supervisor calls
            # start only for bots it believes are stopped, and a live one
            # would have been adopted by reconcile instead. Clear it so a
            # crashed predecessor cannot block the start forever.
            self._tmux(bot, "kill-session", "-t", "main")
            # The server reads botmgr/tmux.conf at start (via -f in
            # _tmux), so default-shell and remain-on-exit already apply to
            # this first pane.
            proc = self._tmux(
                bot,
                "new-session",
                "-d",
                "-s",
                "main",
                "-x",
                "200",
                "-y",
                "50",
                pane_command,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"tmux new-session failed: {proc.stderr.strip()}"
                )
        except Exception:
            # _tmux raises on timeout instead of returning a failed
            # process; the FIFO must not leak on that path either.
            fifo.unlink(missing_ok=True)
            raise
        try:
            # Attach log capture before the bot starts producing output.
            self.set_capture(bot, True)
            # Capture the pane's identity BEFORE unblocking the shim.
            # The shim is blocked opening the FIFO, so its process
            # cannot exit yet; and because it execs into the bot (same
            # pid, same start time), the identity read now names the
            # bot itself. Reading it after the FIFO write instead raced
            # with bots that exit the instant they start: by the time
            # the helper looked, tmux had already reaped the pane
            # process and the start failed as "vanished".
            identity: ProcessIdentity | None = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                pid = self._pane_pid(bot)
                if pid is not None:
                    identity = procident.read_identity(pid)
                    if identity is not None:
                        break
                time.sleep(0.05)
            if identity is None:
                raise RuntimeError("pane process vanished during start")
            # Writing the launch configuration unblocks the shim and
            # it execs into the bot. The write waits for the reader
            # with a bound, so a pane that died before the shim
            # started cannot hang the helper.
            payload = json.dumps(launch).encode()
            try:
                _write_fifo(fifo, payload, timeout=10.0)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError(f"writing launch FIFO failed: {exc}") from exc
            return identity
        except Exception:
            # Any failure after the session exists must not leak the
            # session or the (possibly secret-bearing) FIFO.
            self._tmux(bot, "kill-session", "-t", "main")
            fifo.unlink(missing_ok=True)
            raise

    def bot_status(
        self, bot: str, identity: ProcessIdentity
    ) -> tuple[bool, int | None]:
        if procident.verify(identity):
            return True, None
        return False, self._pane_exit_code(bot)

    # -- console / capture ---------------------------------------------------
    def _capture_target(self, bot: str) -> Path:
        return self.root / "bots" / bot / "logs" / "console.log"

    def capture_state(self, bot: str) -> bool:
        proc = self._tmux(bot, "display-message", "-p", "-t", "main", "#{pane_pipe}")
        return proc.returncode == 0 and proc.stdout.strip() == "1"

    def set_capture(self, bot: str, enabled: bool) -> None:
        currently = self.capture_state(bot)
        if enabled and not currently:
            target = self._capture_target(bot)
            # The writer stamps every saved line with its write time.
            command = shlex.join(
                [
                    "/usr/bin/python3",
                    str(self.package_dir / "logwriter.py"),
                    str(target),
                ]
            )
            proc = self._tmux(bot, "pipe-pane", "-t", "main", command)
            if proc.returncode != 0:
                raise RuntimeError(f"pipe-pane failed: {proc.stderr.strip()}")
        elif not enabled and currently:
            proc = self._tmux(bot, "pipe-pane", "-t", "main")
            if proc.returncode != 0:
                raise RuntimeError(f"pipe-pane close failed: {proc.stderr.strip()}")

    def console_snapshot(self, bot: str) -> str:
        proc = self._tmux(bot, "capture-pane", "-p", "-t", "main")
        if proc.returncode != 0:
            # A bot that is not running has no tmux server to capture:
            # that is an empty pane, not an error (the panel falls
            # back to the captured log file for stopped bots).
            stderr = proc.stderr.strip()
            if any(
                marker in stderr
                for marker in (
                    "error connecting to",
                    "no server running",
                    "can't find session",
                    "no sessions",
                )
            ):
                return ""
            raise RuntimeError(f"capture-pane failed: {stderr}")
        return proc.stdout

    def console_send(self, bot: str, text: str, enter: bool) -> None:
        if text:
            proc = self._tmux(bot, "send-keys", "-t", "main", "-l", "--", text)
            if proc.returncode != 0:
                raise RuntimeError(f"send-keys failed: {proc.stderr.strip()}")
        if enter:
            proc = self._tmux(bot, "send-keys", "-t", "main", "Enter")
            if proc.returncode != 0:
                raise RuntimeError(f"send-keys Enter failed: {proc.stderr.strip()}")

    def console_resize(self, bot: str, cols: int, rows: int) -> None:
        proc = self._tmux(
            bot, "resize-pane", "-t", "main", "-x", str(cols), "-y", str(rows)
        )
        if proc.returncode != 0:
            raise RuntimeError(f"resize-pane failed: {proc.stderr.strip()}")

    def console_clear_history(self, bot: str) -> None:
        proc = self._tmux(bot, "clear-history", "-t", "main")
        if proc.returncode != 0:
            raise RuntimeError(f"clear-history failed: {proc.stderr.strip()}")

    def disk_usage(self, bot: str) -> int:
        total = 0
        base = self.root / "bots" / bot
        for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
            for name in filenames:
                try:
                    total += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    continue
        return total

    # -- file management (PLAN 2.8; validation lives in botmgr.fileops) ------
    def _bot_root(self, bot: str) -> Path:
        return self.root / "bots" / bot

    def file_list(self, bot: str, zone: str, rel: str) -> list[dict[str, object]]:
        return [
            entry.to_json()
            for entry in fileops.list_dir(self._bot_root(bot), zone, rel)
        ]

    def file_read(self, bot: str, zone: str, rel: str) -> dict[str, object]:
        return fileops.read_file(self._bot_root(bot), zone, rel)

    def _chown_written(self, bot: str, zone: str, path: Path) -> None:
        """Hand a helper-written path to its real owner. The helper
        runs as root, so without this a panel-written file lands
        root-owned inside somebody else's tree. In data/ the owner is
        the bot; in the edit staging area it is the manager, whose
        deploy worker must read the tree back when the edit is saved
        as a release (a bot-owned 0640 staging file is unreadable to
        it). Best effort: a non-root executor (direct mode) already
        owns what it writes. Symlinks are never followed."""
        try:
            bot_entry = pwd.getpwnam(username_for(bot))
        except KeyError:
            return
        if zone == "data":
            uid, gid = bot_entry.pw_uid, bot_entry.pw_gid
        elif zone == "edit":
            uid, gid = os.stat(self.root).st_uid, bot_entry.pw_gid
        else:
            return
        try:
            if path.is_symlink():
                return
            os.chown(path, uid, gid, follow_symlinks=False)
        except OSError:
            pass

    def file_write(self, bot: str, zone: str, rel: str, content: str) -> None:
        target = fileops.write_file(self._bot_root(bot), zone, rel, content)
        self._chown_written(bot, zone, target)

    def file_mkdir(self, bot: str, zone: str, rel: str) -> None:
        target = fileops.make_dir(self._bot_root(bot), zone, rel)
        self._chown_written(bot, zone, target)
        target.chmod(0o750)

    def file_delete(self, bot: str, zone: str, rel: str) -> None:
        fileops.delete_path(self._bot_root(bot), zone, rel)

    def file_rename(self, bot: str, zone: str, src: str, dst: str) -> None:
        fileops.rename_path(self._bot_root(bot), zone, src, dst)

    def stop_bot(self, bot: str, identity: ProcessIdentity, grace: float) -> int:
        exit_code = self._pane_exit_code(bot)
        if procident.verify(identity):
            targets = _descendants_of(identity.pid)
            targets.add(identity.pid)
            # Remember each target's start time: the grace wait below can
            # take a minute, and a PID recycled in that window must never
            # be signalled — it names an unrelated process by then.
            snapshot = {
                pid: start
                for pid in targets
                if (start := procident.start_time_of(pid)) is not None
            }
            _signal_all(targets, 15)  # SIGTERM
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and procident.verify(identity):
                time.sleep(0.1)
            if procident.verify(identity):
                _signal_all(_still_ours(snapshot), 9)  # SIGKILL
                for _ in range(20):
                    if not procident.verify(identity):
                        break
                    time.sleep(0.1)
            # Sweep any descendant that survived (e.g. it escaped the
            # process group with setsid but is still our descendant by birth
            # is impossible after reparenting; this catches the ones that
            # were still in the tree when we snapshotted it).
            _signal_all(_still_ours(snapshot), 9)
            exit_code = self._pane_exit_code(bot)
        self._tmux(bot, "kill-session", "-t", "main")
        # If the bot also has a systemd unit, stop it so the unit's main
        # process (botunit) exits instead of lingering as an empty unit.
        unit_path = Path("/etc/systemd/system") / self._unit_name(bot)
        if unit_path.exists():
            _run(["systemctl", "stop", self._unit_name(bot)])
        return exit_code if exit_code is not None else -1


def _copy_tree_no_links(source: Path, target: Path) -> None:
    """Copy a release tree into the edit staging area. Symlinks are
    refused (releases never legitimately contain them) and bytecode
    caches are skipped so they cannot leak into the next release."""
    for entry in os.scandir(source):
        if entry.is_symlink():
            raise RuntimeError(f"refusing to copy a symlinked entry: {entry.path}")
        dest = target / entry.name
        if entry.is_dir(follow_symlinks=False):
            if entry.name == "__pycache__":
                continue
            dest.mkdir()
            _copy_tree_no_links(Path(entry.path), dest)
        else:
            shutil.copy2(entry.path, dest)


def _write_fifo(fifo: Path, payload: bytes, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "timed out waiting for the launcher shim to open the FIFO"
                ) from None
            time.sleep(0.05)
    try:
        os.set_blocking(fd, True)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def _descendants_of(pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rparen = stat.rfind(")")
        if rparen < 0:
            continue
        fields = stat[rparen + 2 :].split()
        if len(fields) <= 1:
            continue
        try:
            ppid = int(fields[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    found: set[int] = set()
    stack = [pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _signal_all(pids: set[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _still_ours(snapshot: dict[int, int]) -> set[int]:
    """Filter a {pid: start_time} snapshot to processes that are still
    the very processes it recorded (same PID, same start time)."""
    return {
        pid
        for pid, start in snapshot.items()
        if procident.start_time_of(pid) == start
    }


def _build_executor() -> SystemExecutor:
    root = Path(os.environ.get("BOTENCLAVE_ROOT", str(DEFAULT_ROOT)))
    package_dir_env = os.environ.get("BOTENCLAVE_PACKAGE_DIR")
    package_dir = Path(package_dir_env) if package_dir_env else None
    manager_group = os.environ.get("BOTENCLAVE_MANAGER_GROUP") or None
    if manager_group is None and "SUDO_USER" not in os.environ:
        # Direct (non-sudo) run: the manager is this same user, so its
        # primary group is the group that needs read access to bot logs.
        try:
            manager_group = grp.getgrgid(os.getegid()).gr_name
        except KeyError:
            manager_group = None
    return SystemExecutor(
        root, manager_group=manager_group, package_dir=package_dir
    )


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    import fcntl
    import struct
    import termios

    fcntl.ioctl(
        fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
    )


def _session_exists(executor: SystemExecutor, bot: str) -> bool:
    try:
        proc = executor._tmux(bot, "has-session", "-t", "main")
    except OSError:
        return False
    return proc.returncode == 0


def _wait_for_session(
    executor: SystemExecutor,
    bot: str,
    timeout: float = 30.0,
    interval: float = 0.25,
) -> bool:
    """Wait for the bot's tmux session to appear.

    The console is often opened before the bot is started. Attaching
    immediately would fail, and tmux's error text would be forwarded
    as pane output, which the manager mistakes for a healthy stream
    and never falls back from. Poll instead; the caller gives up
    quietly when the bot never starts.
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        if _session_exists(executor, bot):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def run_console_stream(
    executor: SystemExecutor, bot: str, cols: int, rows: int
) -> int:
    """Streaming console mode: attach a tmux client to the bot's pane on
    a fresh PTY (as the bot user) and pump raw terminal bytes between
    that PTY and this process's stdio, framed as newline-delimited JSON
    ({\"type\": \"data\", \"data\": <base64>} / {\"type\": \"resize\", ...}
    in, data / {\"type\": \"exit\"} out). This is the one helper mode that
    is not a single request/response: it lives exactly as long as the
    operator's console session, like the terminal proxy behind a
    hypervisor's web console."""
    import base64
    import pty
    import select

    bot = validate_bot_name(bot)
    cols = min(max(cols, 1), 500)
    rows = min(max(rows, 1), 200)
    if not _wait_for_session(executor, bot):
        # No pane to attach to: end without emitting any data, so
        # the manager degrades to snapshot polling for this console.
        sys.stdout.write(json.dumps({"type": "exit"}) + "\n")
        sys.stdout.flush()
        return 0
    user = username_for(bot)
    inner = shlex.join(
        [
            "env",
            "TERM=xterm-256color",
            "tmux",
            "-u",
            "-L",
            socket_name_for(bot),
            "-f",
            str(executor.package_dir / "tmux.conf"),
            "attach-session",
            "-t",
            "main",
        ]
    )
    master, slave = pty.openpty()
    _set_winsize(master, cols, rows)
    proc = subprocess.Popen(
        ["su", "-s", "/bin/sh", user, "-c", inner],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    stdout = sys.stdout.buffer

    def emit(frame: dict[str, object]) -> None:
        stdout.write(json.dumps(frame).encode() + b"\n")
        stdout.flush()

    try:
        pending = b""
        while True:
            ready, _, _ = select.select([0, master], [], [], 0.5)
            if master in ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                emit(
                    {
                        "type": "data",
                        "data": base64.b64encode(chunk).decode(),
                    }
                )
            if 0 in ready:
                block = os.read(0, 65536)
                if not block:
                    break
                pending += block
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    kind = frame.get("type")
                    if kind == "data":
                        data = frame.get("data")
                        if isinstance(data, str):
                            try:
                                os.write(
                                    master, base64.b64decode(data)
                                )
                            except (ValueError, OSError):
                                pass
                    elif kind == "resize":
                        c, r = frame.get("cols"), frame.get("rows")
                        if isinstance(c, int) and isinstance(r, int):
                            _set_winsize(
                                master,
                                min(max(c, 1), 500),
                                min(max(r, 1), 200),
                            )
                    elif kind == "close":
                        return 0
    finally:
        with contextlib.suppress(OSError):
            os.close(master)
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGHUP)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=3)
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        emit({"type": "exit"})
    return 0


def main(argv: list[str]) -> int:
    # The manager serves with a private umask (its databases hold
    # session tokens) and sudo preserves the caller's umask, so set
    # the conventional one explicitly: the tmux servers started here
    # pass it on to the bots' panes and log capture, whose files the
    # manager and the bots must be able to read.
    os.umask(0o022)
    if len(argv) == 5 and argv[1] == "--console-stream":
        try:
            cols, rows = int(argv[3]), int(argv[4])
            return run_console_stream(_build_executor(), argv[2], cols, rows)
        except Exception:  # noqa: BLE001 - no JSON channel in stream mode
            return 2
    root = Path(os.environ.get("BOTENCLAVE_ROOT", str(DEFAULT_ROOT)))
    helper = Helper(root=root, executor=_build_executor())
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        response: dict[str, object] = {
            "ok": False,
            "error": "bad_request",
            "message": f"request is not valid JSON: {exc}",
        }
        print(json.dumps(response))
        return 1
    response = helper.handle(request)
    print(json.dumps(response))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
