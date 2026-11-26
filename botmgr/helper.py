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


def subprocess_transport(command: list[str], root: Path) -> Transport:
    """Run the helper as a subprocess per request (dev / direct-root use)."""

    def transport(request: dict[str, Any]) -> dict[str, Any]:
        env = {**os.environ, "BOTENCLAVE_ROOT": str(root)}
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


def direct_transport(root: Path) -> Transport:
    """Transport that runs botmgr.helperd with the current interpreter."""
    return subprocess_transport([sys.executable, "-m", "botmgr.helperd"], root)


def sudo_transport(helper_entry: str, root: Path) -> Transport:
    """Production transport: the root-owned helper entry point via sudo -n."""
    return subprocess_transport(["sudo", "-n", helper_entry], root)
