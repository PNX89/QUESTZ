"""The page surface the canary drives. No runtime import of playwright lives here, which
is what lets `import questz.driver` work with no browser and no extra installed."""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from playwright.sync_api import Page

__all__ = ["PageDriver", "PlaywrightDriver"]


class PageDriver(Protocol):
    def goto(self, url: str) -> int: ...
    def wait_for(self, selector: str, *, timeout_ms: int = 5000) -> bool: ...
    def html(self, selector: str | None = None) -> str: ...
    def fill(self, selector: str, value: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def screenshot(self, path: Path, *, mask: Sequence[str] = ()) -> Path: ...


class PlaywrightDriver:
    """Constructed from a page the caller already owns, so this module never starts or
    imports a browser."""

    def __init__(self, page: Page, *, default_timeout_ms: int = 5000) -> None:
        self._page = page
        self._default_timeout_ms = default_timeout_ms
        self.last_mask: tuple[str, ...] = ()
        page.set_default_timeout(default_timeout_ms)

    def goto(self, url: str) -> int:
        try:
            response = self._page.goto(url, wait_until="domcontentloaded")
        except Exception:
            # Chromium commits its error page asynchronously after a refused navigation.
            # Letting that land before the caller retries is what stops the next attempt
            # dying with "interrupted by another navigation to chrome-error://chromewebdata".
            with contextlib.suppress(Exception):
                self._page.wait_for_load_state("domcontentloaded")
            raise
        # A navigation that produced no response (about:blank, a same document hash change)
        # is reported as 0 and read as UNAVAILABLE upstream.
        return 0 if response is None else response.status

    def wait_for(self, selector: str, *, timeout_ms: int = 5000) -> bool:
        """Waits on an explicit readiness selector. Never on `networkidle`, which Playwright
        marks discouraged for exactly this use."""
        try:
            self._page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
        except Exception as exc:
            # playwright.sync_api.TimeoutError cannot be imported without making playwright
            # a runtime dependency of this module, so the type is matched by name.
            if type(exc).__name__ != "TimeoutError":
                raise
            return False
        return True

    def html(self, selector: str | None = None) -> str:
        if selector is None:
            return str(self._page.content())
        locator = self._page.locator(selector)
        if locator.count() == 0:
            return ""
        return str(locator.first.evaluate("element => element.outerHTML"))

    def fill(self, selector: str, value: str) -> None:
        self._page.locator(selector).fill(value)

    def click(self, selector: str) -> None:
        self._page.locator(selector).click()

    def screenshot(self, path: Path, *, mask: Sequence[str] = ()) -> Path:
        """Screenshots, not JSON, are the real leak vector in an evidence journal, so the
        contract's secret selectors are painted over by the browser before the file exists."""
        self.last_mask = tuple(mask)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._page.screenshot(
            path=str(path),
            full_page=True,
            mask=[self._page.locator(selector) for selector in self.last_mask],
            mask_color="#000000",
        )
        return path
