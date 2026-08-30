from __future__ import annotations

import json
from dataclasses import replace

import pytest

from questz.canary import check_html, parse_decimal, run
from questz.types import SEVERITY_ORDER, DriftReport, Finding, TransientError

COSMETIC = [
    "items.attr-order.html",
    "items.class-churn.html",
    "items.whitespace.html",
    "items.extra-rows.html",
    "items.framework-ids.html",
    "items.analytics-script.html",
]


def _with_row_count(html: str, count: int) -> str:
    start = html.index("<tbody>") + len("<tbody>")
    end = html.index("</tbody>")
    first_row = html[start:end].split("</tr>")[0] + "</tr>"
    return html[:start] + first_row * count + html[end:]


def _finding(report: DriftReport, kind: str, selector: str | None = None) -> Finding:
    for finding in report.findings:
        if finding.kind == kind and (selector is None or finding.selector == selector):
            return finding
    raise AssertionError(
        f"no {kind} finding for {selector!r} in {[f.kind for f in report.findings]}"
    )


def test_the_recorded_page_is_ok(testsite_html, contract):
    report = check_html(testsite_html("v1/items.html"), contract, target="v1")
    assert report.status == "OK"
    assert report.ok
    assert report.findings == ()
    assert report.signature_observed == contract.signature
    assert report.signature_diff == ()


@pytest.mark.parametrize("variant", COSMETIC)
def test_cosmetic_variants_produce_no_findings(testsite_html, contract, variant):
    report = check_html(testsite_html(f"v1c/{variant}"), contract, target=variant)
    assert report.status == "OK"
    assert report.findings == ()


def test_the_redeploy_reports_the_three_declared_violations(testsite_html, contract):
    report = check_html(testsite_html("v2/items.html"), contract, target="v2")
    assert report.status == "DRIFT"
    assert report.max_severity == "CRITICAL"
    price = _finding(
        report, "missing_selector", 'tr[data-testid="item-row"] td[data-testid="price"]'
    )
    stock = _finding(
        report, "missing_selector", 'tr[data-testid="item-row"] td[data-testid="stock"]'
    )
    added = _finding(report, "structure_added")
    assert price.severity == "CRITICAL"
    assert price.found == "0"
    assert stock.severity == "CRITICAL"
    assert added.severity == "WARNING"
    assert "col-sku" in added.detail
    assert _finding(report, "structure_removed").severity == "MAJOR"


def test_a_page_that_never_closes_its_cells_is_still_ok(testsite_html, contract):
    """The parser owns this, but the symptom lands here: nested cells collapse every value
    in the row into the first one, and the operator gets sent after a price format problem
    that does not exist."""
    sloppy = testsite_html("v1/items.html").replace("</td>", "").replace("</tr>", "")
    report = check_html(sloppy, contract, target="sloppy")
    assert report.status == "OK"
    assert report.signature_observed == contract.signature


def test_a_wrapper_div_is_reported_as_one_move_not_eleven_deletions(testsite_html, contract):
    """Depth is part of every serialized line, so one layout wrapper invalidates the whole
    subtree. Reporting nodes as gone when they are still on the page sends whoever got
    paged looking for something that never happened."""
    html = testsite_html("v1/items.html")
    wrapped = html.replace(
        '<table data-testid="items-table" class="grid">',
        '<div class="table-wrap"><table data-testid="items-table" class="grid">',
        1,
    ).replace("</table>", "</table></div>", 1)
    report = check_html(wrapped, contract, target="wrapped")
    moved = _finding(report, "structure_moved")
    assert [finding.kind for finding in report.findings] == ["structure_moved", "structure_added"]
    assert moved.severity == "WARNING"
    assert moved.found == "11 re-nested 1 level deeper"
    assert _finding(report, "structure_added").found == "1 new"
    assert report.max_severity == "WARNING"


def _with_badge(html: str) -> str:
    """The most ordinary benign change on a commerce page: a visible label appears inside
    a cell the contract depends on."""
    return html.replace(
        'class="cell name">Anodised bracket',
        'class="cell name"><span class="badge">Sale</span>Anodised bracket',
        1,
    )


def test_a_benign_addition_gates_by_default_and_stops_gating_at_a_higher_threshold(
    testsite_html, contract
):
    """Fail closed by default, but not firing on changes that do not matter is what decides
    whether anyone leaves the check switched on, so the threshold is an argument."""
    html = _with_badge(testsite_html("v1/items.html"))
    default = check_html(html, contract, target="badge")
    relaxed = check_html(html, contract, target="badge", fail_on="MAJOR")
    assert default.status == "DRIFT"
    assert relaxed.status == "OK"
    assert relaxed.findings == default.findings, "raising the bar hides nothing, it only stops"
    assert relaxed.max_severity == "WARNING"
    assert "not a stop" in relaxed.to_text()


