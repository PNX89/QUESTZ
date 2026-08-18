"""Three runs against the bundled target.

happy    v1 end to end: the CSV is written.
drift    v2: the canary stops the job before anything is written.
degrade  v1 with the network refused underneath it: the breaker opens, the cache serves
         stale bytes out loud, and a later run recovers through HALF_OPEN.
"""

from __future__ import annotations

import argparse
import itertools
import random
import shutil
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pricewatch import JobResult, run_job

from questz.breaker import Breaker, BreakerPolicy, BreakerStore, RetryPolicy
from questz.cache import Cache
from questz.canary import check_html
from questz.canary import load as load_contract
from questz.cli import BROWSER_HINT, EXIT_OK, EXIT_USAGE
from questz.clock import SYSTEM_CLOCK, Clock, FakeClock
from questz.driver import PlaywrightDriver
from questz.journal import Journal, read_run
from questz.testsite import TESTSITE_ROOT, serve
from questz.types import QuestzError, Severity

__all__ = ["SCENARIOS", "Ctx", "main", "run_scenario", "table_markdown"]

CONTRACT_PATH = Path(__file__).resolve().parent / "contracts" / "items.json"
SCENARIOS = ("happy", "drift", "degrade")

# --deterministic pins the port so the journal is reproducible byte for byte. Every other
# run takes an ephemeral port, which is what stops parallel CI legs colliding.
DETERMINISTIC_PORT = 8000
CACHE_TTL_SECONDS = 60.0
MAX_STALE_SECONDS = 86400.0
ABORTED_REQUESTS = 3

# The last four are the axis this detector is weakest on: something visible appears inside
# the container. They are here because a fixture set that only contains the cases a tool
# passes is a demonstration, not a measurement.
TABLE_ROWS: tuple[tuple[str, str], ...] = (
    ("v1/items.html", "the recorded page"),
    ("v1c/items.attr-order.html", "attribute order changed"),
    ("v1c/items.class-churn.html", "build hashed class names"),
    ("v1c/items.whitespace.html", "markup reflowed"),
    ("v1c/items.extra-rows.html", "13 rows instead of 12"),
    ("v1c/items.framework-ids.html", "framework ids and an inline style"),
    ("v1c/items.analytics-script.html", "analytics script and a comment added"),
    ("v1c/items.hydration-template.html", "a hydration payload in a template"),
    ("v1c/items.badge-span.html", "a Sale badge inside one product name"),
    ("v1c/items.wrapper-div.html", "a scroll wrapper around the table"),
    ("v1c/items.consent-banner.html", "a consent banner inside the container"),
    ("v2/items.html", "price renamed, stock moved out of the row, a column added"),
)
RELAXED: Severity = "MAJOR"
V2_DECLARED = (
    ("missing_selector", 'tr[data-testid="item-row"] td[data-testid="price"]'),
    ("missing_selector", 'tr[data-testid="item-row"] td[data-testid="stock"]'),
    ("structure_added", None),
)


@dataclass(frozen=True, slots=True)
class Ctx:
    scenario: str
    out: Path
    clock: Clock
    run_id: str
    rng: random.Random
    deterministic: bool

    def advance(self, seconds: float) -> None:
        """Crossing a 60 second cooldown by waiting 60 seconds is not a demonstration, so
        the degradation run always takes an injected clock."""
        if not isinstance(self.clock, FakeClock):
            raise QuestzError(f"scenario {self.scenario!r} needs an advanceable clock")
        self.clock.advance(seconds)


def _line(label: str, value: str) -> str:
    return f"{label + ':':<10}{value}"


def _entry_count(path: Path) -> int:
    return len(read_run(path).entries)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _findings_summary(result: JobResult) -> str:
    report = result.report
    if report is None:
        return result.detail or result.status
    if report.ok:
        return "OK, no findings"
    findings = _count(len(report.findings), "finding")
    return f"{report.status}, {findings}, max severity {report.max_severity}"


@contextmanager
def _site(variant: str, *, port: int) -> Iterator[str]:
    try:
        server, url = serve(variant, port=port)
    except OSError as exc:
        raise QuestzError(
            f"could not bind port {port} for the bundled target ({exc.strerror or exc});"
            " --deterministic pins the port so the run is reproducible"
        ) from None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _page() -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise QuestzError(BROWSER_HINT) from None
    with sync_playwright() as engine:
        try:
            browser = engine.chromium.launch()
        except Exception as exc:
            raise QuestzError(f"{str(exc).splitlines()[0]}; {BROWSER_HINT}") from None
        try:
            yield browser.new_page()
        finally:
            browser.close()


def _abort_first(page: Any, *, limit: int) -> None:
    """Deterministic fault injection. Nothing here is time based or random: the first
    `limit` requests for items.html are refused at the network layer, the rest are served."""
    seen = itertools.count(1)

    def handler(route: Any) -> None:
        if next(seen) <= limit:
            route.abort("connectionfailed")
        else:
            route.continue_()

    page.route("**/items.html", handler)


def _journal(ctx: Ctx) -> Journal:
    return Journal(ctx.out / "run.jsonl", run_id=ctx.run_id, clock=ctx.clock)


