from pathlib import Path
from typing import Any

from botmgr.backend import Backend
from botmgr.console import CaptureController
from botmgr.helper import HelperClient
from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity
from botmgr.reconcile import reconcile
from botmgr.state import BotState
from botmgr.store import StateStore
from botmgr.supervisor import Supervisor

CONFIG = LaunchConfig(
    command=("python3", "bot.py"),
    cwd="/srv/botenclave/bots/alpha/data",
    env={"A": "1"},
    rlimit_nofile=2048,
)
IDENT = ProcessIdentity(pid=777, start_time=42, boot_id="boot-9")


def test_store_round_trip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "botmgr.db")
    store.register_bot("alpha", CONFIG)
    store.save_state("alpha", BotState.RUNNING, IDENT)
    loaded = store.load_all()
    store.close()
    assert len(loaded) == 1
    bot = loaded[0]
    assert bot.bot_id == "alpha"
    assert bot.config == CONFIG
    assert bot.state is BotState.RUNNING
    assert bot.identity == IDENT


def test_store_remove(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "botmgr.db")
    store.register_bot("alpha", CONFIG)
    store.remove_bot("alpha")
    assert store.load_all() == []
    store.close()


class FakeBackend(Backend):
    def start(self, bot_id: str, config: LaunchConfig) -> ProcessIdentity:
        return IDENT

    def stop(self, identity: ProcessIdentity, grace_seconds: float) -> int:
        return -15

    def poll_exit(self, identity: ProcessIdentity) -> int | None:
        return None


class FakeTransport:
    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if request["op"] == "bot_status":
            return {
                "ok": True,
                "result": {"alive": self.alive, "exit_code": None if self.alive else 3},
            }
        if request["op"] == "capture_state":
            return {"ok": True, "result": {"enabled": False}}
        if request["op"] == "set_capture":
            return {"ok": True, "result": {"enabled": request["enabled"]}}
        raise AssertionError(request["op"])


def _setup(tmp_path: Path, alive: bool) -> tuple[Supervisor, StateStore, HelperClient]:
    store = StateStore(tmp_path / "botmgr.db")
    store.register_bot("alpha", CONFIG)
    store.save_state("alpha", BotState.RUNNING, IDENT)
    client = HelperClient(FakeTransport(alive))
    supervisor = Supervisor(FakeBackend(), store=store)
    return supervisor, store, client


def test_reconcile_adopts_live_bot(tmp_path: Path) -> None:
    supervisor, store, client = _setup(tmp_path, alive=True)
    report = reconcile(supervisor, store, client, CaptureController(client))
    assert report.adopted_running == ["alpha"]
    assert report.found_dead == []
    assert report.capture_reattached == ["alpha"]
    assert supervisor.state("alpha") is BotState.RUNNING
    assert supervisor.identity("alpha") == IDENT
    store.close()


def test_reconcile_counts_dead_bot_as_a_startup_failure(tmp_path: Path) -> None:
    # A bot found dead after manager downtime is not retried blindly:
    # its death time is unknown, so it parks in ATTENTION (autostart,
    # when enabled, is the explicit way to bring it back).
    supervisor, store, client = _setup(tmp_path, alive=False)
    report = reconcile(supervisor, store, client, CaptureController(client))
    assert report.found_dead == ["alpha"]
    assert supervisor.state("alpha") is BotState.ATTENTION
    stored = store.load_all()
    assert stored[0].state is BotState.ATTENTION
    store.close()


def test_supervisor_persists_transitions(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "botmgr.db")
    supervisor = Supervisor(FakeBackend(), store=store)
    supervisor.register("alpha", CONFIG)
    supervisor.start("alpha")
    stored = store.load_all()
    assert stored[0].state is BotState.RUNNING
    assert stored[0].identity == IDENT
    supervisor.stop("alpha")
    stored = store.load_all()
    assert stored[0].state is BotState.STOPPED
    assert stored[0].identity is None
    store.close()
