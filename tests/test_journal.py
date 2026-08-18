from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

import questz
from questz.clock import FakeClock
from questz.journal import Journal, read_run, render_report
from questz.types import QuestzError

ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _journal(tmp_path, clock, *, run_id="questz-demo", name="run.jsonl") -> Journal:
    return Journal(tmp_path / name, run_id=run_id, clock=clock)


def test_every_line_parses_and_seq_increases_across_two_writers(tmp_path, fake_clock):
    first = _journal(tmp_path, fake_clock)
    first.event("run.start", scenario="happy")
    first.event("cache.miss", key="items", reason="absent")
    first.close()

    second = _journal(tmp_path, fake_clock)
    second.event("run.end", outcome="ok", exit_code=0)
    second.close()

    lines = (tmp_path / "run.jsonl").read_text(encoding="utf-8").splitlines()
    sequence = [json.loads(line)["seq"] for line in lines]
    assert len(lines) == 3
    assert sequence == sorted(set(sequence))
    assert sequence == [1, 2, 3]


def test_timestamps_are_iso_8601_utc(tmp_path, fake_clock):
    journal = _journal(tmp_path, fake_clock)
    journal.event("run.start", scenario="happy")
    journal.close()
    entry = read_run(tmp_path / "run.jsonl").entries[0]
    assert ISO_UTC.match(entry.ts)


def test_a_payload_key_outside_the_allowlist_is_never_written(tmp_path, fake_clock):
    journal = _journal(tmp_path, fake_clock)
    journal.event("cache.hit", key="items", age_seconds=1.0, raw_html="<table>secret</table>")
    journal.close()
    raw = (tmp_path / "run.jsonl").read_bytes()
    assert b"raw_html" not in raw
    assert b"<table>" not in raw
    assert b'"key":"items"' in raw


def test_a_secret_value_is_never_written(tmp_path, fake_clock):
    journal = _journal(tmp_path, fake_clock)
    journal.event(
        "canary.result",
        status="OK",
        contract="items",
        reason="password=hunter2",
        findings=[{"session_token": "abc123", "expected": "5 to 50"}],
    )
    journal.close()
    raw = (tmp_path / "run.jsonl").read_bytes()
    assert b"abc123" not in raw
    assert b"<redacted>" in raw
    assert b"hunter2" in raw, "a reason string is content, only keyed secrets are masked"


def test_a_url_query_string_is_stripped(tmp_path, fake_clock):
    journal = _journal(tmp_path, fake_clock)
    journal.event(
        "canary.result",
        status="OK",
        target="https://portal.test/items?session=abc123&page=2#row-4",
    )
    journal.close()
    payload = read_run(tmp_path / "run.jsonl").entries[0].payload
    assert payload["target"] == "https://portal.test/items?<redacted>"


def test_a_recorded_artifact_path_exists(tmp_path, fake_clock):
    shot = tmp_path / "artifacts" / "login.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    journal = _journal(tmp_path, fake_clock)
    journal.event(
        "artifact.saved", artifact=str(shot), kind="screenshot", bytes=shot.stat().st_size
    )
    journal.close()
    entry = read_run(tmp_path / "run.jsonl").entries[0]
    assert entry.artifact is not None
    assert (tmp_path / "artifacts" / "login.png").exists()
    assert entry.payload["kind"] == "screenshot"


def test_the_step_list_and_outcomes_survive_a_read_back(tmp_path, fake_clock):
    journal = _journal(tmp_path, fake_clock)
    with journal.step("login"):
        fake_clock.advance(0.25)
    with pytest.raises(RuntimeError), journal.step("fetch items"):
        raise RuntimeError("nope")
    journal.close()

    run = read_run(tmp_path / "run.jsonl")
    report = render_report(run)
    assert run.run_id == "questz-demo"
    assert "ok" in report
    assert "login" in report
    assert "error" in report
    assert "fetch items (RuntimeError)" in report
    assert "250.0ms" in report
    assert "errors: 1" in report


def test_two_runs_on_a_fake_clock_are_byte_identical(tmp_path, testsite_html, contract):
    def write(name: str) -> bytes:
        journal = Journal(tmp_path / name, run_id="questz-demo", clock=FakeClock())
        journal.event("run.start", scenario="happy", contract="items")
        with journal.step("login"):
            pass
        journal.event("cache.miss", key="items", reason="absent")
        journal.close()
        return (tmp_path / name).read_bytes()

    assert write("first.jsonl") == write("second.jsonl")


def test_an_unknown_event_carries_no_payload(tmp_path, fake_clock):
    journal = _journal(tmp_path, fake_clock)
    journal.event("something.new", detail="whatever")
    journal.close()
    entry = read_run(tmp_path / "run.jsonl").entries[0]
    assert entry.payload == {}


def test_a_malformed_line_is_rejected_naming_the_line(tmp_path, fake_clock):
    journal = _journal(tmp_path, fake_clock)
    journal.event("run.start", scenario="happy")
    journal.close()
    path = tmp_path / "run.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{ not json\n")
    with pytest.raises(QuestzError, match=":2:"):
        read_run(path)


def test_event_producers_never_import_the_journal():
    """The dependency direction is one way: producers emit through the JournalSink Protocol
    in questz.types, and only the journal serializes."""
    package = Path(questz.__file__).parent
    for name in (
        "types.py",
        "clock.py",
        "normalize.py",
        "cache.py",
        "breaker.py",
        "canary.py",
        "driver.py",
    ):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "questz.journal" not in imported, name


def test_an_empty_run_renders_without_steps(tmp_path, fake_clock):
    journal = _journal(tmp_path, fake_clock)
    journal.close()
    (tmp_path / "run.jsonl").touch()
    assert "none" in render_report(read_run(tmp_path / "run.jsonl"))
