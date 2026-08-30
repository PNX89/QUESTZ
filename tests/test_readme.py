"""Documentation integrity.

The README makes claims that only running the code can keep true, so this file runs them.
A README that has quietly drifted from the code is worse than no README, and on a repo
whose subject is drift detection it is also embarrassing.
"""

from __future__ import annotations

import html
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import scenarios

from questz import __version__, cli

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
README_TEXT = README.read_text(encoding="utf-8")

# A ceiling, not a target. Past this the design decisions belong in docs/ rather than in
# the file somebody reads to decide whether to keep reading.
# Raised from 480 to 486 on 24-8-2026, and the reason is recorded because a budget quietly
# raised is a budget that has stopped meaning anything. The six lines are the animated frame in
# the first screenful and its caption, which the build standard requires every README to carry.
# Raised again from 486 to 499 on 30-8-2026, and it bought thirteen lines of checking. Eight are
# the marker pairs around the hero output and the three scenario transcripts, which render as
# nothing and exist so that those 82 lines are diffed against a live run instead of trusted; one
# says so in the prose; four name the command that regenerates the evidence screenshot and what
# now reads its pixels back. Nothing was trimmed to make room and nothing was allowed to grow:
# the next unexplained line still fails this test.
LINE_BUDGET = 499
TOOLSET_START = "<!-- toolset:start -->"
TOOLSET_END = "<!-- toolset:end -->"

_DOCUMENTED_COMMAND = re.compile(r"^uv run (questz .+?) +# exit (\d)$", re.MULTILINE)
_DETECTION_TABLE = re.compile(
    r"<!-- detection-table -->\n(.*?)\n<!-- /detection-table -->", re.DOTALL
)
_QUICKSTART = re.compile(r"<!-- quickstart -->\n```bash\n(.*?)```\n<!-- /quickstart -->", re.DOTALL)
_HERO = re.compile(r"<!-- hero-output -->\n```console\n(.*?)```\n<!-- /hero-output -->", re.DOTALL)

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
    """The budget polices prose, so the generated cross-link block does not count.

    That block is written by `toolset_block.py` from one manifest shared across the toolset,
    and it grows by a line every time another repository ships. Counting it would mean a
    README that never changed could fail this test because a different repository was
    published, and the author would then be tempted to raise the budget, which is the one
    thing it exists to stop.
    """
    text = README_TEXT
    start, end = text.find(TOOLSET_START), text.find(TOOLSET_END)
    assert start != -1 and end > start, "the generated cross-link block is missing"
    prose = text[:start] + text[end + len(TOOLSET_END) :]
    assert len(prose.splitlines()) <= LINE_BUDGET


@pytest.mark.parametrize("word", ["guarantee", "self healing locator", "prevents", "eliminates"])
def test_the_readme_makes_no_claim_it_cannot_keep(word: str) -> None:
    """`flags`, `fails closed on` and `surfaces` are claims about behaviour. The words here
    are claims about outcomes, and this tool cannot make them."""
    lowered = README_TEXT.lower()
    # Healenium is named as prior art, so the phrase is allowed exactly where it describes
    # somebody else's tool.
    allowed = 1 if word == "self healing locator" else 0
    assert lowered.count(word) == allowed


