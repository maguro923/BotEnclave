import pytest

from botmgr.state import BotState, BotStateMachine, InvalidTransition


def test_initial_state_is_stopped() -> None:
    m = BotStateMachine()
    assert m.state is BotState.STOPPED


def test_start_moves_to_starting_then_running() -> None:
    m = BotStateMachine()
    m.start(now=100.0)
    assert m.state is BotState.STARTING
    m.mark_running(now=100.5)
    assert m.state is BotState.RUNNING


def test_start_from_running_is_rejected() -> None:
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.1)
    with pytest.raises(InvalidTransition):
        m.start(now=1.0)


def test_manual_stop_cycle() -> None:
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.1)
    m.request_stop(now=10.0)
    assert m.state is BotState.STOPPING
    m.mark_stopped(now=10.5)
    assert m.state is BotState.STOPPED


def test_first_crash_goes_to_backoff_with_first_delay() -> None:
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.1)
    m.mark_exited(now=30.0, exit_code=1)
    assert m.state is BotState.BACKOFF
    assert m.retry_at == 35.0


def test_default_policy_retries_once_then_attention() -> None:
    m = BotStateMachine()
    assert m.restart_policy == "once"
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.mark_exited(now=30.0, exit_code=1)
    assert m.state is BotState.BACKOFF
    assert m.retry_at == 35.0
    m.start(now=35.0)
    m.mark_running(now=35.0)
    m.mark_exited(now=65.0, exit_code=1)
    assert m.state is BotState.ATTENTION
    assert m.retry_at is None


def test_policy_none_never_restarts() -> None:
    m = BotStateMachine(restart_policy="none")
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.mark_exited(now=30.0, exit_code=1)
    assert m.state is BotState.ATTENTION
    assert m.retry_at is None


def test_policy_three_uses_the_5_15_60_schedule() -> None:
    m = BotStateMachine(restart_policy="three")
    now = 0.0
    for expected_delay in (5.0, 15.0, 60.0):
        m.start(now=now)
        m.mark_running(now=now)
        m.mark_exited(now=now + 30.0, exit_code=1)
        assert m.state is BotState.BACKOFF
        assert m.retry_at == now + 30.0 + expected_delay
        now = m.retry_at
    m.start(now=now)
    m.mark_running(now=now)
    m.mark_exited(now=now + 30.0, exit_code=1)
    assert m.state is BotState.ATTENTION


def test_backoff_expiry_allows_restart_via_tick() -> None:
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.mark_exited(now=30.0, exit_code=1)
    assert m.tick(now=34.9) is False
    assert m.state is BotState.BACKOFF
    assert m.tick(now=35.0) is True
    assert m.state is BotState.STARTING


@pytest.mark.parametrize("policy", ["none", "once", "three"])
def test_startup_failure_is_never_retried(policy: str) -> None:
    # A run that dies within the startup phase (10 seconds) parks in
    # ATTENTION whatever the policy: startup errors are deterministic
    # and retrying them would only churn the log.
    m = BotStateMachine(restart_policy=policy)
    m.start(now=100.0)
    m.mark_running(now=100.1)
    m.mark_exited(now=105.0, exit_code=1)
    assert m.state is BotState.ATTENTION
    assert m.retry_at is None


def test_exit_at_the_startup_boundary_is_a_normal_failure() -> None:
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.mark_exited(now=10.0, exit_code=1)
    assert m.state is BotState.BACKOFF


def test_startup_failure_on_a_retry_stops_immediately() -> None:
    m = BotStateMachine(restart_policy="three")
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.mark_exited(now=30.0, exit_code=1)
    assert m.state is BotState.BACKOFF
    m.start(now=35.0)
    m.mark_running(now=35.0)
    m.mark_exited(now=37.0, exit_code=1)
    assert m.state is BotState.ATTENTION


def test_run_surviving_sixty_seconds_counts_as_success() -> None:
    m = BotStateMachine(restart_policy="three")
    now = 0.0
    for expected_delay in (5.0, 15.0):
        m.start(now=now)
        m.mark_running(now=now)
        m.mark_exited(now=now + 30.0, exit_code=1)
        assert m.retry_at == now + 30.0 + expected_delay
        now = m.retry_at
    # A run past the 60-second success threshold resets the budget, so
    # the next failure starts the schedule from the beginning.
    m.start(now=now)
    m.mark_running(now=now)
    m.mark_exited(now=now + 61.0, exit_code=1)
    assert m.state is BotState.BACKOFF
    assert m.retry_at == now + 61.0 + 5.0


def test_set_restart_policy_validates() -> None:
    m = BotStateMachine()
    m.set_restart_policy("three")
    assert m.restart_policy == "three"
    with pytest.raises(ValueError):
        m.set_restart_policy("forever")


def test_exit_while_stopping_is_not_a_crash() -> None:
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.request_stop(now=5.0)
    m.mark_exited(now=6.0, exit_code=-15)
    assert m.state is BotState.STOPPED


def test_clean_exit_is_treated_as_crash_for_supervision() -> None:
    # A bot that exits on its own, even with code 0, did not ask to stop;
    # supervision treats it as an unexpected exit.
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.mark_exited(now=30.0, exit_code=0)
    assert m.state is BotState.BACKOFF


def test_attention_can_be_started_manually() -> None:
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.mark_exited(now=30.0, exit_code=1)
    m.start(now=35.0)
    m.mark_running(now=35.0)
    m.mark_exited(now=65.0, exit_code=1)
    assert m.state is BotState.ATTENTION
    # A manual start is an operator decision: the retry budget resets.
    m.start(now=1000.0)
    assert m.state is BotState.STARTING
    m.mark_running(now=1000.0)
    m.mark_exited(now=1030.0, exit_code=1)
    assert m.state is BotState.BACKOFF


def test_request_stop_from_stopping_is_a_noop() -> None:
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.request_stop(now=1.0)
    assert m.state is BotState.STOPPING
    m.request_stop(now=2.0)  # retrying a failed stop must not raise
    assert m.state is BotState.STOPPING
