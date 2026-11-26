"""Watchdog: the periodic supervision loop (PLAN sections 1.5 and 1.10).

One step does, for every bot:
- drive the supervisor (exit polling, due restarts);
- re-attach log capture that fell off, unless private mode holds it off;
- rotate the console log when it outgrows its budget;
- sample resident memory and report bots over their RSS budget
  (reporting only; acting on it is a policy decision surfaced as an event).

The step returns human-readable event strings; the caller decides how to
log or surface them. Nothing here blocks for long: every operation is a
bounded helper call or a local file operation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from botmgr import logfiles
from botmgr.console import CaptureController
from botmgr.helper import HelperClient
from botmgr.state import BotState
from botmgr.supervisor import Supervisor

DEFAULT_RSS_LIMIT_BYTES = 512 * 1024 * 1024
DEFAULT_DISK_EVERY_STEPS = 120  # at a 5s cadence: about every 10 minutes


def read_rss_bytes(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                return int(parts[1]) * 1024
    return None


class Watchdog:
    def __init__(
        self,
        supervisor: Supervisor,
        client: HelperClient,
        capture: CaptureController,
        root: Path,
        rss_limit_bytes: int = DEFAULT_RSS_LIMIT_BYTES,
        usage_sink: Callable[[str, int], None] | None = None,
        disk_every_steps: int = DEFAULT_DISK_EVERY_STEPS,
    ) -> None:
        self._supervisor = supervisor
        self._client = client
        self._capture = capture
        self._root = root
        self._rss_limit = rss_limit_bytes
        self._usage_sink = usage_sink
        self._disk_every = max(1, disk_every_steps)
        self._steps = 0

    def step(self) -> list[str]:
        events: list[str] = []
        self._steps += 1
        try:
            self._supervisor.tick()
        except Exception:  # noqa: BLE001 - the watchdog must survive a supervision failure
            events.append("supervisor tick failed")
        for bot in self._supervisor.bot_ids():
            try:
                state = self._supervisor.state(bot)
                if state is BotState.RUNNING:
                    events.extend(self._check_running(bot))
                log_path = self._root / "bots" / bot / "logs" / "console.log"
                if logfiles.rotate_copytruncate(log_path):
                    events.append(f"{bot}: console log rotated")
            except Exception:  # noqa: BLE001 - one bot's failure must not stop the others
                events.append(f"{bot}: watchdog check failed")
            if self._usage_sink is not None and self._steps % self._disk_every == 0:
                # Low-frequency disk measurement for the guest quota
                # (PLAN 1.5 / 3.2); the helper walks the tree as root
                # because data/ is 0700.
                try:
                    self._usage_sink(bot, self._client.disk_usage(bot))
                except Exception:  # noqa: BLE001, S110 - measurement must not kill the loop
                    pass
        return events

    def _check_running(self, bot: str) -> list[str]:
        events: list[str] = []
        if not self._capture.private_active(bot):
            was_on = self._client.capture_state(bot)
            self._capture.ensure_capture(bot)
            if not was_on:
                events.append(f"{bot}: log capture re-attached")
        identity = self._supervisor.identity(bot)
        if identity is not None:
            rss = read_rss_bytes(identity.pid)
            if rss is not None and rss > self._rss_limit:
                events.append(
                    f"{bot}: RSS {rss} exceeds budget {self._rss_limit}"
                )
        return events
