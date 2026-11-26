"""Supervisor: owns one state machine per bot and drives the backend.

All public methods take a per-bot operation lock; a second operation while
one is in flight raises BusyError instead of racing (the web layer maps that
to 409). Time comes from an injectable clock so tests can drive backoff and
exit polling deterministically.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from botmgr.backend import Backend
from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity
from botmgr.state import BotState, BotStateMachine

STOP_GRACE_SECONDS = 10.0


class BusyError(Exception):
    """Another operation for the same bot is already in progress."""


class UnknownBotError(KeyError):
    """The bot id has not been registered with this supervisor."""


class _BotEntry:
    def __init__(self, config: LaunchConfig) -> None:
        self.config = config
        self.machine = BotStateMachine()
        self.identity: ProcessIdentity | None = None
        self.lock = threading.Lock()


class Supervisor:
    def __init__(
        self,
        backend: Backend,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._now = now_fn
        self._bots: dict[str, _BotEntry] = {}
        self.last_errors: dict[str, Exception] = {}

    def register(self, bot_id: str, config: LaunchConfig) -> None:
        if bot_id in self._bots:
            raise ValueError(f"bot already registered: {bot_id}")
        self._bots[bot_id] = _BotEntry(config)

    def _entry(self, bot_id: str) -> _BotEntry:
        try:
            return self._bots[bot_id]
        except KeyError:
            raise UnknownBotError(bot_id) from None

    def state(self, bot_id: str) -> BotState:
        return self._entry(bot_id).machine.state

    def identity(self, bot_id: str) -> ProcessIdentity | None:
        return self._entry(bot_id).identity

    def _locked(self, entry: _BotEntry) -> None:
        if not entry.lock.acquire(blocking=False):
            raise BusyError("operation already in progress for this bot")

    def start(self, bot_id: str) -> None:
        entry = self._entry(bot_id)
        self._locked(entry)
        try:
            entry.machine.start(self._now())
            self._spawn(bot_id, entry)
        finally:
            entry.lock.release()

    def _spawn(self, bot_id: str, entry: _BotEntry) -> None:
        try:
            entry.identity = self._backend.start(bot_id, entry.config)
        except Exception:
            entry.identity = None
            entry.machine.mark_exited(self._now(), exit_code=-1)
            raise
        entry.machine.mark_running(self._now())

    def stop(self, bot_id: str) -> None:
        entry = self._entry(bot_id)
        self._locked(entry)
        try:
            entry.machine.request_stop(self._now())
            if entry.machine.state is BotState.STOPPED:
                # Was in BACKOFF: nothing is running, nothing to signal.
                entry.identity = None
                return
            identity = entry.identity
            if identity is not None:
                exit_code = self._backend.stop(identity, STOP_GRACE_SECONDS)
                entry.identity = None
                entry.machine.mark_exited(self._now(), exit_code)
        finally:
            entry.lock.release()

    def tick(self) -> None:
        """Poll for exits and fire due restarts, for every registered bot."""
        now = self._now()
        for bot_id, entry in self._bots.items():
            if not entry.lock.acquire(blocking=False):
                continue
            try:
                self._tick_bot(bot_id, entry, now)
            finally:
                entry.lock.release()

    def _tick_bot(self, bot_id: str, entry: _BotEntry, now: float) -> None:
        machine = entry.machine
        if (
            machine.state in (BotState.STARTING, BotState.RUNNING)
            and entry.identity is not None
        ):
            exit_code = self._backend.poll_exit(entry.identity)
            if exit_code is not None:
                entry.identity = None
                machine.mark_exited(now, exit_code)
                return
        if machine.tick(now):
            try:
                self._spawn(bot_id, entry)
            except Exception as exc:  # noqa: BLE001 - the loop must survive any backend failure
                # The failed attempt is already recorded by the state
                # machine (backoff or attention); keep the error visible
                # here instead of letting one bot's spawn failure stop the
                # polling loop for the others.
                self.last_errors[bot_id] = exc
            else:
                self.last_errors.pop(bot_id, None)
