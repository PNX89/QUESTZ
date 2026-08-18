"""Documentation integrity.

The README makes claims that only running the code can keep true, so this file runs them.
A README that has quietly drifted from the code is worse than no README, and on a repo
whose subject is drift detection it is also embarrassing.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import scenarios

from questz import cli

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
README_TEXT = README.read_text(encoding="utf-8")

# A ceiling, not a target. Past this the design decisions belong in docs/ rather than in
# the file somebody reads to decide whether to keep reading.
LINE_BUDGET = 480

_DOCUMENTED_COMMAND = re.compile(r"^uv run (questz .+?) +# exit (\d)$", re.MULTILINE)
_DETECTION_TABLE = re.compile(
    r"<!-- detection-table -->\n(.*?)\n<!-- /detection-table -->", re.DOTALL
)
_QUICKSTART = re.compile(r"<!-- quickstart -->\n```bash\n(.*?)```\n<!-- /quickstart -->", re.DOTALL)

# Every source the README leans on for a technical claim. Presence only: the suite contacts
# nothing outside this machine, so liveness is a release check, not a test.
REQUIRED_CITATIONS = (
    "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Synthetics_Canaries.html",
    "https://resilience4j.readme.io/docs/circuitbreaker",
    "https://martinfowler.com/bliki/CircuitBreaker.html",
    "https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/",
    "https://www.rfc-editor.org/rfc/rfc5861",
    "https://www.rfc-editor.org/rfc/rfc9309",
    "https://playwright.dev/python/docs/api/class-page",
    "https://playwright.dev/python/docs/aria-snapshots",
    "https://playwright.dev/python/docs/ci",
    "https://playwright.dev/python/docs/library",
    "https://playwright.dev/python/docs/test-runners",
    "https://github.com/scrapinghub/spidermon",
    "https://healenium.io/",
    "https://toscrape.com/",
    "https://docs.python.org/3/library/http.server.html",
    "https://lwn.net/Articles/682988/",
)


def _documented_commands() -> list[tuple[str, int]]:
    return [(command, int(code)) for command, code in _DOCUMENTED_COMMAND.findall(README_TEXT)]


@pytest.mark.parametrize(("command", "expected"), _documented_commands())
def test_every_documented_command_exits_the_way_the_readme_says(
    monkeypatch, capsys, command: str, expected: int
) -> None:
    """The README quotes exit codes as the interface, so the README's own commands are run."""
    monkeypatch.chdir(REPO_ROOT)
    argv = shlex.split(command)[1:]
    code = cli.main(argv)
    capsys.readouterr()
    assert code == expected, command


def test_the_readme_documents_all_four_exit_codes_by_running_three_of_them() -> None:
    commands = _documented_commands()
    assert len(commands) >= 3
    assert {code for _, code in commands} == {
        cli.EXIT_OK,
        cli.EXIT_DRIFT,
        cli.EXIT_BLOCKED,
    }


def test_the_quickstart_is_three_commands_ending_in_the_hero_check() -> None:
    found = _QUICKSTART.search(README_TEXT)
    assert found is not None, "the quickstart block is missing its markers"
    commands = [line for line in found.group(1).splitlines() if line.strip()]
    assert len(commands) == 3
    assert commands[0].startswith("git clone ")
    assert commands[1] == "uv sync"
    hero = _DOCUMENTED_COMMAND.match(commands[2])
    assert hero is not None, "the quickstart check must document its exit code"
    assert (hero.group(1), int(hero.group(2))) in _documented_commands()


def test_the_detection_table_matches_a_fresh_generation() -> None:
    found = _DETECTION_TABLE.search(README_TEXT)
    assert found is not None, "the detection table is missing its markers"
    assert found.group(1) == scenarios.table_markdown()


def test_every_technical_claim_keeps_its_citation() -> None:
    assert [url for url in REQUIRED_CITATIONS if url not in README_TEXT] == []


def test_the_readme_counts_the_tests_that_need_a_browser() -> None:
    """The number is in the README, so it is checked here rather than trusted. Every e2e
    test lives in one module under one module level marker, which is what makes it a fact
    about the tree rather than a fact about a particular run."""
    e2e = REPO_ROOT / "tests" / "test_e2e.py"
    stated = re.search(r"the (\d+) tests in\n`tests/test_e2e\.py`", README_TEXT)
    assert stated is not None, "the browser test count is missing from the README"
    assert int(stated.group(1)) == len(
        re.findall(r"^def test_", e2e.read_text(encoding="utf-8"), re.MULTILINE)
    )
    # Assembled rather than written out, so this file does not match its own search.
    marker = "pytest." + "mark.e2e"
    others = [
        path.name
        for path in (REPO_ROOT / "tests").glob("test_*.py")
        if path != e2e and marker in path.read_text(encoding="utf-8")
    ]
    assert others == [], "an e2e marker outside test_e2e.py makes the README count wrong"


def test_the_readme_stays_inside_its_length_budget() -> None:
    assert len(README_TEXT.splitlines()) <= LINE_BUDGET


@pytest.mark.parametrize("word", ["guarantee", "self healing locator", "prevents", "eliminates"])
def test_the_readme_makes_no_claim_it_cannot_keep(word: str) -> None:
    """`flags`, `fails closed on` and `surfaces` are claims about behaviour. The words here
    are claims about outcomes, and this tool cannot make them."""
    lowered = README_TEXT.lower()
    # Healenium is named as prior art, so the phrase is allowed exactly where it describes
    # somebody else's tool.
    allowed = 1 if word == "self healing locator" else 0
    assert lowered.count(word) == allowed
