from __future__ import annotations

import json
from pathlib import Path

import pytest

from questz import __version__, cli
from questz.canary import load


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _check(capsys, contract_path: Path, html: Path, *extra: str) -> tuple[int, str, str]:
    return _run(
        capsys, "canary", "check", "--contract", str(contract_path), "--html", str(html), *extra
    )


def test_the_recorded_page_exits_zero(capsys, contract_path, testsite_file):
    code, stdout, stderr = _check(capsys, contract_path, testsite_file("v1/items.html"))
    assert code == cli.EXIT_OK
    assert "questz canary: OK" in stdout
    assert stderr == ""


def test_the_redeploy_exits_one_and_names_the_selector(capsys, contract_path, testsite_file):
    code, stdout, _ = _check(capsys, contract_path, testsite_file("v2/items.html"))
    assert code == cli.EXIT_DRIFT
    assert "questz canary: DRIFT" in stdout
    assert 'td[data-testid="price"]' in stdout
    assert "CRITICAL" in stdout


def test_fail_on_major_lets_a_warning_only_page_through_with_exit_zero(
    capsys, contract_path, testsite_file, tmp_path
):
    """A wrapper div around the table is one of the four most ordinary benign changes on a
    real page. It is worth printing and, for some operators, not worth stopping the job."""
    wrapped = tmp_path / "wrapped.html"
    wrapped.write_text(
        testsite_file("v1/items.html")
        .read_text(encoding="utf-8")
        .replace(
            '<table data-testid="items-table" class="grid">',
            '<div class="table-wrap"><table data-testid="items-table" class="grid">',
            1,
        )
        .replace("</table>", "</table></div>", 1),
        encoding="utf-8",
    )
    strict, strict_out, _ = _check(capsys, contract_path, wrapped)
    relaxed, relaxed_out, _ = _check(capsys, contract_path, wrapped, "--fail-on", "major")
    assert strict == cli.EXIT_DRIFT
    assert relaxed == cli.EXIT_OK
    assert "structure_moved" in strict_out
    assert "structure_moved" in relaxed_out, "a threshold decides the exit code, not the report"


def test_an_interstitial_exits_three(capsys, contract_path, testsite_file):
    """A page that never arrived and a page that changed need different pager behaviour."""
    code, stdout, _ = _check(capsys, contract_path, testsite_file("v2i/items.html"))
    assert code == cli.EXIT_BLOCKED
    assert "questz canary: BLOCKED" in stdout
    assert "consent-gate" in stdout


def test_json_is_additive_and_the_text_report_still_prints(
    capsys, contract_path, testsite_file, tmp_path
):
    out = tmp_path / "report.json"
    code, stdout, _ = _check(
        capsys, contract_path, testsite_file("v2/items.html"), "--json", str(out), "--deterministic"
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert code == cli.EXIT_DRIFT
    assert "questz canary: DRIFT" in stdout
    assert payload["status"] == "DRIFT"
    assert payload["max_severity"] == "CRITICAL"
    assert payload["questz_version"] == __version__
    assert payload["generated_at"] == "2026-01-01T00:00:00.000Z"
    assert len(payload["findings"]) == 4


def test_a_missing_contract_exits_two_with_one_readable_line(capsys, tmp_path, testsite_file):
    code, stdout, stderr = _check(capsys, tmp_path / "absent.json", testsite_file("v1/items.html"))
    assert code == cli.EXIT_USAGE
    assert stdout == ""
    assert stderr.splitlines() == [f"questz: contract file not found: {tmp_path / 'absent.json'}"]
    assert "Traceback" not in stderr


def test_an_unsupported_selector_in_a_contract_exits_two_naming_the_fragment(
    capsys, contract_path, testsite_file, tmp_path
):
    raw = json.loads(contract_path.read_text(encoding="utf-8"))
    raw["required"][0]["selector"] = "tr:nth-child(2)"
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(raw), encoding="utf-8")
    code, _, stderr = _check(capsys, broken, testsite_file("v1/items.html"))
    assert code == cli.EXIT_USAGE
    assert "tr:nth-child(2)" in stderr
    assert "pseudo classes" in stderr


def test_a_contract_selector_with_no_attribute_name_exits_two_rather_than_one(
    capsys, contract_path, testsite_file, tmp_path
):
    """A malformed contract is an operator fix, and the code for that is 2. This one raised
    an IndexError from inside the error message it was building, which cli.main does not
    catch, so the process exited 1 and a scheduler read a typo in a JSON file as the site
    having been redeployed."""
    raw = json.loads(contract_path.read_text(encoding="utf-8"))
    raw["container"] = '[="items-table"]'
    broken = tmp_path / "no-attribute-name.json"
    broken.write_text(json.dumps(raw), encoding="utf-8")
    code, _, stderr = _check(capsys, broken, testsite_file("v1/items.html"))
    assert code == cli.EXIT_USAGE
    assert code != cli.EXIT_DRIFT
    assert '[="items-table"]' in stderr


def test_an_unreadable_html_file_exits_two(capsys, contract_path, tmp_path):
    code, _, stderr = _check(capsys, contract_path, tmp_path / "gone.html")
    assert code == cli.EXIT_USAGE
    assert "gone.html" in stderr