def test_a_critical_finding_still_stops_at_every_threshold(testsite_html, contract):
    for fail_on in ("WARNING", "MAJOR", "CRITICAL"):
        report = check_html(testsite_html("v2/items.html"), contract, target="v2", fail_on=fail_on)
        assert report.status == "DRIFT", fail_on


def test_the_threshold_the_report_was_judged_against_is_recorded(testsite_html, contract):
    report = check_html(testsite_html("v1/items.html"), contract, fail_on="CRITICAL")
    assert report.fail_on == "CRITICAL"
    assert report.to_dict()["fail_on"] == "CRITICAL"


def test_a_page_that_never_became_ready_stops_whatever_the_threshold_says(
    fake_driver, testsite_html, contract
):
    """The threshold judges what changed on a page that arrived. One that never did is a
    different question, and it is not the operator's to relax here."""
    driver = fake_driver(html_text=testsite_html("v1/items.html"), ready=False)
    assert run(driver, contract, fail_on="CRITICAL").status == "DRIFT"


def test_a_node_that_really_left_is_still_reported_as_gone(testsite_html, contract):
    """The move detector must not launder a deletion, and the other way round: a node that
    only changed position must not be laundered into a deletion plus an unrelated addition.
    Only nodes with a matching line, at the same depth or a different one, are re-nested;
    everything else is still missing."""
    html = testsite_html("v1/items.html").replace(
        '<th data-testid="col-stock" class="th">Availability</th>', "", 1
    )
    report = check_html(html, contract, target="removed")
    assert _finding(report, "structure_removed").found == "1 gone"
    assert "col-stock" in _finding(report, "structure_removed").detail

    swapped = testsite_html("v1/items.html").replace(
        '<th data-testid="col-price" class="th">Price</th>\n'
        '      <th data-testid="col-stock" class="th">Availability</th>',
        '<th data-testid="col-stock" class="th">Availability</th>\n'
        '      <th data-testid="col-price" class="th">Price</th>',
        1,
    )
    swap_report = check_html(swapped, contract, target="swapped")
    # Both columns are still on the page; only their order changed. Reporting one of them
    # as a MAJOR removal and an unrelated WARNING addition is what a reader on call is
    # least equipped to act on: nothing to fix, and the wrong severity to trust that on.
    assert [finding.kind for finding in swap_report.findings] == ["structure_moved"]
    assert _finding(swap_report, "structure_moved").severity == "WARNING"
    assert _finding(swap_report, "structure_moved").found == "1 reordered"
    assert swap_report.max_severity == "WARNING"


def test_the_findings_are_ordered_by_severity(testsite_html, contract):
    report = check_html(testsite_html("v2/items.html"), contract, target="v2")
    severities = [finding.severity for finding in report.findings]
    assert severities == ["CRITICAL", "CRITICAL", "MAJOR", "WARNING"]


def test_one_missing_selector_is_reported_once(testsite_html, contract):
    report = check_html(testsite_html("v2/items.html"), contract, target="v2")
    selectors = [f.selector for f in report.findings if f.kind == "missing_selector"]
    assert len(selectors) == len(set(selectors))


@pytest.mark.parametrize("count", [3, 60])
def test_a_row_count_outside_the_declared_range_is_major(testsite_html, contract, count):
    report = check_html(_with_row_count(testsite_html("v1/items.html"), count), contract)
    finding = _finding(report, "count_out_of_range", 'tr[data-testid="item-row"]')
    assert report.status == "DRIFT"
    assert finding.severity == "MAJOR"
    assert finding.expected == "5 to 50"
    assert finding.found == str(count)


def test_a_price_that_is_not_a_decimal_is_critical(testsite_html, contract):
    html = testsite_html("v1/items.html").replace(">€19.99<", ">N/A<", 1)
    report = check_html(html, contract)
    finding = _finding(report, "field_shape")
    assert finding.severity == "CRITICAL"
    assert "N/A" in finding.found
    assert "'price'" in finding.detail


def test_a_stock_value_outside_the_enum_is_critical(testsite_html, contract):
    html = testsite_html("v1/items.html").replace(">low<", ">backordered<", 1)
    report = check_html(html, contract)
    finding = _finding(report, "field_shape")
    assert finding.severity == "CRITICAL"
    assert "backordered" in finding.found


