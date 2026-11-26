"""Supervisor: owns one state machine per bot and drives the backend.

All public methods take a per-bot operation lock; a second operation while
one is in flight raises BusyError instead of racing (the web layer maps that
to 409). Time comes from an injectable clock so tests can drive backoff and
exit polling deterministically.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from botmgr.backend import Backend
from botmgr.envstore import EnvResolution
from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity
from botmgr.state import BotState, BotStateMachine

if TYPE_CHECKING:
    from botmgr.store import StateStore

STOP_GRACE_SECONDS = 10.0


class BusyError(Exception):
    """Another operation for the same bot is already in progress."""


class SecretPendingError(Exception):
    """The bot's secrets are not available to this process (PLAN 2.3)."""


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
        store: StateStore | None = None,
        env_provider: Callable[[str], EnvResolution] | None = None,
    ) -> None:
        self._backend = backend
        self._now = now_fn
        self._store = store
        self._env_provider = env_provider
        self._bots: dict[str, _BotEntry] = {}
        self.last_errors: dict[str, Exception] = {}

    def _resolve(self, bot_id: str) -> EnvResolution | None:
        if self._env_provider is None:
            return None
        return self._env_provider(bot_id)

    def register(
        self, bot_id: str, config: LaunchConfig, owner_id: int | None = None
    ) -> None:
        if bot_id in self._bots:
            raise ValueError(f"bot already registered: {bot_id}")
        self._bots[bot_id] = _BotEntry(config)
        if self._store is not None:
            self._store.register_bot(bot_id, config, owner_id)

    def bot_ids(self) -> list[str]:
        return sorted(self._bots)

    def _persist(self, bot_id: str, entry: _BotEntry) -> None:
        if self._store is not None:
            self._store.save_state(bot_id, entry.machine.state, entry.identity)

    def restore_running(
        self, bot_id: str, config: LaunchConfig, identity: ProcessIdentity
    ) -> None:
        if bot_id not in self._bots:
            self.register(bot_id, config)
        entry = self._bots[bot_id]
        entry.identity = identity
        adopt = getattr(self._backend, "adopt", None)
        if adopt is not None:
            adopt(bot_id, identity)
        entry.machine.restore_running(self._now())
        self._persist(bot_id, entry)

    def restore_exited(self, bot_id: str, config: LaunchConfig) -> None:
        """Adopt a bot that died while the manager was down: count it as a
        crash now so the normal backoff/attention rules apply."""
        if bot_id not in self._bots:
            self.register(bot_id, config)
        entry = self._bots[bot_id]
        now = self._now()
        entry.machine.start(now)
        entry.machine.mark_exited(now, exit_code=-1)
        self._persist(bot_id, entry)

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
            resolution = self._resolve(bot_id)
            if resolution is not None and resolution.env is None:
                entry.machine.mark_secret_pending(self._now())
                raise SecretPendingError(bot_id)
            entry.machine.start(self._now())
            self._spawn(bot_id, entry, resolution)
        finally:
            self._persist(bot_id, entry)
            entry.lock.release()

    def set_config(self, bot_id: str, config: LaunchConfig) -> None:
        """Replace a bot's launch configuration (deploy / rollback). The
        caller stops the bot first; the stored config never carries secret
        values, which are merged only into the spawn-time copy."""
        entry = self._entry(bot_id)
        if entry.machine.state in (
            BotState.STARTING,
            BotState.RUNNING,
            BotState.STOPPING,
        ):
            raise BusyError(f"bot {bot_id} is running; stop it before reconfiguring")
        entry.config = config
        if self._store is not None:
            self._store.register_bot(bot_id, config)

    def _spawn(
        self,
        bot_id: str,
        entry: _BotEntry,
        resolution: EnvResolution | None = None,
    ) -> None:
        if resolution is None:
            resolution = self._resolve(bot_id)
        config = entry.config
        if resolution is not None:
            if resolution.env is None:
                entry.machine.mark_secret_pending(self._now())
                raise SecretPendingError(bot_id)
            config = dataclasses.replace(
                config,
                env={**config.env, **resolution.env},
                use_credential=resolution.use_credential,
                dotenv_mode=resolution.dotenv_mode,
                secret_keys=tuple(
                    sorted(set(resolution.env) - set(entry.config.env))
                ),
            )
        try:
            entry.identity = self._backend.start(bot_id, config)
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
            self._persist(bot_id, entry)
            entry.lock.release()

    def tick(self) -> None:
        """Poll for exits and fire due restarts, for every registered bot."""
        now = self._now()
        for bot_id, entry in list(self._bots.items()):
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
            try:
                exit_code = self._backend.poll_exit(entry.identity)
            except Exception as exc:  # noqa: BLE001 - one bot's helper failure must not stop polling for the others
                self.last_errors[bot_id] = exc
                return
            if exit_code is not None:
                entry.identity = None
                machine.mark_exited(now, exit_code)
                self._persist(bot_id, entry)
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
            self._persist(bot_id, entry)
