"""The browser leg.

Nothing here imports playwright: the `page` fixture comes from pytest-playwright, and
conftest skips this whole file when there is no chromium to launch.

Screenshots are not compared to a reference image, because headless Chromium font rendering
differs across operating systems and browser builds. That argument covers text, and it was
quietly read as covering the mask as well, which left the one security claim in this repo
standing on an assertion that could not fail. A solid rectangle sampled at its centre, in a
file the same browser wrote seconds earlier, has none of that fragility.
"""

from __future__ import annotations

import base64
import itertools
import json
import random
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pricewatch
import pytest

from questz.breaker import Breaker, BreakerPolicy, BreakerState, RetryPolicy
from questz.cache import Cache
from questz.canary import load
from questz.clock import FakeClock
from questz.driver import PlaywrightDriver
from questz.journal import Journal, read_run, render_report
from questz.normalize import matches

pytestmark = pytest.mark.e2e

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# The colour the README's prose and its committed evidence screenshot both claim the mask is.
# Written here rather than imported from the driver, so painting a different colour is a
# difference between the code and the document rather than a rename that agrees with itself.
MASK = (0, 0, 0)

# Decoded by the browser that wrote the file, so nothing in this suite carries a PNG reader.
# An image from a data: URL does not taint the canvas. Points are in image pixels: a full page
# shot taken at scroll zero puts a CSS pixel at the device pixel ratio, and each caller knows
# which ratio applies to the file it is asking about.
_READ_PIXELS = """
async ([dataUrl, points]) => {
  const image = new Image();
  image.src = dataUrl;
  await image.decode();
  const canvas = document.createElement('canvas');
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  const context = canvas.getContext('2d');
  context.drawImage(image, 0, 0);
  return points.map(([x, y]) => {
    const pixel = context.getImageData(Math.round(x), Math.round(y), 1, 1).data;
    return [pixel[0], pixel[1], pixel[2]];
  });
}
"""
REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
EVIDENCE = REPO_ROOT / "docs" / "evidence"
CONTRACT = REPO_ROOT / "examples" / "contracts" / "items.json"
_ROLLUP = re.compile(r"<!-- report-rollup -->\n```console\n(.*?)```\n<!-- /report-rollup -->", re.S)
_SCENARIO_NAMES = re.compile(r"<!-- scenario-(\w+) -->")
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


def _middles(boxes: list[dict], ratio: float) -> list[list[float]]:
    """The middle of each box, in image pixels. The middle, because that is where a few pixels
    of font driven layout drift cannot reach the edge of the rectangle the browser painted."""
    return [
        [(box["x"] + box["width"] / 2) * ratio, (box["y"] + box["height"] / 2) * ratio]
        for box in boxes
    ]


def _centres(page: Any, selectors: tuple[str, ...]) -> list[list[float]]:
    boxes = [page.locator(selector).bounding_box() for selector in selectors]
    assert all(box is not None for box in boxes), f"a secret selector renders nothing: {selectors}"
    return _middles(boxes, page.evaluate("() => window.devicePixelRatio"))


def _colours(page: Any, shot: Path, points: list[list[float]]) -> list[tuple[int, ...]]:
    data_url = "data:image/png;base64," + base64.b64encode(shot.read_bytes()).decode("ascii")
    return [tuple(pixel) for pixel in page.evaluate(_READ_PIXELS, [data_url, points])]


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
    page, testsite_server, contract, harness, mask_of
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
        # By name: the job takes two shots, and reading back whichever was last meant the one
        # taken with the password on the screen was covered by nothing.
        assert mask_of(driver.shots, name) == contract.secret_selectors
    assert contract.secret_selectors != ()


