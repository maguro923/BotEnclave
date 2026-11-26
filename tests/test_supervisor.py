import pytest

from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity
from botmgr.state import BotState, InvalidTransition
from botmgr.supervisor import Supervisor, UnknownBotError

CONFIG = LaunchConfig(command=("python", "bot.py"), cwd="/tmp", env={})


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeBackend:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[ProcessIdentity] = []
        self.exits: dict[int, int] = {}
        self.fail_starts = False
        self.poll_fail_pids: set[int] = set()
        self.fail_stop_times = 0
        self._next_pid = 1000

    def start(self, bot_id: str, config: LaunchConfig) -> ProcessIdentity:
        if self.fail_starts:
            raise RuntimeError("spawn failed")
        self.started.append(bot_id)
        self._next_pid += 1
        return ProcessIdentity(pid=self._next_pid, start_time=1, boot_id="test")

    def stop(self, identity: ProcessIdentity, grace_seconds: float) -> int:
        if self.fail_stop_times > 0:
            self.fail_stop_times -= 1
            raise RuntimeError("stop failed")
        self.stopped.append(identity)
        return -15

    def poll_exit(self, identity: ProcessIdentity) -> int | None:
        if identity.pid in self.poll_fail_pids:
            raise RuntimeError("poll failed")
        return self.exits.get(identity.pid)


def make_supervisor() -> tuple[Supervisor, FakeBackend, FakeClock]:
    backend = FakeBackend()
    clock = FakeClock()
    sup = Supervisor(backend, now_fn=clock)
    sup.register("alpha", CONFIG)
    return sup, backend, clock


def test_start_runs_bot() -> None:
    sup, backend, _clock = make_supervisor()
    sup.start("alpha")
    assert sup.state("alpha") is BotState.RUNNING
    assert backend.started == ["alpha"]
    assert sup.identity("alpha") is not None


def test_double_start_rejected() -> None:
    sup, _backend, _clock = make_supervisor()
    sup.start("alpha")
    with pytest.raises(InvalidTransition):
        sup.start("alpha")


def test_unknown_bot() -> None:
    sup, _backend, _clock = make_supervisor()
    with pytest.raises(UnknownBotError):
        sup.start("ghost")


def test_stop_terminates_and_clears_identity() -> None:
    sup, backend, _clock = make_supervisor()
    sup.start("alpha")
    sup.stop("alpha")
    assert sup.state("alpha") is BotState.STOPPED
    assert len(backend.stopped) == 1
    assert sup.identity("alpha") is None


def test_unexpected_exit_goes_to_backoff_then_restarts_on_tick() -> None:
    sup, backend, clock = make_supervisor()
    sup.start("alpha")
    ident = sup.identity("alpha")
    assert ident is not None
    backend.exits[ident.pid] = 1
    clock.now = 5.0
    sup.tick()
    assert sup.state("alpha") is BotState.BACKOFF
    clock.now = 9.9
    sup.tick()
    assert sup.state("alpha") is BotState.BACKOFF
    clock.now = 10.0
    sup.tick()
    assert sup.state("alpha") is BotState.RUNNING
    assert backend.started == ["alpha", "alpha"]


def test_stop_during_backoff_does_not_signal_anything() -> None:
    sup, backend, clock = make_supervisor()
    sup.start("alpha")
    ident = sup.identity("alpha")
    assert ident is not None
    backend.exits[ident.pid] = 1
    clock.now = 1.0
    sup.tick()
    assert sup.state("alpha") is BotState.BACKOFF
    sup.stop("alpha")
    assert sup.state("alpha") is BotState.STOPPED
    assert backend.stopped == []


def test_spawn_failure_counts_as_crash() -> None:
    sup, backend, _clock = make_supervisor()
    backend.fail_starts = True
    with pytest.raises(RuntimeError):
        sup.start("alpha")
    assert sup.state("alpha") is BotState.BACKOFF
    assert sup.identity("alpha") is None


def test_repeated_spawn_failures_reach_attention_without_stalling_tick() -> None:
    sup, backend, clock = make_supervisor()
    backend.fail_starts = True
    with pytest.raises(RuntimeError):
        sup.start("alpha")
    # tick must keep processing without raising, and the machine must land
    # in ATTENTION once the failures-without-success limit is reached.
    for _ in range(10):
        clock.now += 301.0
        sup.tick()
    assert sup.state("alpha") is BotState.ATTENTION
    assert isinstance(sup.last_errors.get("alpha"), RuntimeError)


def test_tick_survives_poll_failure_and_still_polls_others() -> None:
    sup, backend, _clock = make_supervisor()
    sup.register("beta", CONFIG)
    sup.start("alpha")
    sup.start("beta")
    alpha_identity = sup.identity("alpha")
    beta_identity = sup.identity("beta")
    assert alpha_identity is not None and beta_identity is not None
    backend.poll_fail_pids.add(alpha_identity.pid)
    backend.exits[beta_identity.pid] = 3
    sup.tick()  # must not raise
    assert isinstance(sup.last_errors.get("alpha"), RuntimeError)
    assert sup.state("alpha") is BotState.RUNNING  # unknown, not crashed
    assert sup.state("beta") is BotState.BACKOFF  # exit was still observed


def test_failed_stop_leaves_bot_retryable() -> None:
    sup, backend, _clock = make_supervisor()
    sup.start("alpha")
    backend.fail_stop_times = 1
    with pytest.raises(RuntimeError):
        sup.stop("alpha")
    assert sup.state("alpha") is BotState.STOPPING
    assert sup.identity("alpha") is not None
    sup.stop("alpha")  # retry succeeds against the same identity
    assert sup.state("alpha") is BotState.STOPPED
    assert sup.identity("alpha") is None