def test_prices_keep_their_currency_symbol_and_still_parse(testsite_html, contract):
    html = testsite_html("v1/items.html").replace(">€19.99<", ">1.234,56 EUR<", 1)
    assert check_html(html, contract).status == "OK"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("€19.99", "19.99"),
        ("USD 19.99", "19.99"),
        ("19,99 EUR", "19.99"),
        ("-€5.00", "-5.00"),
        ("1.234,56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("1 234,56", "1234.56"),
        ("1.234.567", "1234567"),
        ("12.345.678,90", "12345678.90"),
        ("12345.67", "12345.67"),
        ("1234", "1234"),
        # A three digit fraction, which is the only width that reaches the rule about two
        # different separator characters: every other row here is resolved before it, so the
        # branch that reads this as 1234.567 rather than 1234567 was never run.
        ("1.234,567", "1234.567"),
        # A group separated by a space and nothing after it. Read as a decimal point this is
        # 1.234, and the CSV would be out by a factor of a thousand.
        ("1 234", "1234"),
    ],
)
def test_the_decimal_parser_reads_every_unambiguous_shape(text, expected):
    assert str(parse_decimal(text)) == expected


@pytest.mark.parametrize("text", ["", "N/A", "1e3", "1.2.3.4", "1.23.456", "--5", "12,34,56"])
def test_the_decimal_parser_refuses_what_it_cannot_read(text):
    """'1e3' is the interesting one: a parser that filters the string down to digits and
    separators turns it into 13, and 13 is a plausible price."""
    assert parse_decimal(text) is None


@pytest.mark.parametrize("text", ["1.234", "1,234", "€1.234"])
def test_a_value_that_reads_two_ways_is_refused_rather_than_guessed(text):
    """A thousand euros, or one euro twenty-three. Guessing writes a clean CSV that is
    wrong by 1000x, which is the exact failure this repo exists to catch."""
    assert parse_decimal(text) is None


@pytest.mark.parametrize(
    ("text", "separator", "expected"),
    [("1.234", ".", "1.234"), ("1.234", ",", "1234"), ("1,234", ",", "1.234")],
)
def test_a_declared_decimal_separator_resolves_the_ambiguity(text, separator, expected):
    assert str(parse_decimal(text, decimal_separator=separator)) == expected


@pytest.mark.parametrize("text", ["1,234.567", "1,234.56"])
def test_a_value_that_contradicts_the_declared_separator_is_refused(text):
    """The contract says the comma is this target's decimal point and the cell uses it between
    two groups of digits, so the declaration and the value disagree. Refusing is the only safe
    reading: dropping the separators and returning 1234567 is the thousandfold error again,
    with a contract behind it saying it was checked."""
    assert parse_decimal(text, decimal_separator=",") is None


def test_an_ambiguous_price_is_a_finding_rather_than_a_thousandfold_error(testsite_html, contract):
    html = testsite_html("v1/items.html").replace(">€19.99<", ">€1.234<", 1)
    report = check_html(html, contract)
    assert report.status == "DRIFT"
    assert _finding(report, "field_shape").severity == "CRITICAL"


def test_the_same_price_passes_once_the_contract_declares_its_separator(testsite_html, contract):
    html = testsite_html("v1/items.html").replace(">€19.99<", ">€1.234<", 1)
    assert check_html(html, replace(contract, decimal_separator=".")).status == "OK"


def test_max_severity_orders_critical_above_major_above_warning():
    def report(*severities):
        return DriftReport(
            status="DRIFT",
            target="t",
            contract_name="items",
            contract_version=1,
            findings=tuple(
                Finding("structure_added", severity, None, "", "") for severity in severities
            ),
            signature_expected="a",
            signature_observed="b",
            signature_diff=(),
            observed_counts=(),
        )

    assert report().max_severity is None
    assert report("WARNING").max_severity == "WARNING"
    assert report("WARNING", "MAJOR").max_severity == "MAJOR"
    assert report("WARNING", "CRITICAL", "MAJOR").max_severity == "CRITICAL"
    # In an order where a tie gives the wrong answer. `max` returns the first of equal keys,
    # so listing the higher severity first let this pass with the lattice flattened, which is
    # how MAJOR could be set equal to CRITICAL with nothing in the suite turning red.
    assert report("MAJOR", "CRITICAL").max_severity == "CRITICAL"
    assert report("WARNING", "MAJOR").max_severity == "MAJOR"
    assert report("MAJOR", "WARNING").max_severity == "MAJOR"


