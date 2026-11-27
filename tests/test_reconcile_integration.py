"""Integration tests for reconcile across manager processes (the
manager-kill and tmux-kill fixtures of PLAN section 4.1).

Each CLI invocation is a separate process, so a bot that survives between
invocations proves the manager is disposable; killing the tmux server
proves the loss is detected and counted as a crash.
"""

import os
import subprocess
import sys
import time

import pytest

from botmgr import procident
from botmgr.helperd import Helper
from botmgr.state import BotState
from botmgr.store import StateStore

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="reconcile integration tests require root"
)

BOT = "ctest1"


def run_cli(helper: Helper, *args: str) -> subprocess.CompletedProcess[str]:
    package_dir = helper.package_dir  # type: ignore[attr-defined]
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "botmgr.cli",
            "--root",
            str(helper.root),
            "--package-dir",
            str(package_dir),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def stored_bot(helper: Helper):  # type: ignore[no-untyped-def]
    store = StateStore(helper.root / "botmgr.db")
    try:
        bots = store.load_all()
    finally:
        store.close()
    assert len(bots) == 1
    return bots[0]


def start_sleeper(helper: Helper) -> None:
    fixtures = helper.fixtures_dir  # type: ignore[attr-defined]
    run_cli(helper, "user-create", BOT)
    run_cli(
        helper,
        "bot-add",
        BOT,
        "--",
        "/usr/bin/python3",
        str(fixtures / "sleeper.py"),
    )
    out = run_cli(helper, "bot-start", BOT)
    assert "running" in out.stdout


def test_manager_restart_adopts_running_bot(helper: Helper) -> None:
    start_sleeper(helper)
    bot = stored_bot(helper)
    assert bot.state is BotState.RUNNING
    assert bot.identity is not None
    assert procident.verify(bot.identity)

    # A brand-new manager process adopts the still-running bot.
    out = run_cli(helper, "bot-status", BOT)
    assert "still running, adopted" in out.stdout
    assert f"{BOT}: running" in out.stdout
    assert procident.verify(bot.identity)

    out = run_cli(helper, "bot-stop", BOT)
    assert f"{BOT}: stopped" in out.stdout
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and procident.verify(bot.identity):
        time.sleep(0.1)
    assert not procident.verify(bot.identity)


def test_tmux_kill_is_detected_as_crash(helper: Helper) -> None:
    start_sleeper(helper)
    bot = stored_bot(helper)
    assert bot.identity is not None
    subprocess.run(
        ["su", "-s", "/bin/sh", f"bm-{BOT}", "-c", f"tmux -L be-{BOT} kill-server"],
        check=False,
        capture_output=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and procident.verify(bot.identity):
        time.sleep(0.1)
    assert not procident.verify(bot.identity)

    out = run_cli(helper, "bot-status", BOT)
    assert "had exited" in out.stdout
    # The sleeper was killed seconds after starting, i.e. inside its
    # startup phase, so the exit parks it in ATTENTION (no retry).
    assert f"{BOT}: attention" in out.stdout
    assert stored_bot(helper).state is BotState.ATTENTION