def test_the_mask_is_painted_into_the_login_screenshot_not_only_passed_to_the_call(
    page, testsite_server, contract, harness
):
    """What stood here read `driver.last_mask`, an attribute the driver sets from its own
    argument on the line above the call, so it stayed true whatever the browser then did.
    Handing playwright an empty mask list left the entire suite green while the file on disk
    showed the password in plain text.

    So this asks the file. The unmasked control is what proves the reader is reading rather
    than answering black to everything, and pure black is only available from the mask: the
    darkest ink the bundled site paints is #14202b.
    """
    driver = PlaywrightDriver(page)
    driver.goto(f"{testsite_server('v1')}/login.html")
    driver.fill(pricewatch.USERNAME_FIELD, pricewatch.DEMO_USER)
    driver.fill(pricewatch.PASSWORD_FIELD, pricewatch.DEMO_PASSWORD)
    masked = driver.screenshot(harness.out / "masked.png", mask=contract.secret_selectors)
    # The control is the same page with nothing painted over it. It carries the demo
    # credentials the bundled site prints on its own login page and nothing else.
    control = driver.screenshot(harness.out / "unmasked-control.png")

    assert pricewatch.PASSWORD_FIELD in contract.secret_selectors
    centres = _centres(page, contract.secret_selectors)
    assert len(centres) == 3, "the contract masks the two fields and the hint line"
    assert _colours(page, masked, centres) == [MASK] * 3
    assert MASK not in _colours(page, control, centres)


def test_the_committed_evidence_screenshot_is_black_where_it_recorded_painting(page):
    """The README says of this file that it "is the login page with both credential fields
    filled, which is what makes the claim checkable rather than asserted". Nothing checked it,
    and it was the one committed artefact with no command behind it.

    The boxes come from the run that painted them, recorded beside the image by
    `scripts/capture_evidence.py --screenshot`, rather than from a live layout: this file was
    rendered by whichever machine last ran that command, and only the geometry it carries can
    be compared against safely on another one.
    """
    recorded = json.loads((EVIDENCE / "screenshot.json").read_text(encoding="utf-8"))
    shot = REPO_ROOT / "docs" / "evidence-login-masked.png"
    width, height = struct.unpack(">II", shot.read_bytes()[16:24])
    assert (width, height) == (recorded["width"], recorded["height"]), (
        "the committed screenshot is not the one the recorded geometry describes. "
        "Run: uv run python scripts/capture_evidence.py --screenshot"
    )
    painted = [box["selector"] for box in recorded["masked"]]
    assert painted == list(load(CONTRACT).secret_selectors), "the mask list has moved on"
    centres = _middles(recorded["masked"], recorded["device_pixel_ratio"])
    assert _colours(page, shot, centres) == [MASK] * len(centres)


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


def _scenario_block(scenario: str) -> list[str]:
    readme = README.read_text(encoding="utf-8")
    # Pinned by name and by size. Reading the list of scenarios out of the README would mean
    # that deleting a block covered one fewer and stayed green, which reads exactly like a pass,
    # and adding a fourth would ship a transcript nothing runs.
    assert _SCENARIO_NAMES.findall(readme) == ["happy", "drift", "degrade"]
    pattern = rf"<!-- scenario-{scenario} -->\n```console\n(.*?)```\n<!-- /scenario-{scenario} -->"
    found = re.search(pattern, readme, re.S)
    assert found is not None, f"the {scenario} transcript is missing its markers"
    # The command line is the reader's instruction, not output. Everything after it is output.
    return [line for line in found.group(1).splitlines() if not line.startswith("$ ")]


@pytest.mark.parametrize("scenario", ["happy", "drift", "degrade"])
def test_the_scenario_transcripts_in_the_readme_come_out_of_a_real_run(tmp_path, scenario):
    """82 of the README's 89 lines of console output were compared to nothing.

    The rollup above was inside markers and diffed; these three were pasted, so any of them
    could be edited to say the opposite of what the tool does with the suite green. On a
    repository whose own line twelve lines above them says "a transcript nothing re-runs is a
    transcript that has already drifted".

    The out directory is substituted back rather than the paths being skipped, because
    "artifacts/drift/items.csv does not exist" is one of the claims being checked.
    """
    quoted = _scenario_block(scenario)
    demo = subprocess.run(
        [
            sys.executable,
            "-m",
            "questz.cli",
            "demo",
            "--scenario",
            scenario,
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
    printed = [
        _PORT.sub("127.0.0.1", line.replace(str(tmp_path), "artifacts"))
        for line in demo.stdout.splitlines()
    ]
    assert printed, demo.stderr
    # The tail, because the drift block is quoted through `tail -6`.
    assert printed[-len(quoted) :] == [_PORT.sub("127.0.0.1", line) for line in quoted]
    # The exit code the transcript's own last line states, against the one the process
    # returned. Read out of the line that carries it rather than searched for in the block,
    # because a 27 line transcript contains every digit somewhere.
    assert quoted[-1].startswith("exit:"), quoted[-1]
    assert demo.returncode == int(quoted[-1].removeprefix("exit:"))


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
