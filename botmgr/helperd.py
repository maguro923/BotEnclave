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

import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Protocol

from botmgr import procident
from botmgr.launcher import config_from_json
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
    def start_bot(self, bot: str, launch: dict[str, object]) -> ProcessIdentity: ...
    def stop_bot(self, bot: str, identity: ProcessIdentity, grace: float) -> int: ...
    def bot_status(
        self, bot: str, identity: ProcessIdentity
    ) -> tuple[bool, int | None]: ...


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
        raise RequestError("bad_request", f"unknown op: {op!r}")


# ---------------------------------------------------------------------------
# Real executor (runs as root on the target host)
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    # Fixed argv built from validated inputs only; never a shell.
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)


def _user_exists(user: str) -> bool:
    return _run(["id", "-u", user]).returncode == 0


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
        # The bot folder itself belongs to the bot user (PLAN section 2.4):
        # without that, the bot cannot even traverse into its own tree.
        _run(["chown", f"{user}:{group}", str(bot_dir)])
        bot_dir.chmod(0o710)
        # Bot users must be able to traverse the shared parent to reach
        # their own folder, without being able to list the others.
        (self.root / "bots").chmod(0o711)

    def delete_user(self, bot: str) -> None:
        user = username_for(bot)
        if _user_exists(user):
            proc = _run(["userdel", user])
            if proc.returncode != 0:
                raise RuntimeError(f"userdel failed: {proc.stderr.strip()}")

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
        # as the tmux client itself is done.
        with (
            tempfile.TemporaryFile(mode="w+") as out,
            tempfile.TemporaryFile(mode="w+") as err,
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

    def start_bot(self, bot: str, launch: dict[str, object]) -> ProcessIdentity:
        user = username_for(bot)
        data_dir = self.root / "bots" / bot / "data"
        fifo = data_dir / f".launch-{os.urandom(6).hex()}.fifo"
        os.mkfifo(fifo, 0o600)
        _run(["chown", f"{user}:{user}", str(fifo)])
        package_parent = str(self.package_dir.parent)
        pane_command = (
            f"PYTHONPATH={package_parent} exec /usr/bin/python3"
            f" -m botmgr.launcher {fifo}"
        )
        # The server reads botmgr/tmux.conf at start (via -f in _tmux), so
        # default-shell and remain-on-exit already apply to this first pane.
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
            fifo.unlink(missing_ok=True)
            raise RuntimeError(f"tmux new-session failed: {proc.stderr.strip()}")
        # The shim is blocked opening the FIFO for reading; writing the
        # launch configuration unblocks it and it execs into the bot. The
        # write waits for the reader with a bound, so a pane that died
        # before the shim started cannot hang the helper.
        payload = json.dumps(launch).encode()
        try:
            _write_fifo(fifo, payload, timeout=10.0)
        except (OSError, RuntimeError) as exc:
            self._tmux(bot, "kill-session", "-t", "main")
            fifo.unlink(missing_ok=True)
            raise RuntimeError(f"writing launch FIFO failed: {exc}") from exc
        pid = self._pane_pid(bot)
        if pid is None:
            raise RuntimeError("could not determine pane pid")
        identity = procident.read_identity(pid)
        if identity is None:
            raise RuntimeError("pane process vanished during start")
        return identity

    def bot_status(
        self, bot: str, identity: ProcessIdentity
    ) -> tuple[bool, int | None]:
        if procident.verify(identity):
            return True, None
        return False, self._pane_exit_code(bot)

    def stop_bot(self, bot: str, identity: ProcessIdentity, grace: float) -> int:
        exit_code = self._pane_exit_code(bot)
        if procident.verify(identity):
            targets = _descendants_of(identity.pid)
            targets.add(identity.pid)
            _signal_all(targets, 15)  # SIGTERM
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and procident.verify(identity):
                time.sleep(0.1)
            if procident.verify(identity):
                _signal_all(targets, 9)  # SIGKILL
                for _ in range(20):
                    if not procident.verify(identity):
                        break
                    time.sleep(0.1)
            # Sweep any descendant that survived (e.g. it escaped the
            # process group with setsid but is still our descendant by birth
            # is impossible after reparenting; this catches the ones that
            # were still in the tree when we snapshotted it).
            for pid in targets:
                try:
                    os.kill(pid, 0)
                except OSError:
                    continue
                _signal_all({pid}, 9)
            exit_code = self._pane_exit_code(bot)
        self._tmux(bot, "kill-session", "-t", "main")
        return exit_code if exit_code is not None else -1


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


def main(argv: list[str]) -> int:
    root = Path(os.environ.get("BOTENCLAVE_ROOT", str(DEFAULT_ROOT)))
    helper = Helper(root=root, executor=SystemExecutor(root))
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
