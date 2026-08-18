"""The demo job: sign in, read the catalogue, write a CSV.

It is deliberately ordinary. The only unusual thing about it is that it asks the canary
whether the page still matches the contract before it writes anything, and stops when the
answer is no.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from questz.breaker import Breaker, RetryPolicy
from questz.cache import Cache
from questz.canary import Contract, check_html, parse_decimal
from questz.cli import EXIT_BLOCKED, STATUS_EXIT
from questz.driver import PageDriver
from questz.journal import Journal
from questz.normalize import parse, select
from questz.types import CircuitOpenError, DriftReport, PermanentError, TransientError

__all__ = [
    "CELL_SELECTORS",
    "CSV_HEADER",
    "DEMO_PASSWORD",
    "DEMO_USER",
    "JobResult",
    "Row",
    "extract",
    "log_in",
    "run_job",
    "write_csv",
]

# The bundled target checks these in client side JavaScript. Nothing is authenticated.
DEMO_USER = "demo"
DEMO_PASSWORD = "demo-password"

CSV_HEADER = ("name", "price", "stock")
ROW_SELECTOR = 'tr[data-testid="item-row"]'
CELL_SELECTORS = {
    "name": 'td[data-testid="name"]',
    "price": 'td[data-testid="price"]',
    "stock": 'td[data-testid="stock"]',
}
USERNAME_FIELD = '[data-testid="username"]'
PASSWORD_FIELD = '[data-testid="password"]'
SUBMIT_BUTTON = '[data-testid="submit"]'


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    price: Decimal
    stock: str


@dataclass(frozen=True, slots=True)
class JobResult:
    status: str
    exit_code: int
    report: DriftReport | None = None
    csv_path: Path | None = None
    rows: tuple[Row, ...] = ()
    stale_seconds: float | None = None
    detail: str = ""


def _price(text: str) -> Decimal:
    """The canary validated this cell with the same parser. If it did not, because the
    contract declares no price field, refusing is still better than a plausible number."""
    value = parse_decimal(text)
    if value is None:
        raise PermanentError(f"price cell {text!r} does not parse as a decimal")
    return value


def extract(html: str) -> tuple[Row, ...]:
    """Only ever reached after an OK report, so every declared cell is present."""
    rows: list[Row] = []
    for line in select(parse(html), ROW_SELECTOR):
        cells = {name: select(line, selector) for name, selector in CELL_SELECTORS.items()}
        rows.append(
            Row(
                name=cells["name"][0].text,
                price=_price(cells["price"][0].text),
                stock=cells["stock"][0].text,
            )
        )
    return tuple(rows)


def write_csv(rows: tuple[Row, ...], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows([row.name, f"{row.price:.2f}", row.stock] for row in rows)
    return path


def log_in(
    driver: PageDriver, contract: Contract, *, base_url: str, journal: Journal, out_dir: Path
) -> Path:
    """The credentials go into the page and never into the journal. The screenshot is taken
    with both fields filled, so the mask is what stops them reaching disk."""
    with journal.step("login"):
        driver.goto(f"{base_url}/login.html")
        driver.fill(USERNAME_FIELD, DEMO_USER)
        driver.fill(PASSWORD_FIELD, DEMO_PASSWORD)
        shot = driver.screenshot(out_dir / "login-masked.png", mask=contract.secret_selectors)
        journal.event(
            "artifact.saved",
            artifact=str(shot),
            kind="screenshot",
            bytes=shot.stat().st_size,
        )
        driver.click(SUBMIT_BUTTON)
        driver.wait_for(contract.ready_when)
    return shot


def fetch(
    driver: PageDriver,
    contract: Contract,
    *,
    url: str,
    breaker: Breaker,
    cache: Cache,
    retry: RetryPolicy | None = None,
    timeout_ms: int = 5000,
) -> tuple[str, float | None]:
    """One logical fetch is one breaker outcome. When it fails, RFC 5861 stale-if-error
    decides whether yesterday's bytes are still worth having."""

    def load() -> str:
        try:
            status = driver.goto(url)
        except Exception as exc:
            # Playwright names its own exception class `Error`, so the first line of the
            # message is the part an operator can act on.
            raise TransientError(f"navigation failed: {str(exc).splitlines()[0]}") from exc
        if not 200 <= status <= 299:
            raise TransientError(f"HTTP {status}")
        driver.wait_for(contract.ready_when, timeout_ms=timeout_ms)
        return driver.html()

    try:
        html = breaker.call(load, name="fetch items", retry=retry)
    except CircuitOpenError:
        # An open circuit means stop. Serving stale bytes past it would defeat the breaker.
        raise
    except Exception:
        entry = cache.get_stale_if_error(url)
        if entry is None:
            raise
        return entry.value.decode("utf-8"), entry.age_seconds
    cache.put(url, html.encode("utf-8"))
    return html, None


def _journal_result(journal: Journal, report: DriftReport) -> None:
    journal.event(
        "canary.result",
        level="INFO" if report.ok else "ERROR",
        status=report.status,
        contract=report.contract_name,
        version=report.contract_version,
        target=report.target,
        findings=[finding.to_dict() for finding in report.findings],
        max_severity=report.max_severity,
        reason=report.reason,
        signature_expected=report.signature_expected,
        signature_observed=report.signature_observed,
    )


def run_job(
    driver: PageDriver,
    contract: Contract,
    *,
    base_url: str,
    journal: Journal,
    breaker: Breaker,
    cache: Cache,
    out_dir: Path,
    login: bool = True,
    retry: RetryPolicy | None = None,
    timeout_ms: int = 5000,
) -> JobResult:
    url = f"{base_url}/items.html"
    contract = replace(contract, url=url)
    if login:
        log_in(driver, contract, base_url=base_url, journal=journal, out_dir=out_dir)
    try:
        with journal.step("fetch"):
            html, stale_seconds = fetch(
                driver,
                contract,
                url=url,
                breaker=breaker,
                cache=cache,
                retry=retry,
                timeout_ms=timeout_ms,
            )
    except CircuitOpenError as exc:
        journal.event("error", level="ERROR", error=type(exc).__name__, message=str(exc))
        return JobResult(status="REFUSED", exit_code=EXIT_BLOCKED, detail=str(exc))
    except Exception as exc:
        journal.event("error", level="ERROR", error=type(exc).__name__, message=str(exc))
        return JobResult(
            status="UNAVAILABLE", exit_code=STATUS_EXIT["UNAVAILABLE"], detail=str(exc)
        )
    shot = driver.screenshot(out_dir / "items.png", mask=contract.secret_selectors)
    journal.event(
        "artifact.saved", artifact=str(shot), kind="screenshot", bytes=shot.stat().st_size
    )
    with journal.step("canary"):
        report = check_html(html, contract, target=url)
        _journal_result(journal, report)
    if not report.ok:
        # Nothing downstream of here runs, which is the whole point: no CSV is written from
        # a page that no longer matches what this job assumes.
        return JobResult(
            status=report.status,
            exit_code=STATUS_EXIT[report.status],
            report=report,
            stale_seconds=stale_seconds,
        )
    with journal.step("extract"):
        rows = extract(html)
        csv_path = write_csv(rows, out_dir / "items.csv")
        journal.event(
            "artifact.saved", artifact=str(csv_path), kind="csv", bytes=csv_path.stat().st_size
        )
    return JobResult(
        status=report.status,
        exit_code=STATUS_EXIT[report.status],
        report=report,
        csv_path=csv_path,
        rows=rows,
        stale_seconds=stale_seconds,
    )
