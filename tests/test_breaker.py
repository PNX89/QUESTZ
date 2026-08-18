from __future__ import annotations

import random

import pytest

from questz.breaker import (
    Breaker,
    BreakerPolicy,
    BreakerState,
    BreakerStore,
    RetryPolicy,
    full_jitter_delay,
)
from questz.types import CircuitOpenError, PermanentError, TransientError

FAST = BreakerPolicy(cooldown_seconds=60.0)
# Isolates one trip condition at a time by putting the other two out of reach.
ONLY_TOTAL = BreakerPolicy(
    consecutive_failure_threshold=99,
    total_failure_threshold=5,
    failure_rate_threshold=1.0,
    minimum_number_of_calls=100,
)
ONLY_RATE = BreakerPolicy(
    consecutive_failure_threshold=99,
    total_failure_threshold=99,
    failure_rate_threshold=0.5,
    minimum_number_of_calls=4,
)
ONLY_CONSECUTIVE = BreakerPolicy(
    consecutive_failure_threshold=3,
    total_failure_threshold=99,
    failure_rate_threshold=1.0,
    minimum_number_of_calls=100,
)


def _breaker(policy=FAST, **kwargs) -> Breaker:
    return Breaker(policy, name="items", **kwargs)


def test_two_consecutive_failures_stay_closed_and_the_third_opens(fake_clock):
    breaker = _breaker(clock=fake_clock)
    breaker.record_failure("boom")
    breaker.record_failure("boom")
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure("boom")
    assert breaker.state is BreakerState.OPEN


def test_a_success_zeroes_the_consecutive_counter(fake_clock):
    breaker = _breaker(ONLY_CONSECUTIVE, clock=fake_clock)
    breaker.record_failure("boom")
    breaker.record_failure("boom")
    breaker.record_success()
    assert breaker.consecutive_failures == 0
    breaker.record_failure("boom")
    breaker.record_failure("boom")
    assert breaker.state is BreakerState.CLOSED
    assert breaker.total_failures == 4


def test_the_total_threshold_opens_on_non_consecutive_failures(fake_clock):
    breaker = _breaker(ONLY_TOTAL, clock=fake_clock)
    for _ in range(4):
        breaker.record_failure("boom")
        breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure("boom")
    assert breaker.state is BreakerState.OPEN
    assert breaker.total_failures == 5


def test_two_failures_in_four_calls_do_not_open_and_three_do(fake_clock):
    breaker = _breaker(ONLY_RATE, clock=fake_clock)
    breaker.record_failure("boom")
    breaker.record_success()
    breaker.record_failure("boom")
    breaker.record_success()
    assert breaker.recorded_calls == 4
    assert breaker.state is BreakerState.CLOSED

    other = _breaker(ONLY_RATE, clock=fake_clock)
    other.record_failure("boom")
    other.record_success()
    other.record_failure("boom")
    other.record_failure("boom")
    assert other.recorded_calls == 4
    assert other.state is BreakerState.OPEN


def test_scattered_failures_on_a_healthy_target_never_open_the_breaker(fake_clock):
    """A job every five minutes for four days, one transient failure in a hundred, never
    two in a row. A total that only ever grows opens three times over that run for no
    reason, and the operator learns to ignore the alert."""
    breaker = _breaker(clock=fake_clock)
    for call in range(1200):
        if call % 100 == 99:
            breaker.record_failure("blip")
        else:
            breaker.record_success()
        assert breaker.state is BreakerState.CLOSED, call
    assert breaker.recorded_calls == FAST.sliding_window_size
    assert breaker.total_failures <= 1


def test_a_failure_ages_out_of_the_window(fake_clock):
    policy = BreakerPolicy(
        consecutive_failure_threshold=99,
        total_failure_threshold=2,
        failure_rate_threshold=1.0,
        minimum_number_of_calls=100,
        sliding_window_size=4,
    )
    breaker = _breaker(policy, clock=fake_clock)
    breaker.record_failure("boom")
    for _ in range(4):
        breaker.record_success()
    assert breaker.total_failures == 0
    breaker.record_failure("boom")
    breaker.record_success()
    breaker.record_failure("boom")
    assert breaker.state is BreakerState.OPEN, "two failures inside one window is still a burst"


