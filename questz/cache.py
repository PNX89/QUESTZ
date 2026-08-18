"""Atomic writes, and an RFC 5861 `stale-if-error` cache that says so out loud."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from questz.clock import SYSTEM_CLOCK, Clock
from questz.types import CacheError, JournalSink

try:
    import fcntl as _fcntl

    fcntl: ModuleType | None = _fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

__all__ = ["Cache", "CacheEntry", "atomic_write_bytes"]

_DEFAULT_MODE = 0o644


def atomic_write_bytes(
    path: Path, data: bytes, *, mode: int | None = None, full_fsync: bool = False
) -> None:
    """Write bytes so a reader never sees a half written file.

    The temporary file has to live in the target's own directory: `os.replace` across
    filesystems raises OSError EXDEV.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(dir=parent, prefix=path.name + ".", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(handle, view) :]
        os.fsync(handle)
        if full_fsync and fcntl is not None and hasattr(fcntl, "F_FULLFSYNC"):
            # macOS fsync leaves the data in the drive's own write buffer. F_FULLFSYNC is
            # several times slower, which is why it is opt in.
            fcntl.fcntl(handle, fcntl.F_FULLFSYNC)
        try:
            target_mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            target_mode = _DEFAULT_MODE if mode is None else mode
        # mkstemp creates 0600 and os.replace does not restore the previous mode, so
        # without this an atomic update silently tightens the file to owner only.
        os.fchmod(handle, target_mode)
        os.close(handle)
        handle = -1
        os.replace(temporary, path)
        if os.name != "nt":
            # Without fsyncing the directory the rename itself may not survive power loss.
            # Opening a directory fails on Windows, so it is skipped there.
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if handle != -1:
            with contextlib.suppress(OSError):
                os.close(handle)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


@dataclass(frozen=True, slots=True)
class CacheEntry:
    value: bytes
    stored_at: datetime
    age_seconds: float
    stale: bool


class Cache:
    def __init__(
        self,
        root: Path,
        *,
        ttl_seconds: float,
        max_stale_seconds: float | None = None,
        clock: Clock = SYSTEM_CLOCK,
        journal: JournalSink | None = None,
        full_fsync: bool = False,
    ) -> None:
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.max_stale_seconds = max_stale_seconds
        self._clock = clock
        self._journal = journal
        self._full_fsync = full_fsync

    def path_for(self, key: str) -> Path:
        """Hashed, never the raw key: keys carry '/' and '..' and would escape the root."""
        return self.root / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"

    def put(self, key: str, value: bytes) -> Path:
        path = self.path_for(key)
        envelope = {
            "key_sha256": path.stem,
            "stored_at": self._clock.now().astimezone(UTC).isoformat(),
            "value_base64": base64.b64encode(value).decode("ascii"),
        }
        payload = json.dumps(envelope, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        atomic_write_bytes(path, payload, mode=_DEFAULT_MODE, full_fsync=self._full_fsync)
        return path

    def _read(self, key: str) -> tuple[bytes, datetime, float] | None:
        path = self.path_for(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            value = base64.b64decode(raw["value_base64"], validate=True)
            stored_at = datetime.fromisoformat(raw["stored_at"])
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CacheError(f"unreadable cache entry: {path} ({exc.strerror or exc})") from exc
        except (KeyError, ValueError, TypeError) as exc:
            # ValueError covers UnicodeDecodeError, which is what a truncated or non-UTF-8
            # entry raises. It is deliberately in the same bucket as malformed JSON.
            raise CacheError(f"corrupt cache entry: {path} ({type(exc).__name__})") from exc
        age = (self._clock.now() - stored_at).total_seconds()
        return value, stored_at, age

    def _event(self, name: str, level: str, **payload: object) -> None:
        if self._journal is not None:
            self._journal.event(name, level=level, **payload)

    def get(self, key: str) -> CacheEntry | None:
        found = self._read(key)
        if found is None:
            self._event("cache.miss", "INFO", key=key, reason="absent")
            return None
        value, stored_at, age = found
        if age > self.ttl_seconds:
            self._event("cache.miss", "INFO", key=key, reason="expired")
            return None
        self._event("cache.hit", "INFO", key=key, age_seconds=age)
        return CacheEntry(value=value, stored_at=stored_at, age_seconds=age, stale=False)

    def get_stale_if_error(self, key: str) -> CacheEntry | None:
        """RFC 5861 `stale-if-error`. A stale serve is always journaled with its age: the
        incident is never "served stale", it is "served stale silently and nobody noticed"."""
        found = self._read(key)
        if found is None:
            self._event("cache.miss", "INFO", key=key, reason="absent")
            return None
        value, stored_at, age = found
        if age <= self.ttl_seconds:
            self._event("cache.hit", "INFO", key=key, age_seconds=age)
            return CacheEntry(value=value, stored_at=stored_at, age_seconds=age, stale=False)
        if self.max_stale_seconds is not None and age > self.max_stale_seconds:
            self._event("cache.miss", "WARN", key=key, reason="beyond max_stale_seconds")
            return None
        self._event(
            "cache.stale",
            "WARN",
            key=key,
            age_seconds=age,
            max_stale_seconds=self.max_stale_seconds,
        )
        return CacheEntry(value=value, stored_at=stored_at, age_seconds=age, stale=True)