def test_the_severity_lattice_is_a_strict_ordering():
    """`SEVERITY_ORDER` is what `_gates`, the finding sort and `max_severity` all read, so
    `--fail-on` means nothing if two severities can be equal. It is asserted directly because
    every use of it reads the winner and never the gap."""
    assert SEVERITY_ORDER["CRITICAL"] > SEVERITY_ORDER["MAJOR"] > SEVERITY_ORDER["WARNING"]
    assert sorted(SEVERITY_ORDER) == ["CRITICAL", "MAJOR", "WARNING"]


def test_the_report_survives_a_json_round_trip(testsite_html, contract):
    report = check_html(testsite_html("v2/items.html"), contract, target="v2")
    restored = json.loads(json.dumps(report.to_dict()))
    assert restored == report.to_dict()
    assert restored["max_severity"] == "CRITICAL"
    assert restored["status"] == "DRIFT"


def test_the_text_report_names_the_status_and_every_selector(testsite_html, contract):
    text = check_html(testsite_html("v2/items.html"), contract, target="v2").to_text()
    assert "questz canary: DRIFT" in text
    assert 'td[data-testid="price"]' in text
    assert "structure diff:" in text


def test_a_navigation_error_is_unavailable_not_drift(fake_driver, contract):
    report = run(fake_driver(goto_error=TransientError("connection refused")), contract)
    assert report.status == "UNAVAILABLE"
    assert report.findings == ()
    assert "TransientError" in report.reason
    assert "connection refused" in report.reason


def test_a_non_2xx_status_is_unavailable(fake_driver, contract):
    report = run(fake_driver(status=503), contract)
    assert report.status == "UNAVAILABLE"
    assert report.reason == "HTTP 503"
    assert report.findings == ()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"wait_error": RuntimeError("Page.wait_for_selector: Target page has been closed")},
            "closed",
        ),
        (
            {"html_error": RuntimeError("Page.content: Execution context was destroyed")},
            "destroyed",
        ),
    ],
)
def test_a_browser_that_breaks_after_the_navigation_is_unavailable_not_a_traceback(
    fake_driver, contract, kwargs, message
):
    """The guard used to end at `goto`, and the two calls after it are exactly where a page
    that closes or navigates mid check raises. `run` then left as a raw exception, past
    cli.main's QuestzError handler, and the process exited 1, which this tool's own exit code
    table reads as the site having changed.
    """
    report = run(fake_driver(**kwargs), contract)
    assert report.status == "UNAVAILABLE"
    assert report.findings == ()
    assert "RuntimeError" in report.reason
    assert message in report.reason


def test_an_interstitial_is_blocked_and_lists_what_was_served(fake_driver, testsite_html, contract):
    driver = fake_driver(html_text=testsite_html("v2i/items.html"), ready=False)
    report = run(driver, contract)
    assert report.status == "BLOCKED"
    assert report.findings == ()
    assert "consent-gate" in report.reason
    assert "site-header" in report.reason
    assert "readiness selector" in report.reason


def test_a_readiness_timeout_with_the_container_present_is_drift(
    fake_driver, testsite_html, contract
):
    html = _with_row_count(testsite_html("v1/items.html"), 0)
    report = run(fake_driver(html_text=html, ready=False), contract)
    assert report.status == "DRIFT"
    assert _finding(report, "missing_selector", contract.ready_when).severity == "CRITICAL"
    # The readiness timeout took the elif branch in run(): the contract rules already named
    # this selector, so it must not be named a second time as a MAJOR "not ready" finding.
    selectors = [f.selector for f in report.findings if f.kind == "missing_selector"]
    assert len(selectors) == len(set(selectors))


def test_a_readiness_timeout_on_a_matching_page_still_fails_closed(
    fake_driver, testsite_html, contract
):
    report = run(fake_driver(html_text=testsite_html("v1/items.html"), ready=False), contract)
    assert report.status == "DRIFT"
    finding = _finding(report, "missing_selector", contract.ready_when)
    assert finding.severity == "MAJOR"
    assert finding.detail.startswith("readiness")


def test_a_good_page_through_the_driver_is_ok(fake_driver, testsite_html, contract):
    driver = fake_driver(html_text=testsite_html("v1/items.html"))
    report = run(driver, contract)
    assert report.status == "OK"
    assert driver.visited == [contract.url]


def test_the_result_is_journaled_with_its_status(
    fake_driver, testsite_html, contract, journal_sink
):
    run(fake_driver(html_text=testsite_html("v2/items.html")), contract, journal=journal_sink)
    name, level, payload = journal_sink.events[-1]
    assert name == "canary.result"
    assert level == "ERROR"
    assert payload["status"] == "DRIFT"
    assert payload["max_severity"] == "CRITICAL"
