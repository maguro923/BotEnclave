"""Execution backend boundary.

The supervisor never touches tmux, sudo, or systemd directly; it goes
through this protocol (PLAN section 5.1, M1a). The standard implementation
drives the privileged helper and per-bot tmux servers; the single-user
implementation (M6) will be a second implementation of the same protocol.
"""

from __future__ import annotations

from typing import Protocol

from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity


class Backend(Protocol):
    def start(self, bot_id: str, config: LaunchConfig) -> ProcessIdentity:
        """Start the bot and return the identity of its main process."""
        ...

    def stop(self, identity: ProcessIdentity, grace_seconds: float) -> int:
        """Terminate the process group (SIGTERM, grace, SIGKILL, sweep) and
        return the main process's exit code."""
        ...

    def poll_exit(self, identity: ProcessIdentity) -> int | None:
        """Return the exit code if the process has ended, else None."""
        ...