def _unreadable(tmp_path: Path, kind: str, name: str) -> Path:
    path = tmp_path / name
    if kind == "a directory":
        path.mkdir()
    else:
        # UTF-16 with a BOM: what a Windows editor writes, and undecodable as UTF-8.
        path.write_bytes("{}".encode("utf-16"))
    return path


@pytest.mark.parametrize("kind", ["a directory", "not utf-8"])
def test_a_contract_that_cannot_be_read_exits_two_rather_than_reporting_drift(
    capsys, tmp_path, testsite_file, kind
):
    """Exit 1 means DRIFT to whatever runs this, so a contract path naming a directory or a
    file in the wrong encoding has to land on 2 instead, and it has to do it without a
    traceback: UnicodeDecodeError is a ValueError and slips past every `except OSError`."""
    code, stdout, stderr = _check(
        capsys, _unreadable(tmp_path, kind, "contract"), testsite_file("v1/items.html")
    )
    assert code == cli.EXIT_USAGE
    assert stdout == ""
    assert len(stderr.splitlines()) == 1
    assert "Traceback" not in stderr


@pytest.mark.parametrize("kind", ["a directory", "not utf-8"])
def test_an_html_target_that_cannot_be_read_exits_two(capsys, contract_path, tmp_path, kind):
    code, stdout, stderr = _check(capsys, contract_path, _unreadable(tmp_path, kind, "page.html"))
    assert code == cli.EXIT_USAGE
    assert stdout == ""
    assert "page.html" in stderr
    assert "Traceback" not in stderr


@pytest.mark.parametrize("kind", ["a directory", "not utf-8"])
def test_a_journal_that_cannot_be_read_exits_two(capsys, tmp_path, kind):
    code, stdout, stderr = _run(capsys, "report", str(_unreadable(tmp_path, kind, "run.jsonl")))
    assert code == cli.EXIT_USAGE
    assert stdout == ""
    assert "run.jsonl" in stderr
    assert "Traceback" not in stderr


def test_record_writes_a_contract_the_checker_then_accepts(capsys, tmp_path, testsite_file):
    out = tmp_path / "recorded.json"
    row = 'tr[data-testid="item-row"]'
    code, stdout, _ = _run(
        capsys,
        "canary",
        "record",
        "--html",
        str(testsite_file("v1/items.html")),
        "--name",
        "items",
        "--container",
        '[data-testid="items"]',
        "--ready-when",
        row,
        "--out",
        str(out),
        "--selector",
        '[data-testid="items-table"]',
        "--selector",
        f"{row}:5:50",
        "--field",
        f'price:{row} td[data-testid="price"]:decimal',
        "--field",
        f'stock:{row} td[data-testid="stock"]:enum:in-stock,low,out-of-stock',
        "--secret",
        '[data-testid="password"]',
    )
    assert code == cli.EXIT_OK
    assert "Widen the ranges by hand" in stdout
    contract = load(out)
    pinned, widened = contract.required
    assert (pinned.min_count, pinned.max_count) == (1, 1)
    assert (widened.min_count, widened.max_count) == (5, 50)
    assert contract.fields[1].allowed == ("in-stock", "low", "out-of-stock")
    assert contract.secret_selectors == ('[data-testid="password"]',)
    assert _check(capsys, out, testsite_file("v1/items.html"))[0] == cli.EXIT_OK
    assert _check(capsys, out, testsite_file("v2/items.html"))[0] == cli.EXIT_DRIFT


@pytest.mark.parametrize(
    "field",
    ["price:td.price", "price:td.price:money", "stock:td.stock:enum"],
)
def test_a_malformed_field_argument_exits_two(capsys, tmp_path, testsite_file, field):
    code, _, stderr = _run(
        capsys,
        "canary",
        "record",
        "--html",
        str(testsite_file("v1/items.html")),
        "--name",
        "items",
        "--container",
        '[data-testid="items"]',
        "--ready-when",
        'tr[data-testid="item-row"]',
        "--out",
        str(tmp_path / "out.json"),
        "--field",
        field,
    )
    assert code == cli.EXIT_USAGE
    assert field in stderr


def test_report_prints_the_step_summary(capsys, tmp_path, fake_clock):
    from questz.journal import Journal

    journal = Journal(tmp_path / "run.jsonl", run_id="questz-demo", clock=fake_clock)
    with journal.step("fetch"):
        pass
    journal.close()
    code, stdout, _ = _run(capsys, "report", str(tmp_path / "run.jsonl"))
    assert code == cli.EXIT_OK
    assert "run questz-demo" in stdout
    assert "steps (1):" in stdout
    assert "fetch" in stdout


def test_report_on_a_missing_journal_exits_two(capsys, tmp_path):
    code, _, stderr = _run(capsys, "report", str(tmp_path / "nothing.jsonl"))
    assert code == cli.EXIT_USAGE
    assert "nothing.jsonl" in stderr


def test_version_prints_the_package_version(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == f"questz {__version__}"


@pytest.mark.parametrize(
    "argv",
    [
        ["canary", "check", "--contract", "c.json"],
        ["canary", "check", "--contract", "c.json", "--html", "a.html", "--url", "http://x"],
        ["serve", "--variant", "v9"],
        ["demo", "--scenario", "sideways"],
        [],
    ],
)
def test_argument_errors_exit_two(argv):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == cli.EXIT_USAGE
