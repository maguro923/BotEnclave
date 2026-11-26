from pathlib import Path

from botmgr.console import CaptureController
from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity
from botmgr.state import BotState
from botmgr.supervisor import Supervisor
from botmgr.watchdog import Watchdog

CONFIG = LaunchConfig(command=("python", "bot.py"), cwd="/tmp", env={})


class FakeBackend:
    def __init__(self) -> None:
        self._next_pid = 2000

    def start(self, bot_id: str, config: LaunchConfig) -> ProcessIdentity:
        self._next_pid += 1
        return ProcessIdentity(pid=self._next_pid, start_time=1, boot_id="test")

    def stop(self, identity: ProcessIdentity, grace_seconds: float) -> int:
        return -15

    def poll_exit(self, identity: ProcessIdentity) -> int | None:
        return None


class StubClient:
    """capture_state raises for bots in fail_bots, like a wedged helper."""

    def __init__(self, fail_bots: set[str]) -> None:
        self.fail_bots = fail_bots
        self.capture: dict[str, bool] = {}

    def capture_state(self, bot: str) -> bool:
        if bot in self.fail_bots:
            raise RuntimeError("helper unreachable")
        return self.capture.get(bot, True)

    def set_capture(self, bot: str, enabled: bool) -> None:
        self.capture[bot] = enabled

    def disk_usage(self, bot: str) -> int:
        return 0


def _watchdog(tmp_path: Path, client: StubClient) -> tuple[Watchdog, Supervisor]:
    supervisor = Supervisor(FakeBackend())
    supervisor.register("alpha", CONFIG)
    supervisor.register("beta", CONFIG)
    supervisor.start("alpha")
    supervisor.start("beta")
    capture = CaptureController(client)  # type: ignore[arg-type]
    return Watchdog(supervisor, client, capture, tmp_path), supervisor  # type: ignore[arg-type]


def test_step_survives_one_bots_helper_failure(tmp_path: Path) -> None:
    client = StubClient(fail_bots={"alpha"})
    watchdog, supervisor = _watchdog(tmp_path, client)
    events = watchdog.step()  # must not raise
    assert "alpha: watchdog check failed" in events
    assert not any(e.startswith("beta:") and "failed" in e for e in events)
    assert supervisor.state("beta") is BotState.RUNNING


def test_step_survives_supervisor_tick_failure(tmp_path: Path) -> None:
    class ExplodingSupervisor:
        def tick(self) -> None:
            raise RuntimeError("tick exploded")

        def bot_ids(self) -> list[str]:
            return []

    client = StubClient(fail_bots=set())
    watchdog = Watchdog(
        ExplodingSupervisor(),  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        CaptureController(client),  # type: ignore[arg-type]
        tmp_path,
    )
    assert watchdog.step() == ["supervisor tick failed"]
