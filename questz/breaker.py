"""Circuit breaker with persisted state, plus the retry loop that lives inside it.

The vocabulary is Resilience4j's (`failureRateThreshold`, `minimumNumberOfCalls`,
`permittedNumberOfCallsInHalfOpenState`) and the half open semantics are Fowler's.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from questz.cache import atomic_write_bytes
from questz.clock import SYSTEM_CLOCK, Clock
from questz.types import CacheError, CircuitOpenError, JournalSink, PermanentError, TransientError

__all__ = [
    "Breaker",
    "BreakerPolicy",
    "BreakerSnapshot",
    "BreakerState",
    "BreakerStore",
    "RetryPolicy",
    "full_jitter_delay",
]

T = TypeVar("T")


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True, slots=True)
class BreakerPolicy:
    consecutive_failure_threshold: int = 3
    total_failure_threshold: int = 5
    failure_rate_threshold: float = 0.5
    minimum_number_of_calls: int = 4
    cooldown_seconds: float = 60.0
    half_open_max_calls: int = 1


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_seconds: float = 0.2
    cap_seconds: float = 5.0
    # An injected generator per policy, never global random.seed(), which leaks state
    # across tests and parallel workers.
    rng: random.Random = field(default_factory=lambda: random.Random(0))


@dataclass(frozen=True, slots=True)
class BreakerSnapshot:
    name: str
    state: BreakerState
    consecutive_failures: int
    total_failures: int
    recorded_calls: int
    opened_at: float | None
    half_open_calls: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "half_open_calls": self.half_open_calls,
            "opened_at": self.opened_at,
            "recorded_calls": self.recorded_calls,
            "state": self.state.value,
            "total_failures": self.total_failures,
        }

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> BreakerSnapshot:
        opened_at = raw["opened_at"]
        return cls(
            name=name,
            state=BreakerState(raw["state"]),
            consecutive_failures=int(raw["consecutive_failures"]),
            total_failures=int(raw["total_failures"]),
            recorded_calls=int(raw["recorded_calls"]),
            opened_at=None if opened_at is None else float(opened_at),
            half_open_calls=int(raw["half_open_calls"]),
        )


def full_jitter_delay(attempt: int, *, base: float, cap: float, rng: random.Random) -> float:
    """Full jitter, from the AWS Architecture Blog:
    sleep = random_between(0, min(cap, base * 2 ** attempt))."""
    return rng.uniform(0.0, min(cap, base * 2**attempt))


class BreakerStore:
    """State on disk, so a two minute scraping process can still reach HALF_OPEN on its
    next invocation. One atomic writer is shared with the cache."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read_all(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise CacheError(
                f"unreadable breaker store: {self.path} ({exc.strerror or exc})"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # UnicodeDecodeError is a ValueError. A state file with one non-UTF-8 byte in
            # it must degrade to a fresh breaker, never crash the job the breaker protects.
            raise CacheError(f"corrupt breaker store: {self.path} ({exc})") from exc
        if not isinstance(raw, dict):
            raise CacheError(f"corrupt breaker store: {self.path} (not a JSON object)")
        return raw

    def load(self, name: str) -> BreakerSnapshot | None:
        raw = self._read_all().get(name)
        if raw is None:
            return None
        try:
            return BreakerSnapshot.from_dict(name, raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise CacheError(f"corrupt breaker store: {self.path} ({type(exc).__name__})") from exc

    def save(self, snapshot: BreakerSnapshot) -> None:
        try:
            data = self._read_all()
        except CacheError:
            data = {}
        data[snapshot.name] = snapshot.to_dict()
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        atomic_write_bytes(self.path, payload)


class Breaker:
    def __init__(
        self,
        policy: BreakerPolicy,
        *,
        name: str = "default",
        clock: Clock = SYSTEM_CLOCK,
        store: BreakerStore | None = None,
        journal: JournalSink | None = None,
    ) -> None:
        self.policy = policy
        self.name = name
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self.total_failures = 0
        self.recorded_calls = 0
        self.opened_at: float | None = None
        self.half_open_calls = 0
        self._clock = clock
        self._store = store
        self._journal = journal
        if store is not None:
            self._restore(store)

    def _restore(self, store: BreakerStore) -> None:
        try:
            restored = store.load(self.name)
        except CacheError as exc:
            self._event("WARN", reason=str(exc))
            return
        if restored is None:
            return
        self.state = restored.state
        self.consecutive_failures = restored.consecutive_failures
        self.total_failures = restored.total_failures
        self.recorded_calls = restored.recorded_calls
        self.opened_at = restored.opened_at
        self.half_open_calls = restored.half_open_calls

    def snapshot(self) -> BreakerSnapshot:
        return BreakerSnapshot(
            name=self.name,
            state=self.state,
            consecutive_failures=self.consecutive_failures,
            total_failures=self.total_failures,
            recorded_calls=self.recorded_calls,
            opened_at=self.opened_at,
            half_open_calls=self.half_open_calls,
        )

    def _event(self, level: str, *, reason: str, previous: str | None = None) -> None:
        if self._journal is None:
            return
        self._journal.event(
            "breaker.state",
            level=level,
            breaker=self.name,
            state=self.state.value,
            previous=previous,
            reason=reason,
            consecutive_failures=self.consecutive_failures,
            total_failures=self.total_failures,
            recorded_calls=self.recorded_calls,
        )

    def _persist(self) -> None:
        if self._store is not None:
            self._store.save(self.snapshot())

    def _transition(self, state: BreakerState, reason: str) -> None:
        previous = self.state
        if previous is state:
            return
        self.state = state
        level = "INFO" if state is BreakerState.CLOSED else "WARN"
        self._event(level, reason=reason, previous=previous.value)

    def _cooldown_remaining(self) -> float:
        if self.opened_at is None:
            return 0.0
        elapsed = self._clock.now().timestamp() - self.opened_at
        return max(0.0, self.policy.cooldown_seconds - elapsed)

    def _open(self, reason: str) -> None:
        self.opened_at = self._clock.now().timestamp()
        self.half_open_calls = 0
        self._transition(BreakerState.OPEN, reason)

    def _should_open(self) -> bool:
        policy = self.policy
        if self.consecutive_failures >= policy.consecutive_failure_threshold:
            return True
        if self.total_failures >= policy.total_failure_threshold:
            return True
        # The denominator is calls recorded so far, and the rate must be strictly exceeded:
        # 2 of 4 does not trip at a 0.5 threshold, 3 of 4 does.
        if self.recorded_calls < policy.minimum_number_of_calls:
            return False
        return self.total_failures / self.recorded_calls > policy.failure_rate_threshold

    def allow(self) -> None:
        if self.state is BreakerState.OPEN:
            remaining = self._cooldown_remaining()
            if remaining > 0:
                raise CircuitOpenError(
                    f"breaker {self.name!r} is OPEN, {remaining:.1f}s of cooldown left"
                )
            self.half_open_calls = 0
            self._transition(BreakerState.HALF_OPEN, "cooldown elapsed")
        if self.state is BreakerState.HALF_OPEN:
            if self.half_open_calls >= self.policy.half_open_max_calls:
                raise CircuitOpenError(
                    f"breaker {self.name!r} is HALF_OPEN and a trial call is already in flight"
                )
            self.half_open_calls += 1
            self._persist()

    def record_success(self) -> None:
        if self.state is BreakerState.HALF_OPEN:
            self.consecutive_failures = 0
            self.total_failures = 0
            self.recorded_calls = 0
            self.half_open_calls = 0
            self.opened_at = None
            self._transition(BreakerState.CLOSED, "half open trial succeeded")
        else:
            self.recorded_calls += 1
            self.consecutive_failures = 0
        self._persist()

    def record_failure(self, reason: str) -> None:
        self.recorded_calls += 1
        self.consecutive_failures += 1
        self.total_failures += 1
        if self.state is BreakerState.HALF_OPEN:
            self._open(f"half open trial failed: {reason}")
        elif self._should_open():
            self._open(reason)
        self._persist()

    def call(self, action: Callable[[], T], *, name: str, retry: RetryPolicy | None = None) -> T:
        """One logical action is exactly one breaker outcome, recorded after the retries
        are exhausted. Retrying inside the breaker is what stops a 3 attempt action from
        opening a 3 failure breaker on its own."""
        self.allow()
        policy = retry or RetryPolicy()
        attempt = 0
        while True:
            try:
                result = action()
            except PermanentError as exc:
                self.record_failure(f"{name}: {type(exc).__name__}: {exc}")
                raise
            except Exception as exc:
                retryable = isinstance(exc, TransientError)
                if not retryable or attempt + 1 >= policy.max_attempts:
                    self.record_failure(f"{name}: {type(exc).__name__}: {exc}")
                    raise
                delay = full_jitter_delay(
                    attempt, base=policy.base_seconds, cap=policy.cap_seconds, rng=policy.rng
                )
                if self._journal is not None:
                    self._journal.event(
                        "retry.attempt",
                        level="WARN",
                        action=name,
                        attempt=attempt + 1,
                        max_attempts=policy.max_attempts,
                        delay_seconds=delay,
                        error=type(exc).__name__,
                    )
                self._clock.sleep(delay)
                attempt += 1
            else:
                self.record_success()
                return result
