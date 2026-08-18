"""Append only JSONL evidence. This module consumes events and never produces them, so
nothing it writes about is allowed to import it."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from questz.clock import SYSTEM_CLOCK, Clock
from questz.types import QuestzError

__all__ = ["EVENT_FIELDS", "Journal", "JournalEntry", "RunRecord", "read_run", "render_report"]

# Default deny. A payload key that is not listed here is never written, which is a
# different guarantee from masking a value after the fact.
EVENT_FIELDS: dict[str, frozenset[str]] = {
    "run.start": frozenset({"scenario", "variant", "url", "contract", "deterministic"}),
    "run.end": frozenset({"outcome", "exit_code"}),
    "canary.result": frozenset(
        {
            "status",
            "contract",
            "version",
            "target",
            "findings",
            "max_severity",
            "reason",
            "signature_expected",
            "signature_observed",
        }
    ),
    "step.start": frozenset(),
    "step.end": frozenset({"outcome", "error"}),
    # "breaker" rather than "name": `event(name, ...)` already owns that argument.
    "breaker.state": frozenset(
        {
            "breaker",
            "state",
            "previous",
            "reason",
            "consecutive_failures",
            "total_failures",
            "recorded_calls",
        }
    ),
    "cache.hit": frozenset({"key", "age_seconds"}),
    "cache.stale": frozenset({"key", "age_seconds", "max_stale_seconds"}),
    "cache.miss": frozenset({"key", "reason"}),
    "retry.attempt": frozenset({"action", "attempt", "max_attempts", "delay_seconds", "error"}),
    "artifact.saved": frozenset({"kind", "bytes"}),
    "error": frozenset({"error", "message"}),
}

_SECRET_KEY = re.compile(r"(?i)pass|secret|token|auth|cookie|session|api[_-]?key")
_REDACTED = "<redacted>"
# A URL anywhere inside a string, not only a value that is entirely one. Driver failures
# arrive as "navigation failed: Page.goto: net::ERR_TUNNEL_FAILED at http://u:p@proxy/x",
# so a redactor that only handles a bare URL misses the case that actually leaks.
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")


def _redact_url(text: str) -> str:
    """Blank the userinfo, mark the query, drop the fragment.

    The userinfo is the part that leaks: `http://user:hunter2@proxy.example.com:8080/x` is
    the ordinary way a scraping proxy is configured, and a journal is meant to be pasted
    into a ticket. The fragment is dropped rather than marked, because writing `?<redacted>`
    onto a URL that never had a query describes a query string that did not exist.
    """
    try:
        parts = urlsplit(text)
    except ValueError:
        # An unparseable URL is redacted whole. A redactor that fails open is not one.
        return _REDACTED
    if not parts.scheme or not parts.netloc:
        return text
    _, credentials, host = parts.netloc.rpartition("@")
    netloc = f"{_REDACTED}@{host}" if credentials else host
    return urlunsplit((parts.scheme, netloc, parts.path, _REDACTED if parts.query else "", ""))


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _URL_IN_TEXT.sub(lambda found: _redact_url(found.group(0)), value)
    if isinstance(value, dict):
        return {key: _redact(str(key), item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]
    return value


def _redact(key: str, value: Any) -> Any:
    if _SECRET_KEY.search(key):
        return _REDACTED
    return _redact_value(value)


@dataclass(frozen=True, slots=True)
class JournalEntry:
    ts: str
    run_id: str
    seq: int
    event: str
    level: str
    step: str | None
    duration_ms: float | None
    artifact: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    entries: tuple[JournalEntry, ...]


def _read_journal_text(path: Path, *, missing_ok: bool) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if missing_ok:
            return None
        raise QuestzError(f"journal file not found: {path}") from None
    except UnicodeDecodeError as exc:
        # A ValueError, so it slips past every `except OSError`. The evidence file is the
        # artifact this tool asks people to keep, so refusing to guess is the point.
        raise QuestzError(f"{path} is not valid UTF-8: {exc.reason} at byte {exc.start}") from None
    except OSError as exc:
        raise QuestzError(f"cannot read journal {path}: {exc.strerror or exc}") from None


def _last_seq(path: Path) -> int:
    # An unreadable tail is fatal rather than assumed empty: continuing would hand the next
    # entry a sequence number that is already in the file.
    text = _read_journal_text(path, missing_ok=True)
    if text is None:
        return 0
    last = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            last = max(last, int(json.loads(line)["seq"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return last


class Journal:
    def __init__(self, path: Path, *, run_id: str, clock: Clock = SYSTEM_CLOCK) -> None:
        self.path = path
        self.run_id = run_id
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        # seq continues past whatever is already in the file, so a second writer appending
        # to the same journal cannot reuse a sequence number.
        self._seq = _last_seq(path)
        self._handle = path.open("a", encoding="utf-8")

    def _timestamp(self) -> str:
        moment = self._clock.now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def event(
        self,
        name: str,
        *,
        level: str = "INFO",
        step: str | None = None,
        duration_ms: float | None = None,
        artifact: str | None = None,
        **payload: Any,
    ) -> None:
        allowed = EVENT_FIELDS.get(name, frozenset())
        kept = {key: _redact(key, value) for key, value in payload.items() if key in allowed}
        self._seq += 1
        record = {
            "artifact": None if artifact is None else _redact_value(artifact),
            "duration_ms": duration_ms,
            "event": name,
            "level": level,
            "payload": kept,
            "run_id": self.run_id,
            "seq": self._seq,
            "step": step,
            "ts": self._timestamp(),
        }
        self._handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        self._handle.flush()

    def step(self, name: str) -> AbstractContextManager[None]:
        return self._step(name)

    @contextmanager
    def _step(self, name: str) -> Iterator[None]:
        self.event("step.start", step=name)
        started = self._clock.monotonic()
        try:
            yield
        except Exception as exc:
            self.event(
                "step.end",
                level="ERROR",
                step=name,
                duration_ms=(self._clock.monotonic() - started) * 1000.0,
                outcome="error",
                error=type(exc).__name__,
            )
            raise
        self.event(
            "step.end",
            step=name,
            duration_ms=(self._clock.monotonic() - started) * 1000.0,
            outcome="ok",
        )

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()


def read_run(path: Path) -> RunRecord:
    text = _read_journal_text(path, missing_ok=False) or ""
    entries: list[JournalEntry] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            entries.append(
                JournalEntry(
                    ts=raw["ts"],
                    run_id=raw["run_id"],
                    seq=int(raw["seq"]),
                    event=raw["event"],
                    level=raw["level"],
                    step=raw["step"],
                    duration_ms=raw["duration_ms"],
                    artifact=raw["artifact"],
                    payload=raw["payload"],
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise QuestzError(
                f"{path}:{number}: malformed journal line ({type(exc).__name__})"
            ) from exc
    return RunRecord(run_id=entries[0].run_id if entries else "", entries=tuple(entries))


def render_report(run: RunRecord) -> str:
    lines = [f"run {run.run_id}", f"entries {len(run.entries)}"]
    if run.entries:
        lines.append(f"window {run.entries[0].ts} to {run.entries[-1].ts}")
    steps = [entry for entry in run.entries if entry.event == "step.end"]
    lines.extend(["", f"steps ({len(steps)}):"])
    for entry in steps:
        outcome = str(entry.payload.get("outcome", "?"))
        duration = "-" if entry.duration_ms is None else f"{entry.duration_ms:.1f}ms"
        error = entry.payload.get("error")
        suffix = f" ({error})" if error else ""
        lines.append(f"  {outcome:<6} {duration:>10}  {entry.step}{suffix}")
    if not steps:
        lines.append("  none")
    lines.append("")
    for entry in run.entries:
        if entry.event == "canary.result":
            severity = entry.payload.get("max_severity") or "none"
            count = len(entry.payload.get("findings") or ())
            lines.append(
                f"canary: {entry.payload.get('status')}, {count} findings, max severity {severity}"
            )
    stale = [entry for entry in run.entries if entry.event == "cache.stale"]
    if stale:
        oldest = max(float(entry.payload.get("age_seconds", 0.0)) for entry in stale)
        lines.append(f"cache: {len(stale)} stale serves, oldest {oldest:.1f}s")
    for entry in run.entries:
        if entry.event == "breaker.state":
            previous = entry.payload.get("previous") or "-"
            state = entry.payload.get("state")
            lines.append(
                f"breaker {entry.payload.get('breaker')}: {previous} to {state}"
                f" ({entry.payload.get('reason')})"
            )
    errors = [entry for entry in run.entries if entry.level == "ERROR"]
    lines.append(f"errors: {len(errors)}")
    return "\n".join(lines)
