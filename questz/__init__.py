"""QUESTZ: a pre-flight reliability harness for browser automation."""

from questz import canary, normalize
from questz.breaker import Breaker, BreakerPolicy, BreakerState, BreakerStore
from questz.cache import Cache, CacheEntry, atomic_write_bytes
from questz.canary import Contract, FieldRule, SelectorRule, check_html
from questz.clock import SYSTEM_CLOCK, Clock, FakeClock, SystemClock
from questz.driver import PageDriver, PlaywrightDriver
from questz.journal import Journal, read_run, render_report
from questz.types import (
    CacheError,
    CanaryStatus,
    CircuitOpenError,
    ContractError,
    DriftReport,
    Finding,
    JournalSink,
    PermanentError,
    QuestzError,
    Severity,
    TransientError,
)

__version__ = "0.1.0"

__all__ = [
    "SYSTEM_CLOCK",
    "Breaker",
    "BreakerPolicy",
    "BreakerState",
    "BreakerStore",
    "Cache",
    "CacheEntry",
    "CacheError",
    "CanaryStatus",
    "CircuitOpenError",
    "Clock",
    "Contract",
    "ContractError",
    "DriftReport",
    "FakeClock",
    "FieldRule",
    "Finding",
    "Journal",
    "JournalSink",
    "PageDriver",
    "PermanentError",
    "PlaywrightDriver",
    "QuestzError",
    "SelectorRule",
    "Severity",
    "SystemClock",
    "TransientError",
    "__version__",
    "atomic_write_bytes",
    "canary",
    "check_html",
    "normalize",
    "read_run",
    "render_report",
]