def _cache(ctx: Ctx, journal: Journal) -> Cache:
    return Cache(
        ctx.out / "cache",
        ttl_seconds=CACHE_TTL_SECONDS,
        max_stale_seconds=MAX_STALE_SECONDS,
        clock=ctx.clock,
        journal=journal,
    )


def _start(journal: Journal, ctx: Ctx, *, variant: str, url: str, name: str) -> None:
    journal.event(
        "run.start",
        scenario=ctx.scenario,
        variant=variant,
        url=url,
        contract=name,
        deterministic=ctx.deterministic,
    )


def _block(title: str, pairs: Sequence[tuple[str, str]]) -> list[str]:
    return ["", title, *(f"  {label + ':':<10}{value}" for label, value in pairs)]


def _stale_text(result: JobResult) -> str:
    if result.stale_seconds is None:
        return "fresh"
    return f"stale-if-error served bytes {result.stale_seconds:.1f}s old"


def _state(store: BreakerStore) -> str:
    snapshot = store.load("items")
    return "unknown" if snapshot is None else snapshot.state.value


def _tail(ctx: Ctx, exit_code: int, *, blank: bool = False) -> list[str]:
    path = ctx.out / "run.jsonl"
    lines = [
        _line("journal", f"{path}, {_entry_count(path)} entries"),
        _line("exit", str(exit_code)),
    ]
    return ["", *lines] if blank else lines


def _single_run(ctx: Ctx, variant: str) -> tuple[int, list[str]]:
    """happy and drift differ by one letter of the variant and by nothing else, which is
    the point: the job is the same job."""
    contract = load_contract(CONTRACT_PATH)
    with _site(variant, port=_port(ctx)) as base_url, _page() as page:
        journal = _journal(ctx)
        breaker = Breaker(
            BreakerPolicy(),
            name="items",
            clock=ctx.clock,
            store=BreakerStore(ctx.out / "breaker.json"),
            journal=journal,
        )
        _start(journal, ctx, variant=variant, url=f"{base_url}/items.html", name=contract.name)
        result = run_job(
            PlaywrightDriver(page),
            contract,
            base_url=base_url,
            journal=journal,
            breaker=breaker,
            cache=_cache(ctx, journal),
            out_dir=ctx.out,
        )
        journal.event("run.end", outcome=result.status, exit_code=result.exit_code)
        journal.close()
        state = breaker.snapshot()
    lines = [
        _line("scenario", ctx.scenario),
        _line("variant", f"{variant}, served from 127.0.0.1 by the bundled test site"),
    ]
    if result.report is not None and not result.report.ok:
        lines += ["", result.report.to_text(), ""]
    csv_text = (
        f"not written, {ctx.out / 'items.csv'} does not exist"
        if result.csv_path is None
        else f"{result.csv_path}, {len(result.rows)} rows"
    )
    lines += [
        _line("canary", _findings_summary(result)),
        _line("csv", csv_text),
        _line("breaker", f"{state.state.value}, {_count(state.recorded_calls, 'recorded call')}"),
        *_tail(ctx, result.exit_code),
    ]
    return result.exit_code, lines


def _degrade(ctx: Ctx) -> tuple[int, list[str]]:
    contract = load_contract(CONTRACT_PATH)
    # One failing action is enough to open this breaker: what is being demonstrated is the
    # state machine and the recovery, not the counting, which test_breaker.py owns.
    policy = BreakerPolicy(consecutive_failure_threshold=1, cooldown_seconds=60.0)
    retry = RetryPolicy(max_attempts=3, base_seconds=0.2, cap_seconds=2.0, rng=ctx.rng)
    store = BreakerStore(ctx.out / "breaker.json")
    lines = [
        _line("scenario", ctx.scenario),
        _line("variant", "v1, served from 127.0.0.1 by the bundled test site"),
        _line("clock", "injected, advanced rather than waited on"),
    ]
    with _site("v1", port=_port(ctx)) as base_url, _page() as page:
        journal = _journal(ctx)
        cache = _cache(ctx, journal)
        driver = PlaywrightDriver(page)
        _start(journal, ctx, variant="v1", url=f"{base_url}/items.html", name=contract.name)

        def invoke(*, login: bool) -> JobResult:
            # A fresh Breaker every time, reading the state the last one left on disk: a
            # process that exits after two minutes never reaches a 60 second cooldown.
            breaker = Breaker(policy, name="items", clock=ctx.clock, store=store, journal=journal)
            return run_job(
                driver,
                contract,
                base_url=base_url,
                journal=journal,
                breaker=breaker,
                cache=cache,
                out_dir=ctx.out,
                login=login,
                retry=retry,
            )

        priming = invoke(login=True)
        lines.append(_line("priming", f"1 fetch, {len(priming.rows)} rows cached"))
        # The cached bytes have to outlive the ttl before stale-if-error can mean anything.
        ctx.advance(90.0)
        _abort_first(page, limit=ABORTED_REQUESTS)

        first = invoke(login=False)
        lines += _block(
            f"invocation 1, the first {ABORTED_REQUESTS} requests for items.html are refused",
            [
                ("retries", f"{retry.max_attempts} attempts, then one recorded failure"),
                ("breaker", f"CLOSED to {_state(store)}"),
                ("cache", _stale_text(first)),
                ("canary", _findings_summary(first)),
                ("csv", f"{first.csv_path}, written from the cached bytes"),
                ("exit", str(first.exit_code)),
            ],
        )

        second = invoke(login=False)
        lines += _block(
            "invocation 2, inside the cooldown",
            [
                ("breaker", "OPEN, refused before the request was made"),
                ("detail", second.detail),
                ("exit", str(second.exit_code)),
            ],
        )

        ctx.advance(61.0)
        third = invoke(login=False)
        lines += _block(
            "invocation 3, the clock is 61s past the cooldown",
            [
                ("breaker", f"OPEN to HALF_OPEN to {_state(store)}"),
                ("canary", _findings_summary(third)),
                ("csv", f"{third.csv_path}, {len(third.rows)} rows"),
                ("exit", str(third.exit_code)),
            ],
        )
        journal.event("run.end", outcome=third.status, exit_code=third.exit_code)
        journal.close()
    return third.exit_code, [*lines, *_tail(ctx, third.exit_code, blank=True)]


