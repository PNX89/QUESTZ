"""The browser leg.

Nothing here imports playwright: the `page` fixture comes from pytest-playwright, and
conftest skips this whole file when there is no chromium to launch. Screenshots are checked
for existence and for their PNG magic bytes, never for pixel content, because headless
Chromium font rendering differs across operating systems and browser builds.
"""

from __future__ import annotations

import itertools
import random
import re
import subprocess
import sys
from pathlib import Path

import pricewatch
import pytest

from questz.breaker import Breaker, BreakerPolicy, BreakerState, RetryPolicy
from questz.cache import Cache
from questz.clock import FakeClock
from questz.driver import PlaywrightDriver
from questz.journal import Journal, read_run, render_report
from questz.normalize import matches

pytestmark = pytest.mark.e2e

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
_ROLLUP = re.compile(r"<!-- report-rollup -->\n```console\n(.*?)```\n<!-- /report-rollup -->", re.S)
# The demo pins its port so the journal is reproducible; the assertion should not care.
_PORT = re.compile(r"127\.0\.0\.1:\d+")


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = FakeClock()
        self.out = tmp_path
        self.journal = Journal(tmp_path / "run.jsonl", run_id="e2e", clock=self.clock)
        self.breaker = Breaker(
            BreakerPolicy(), name="items", clock=self.clock, journal=self.journal
        )
        self.cache = Cache(
            tmp_path / "cache", ttl_seconds=60.0, clock=self.clock, journal=self.journal
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _job(page, contract, harness: Harness, base_url: str, **kwargs):
    return pricewatch.run_job(
        PlaywrightDriver(page),
        contract,
        base_url=base_url,
        journal=harness.journal,
        breaker=harness.breaker,
        cache=harness.cache,
        out_dir=harness.out,
        **kwargs,
    )


def test_the_happy_path_writes_a_twelve_row_csv(page, testsite_server, contract, harness):
    result = _job(page, contract, harness, testsite_server("v1"))
    rows = (harness.out / "items.csv").read_text(encoding="utf-8").splitlines()
    assert result.status == "OK"
    assert result.exit_code == 0
    assert len(rows) == 13
    assert rows[1] == "Anodised bracket,19.99,in-stock"
    assert result.stale_seconds is None


def test_the_redeploy_fails_closed_and_writes_no_csv(page, testsite_server, contract, harness):
    result = _job(page, contract, harness, testsite_server("v2"))
    assert result.status == "DRIFT"
    assert result.exit_code == 1
    assert not (harness.out / "items.csv").exists()
    assert result.report is not None
    assert result.report.max_severity == "CRITICAL"


def test_a_consent_interstitial_is_blocked(page, testsite_server, contract, harness):
    """v2i has no login page and no items table, so the job never gets past the gate."""
    result = _job(page, contract, harness, testsite_server("v2i"), login=False, timeout_ms=1000)
    assert result.status == "BLOCKED"
    assert result.exit_code == 3
    assert result.report is not None
    assert "consent-gate" in result.report.reason
    assert not (harness.out / "items.csv").exists()


def test_every_contract_selector_counts_the_same_in_both_engines(
    page, testsite_server, contract, harness
):
    """The supported CSS subset is a strict subset of Playwright's engine, so one contract
    string has to mean the same thing browserlessly and in the browser."""
    base_url = testsite_server("v1")
    pricewatch.log_in(
        PlaywrightDriver(page),
        contract,
        base_url=base_url,
        journal=harness.journal,
        out_dir=harness.out,
    )
    html = page.content()
    selectors = [
        contract.container,
        contract.ready_when,
        *(rule.selector for rule in contract.required),
        *(rule.selector for rule in contract.fields),
        *pricewatch.CELL_SELECTORS.values(),
    ]
    for selector in selectors:
        assert page.locator(selector).count() == len(matches(html, selector)), selector


def test_the_screenshots_exist_and_the_credential_selectors_were_masked(
    page, testsite_server, contract, harness
):
    driver = PlaywrightDriver(page)
    pricewatch.run_job(
        driver,
        contract,
        base_url=testsite_server("v1"),
        journal=harness.journal,
        breaker=harness.breaker,
        cache=harness.cache,
        out_dir=harness.out,
    )
    for name in ("login-masked.png", "items.png"):
        shot = harness.out / name
        assert shot.stat().st_size > 0
        assert shot.read_bytes().startswith(PNG_MAGIC)
    assert driver.last_mask == contract.secret_selectors
    assert contract.secret_selectors != ()


def test_the_report_rollup_printed_in_the_readme_comes_out_of_a_real_run(tmp_path):
    """The README quotes this transcript against a path that only exists once the demo has
    run, so nothing re-ran it and it could drift silently. On a repo about drift.

    In a subprocess because pytest-playwright already owns an asyncio loop in this one, and
    a second sync Playwright inside a running loop raises, which Limitations documents.
    """
    block = _ROLLUP.search(README.read_text(encoding="utf-8"))
    assert block is not None, "the report rollup block is missing its markers"
    quoted = [line for line in block.group(1).splitlines() if not line.startswith("$ ")]

    demo = subprocess.run(
        [
            sys.executable,
            "-m",
            "questz.cli",
            "demo",
            "--scenario",
            "degrade",
            "--deterministic",
            "--out",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert demo.returncode == 0, demo.stderr or demo.stdout
    printed = render_report(read_run(tmp_path / "degrade" / "run.jsonl")).splitlines()
    assert [_PORT.sub("127.0.0.1", line) for line in printed[-len(quoted) :]] == [
        _PORT.sub("127.0.0.1", line) for line in quoted
    ]


def test_two_aborted_requests_recover_on_the_third_as_one_breaker_success(
    page, testsite_server, contract, harness
):
    """Deterministic fault injection at the network layer, and the invariant that survives
    it: three attempts inside one logical action are still one breaker outcome."""
    base_url = testsite_server("v1")
    driver = PlaywrightDriver(page)
    pricewatch.log_in(
        driver, contract, base_url=base_url, journal=harness.journal, out_dir=harness.out
    )
    requests = itertools.count(1)

    def handler(route):
        if next(requests) <= 2:
            route.abort("connectionfailed")
        else:
            route.continue_()

    page.route("**/items.html", handler)
    html, stale_seconds = pricewatch.fetch(
        driver,
        contract,
        url=f"{base_url}/items.html",
        breaker=harness.breaker,
        cache=harness.cache,
        retry=RetryPolicy(max_attempts=3, rng=random.Random(0)),
    )
    snapshot = harness.breaker.snapshot()
    assert 'data-testid="items-table"' in html
    assert stale_seconds is None
    assert snapshot.state is BreakerState.CLOSED
    assert snapshot.recorded_calls == 1
    assert snapshot.total_failures == 0
    assert next(requests) == 4
