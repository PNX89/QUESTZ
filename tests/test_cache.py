from __future__ import annotations

import os
import stat

import pytest

from questz.cache import Cache, atomic_write_bytes
from questz.types import CacheError


def test_put_then_get_returns_the_value_as_fresh(tmp_cache):
    tmp_cache.put("items", b"name,price\n")
    entry = tmp_cache.get("items")
    assert entry is not None
    assert entry.value == b"name,price\n"
    assert entry.stale is False
    assert entry.age_seconds == 0.0


def test_a_missing_key_is_a_miss(tmp_cache):
    assert tmp_cache.get("absent") is None


def test_an_expired_entry_is_not_fresh(tmp_cache, fake_clock):
    tmp_cache.put("items", b"payload")
    # Exactly at the TTL is still fresh: `age > self.ttl_seconds` is the freshness check, and
    # an off by one turning it into `>=` would call this instant expired without anything
    # else in the suite advancing the clock to precisely here.
    fake_clock.advance(60.0)
    entry = tmp_cache.get("items")
    assert entry is not None
    assert entry.stale is False
    fake_clock.advance(1.0)
    assert tmp_cache.get("items") is None


def test_stale_if_error_returns_the_value_with_its_age(tmp_cache, fake_clock):
    tmp_cache.put("items", b"payload")
    # Exactly at the TTL, `get_stale_if_error` still reports a fresh hit: `age <=
    # self.ttl_seconds` is that boundary, and an off by one turning it into `<` would call
    # this instant stale.
    fake_clock.advance(60.0)
    fresh = tmp_cache.get_stale_if_error("items")
    assert fresh is not None
    assert fresh.stale is False
    fake_clock.advance(60.0)
    entry = tmp_cache.get_stale_if_error("items")
    assert entry is not None
    assert entry.stale is True
    assert entry.age_seconds == 120.0
    assert entry.value == b"payload"


def test_stale_if_error_refuses_past_max_stale_seconds(tmp_cache, fake_clock):
    tmp_cache.put("items", b"payload")
    # Exactly at max_stale_seconds the stale entry is still served: `age >
    # self.max_stale_seconds` is the refusal check, and an off by one turning it into `>=`
    # would refuse it one instant before it is actually due.
    fake_clock.advance(3600.0)
    still_served = tmp_cache.get_stale_if_error("items")
    assert still_served is not None
    assert still_served.stale is True
    fake_clock.advance(1.0)
    assert tmp_cache.get_stale_if_error("items") is None


def test_a_stale_serve_is_journaled_at_warn_with_the_age(tmp_path, fake_clock, journal_sink):
    cache = Cache(
        tmp_path / "cache",
        ttl_seconds=60.0,
        max_stale_seconds=3600.0,
        clock=fake_clock,
        journal=journal_sink,
    )
    cache.put("items", b"payload")
    fake_clock.advance(900.0)
    cache.get_stale_if_error("items")
    name, level, payload = journal_sink.events[-1]
    assert (name, level) == ("cache.stale", "WARN")
    assert payload["age_seconds"] == 900.0


def test_a_failed_write_leaves_the_previous_bytes_untouched(tmp_path, monkeypatch):
    """Not a crash simulation: a pytest process cannot kill itself mid syscall. This asserts
    the failure path, and crash atomicity is argued from POSIX rename semantics instead."""
    target = tmp_path / "entry.json"
    atomic_write_bytes(target, b"first")
    opened: list[str] = []
    real_open = os.open

    def recording_open(path, flags, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, flags, *args, **kwargs)

    def exploding_write(handle, data):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "write", exploding_write)
    with pytest.raises(OSError, match="no space left"):
        atomic_write_bytes(target, b"second")

    assert target.read_bytes() == b"first"
    assert list(tmp_path.glob("*.tmp")) == []
    assert str(target) not in opened


def test_the_file_mode_survives_an_overwrite(tmp_path):
    target = tmp_path / "entry.json"
    atomic_write_bytes(target, b"one")
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    atomic_write_bytes(target, b"two")
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    target.chmod(0o600)
    atomic_write_bytes(target, b"three")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_corrupt_entry_raises_naming_the_path(tmp_cache):
    tmp_cache.put("items", b"payload")
    path = tmp_cache.path_for("items")
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(CacheError) as caught:
        tmp_cache.get("items")
    assert str(path) in str(caught.value)


def test_an_entry_that_is_not_utf_8_is_corrupt_rather_than_a_traceback(tmp_cache):
    tmp_cache.put("items", b"payload")
    tmp_cache.path_for("items").write_bytes(b'{"value_base64": "\xff\xfe"}')
    with pytest.raises(CacheError, match="corrupt cache entry"):
        tmp_cache.get("items")


def test_an_entry_path_that_is_a_directory_is_reported_as_unreadable(tmp_cache):
    tmp_cache.path_for("items").mkdir(parents=True)
    with pytest.raises(CacheError, match="unreadable cache entry"):
        tmp_cache.get("items")


@pytest.mark.parametrize("key", ["../../etc/passwd", "https://example.test/a/b?c=1", "a/b/c"])
def test_keys_hash_to_a_safe_filename_inside_the_root(tmp_cache, key):
    path = tmp_cache.path_for(key)
    assert path.parent == tmp_cache.root
    assert path.suffix == ".json"
    assert len(path.stem) == 64
    tmp_cache.put(key, b"payload")
    assert tmp_cache.get(key) is not None


def test_a_large_payload_is_written_in_full(tmp_path, fake_clock):
    cache = Cache(tmp_path / "cache", ttl_seconds=60.0, clock=fake_clock)
    payload = os.urandom(1_000_000)
    cache.put("big", payload)
    entry = cache.get("big")
    assert entry is not None
    assert entry.value == payload
