"""Per-bot supervision state machine.

Pure logic with an injectable clock: every method takes ``now`` so tests can
drive time without sleeping. The supervisor (botmgr.supervisor) owns the side
effects; this module only decides states.

Rules (PLAN section 2.3):
- A run that survives 60 seconds counts as a successful run.
- Unexpected exits restart with exponential backoff: 5s, 15s, 60s, 300s cap.
- Auto-restart stops (ATTENTION) after 5 crashes within 10 minutes, or after
  8 consecutive failures with no successful run in between.
- A run that lasts 10 minutes clears the crash window as well.
"""

from __future__ import annotations

import enum


class BotState(enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"
    BACKOFF = "backoff"
    DEGRADED = "degraded"
    ATTENTION = "attention"
    SECRET_PENDING = "secret-pending"


class InvalidTransition(Exception):
    """Raised when a transition is requested from a state that forbids it."""


SUCCESS_THRESHOLD_SECONDS = 60.0
LONG_RUN_RESET_SECONDS = 600.0
CRASH_WINDOW_SECONDS = 600.0
CRASH_WINDOW_LIMIT = 5
FAILURES_WITHOUT_SUCCESS_LIMIT = 8
BACKOFF_SCHEDULE = (5.0, 15.0, 60.0, 300.0)

_STARTABLE = frozenset(
    {
        BotState.STOPPED,
        BotState.CRASHED,
        BotState.BACKOFF,
        BotState.ATTENTION,
        BotState.SECRET_PENDING,
    }
)


class BotStateMachine:
    def __init__(self) -> None:
        self._state = BotState.STOPPED
        self._started_at: float | None = None
        self._retry_at: float | None = None
        self._crash_times: list[float] = []
        self._failures_without_success = 0
        self.last_exit_code: int | None = None

    @property
    def state(self) -> BotState:
        return self._state

    @property
    def retry_at(self) -> float | None:
        return self._retry_at

    def start(self, now: float) -> None:
        if self._state not in _STARTABLE:
            raise InvalidTransition(f"cannot start from {self._state.value}")
        if self._state is BotState.ATTENTION:
            # A manual start after ATTENTION is an operator decision; give
            # the bot a clean slate.
            self._crash_times.clear()
            self._failures_without_success = 0
        self._state = BotState.STARTING
        self._started_at = now
        self._retry_at = None

    def mark_secret_pending(self, now: float) -> None:
        """The bot cannot start because its secrets are not available
        (memory-only backend after a manager restart, PLAN section 2.3).
        Not a failure: no crash accounting, and a later start is allowed
        once the owner re-enters the values."""
        if self._state not in _STARTABLE and self._state is not BotState.STARTING:
            raise InvalidTransition(
                f"cannot mark secret-pending from {self._state.value}"
            )
        self._state = BotState.SECRET_PENDING
        self._retry_at = None
        self._started_at = None

    def mark_running(self, now: float) -> None:
        if self._state is not BotState.STARTING:
            raise InvalidTransition(f"cannot mark running from {self._state.value}")
        self._state = BotState.RUNNING

    def request_stop(self, now: float) -> None:
        if self._state in (BotState.STARTING, BotState.RUNNING):
            self._state = BotState.STOPPING
        elif self._state is BotState.STOPPING:
            pass  # already stopping: a failed stop stays retryable
        elif self._state is BotState.BACKOFF:
            self._retry_at = None
            self._state = BotState.STOPPED
        else:
            raise InvalidTransition(f"cannot stop from {self._state.value}")

    def mark_stopped(self, now: float) -> None:
        if self._state is not BotState.STOPPING:
            raise InvalidTransition(f"cannot mark stopped from {self._state.value}")
        self._state = BotState.STOPPED
        self._started_at = None

    def mark_exited(self, now: float, exit_code: int) -> None:
        if self._state is BotState.STOPPING:
            self._state = BotState.STOPPED
            self._started_at = None
            self.last_exit_code = exit_code
            return
        if self._state not in (BotState.STARTING, BotState.RUNNING):
            raise InvalidTransition(f"cannot mark exited from {self._state.value}")
        self.last_exit_code = exit_code
        runtime = now - (self._started_at if self._started_at is not None else now)
        self._started_at = None
        if runtime >= LONG_RUN_RESET_SECONDS:
            self._crash_times.clear()
            self._failures_without_success = 0
        elif runtime >= SUCCESS_THRESHOLD_SECONDS:
            self._failures_without_success = 0
        self._crash_times = [t for t in self._crash_times if now - t < CRASH_WINDOW_SECONDS]
        self._crash_times.append(now)
        self._failures_without_success += 1
        if (
            len(self._crash_times) >= CRASH_WINDOW_LIMIT
            or self._failures_without_success >= FAILURES_WITHOUT_SUCCESS_LIMIT
        ):
            self._state = BotState.ATTENTION
            self._retry_at = None
            return
        self._state = BotState.BACKOFF
        index = min(self._failures_without_success - 1, len(BACKOFF_SCHEDULE) - 1)
        self._retry_at = now + BACKOFF_SCHEDULE[index]

    def restore_running(self, now: float) -> None:
        """Adopt a bot that was found still running after a manager restart
        (reconcile). Supervision accounting restarts from the restore time:
        the run's original start time is not recoverable, and pretending
        otherwise would corrupt the crash statistics."""
        if self._state is not BotState.STOPPED:
            raise InvalidTransition(f"cannot restore from {self._state.value}")
        self._state = BotState.RUNNING
        self._started_at = now

    def tick(self, now: float) -> bool:
        """Advance time-based transitions. Returns True if a restart is due."""
        if self._state is BotState.BACKOFF and self._retry_at is not None and now >= self._retry_at:
            self._state = BotState.STARTING
            self._started_at = now
            self._retry_at = None
            return True
        return False