def test_the_rate_is_not_evaluated_below_the_minimum_number_of_calls(fake_clock):
    breaker = _breaker(ONLY_RATE, clock=fake_clock)
    breaker.record_failure("boom")
    breaker.record_success()
    breaker.record_failure("boom")
    assert breaker.recorded_calls == 3
    assert breaker.total_failures / breaker.recorded_calls > 0.5
    assert breaker.state is BreakerState.CLOSED


def test_an_open_breaker_refuses_before_the_action_runs(fake_clock):
    breaker = _breaker(clock=fake_clock)
    calls = []
    for _ in range(3):
        breaker.record_failure("boom")
    with pytest.raises(CircuitOpenError, match="OPEN"):
        breaker.call(lambda: calls.append(1), name="fetch")
    assert calls == []


def test_the_cooldown_elapsing_gives_half_open_on_the_next_allow(fake_clock):
    breaker = _breaker(clock=fake_clock)
    for _ in range(3):
        breaker.record_failure("boom")
    fake_clock.advance(59.0)
    with pytest.raises(CircuitOpenError):
        breaker.allow()
    fake_clock.advance(2.0)
    breaker.allow()
    assert breaker.state is BreakerState.HALF_OPEN


def test_a_half_open_success_closes_and_zeroes_the_counters(fake_clock):
    breaker = _breaker(clock=fake_clock)
    for _ in range(3):
        breaker.record_failure("boom")
    fake_clock.advance(61.0)
    breaker.allow()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 0
    assert breaker.total_failures == 0
    assert breaker.recorded_calls == 0


def test_a_half_open_failure_reopens_and_restarts_the_cooldown(fake_clock):
    breaker = _breaker(clock=fake_clock)
    for _ in range(3):
        breaker.record_failure("boom")
    fake_clock.advance(61.0)
    breaker.allow()
    breaker.record_failure("boom")
    assert breaker.state is BreakerState.OPEN
    fake_clock.advance(59.0)
    with pytest.raises(CircuitOpenError):
        breaker.allow()


def test_a_second_half_open_trial_is_refused_while_one_is_in_flight(fake_clock):
    breaker = _breaker(clock=fake_clock)
    for _ in range(3):
        breaker.record_failure("boom")
    fake_clock.advance(61.0)
    breaker.allow()
    with pytest.raises(CircuitOpenError, match="HALF_OPEN"):
        breaker.allow()


def test_one_failing_action_is_one_breaker_failure_however_many_retries(fake_clock):
    breaker = _breaker(clock=fake_clock)
    attempts = []

    def action():
        attempts.append(1)
        raise TransientError("timed out")

    with pytest.raises(TransientError):
        breaker.call(action, name="fetch", retry=RetryPolicy(max_attempts=3, rng=random.Random(0)))
    assert len(attempts) == 3
    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 1
    assert breaker.recorded_calls == 1


def test_a_permanent_error_runs_exactly_one_attempt(fake_clock):
    breaker = _breaker(clock=fake_clock)
    attempts = []

    def action():
        attempts.append(1)
        raise PermanentError("contract rejected")

    with pytest.raises(PermanentError):
        breaker.call(action, name="fetch", retry=RetryPolicy(max_attempts=5))
    assert len(attempts) == 1
    assert breaker.consecutive_failures == 1


def test_a_successful_action_records_one_success(fake_clock):
    breaker = _breaker(clock=fake_clock)
    assert breaker.call(lambda: "items", name="fetch") == "items"
    assert breaker.recorded_calls == 1
    assert breaker.total_failures == 0


def test_full_jitter_is_reproducible_and_bounded():
    first = [full_jitter_delay(n, base=0.2, cap=5.0, rng=random.Random(0)) for n in range(6)]
    second = [full_jitter_delay(n, base=0.2, cap=5.0, rng=random.Random(0)) for n in range(6)]
    assert first == second
    for attempt, delay in enumerate(first):
        assert 0.0 <= delay <= min(5.0, 0.2 * 2**attempt)


