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
    m.mark_exited(now=5.0, exit_code=1)
    assert m.state is BotState.BACKOFF
    assert m.retry_at == 10.0


def test_backoff_delays_follow_schedule_and_cap() -> None:
    m = BotStateMachine()
    now = 0.0
    for expected_delay in (5.0, 15.0, 60.0, 300.0, 300.0):
        m.start(now=now)
        m.mark_running(now=now)
        crash_at = now + 1.0
        m.mark_exited(now=crash_at, exit_code=1)
        assert m.state is BotState.BACKOFF
        assert m.retry_at == crash_at + expected_delay
        # Jump past the crash window so only the schedule is exercised here;
        # the window rule has its own test below.
        now = m.retry_at + 601.0


def test_backoff_expiry_allows_restart_via_tick() -> None:
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.mark_exited(now=1.0, exit_code=1)
    assert m.tick(now=5.9) is False
    assert m.state is BotState.BACKOFF
    assert m.tick(now=6.0) is True
    assert m.state is BotState.STARTING


def test_five_crashes_within_ten_minutes_require_attention() -> None:
    m = BotStateMachine()
    now = 0.0
    for _ in range(4):
        m.start(now=now)
        m.mark_running(now=now)
        m.mark_exited(now=now + 1.0, exit_code=1)
        assert m.state is BotState.BACKOFF
        now = m.retry_at
    m.start(now=now)
    m.mark_running(now=now)
    m.mark_exited(now=now + 1.0, exit_code=1)
    assert m.state is BotState.ATTENTION
    assert m.retry_at is None


def test_eight_failures_without_success_require_attention() -> None:
    m = BotStateMachine()
    now = 0.0
    # Spread crashes more than ten minutes apart so the window rule never
    # fires; the cumulative-without-success rule must stop the loop instead.
    for i in range(8):
        m.start(now=now)
        m.mark_running(now=now)
        m.mark_exited(now=now + 1.0, exit_code=1)
        if i < 7:
            assert m.state is BotState.BACKOFF
            now = m.retry_at + 601.0
    assert m.state is BotState.ATTENTION


def test_run_surviving_sixty_seconds_counts_as_success() -> None:
    m = BotStateMachine()
    now = 0.0
    for _ in range(7):
        m.start(now=now)
        m.mark_running(now=now)
        m.mark_exited(now=now + 1.0, exit_code=1)
        now = m.retry_at + 601.0
    # A run that survives the 60-second success threshold clears the
    # cumulative failure counter, so the next crash restarts the schedule.
    m.start(now=now)
    m.mark_running(now=now)
    m.mark_exited(now=now + 61.0, exit_code=1)
    assert m.state is BotState.BACKOFF
    assert m.retry_at == now + 61.0 + 5.0


def test_ten_minute_run_resets_crash_window() -> None:
    m = BotStateMachine()
    now = 0.0
    for _ in range(4):
        m.start(now=now)
        m.mark_running(now=now)
        m.mark_exited(now=now + 1.0, exit_code=1)
        now = m.retry_at
    m.start(now=now)
    m.mark_running(now=now)
    m.mark_exited(now=now + 601.0, exit_code=1)
    assert m.state is BotState.BACKOFF
    assert m.retry_at == now + 601.0 + 5.0


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
    now = 0.0
    for _ in range(5):
        m.start(now=now)
        m.mark_running(now=now)
        m.mark_exited(now=now + 1.0, exit_code=1)
        if m.state is BotState.BACKOFF:
            now = m.retry_at
    assert m.state is BotState.ATTENTION
    m.start(now=now + 1000.0)
    assert m.state is BotState.STARTING


def test_request_stop_from_stopping_is_a_noop() -> None:
    m = BotStateMachine()
    m.start(now=0.0)
    m.mark_running(now=0.0)
    m.request_stop(now=1.0)
    assert m.state is BotState.STOPPING
    m.request_stop(now=2.0)  # retrying a failed stop must not raise
    assert m.state is BotState.STOPPING
