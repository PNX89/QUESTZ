"""The one part of PlaywrightDriver that only a browser used to reach, driven by a stub.

`questz.driver` imports playwright under TYPE_CHECKING only, so the driver can be constructed
from any object with the page surface. That matters here: the settling logic runs exactly when
a navigation has been refused, which is the hardest state to arrange on purpose in a real
browser and the reason its defect survived six days of green runs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from questz.driver import PlaywrightDriver


class RacingPage:
    """A page that commits its error navigation only once someone waits for it.

    That is the race, made deterministic. Chromium commits `chrome-error://chromewebdata`
    asynchronously after a refusal, so a caller that does not wait for the URL to change can
    start its retry first and lose it to the interrupting navigation.
    """

    def __init__(self, *, message: str, commits: bool = True) -> None:
        self.message = message
        self.commits = commits
        self.url = "http://testsite.invalid/login.html"
        self.load_state_waits = 0
        self.url_waits: list[float | None] = []

    def set_default_timeout(self, timeout: float) -> None:
        return None

    def goto(self, url: str, *, wait_until: str) -> Any:
        raise RuntimeError(self.message)

    def wait_for_url(self, matcher: Callable[[str], bool], *, timeout: float | None = None) -> None:
        self.url_waits.append(timeout)
        if not self.commits:
            raise TimeoutError("no navigation committed")
        self.url = "chrome-error://chromewebdata/"
        assert matcher(self.url), "the driver's predicate did not accept the committed URL"

    def wait_for_load_state(self, state: str) -> None:
        self.load_state_waits += 1


def _driver(page: RacingPage) -> PlaywrightDriver:
    # The page argument is a Protocol surface at runtime; the annotation is TYPE_CHECKING only.
    return PlaywrightDriver(page, settle_timeout_ms=1000)  # type: ignore[arg-type]


def test_a_refused_navigation_settles_before_the_error_is_raised() -> None:
    page = RacingPage(message="Page.goto: net::ERR_CONNECTION_FAILED at http://x/items.html")
    with pytest.raises(RuntimeError):
        _driver(page).goto("http://testsite.invalid/items.html")

    # The URL was waited on, not just the load state. Waiting on the load state alone returns
    # instantly while the old document is still committed, which is the window that flaked.
    assert page.url_waits == [1000]
    assert page.url == "chrome-error://chromewebdata/"
    assert page.load_state_waits == 1


def test_an_ordinary_timeout_does_not_pay_for_a_navigation_that_is_not_coming() -> None:
    """Only a refused navigation commits an error page, so only that case waits.

    A selector or navigation timeout leaves the document where it is. Waiting on it would add
    the settle bound to every attempt of a real outage, which is the opposite of failing fast.
    """
    page = RacingPage(message="Timeout 5000ms exceeded.")
    with pytest.raises(RuntimeError):
        _driver(page).goto("http://testsite.invalid/items.html")
    assert page.url_waits == []
    assert page.load_state_waits == 0


def test_the_settle_survives_a_browser_that_never_commits_an_error_page() -> None:
    """Firefox and WebKit have no chrome-error page. The wait is bounded and swallowed."""
    page = RacingPage(
        message="Page.goto: net::ERR_CONNECTION_FAILED at http://x/items.html", commits=False
    )
    with pytest.raises(RuntimeError):
        _driver(page).goto("http://testsite.invalid/items.html")
    assert page.url_waits == [1000]
    assert page.load_state_waits == 1
