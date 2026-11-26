"""Reconcile: rebuild the supervisor's view of the world after a manager
restart, from the durable store plus live checks through the helper.

Bots are independent processes under tmux; a manager crash must neither
kill them nor lose track of them. For every stored bot (PLAN section 1.10):
- recorded running + still alive  -> adopt it (RUNNING, identity kept);
- recorded running + gone         -> count a crash (normal backoff rules);
- capture missing on a live bot   -> re-attach it, unless private mode is
  somehow active (it cannot be right after a restart, but the controller
  is the authority and is asked, not bypassed).
"""

from __future__ import annotations

import dataclasses

from botmgr.console import CaptureController
from botmgr.helper import HelperClient
from botmgr.state import BotState
from botmgr.store import StateStore
from botmgr.supervisor import Supervisor

_RUNNING_STATES = (BotState.RUNNING, BotState.STARTING, BotState.BACKOFF)


@dataclasses.dataclass
class ReconcileReport:
    adopted_running: list[str] = dataclasses.field(default_factory=list)
    found_dead: list[str] = dataclasses.field(default_factory=list)
    capture_reattached: list[str] = dataclasses.field(default_factory=list)


def reconcile(
    supervisor: Supervisor,
    store: StateStore,
    client: HelperClient,
    capture: CaptureController,
) -> ReconcileReport:
    report = ReconcileReport()
    for stored in store.load_all():
        bot = stored.bot_id
        if stored.state in _RUNNING_STATES and stored.identity is not None:
            alive, _exit = client.bot_status(bot, stored.identity)
            if alive:
                supervisor.restore_running(bot, stored.config, stored.identity)
                report.adopted_running.append(bot)
                was_on = client.capture_state(bot)
                capture.ensure_capture(bot)
                if not was_on:
                    report.capture_reattached.append(bot)
                continue
            supervisor.restore_exited(bot, stored.config)
            report.found_dead.append(bot)
            continue
        supervisor.register(bot, stored.config)
    return report
