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

    # Backends may additionally provide adopt(bot_id, identity) -> None:
    # register an already-running process the backend did not start in
    # this process (reconcile after a manager restart) so that stop and
    # poll_exit can address it. The supervisor calls it when present.
