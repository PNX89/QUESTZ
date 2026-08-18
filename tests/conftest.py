"""Shared fixtures.

Network policy: the suite contacts nothing outside this machine. Loopback traffic to the
bundled test site on 127.0.0.1 is not network access. `playwright install` needs the
network at setup time, never at test time.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from questz.cache import Cache
from questz.canary import Contract, load
from questz.clock import FakeClock
from questz.testsite import TESTSITE_ROOT, serve

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO_ROOT / "examples" / "contracts" / "items.json"
EXAMPLES_DIR = REPO_ROOT / "examples"
_INSTALL_HINT = "chromium not installed: run 'uv run playwright install --only-shell chromium'"

# examples/ is a scripts directory rather than a package, so the tests reach it the same way
# `questz demo` does.
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))


@dataclass
class FakeDriver:
    """A PageDriver with no browser behind it. This is the whole reason the non-e2e suite
    needs no chromium, and the reason the Protocol is load bearing today."""

    html_text: str = ""
    status: int = 200
    ready: bool = True
    goto_error: Exception | None = None
    visited: list[str] = field(default_factory=list)
    filled: list[tuple[str, str]] = field(default_factory=list)
    clicked: list[str] = field(default_factory=list)
    last_mask: tuple[str, ...] = ()

    def goto(self, url: str) -> int:
        self.visited.append(url)
        if self.goto_error is not None:
            raise self.goto_error
        return self.status

    def wait_for(self, selector: str, *, timeout_ms: int = 5000) -> bool:
        return self.ready

    def html(self, selector: str | None = None) -> str:
        return self.html_text

    def fill(self, selector: str, value: str) -> None:
        self.filled.append((selector, value))

    def click(self, selector: str) -> None:
        self.clicked.append(selector)

    def screenshot(self, path: Path, *, mask: tuple[str, ...] = ()) -> Path:
        self.last_mask = tuple(mask)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return path


@dataclass
class RecordingJournal:
    """A JournalSink that keeps events in memory, so producers can be tested without a file."""

    events: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)

    def event(
        self,
        name: str,
        *,
        level: str = "INFO",
        step: str | None = None,
        duration_ms: float | None = None,
        artifact: str | None = None,
        **payload: object,
    ) -> None:
        self.events.append((name, level, payload))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.events]


@pytest.fixture
def journal_sink() -> RecordingJournal:
    return RecordingJournal()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tmp_cache(tmp_path: Path, fake_clock: FakeClock) -> Cache:
    return Cache(tmp_path / "cache", ttl_seconds=60.0, max_stale_seconds=3600.0, clock=fake_clock)


@pytest.fixture
def fake_driver() -> type[FakeDriver]:
    return FakeDriver


@pytest.fixture(scope="session")
def contract() -> Contract:
    return load(CONTRACT_PATH)


@pytest.fixture(scope="session")
def contract_path() -> Path:
    return CONTRACT_PATH


@pytest.fixture(scope="session")
def testsite_html() -> Callable[[str], str]:
    def read(relative: str) -> str:
        return (TESTSITE_ROOT / relative).read_text(encoding="utf-8")

    return read


@pytest.fixture(scope="session")
def testsite_file() -> Callable[[str], Path]:
    def locate(relative: str) -> Path:
        return TESTSITE_ROOT / relative

    return locate


@pytest.fixture(scope="session")
def testsite_server() -> Iterator[Callable[[str], str]]:
    running: dict[str, tuple[object, str]] = {}

    def start(variant: str = "v1") -> str:
        if variant not in running:
            server, url = serve(variant)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            running[variant] = (server, url)
        return running[variant][1]

    yield start
    for server, _ in running.values():
        server.shutdown()  # type: ignore[attr-defined]
        server.server_close()  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def base_url(testsite_server: Callable[[str], str]) -> str:
    return testsite_server("v1")


def _missing_browser_reason() -> str | None:
    """`playwright install --only-shell chromium` never writes the full browser build, so
    `chromium.executable_path` names a file that is not there while `launch()` works fine.
    Launching once is the only check that answers the question actually being asked."""
    if importlib.util.find_spec("playwright") is None:
        return _INSTALL_HINT
    if importlib.util.find_spec("pytest_playwright") is None:
        return _INSTALL_HINT
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as engine:
            engine.chromium.launch().close()
    except Exception:
        return _INSTALL_HINT
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """The marker is the ergonomic mechanism, this guard is the robust one: a fresh clone
    with no browser must still run `pytest` green."""
    marked = [item for item in items if item.get_closest_marker("e2e")]
    if not marked:
        return
    reason = _missing_browser_reason()
    if reason is None:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in marked:
        item.add_marker(skip)