def _escaped(text: str) -> str:
    """The card is HTML, so the captured output appears in it escaped, not raw."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


DEMO_COMMAND = (
    "questz canary check --contract examples/contracts/items.json "
    '--html questz/testsite/v2/items.html; echo "exit $?"'
)


def test_the_committed_demo_output_still_matches_a_live_run() -> None:
    """The Pages card publishes this output, so a stale copy is a lie on a public page.

    Run through a shell, because the documented command ends in `echo "exit $?"` and the exit
    code is the part a CI job upstream of a data write actually consumes. A capture that
    swallowed the non-zero exit would publish the drift report and drop its conclusion.
    """
    committed = (REPO_ROOT / "docs" / "evidence" / "demo.txt").read_text(encoding="utf-8")
    live = subprocess.run(
        DEMO_COMMAND, shell=True, capture_output=True, text=True, timeout=600, cwd=REPO_ROOT
    ).stdout
    assert committed == live, (
        "docs/evidence/demo.txt no longer matches a live run. "
        "Run: uv run python scripts/capture_evidence.py, then regenerate the card."
    )
    # DRIFT is the outcome this page exists to show. If the fixture pages ever stopped
    # disagreeing, the card would still be fresh and would no longer demonstrate anything.
    assert "exit 1" in committed and "questz canary: DRIFT" in committed


def test_the_hero_output_block_is_the_run_that_was_captured() -> None:
    """The block a reader forms their opinion from, and the one nothing re-ran.

    Four copies of this transcript exist. `docs/evidence/demo.txt` is diffed against a live run
    by the test above, the Pages card is diffed against demo.txt, the animated frame is diffed
    against demo.txt, and the README block a person actually reads was typed. It could be
    falsified to say OK, nine findings and exit 0 with the whole suite green, on a repository
    whose subject is documents drifting away from the code they describe.

    Compared to demo.txt rather than to a fresh run of its own, because demo.txt is already
    held to a live run and one capture is enough: this asserts the README quotes the capture.
    """
    found = _HERO.search(README_TEXT)
    assert found is not None, "the hero output block is missing its markers"
    quoted = found.group(1).splitlines()
    assert quoted[0] == f"$ uv run {DEMO_COMMAND}", (
        "the block does not open on the documented command"
    )
    committed = (REPO_ROOT / "docs" / "evidence" / "demo.txt").read_text(encoding="utf-8")
    assert quoted[1:] == committed.splitlines(), (
        "the README's hero transcript is no longer the captured output. "
        "Run: uv run python scripts/capture_evidence.py, then paste docs/evidence/demo.txt back."
    )


def test_the_published_card_carries_the_output_it_claims_to() -> None:
    card = (REPO_ROOT / "site" / "index.html").read_text(encoding="utf-8")
    demo = (REPO_ROOT / "docs" / "evidence" / "demo.txt").read_text(encoding="utf-8")
    assert _escaped(demo.rstrip()) in card, "the card's terminal block is not the captured output"
    assert "a test fails when it" in card
    assert "/Users/" not in card and "/var/folders/" not in card


def test_the_card_states_numbers_that_are_true_today() -> None:
    facts = json.loads((REPO_ROOT / "docs" / "evidence" / "facts.json").read_text(encoding="utf-8"))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
        cwd=REPO_ROOT,
    )
    match = re.search(r"^(\d+) tests? collected", result.stdout, re.MULTILINE)
    assert match is not None, f"no collection total in:\n{result.stdout[-400:]}"
    assert facts["tests"] == int(match.group(1)), "facts.json's test total is stale"
    # Against the package version, never `git describe`: actions/checkout clones without tags.
    assert facts["release"] == f"v{__version__}"
    card = (REPO_ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert f"<dd>{facts['tests']}</dd>" in card
    assert f"<dd>{facts['release']}</dd>" in card


def test_the_readme_frame_is_built_from_the_captured_output() -> None:
    """The animated frame in the first screenful has to be the real run, not a picture of one.

    Every text line the SVG draws, minus the prompt line it adds and the truncation note it
    ends with, must appear in the captured output in the same order. Written this way rather
    than by re-deriving the generator's truncation arithmetic, because a test that reimplements
    the thing it checks passes for the wrong reason.
    """
    svg = (REPO_ROOT / "docs" / "demo.svg").read_text(encoding="utf-8")
    demo = (REPO_ROOT / "docs" / "evidence" / "demo.txt").read_text(encoding="utf-8")

    drawn = [html.unescape(m) for m in re.findall(r"<text[^>]*>(.*?)</text>", svg, re.DOTALL)]
    assert drawn, "the frame draws no text at all"
    assert drawn[0].startswith("$ "), "the frame does not open on the command it ran"
    assert drawn[-1].startswith("... ") and "more lines" in drawn[-1]

    body = [line for line in drawn[1:-2] if line.strip()]
    haystack = demo.splitlines()
    position = 0
    for line in body:
        stem = line[:-3] if line.endswith("...") else line
        while position < len(haystack) and not haystack[position].startswith(stem):
            position += 1
        assert position < len(haystack), f"the frame draws a line the run never printed: {line!r}"
        position += 1

    # ASCII only, and stricter than the tree scan in test_bytes.py, which admits the euro sign
    # and four space characters because a scraped page carries them. None of those belongs in a
    # generated image: the frame is built by code, so a non ASCII glyph would arrive from a code
    # change rather than from anyone typing one, and it is served through a proxy that renders it
    # where nobody can select the character and look at it. The comment that used to be here
    # named a test in a sibling repository and said it covered this tree.
    assert svg.isascii()
    assert "<script" not in svg, "a README image is served through a proxy that strips script"


def test_the_ci_shape_the_readme_describes_is_the_one_in_the_workflow() -> None:
    """The paragraph used to name three jobs, `lint`, `unit` and `e2e`, and there are two.

    Every command it listed did run, as steps inside one job, so nothing was broken and nothing
    was untrue about the CHECKING. What was wrong was the shape: a reviewer told to look for
    three jobs opens the Actions tab, finds two, and now has to work out which of the two claims
    in front of them is the reliable one. That is a worse outcome than a missing sentence.

    Parsed from the workflow rather than matched as a string, because a job name inside a comment
    would satisfy a substring search, which is a defect this portfolio has already had once.
    """
    import yaml

    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    jobs = set(workflow["jobs"])
    assert jobs == {"checks", "e2e"}, f"the workflow now defines {sorted(jobs)}"

    paragraph = " ".join(README.read_text(encoding="utf-8").split())
    assert "CI is two jobs." in paragraph
    for job in sorted(jobs):
        assert f"`{job}`" in paragraph, f"the README does not name the {job} job"
    assert "three jobs" not in paragraph
    for absent in ("`lint`", "`unit`"):
        assert absent not in paragraph, (
            f"the README names a job called {absent}, and the workflow has no such job"
        )

    e2e = workflow["jobs"]["e2e"]
    assert e2e["timeout-minutes"] == 10, "the README states a 10 minute timeout"
    versions = str(workflow["jobs"]["checks"]["with"]["python-versions"])
    for version in ("3.11", "3.12", "3.13", "3.14"):
        assert version in versions and version in paragraph