def test_retries_sleep_on_the_injected_clock_and_are_journaled(fake_clock, journal_sink):
    breaker = _breaker(clock=fake_clock, journal=journal_sink)

    def action():
        raise TransientError("timed out")

    with pytest.raises(TransientError):
        breaker.call(action, name="fetch", retry=RetryPolicy(max_attempts=3, rng=random.Random(0)))
    assert len(fake_clock.slept) == 2
    assert journal_sink.names().count("retry.attempt") == 2


def test_state_survives_across_breaker_instances(tmp_path, fake_clock):
    store = BreakerStore(tmp_path / "breaker.json")
    first = _breaker(clock=fake_clock, store=store)
    for _ in range(3):
        first.record_failure("boom")
    assert first.state is BreakerState.OPEN

    second = _breaker(clock=fake_clock, store=store)
    assert second.state is BreakerState.OPEN
    assert second.consecutive_failures == 3
    with pytest.raises(CircuitOpenError):
        second.allow()


def test_a_persisted_breaker_recovers_across_three_invocations(tmp_path, fake_clock):
    store = BreakerStore(tmp_path / "breaker.json")
    first = _breaker(clock=fake_clock, store=store)
    for _ in range(3):
        first.record_failure("boom")
    assert first.state is BreakerState.OPEN

    second = _breaker(clock=fake_clock, store=store)
    with pytest.raises(CircuitOpenError):
        second.allow()

    fake_clock.advance(61.0)
    third = _breaker(clock=fake_clock, store=store)
    third.allow()
    assert third.state is BreakerState.HALF_OPEN
    third.record_success()
    assert third.state is BreakerState.CLOSED
    assert _breaker(clock=fake_clock, store=store).state is BreakerState.CLOSED


def test_a_corrupt_store_starts_closed_with_a_journaled_warning(tmp_path, fake_clock, journal_sink):
    path = tmp_path / "breaker.json"
    path.write_text("{ not json", encoding="utf-8")
    breaker = _breaker(clock=fake_clock, store=BreakerStore(path), journal=journal_sink)
    assert breaker.state is BreakerState.CLOSED
    name, level, payload = journal_sink.events[0]
    assert (name, level) == ("breaker.state", "WARN")
    assert "corrupt breaker store" in str(payload["reason"])


def test_the_window_survives_across_breaker_instances(tmp_path, fake_clock):
    store = BreakerStore(tmp_path / "breaker.json")
    first = _breaker(ONLY_RATE, clock=fake_clock, store=store)
    first.record_failure("boom")
    first.record_success()
    first.record_failure("boom")
    assert '"window": "x.x"' in (tmp_path / "breaker.json").read_text(encoding="utf-8")

    second = _breaker(ONLY_RATE, clock=fake_clock, store=store)
    assert second.recorded_calls == 3
    assert second.total_failures == 2
    second.record_failure("boom")
    assert second.state is BreakerState.OPEN, "the restored window is what the rate reads"


def test_a_store_that_is_not_utf_8_starts_closed_instead_of_crashing_the_job(
    tmp_path, fake_clock, journal_sink
):
    """UnicodeDecodeError is a ValueError, so it is not caught by an OSError handler. A
    breaker that dies on its own state file takes down the job it exists to protect."""
    path = tmp_path / "breaker.json"
    path.write_bytes(b'{"items": {"state": "\xff\xfe OPEN"}}')
    breaker = _breaker(clock=fake_clock, store=BreakerStore(path), journal=journal_sink)
    assert breaker.state is BreakerState.CLOSED
    assert "corrupt breaker store" in str(journal_sink.events[0][2]["reason"])


def test_a_store_path_that_is_a_directory_starts_closed_with_a_warning(
    tmp_path, fake_clock, journal_sink
):
    path = tmp_path / "breaker.json"
    path.mkdir()
    breaker = _breaker(clock=fake_clock, store=BreakerStore(path), journal=journal_sink)
    assert breaker.state is BreakerState.CLOSED
    assert "unreadable breaker store" in str(journal_sink.events[0][2]["reason"])


def test_the_store_leaves_no_temporary_file_behind(tmp_path, fake_clock):
    store = BreakerStore(tmp_path / "breaker.json")
    breaker = _breaker(clock=fake_clock, store=store)
    for _ in range(4):
        breaker.record_failure("boom")
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "breaker.json").exists()
