"""Client for the privileged helper (botmgr.helperd).

The manager talks to the helper through a transport callable: in production
that is a sudo invocation of the root-owned helper entry point; in tests it
can be an in-process fake. Requests and responses are the JSON objects of
the helper protocol; this module adds typing and error handling on top.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity

Transport = Callable[[dict[str, Any]], dict[str, Any]]


class HelperError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class HelperClient:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        response = self._transport(request)
        if not response.get("ok"):
            raise HelperError(
                str(response.get("error", "unknown")),
                str(response.get("message", "helper request failed")),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise HelperError("internal", "helper returned no result object")
        return result

    def create_user(self, bot: str) -> str:
        result = self._call({"op": "create_user", "bot": bot})
        return str(result["user"])

    def delete_user(self, bot: str) -> str:
        result = self._call({"op": "delete_user", "bot": bot})
        return str(result["user"])

    def delete_bot_files(self, bot: str) -> None:
        self._call({"op": "delete_bot_files", "bot": bot})

    def store_credential(self, bot: str, text: str) -> None:
        self._call({"op": "store_credential", "bot": bot, "text": text})

    def start_bot(self, bot: str, config: LaunchConfig) -> ProcessIdentity:
        launch: dict[str, Any] = {
            "command": list(config.command),
            "cwd": config.cwd,
            "env": dict(config.env),
        }
        rlimits: dict[str, int] = {}
        if config.rlimit_nofile is not None:
            rlimits["nofile"] = config.rlimit_nofile
        if config.rlimit_nproc is not None:
            rlimits["nproc"] = config.rlimit_nproc
        if rlimits:
            launch["rlimits"] = rlimits
        if config.via_unit:
            launch["via_unit"] = True
        elif config.use_credential:
            # In unit mode the credential is delivered by PID 1, not by
            # the helper, so the flag must not trigger a helper merge.
            launch["use_credential"] = True
        if config.dotenv_mode:
            launch["dotenv_mode"] = True
        if config.secret_keys:
            launch["secret_keys"] = list(config.secret_keys)
        result = self._call({"op": "start_bot", "bot": bot, "launch": launch})
        identity = result["identity"]
        if not isinstance(identity, dict):
            raise HelperError("internal", "helper returned a malformed identity")
        return ProcessIdentity(
            pid=int(identity["pid"]),
            start_time=int(identity["start_time"]),
            boot_id=str(identity["boot_id"]),
        )

    def stop_bot(self, bot: str, identity: ProcessIdentity, grace: float) -> int:
        result = self._call(
            {
                "op": "stop_bot",
                "bot": bot,
                "identity": {
                    "pid": identity.pid,
                    "start_time": identity.start_time,
                    "boot_id": identity.boot_id,
                },
                "grace_seconds": grace,
            }
        )
        return int(result["exit_code"])

    def set_capture(self, bot: str, enabled: bool) -> None:
        self._call({"op": "set_capture", "bot": bot, "enabled": enabled})

    def capture_state(self, bot: str) -> bool:
        result = self._call({"op": "capture_state", "bot": bot})
        return bool(result["enabled"])

    def console_snapshot(self, bot: str) -> str:
        result = self._call({"op": "console_snapshot", "bot": bot})
        return str(result["text"])

    def console_send(self, bot: str, text: str, enter: bool = True) -> None:
        self._call(
            {"op": "console_send", "bot": bot, "text": text, "enter": enter}
        )

    def console_resize(self, bot: str, cols: int, rows: int) -> None:
        self._call(
            {"op": "console_resize", "bot": bot, "cols": cols, "rows": rows}
        )

    def console_clear_history(self, bot: str) -> None:
        self._call({"op": "console_clear_history", "bot": bot})

    def ensure_layout(self, bot: str) -> None:
        self._call({"op": "ensure_layout", "bot": bot})

    def publish_release(self, bot: str, release_id: str) -> None:
        self._call(
            {"op": "publish_release", "bot": bot, "release_id": release_id}
        )

    def point_current(self, bot: str, release_id: str) -> None:
        self._call(
            {"op": "point_current", "bot": bot, "release_id": release_id}
        )

    def edit_begin(self, bot: str) -> str:
        result = self._call({"op": "edit_begin", "bot": bot})
        return str(result["base_release_id"])

    def edit_discard(self, bot: str) -> None:
        self._call({"op": "edit_discard", "bot": bot})

    def disk_usage(self, bot: str) -> int:
        result = self._call({"op": "disk_usage", "bot": bot})
        return int(result["bytes"])

    def file_list(self, bot: str, zone: str, rel: str) -> list[dict[str, Any]]:
        result = self._call(
            {"op": "file_list", "bot": bot, "zone": zone, "path": rel}
        )
        entries = result["entries"]
        return entries if isinstance(entries, list) else []

    def file_read(self, bot: str, zone: str, rel: str) -> dict[str, Any]:
        return self._call(
            {"op": "file_read", "bot": bot, "zone": zone, "path": rel}
        )

    def file_write(self, bot: str, zone: str, rel: str, content: str) -> None:
        self._call(
            {
                "op": "file_write",
                "bot": bot,
                "zone": zone,
                "path": rel,
                "content": content,
            }
        )

    def file_mkdir(self, bot: str, zone: str, rel: str) -> None:
        self._call(
            {"op": "file_mkdir", "bot": bot, "zone": zone, "path": rel}
        )

    def file_delete(self, bot: str, zone: str, rel: str) -> None:
        self._call(
            {"op": "file_delete", "bot": bot, "zone": zone, "path": rel}
        )

    def file_rename(self, bot: str, zone: str, src: str, dst: str) -> None:
        self._call(
            {
                "op": "file_rename",
                "bot": bot,
                "zone": zone,
                "path": src,
                "to": dst,
            }
        )

    def bot_status(
        self, bot: str, identity: ProcessIdentity
    ) -> tuple[bool, int | None]:
        result = self._call(
            {
                "op": "bot_status",
                "bot": bot,
                "identity": {
                    "pid": identity.pid,
                    "start_time": identity.start_time,
                    "boot_id": identity.boot_id,
                },
            }
        )
        exit_code = result["exit_code"]
        return bool(result["alive"]), (
            int(exit_code) if isinstance(exit_code, int) else None
        )


def subprocess_transport(
    command: list[str], root: Path, package_dir: Path | None = None
) -> Transport:
    """Run the helper as a subprocess per request (dev / direct-root use)."""

    def transport(request: dict[str, Any]) -> dict[str, Any]:
        env = {**os.environ, "BOTENCLAVE_ROOT": str(root)}
        if package_dir is not None:
            env["BOTENCLAVE_PACKAGE_DIR"] = str(package_dir)
        proc = subprocess.run(
            command,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=130,
            env=env,
            check=False,
        )
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise HelperError(
                "internal", f"helper produced no JSON (rc={proc.returncode})"
            ) from exc
        if not isinstance(parsed, dict):
            raise HelperError("internal", "helper response is not an object")
        return parsed

    return transport


def direct_transport(root: Path, package_dir: Path | None = None) -> Transport:
    """Transport that runs botmgr.helperd with the current interpreter."""
    return subprocess_transport(
        [sys.executable, "-m", "botmgr.helperd"], root, package_dir
    )


def sudo_transport(
    helper_entry: str, root: Path, package_dir: Path | None = None
) -> Transport:
    """Production transport: the root-owned helper entry point via sudo -n."""
    return subprocess_transport(["sudo", "-n", helper_entry], root, package_dir)
