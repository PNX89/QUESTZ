"""The demo job, browserlessly.

tests/test_e2e.py runs the same job through a real chromium. This file is what keeps the
job itself covered on a clone with no browser installed, which is the reason the driver is
a Protocol in the first place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pricewatch
import pytest
import scenarios

from questz.breaker import Breaker, BreakerPolicy
from questz.cache import Cache
from questz.canary import check_html
from questz.cli import STATUS_EXIT
from questz.journal import Journal
from questz.types import QuestzError

BASE_URL = "http://127.0.0.1:8000"
ITEMS_URL = f"{BASE_URL}/items.html"


@dataclass
class Harness:
    journal: Journal
    breaker: Breaker
    cache: Cache
    out: Path

    @property
    def journal_bytes(self) -> bytes:
        return (self.out / "run.jsonl").read_bytes()

    def events(self, name: str) -> list[dict]:
        return [
            entry
            for entry in (json.loads(line) for line in self.journal_bytes.decode().splitlines())
            if entry["event"] == name
        ]


@pytest.fixture
def harness(tmp_path, fake_clock):
    journal = Journal(tmp_path / "run.jsonl", run_id="test-run", clock=fake_clock)
    breaker = Breaker(BreakerPolicy(), name="items", clock=fake_clock, journal=journal)
    cache = Cache(
        tmp_path / "cache",
        ttl_seconds=60.0,
        max_stale_seconds=86400.0,
        clock=fake_clock,
        journal=journal,
    )
    yield Harness(journal, breaker, cache, tmp_path)
    journal.close()


def _job(driver, contract, harness: Harness, **kwargs):
    return pricewatch.run_job(
        driver,
        contract,
        base_url=BASE_URL,
        journal=harness.journal,
        breaker=harness.breaker,
        cache=harness.cache,
        out_dir=harness.out,
        **kwargs,
    )


def test_the_happy_path_writes_one_csv_row_per_item(fake_driver, testsite_html, contract, harness):
    result = _job(fake_driver(html_text=testsite_html("v1/items.html")), contract, harness)
    rows = (harness.out / "items.csv").read_text(encoding="utf-8").splitlines()
    assert result.status == "OK"
    assert result.exit_code == 0
    assert len(result.rows) == 12
    assert rows[0] == "name,price,stock"
    assert rows[1] == "Anodised bracket,19.99,in-stock"
    assert len(rows) == 13


@pytest.mark.parametrize(
    ("cell", "expected"),
    [("  €19.99  ", "19.99"), ("1.234,56 EUR", "1234.56"), ("USD 19.99", "19.99")],
)
def test_the_job_reads_prices_with_the_parser_the_contract_validated_them_with(
    fake_driver, testsite_html, contract, harness, cell, expected
):
    html = testsite_html("v1/items.html").replace(">€19.99<", f">{cell}<", 1)
    result = _job(fake_driver(html_text=html), contract, harness)
    assert result.status == "OK"
    assert str(result.rows[0].price) == expected


def test_drift_stops_the_job_before_the_csv_exists(fake_driver, testsite_html, contract, harness):
    result = _job(fake_driver(html_text=testsite_html("v2/items.html")), contract, harness)
    assert result.status == "DRIFT"
    assert result.exit_code == 1
    assert result.csv_path is None
    assert not (harness.out / "items.csv").exists()
    assert result.report is not None
    assert result.report.max_severity == "CRITICAL"
    assert harness.events("canary.result")[0]["level"] == "ERROR"


def test_an_interstitial_stops_the_job_with_the_blocked_exit_code(
    fake_driver, testsite_html, contract, harness
):
    driver = fake_driver(html_text=testsite_html("v2i/items.html"), ready=False)
    result = _job(driver, contract, harness)
    assert result.status == "BLOCKED"
    assert result.exit_code == 3
    assert not (harness.out / "items.csv").exists()


def test_the_credential_fields_are_masked_and_never_reach_the_journal(
    fake_driver, testsite_html, contract, harness, mask_of
):
    """Named for the login shot, so it has to look at the login shot. It used to read the
    driver's last mask, and the job takes the items screenshot after the login one, so the
    only assertion standing behind this name was about a page with no password on it."""
    driver = fake_driver(html_text=testsite_html("v1/items.html"))
    _job(driver, contract, harness)
    assert dict(driver.filled) == {
        '[data-testid="username"]': pricewatch.DEMO_USER,
        '[data-testid="password"]': pricewatch.DEMO_PASSWORD,
    }
    assert mask_of(driver.shots, "login-masked.png") == contract.secret_selectors
    assert mask_of(driver.shots, "items.png") == contract.secret_selectors
    assert pricewatch.DEMO_PASSWORD.encode() not in harness.journal_bytes
    assert (harness.out / "login-masked.png").read_bytes().startswith(b"\x89PNG")


def test_a_refused_fetch_falls_back_to_the_cache_and_says_how_old_it_is(
    fake_driver, testsite_html, contract, harness, fake_clock
):
    harness.cache.put(ITEMS_URL, testsite_html("v1/items.html").encode("utf-8"))
    fake_clock.advance(90.0)
    driver = fake_driver(goto_error=OSError("connection refused"))
    result = _job(driver, contract, harness, login=False)
    stale = harness.events("cache.stale")
    assert result.status == "OK"
    # 90 seconds of staleness plus the backoff the retries slept on the injected clock.
    assert result.stale_seconds == pytest.approx(90.0, abs=1.0)
    assert result.stale_seconds > 90.0
    assert len(result.rows) == 12
    assert len(stale) == 1
    assert stale[0]["level"] == "WARN"
    assert stale[0]["payload"]["age_seconds"] == pytest.approx(result.stale_seconds)
    # Three attempts inside one logical action still record exactly one breaker outcome.
    assert harness.breaker.snapshot().total_failures == 1
    assert len(harness.events("retry.attempt")) == 2


def test_an_unreachable_site_with_nothing_cached_exits_three_rather_than_one(
    fake_driver, contract, harness
):
    """The exit code is the product: `questz.cli` says "a scheduler has to page differently
    for 'the site changed' and 'the site is down', so those are 1 and 3, never a shared non
    zero". Nothing drove UNAVAILABLE all the way to an exit code, so its row in `STATUS_EXIT`
    could be pointed at DRIFT with the whole suite green, and a dead site would then have
    been reported to the scheduler as a redeployed one.
    """
    driver = fake_driver(goto_error=OSError("connection refused"))
    result = _job(driver, contract, harness, login=False)
    assert result.status == "UNAVAILABLE"
    assert result.exit_code == 3
    assert result.exit_code != STATUS_EXIT["DRIFT"]
    assert not (harness.out / "items.csv").exists()


def test_an_open_breaker_refuses_before_the_page_is_requested(
    fake_driver, testsite_html, contract, harness
):
    for _ in range(3):
        harness.breaker.record_failure("earlier run")
    driver = fake_driver(html_text=testsite_html("v1/items.html"))
    result = _job(driver, contract, harness, login=False)
    assert result.status == "REFUSED"
    assert result.exit_code == 3
    assert driver.visited == []
    assert "OPEN" in result.detail
    assert harness.events("error")[0]["payload"]["error"] == "CircuitOpenError"


def test_the_detection_table_covers_every_fixture_and_counts_them():
    table = scenarios.table_markdown()
    body = [line for line in table.splitlines() if line.startswith("| `")]
    assert len(body) == len(scenarios.TABLE_ROWS) == 12
    assert all(relative in table for relative, _ in scenarios.TABLE_ROWS)
    assert table.splitlines()[-1] == (
        "Structural variants flagged 1/1, declared contract violations found in v2 3/3."
        " Of 10 cosmetic variants 7 produce no findings at all;"
        " the default threshold stops on 3 of them, `--fail-on major` on 0."
    )


@pytest.mark.parametrize(
    "variant", ["items.badge-span.html", "items.wrapper-div.html", "items.consent-banner.html"]
)
def test_the_fixtures_this_detector_fails_stay_in_the_table_as_warnings(
    testsite_html, contract, variant
):
    """The honest half of the table: a benign visible change inside the container does
    produce findings. They stay WARNING so a threshold can answer them, and if one ever
    reached MAJOR the table would have stopped describing the tool."""
    report = check_html(testsite_html(f"v1c/{variant}"), contract, target=variant)
    assert report.max_severity == "WARNING"
    assert f"v1c/{variant}" in [relative for relative, _ in scenarios.TABLE_ROWS]


def test_the_table_is_stable_across_two_generations():
    assert scenarios.table_markdown() == scenarios.table_markdown()


def test_an_unknown_scenario_is_rejected_by_name(tmp_path):
    with pytest.raises(QuestzError) as caught:
        scenarios.run_scenario("sideways", out=tmp_path)
    assert "sideways" in str(caught.value)
    assert "happy" in str(caught.value)