def _port(ctx: Ctx) -> int:
    return DETERMINISTIC_PORT if ctx.deterministic else 0


def table_markdown() -> str:
    """Regenerated by `python examples/scenarios.py --table`, browserlessly, so the README
    table is never a number somebody typed."""
    contract = load_contract(CONTRACT_PATH)
    rows = [
        "| Variant | Change | Expected | Default | At `major` | Findings | Max severity |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    cosmetic_flagged = 0
    cosmetic_clean = 0
    relaxed_flagged = 0
    structural_flagged = 0
    declared_found = 0
    for relative, change in TABLE_ROWS:
        html = (TESTSITE_ROOT / relative).read_text(encoding="utf-8")
        report = check_html(html, contract, target=relative)
        relaxed = check_html(html, contract, target=relative, fail_on=RELAXED)
        expected = "DRIFT" if relative.startswith("v2/") else "OK"
        severity = report.max_severity or "none"
        rows.append(
            f"| `{relative}` | {change} | {expected} | {report.status} | {relaxed.status} |"
            f" {len(report.findings)} | {severity} |"
        )
        if relative.startswith("v1c/"):
            cosmetic_clean += int(not report.findings)
            cosmetic_flagged += int(report.status != "OK")
            relaxed_flagged += int(relaxed.status != "OK")
        if relative.startswith("v2/"):
            structural_flagged += int(report.status == "DRIFT")
            declared_found = sum(
                any(
                    finding.kind == kind and (selector is None or finding.selector == selector)
                    for finding in report.findings
                )
                for kind, selector in V2_DECLARED
            )
    cosmetic_total = sum(1 for relative, _ in TABLE_ROWS if relative.startswith("v1c/"))
    structural_total = sum(1 for relative, _ in TABLE_ROWS if relative.startswith("v2/"))
    rows.append("")
    rows.append(
        f"Structural variants flagged {structural_flagged}/{structural_total},"
        f" declared contract violations found in v2 {declared_found}/{len(V2_DECLARED)}."
        f" Of {cosmetic_total} cosmetic variants {cosmetic_clean} produce no findings at all;"
        f" the default threshold stops on {cosmetic_flagged} of them,"
        f" `--fail-on {RELAXED.lower()}` on {relaxed_flagged}."
    )
    return "\n".join(rows)


def run_scenario(
    scenario: str, *, out: Path = Path("artifacts"), deterministic: bool = False, seed: int = 0
) -> int:
    if scenario not in SCENARIOS:
        raise QuestzError(f"unknown scenario {scenario!r}, expected one of {list(SCENARIOS)}")
    # The degradation run needs an advanceable clock whether or not the caller asked for a
    # reproducible one.
    clock: Clock = FakeClock() if deterministic or scenario == "degrade" else SYSTEM_CLOCK
    run_id = "questz-demo" if deterministic else f"questz-{clock.now():%Y%m%dT%H%M%S}"
    out_dir = out / scenario
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(
        scenario=scenario,
        out=out_dir,
        clock=clock,
        run_id=run_id,
        rng=random.Random(seed),
        deterministic=deterministic,
    )
    if scenario == "degrade":
        exit_code, lines = _degrade(ctx)
    else:
        exit_code, lines = _single_run(ctx, "v1" if scenario == "happy" else "v2")
    print("\n".join(lines))
    return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scenarios.py", description="Run one demo scenario against the bundled target."
    )
    parser.add_argument("--scenario", default="happy", choices=SCENARIOS)
    parser.add_argument("--out", type=Path, default=Path("artifacts"))
    parser.add_argument("--deterministic", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--table", action="store_true", help="print the detection results table and exit"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.table:
            print(table_markdown())
            return EXIT_OK
        return run_scenario(
            args.scenario, out=args.out, deterministic=args.deterministic, seed=args.seed
        )
    except QuestzError as exc:
        print(f"scenarios: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
