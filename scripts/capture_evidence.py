"""Capture the demo's real output and the numbers the Pages card states.

WHY THIS EXISTS. The card at pnx89.github.io/QUESTZ shows the output of a real run and four
numbers about this repository. Both are committed, which means both can go stale.
`tests/test_readme.py` fails when what is committed stops matching a live run.

THE DEMO EXITS 1 AND THE EXIT CODE IS THE POINT. `questz canary check` returns 0 for OK, 1 for
DRIFT, 2 for a usage error and 3 for UNAVAILABLE or BLOCKED, and that contract is what a CI
job upstream of a data write actually consumes. So the capture runs the command through a
shell and echoes `$?` into the output, exactly as the README presents it, rather than treating
the non-zero exit as a failure to be swallowed. A capture that hid it would publish the report
and drop the only part a pipeline reads.

The browserless target is deliberate too: this runs against a committed fixture page, so the
card needs no browser, no network and no third party site that could change under it.

    uv run python scripts/capture_evidence.py
    uv run python scripts/capture_evidence.py --screenshot   # also the masked login image
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import struct
import subprocess
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "evidence"
SCREENSHOT = ROOT / "docs" / "evidence-login-masked.png"
# The README embeds the screenshot at the width of the form, so the frame is the claim rather
# than a screenful of white space under it.
VIEWPORT = {"width": 1280, "height": 300}
DEMO = (
    "questz canary check --contract examples/contracts/items.json "
    '--html questz/testsite/v2/items.html; echo "exit $?"'
)
# The documented codes. Anything outside this set means the demo broke rather than found drift.
EXPECTED_EXITS = {0, 1, 3}


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, cwd=ROOT, timeout=600)
    if result.returncode:
        raise SystemExit(f"{args[0]} failed: {result.stderr.strip()[:400]}")
    return result.stdout


def capture_demo() -> str:
    """Through a shell, so the `echo "exit $?"` in the documented command actually runs."""
    result = subprocess.run(DEMO, shell=True, capture_output=True, text=True, cwd=ROOT, timeout=600)
    text = result.stdout
    match = re.search(r"^exit (\d+)$", text, re.MULTILINE)
    if not match:
        raise SystemExit(f"the demo output carries no exit line:\n{text[-400:]}")
    code = int(match.group(1))
    if code not in EXPECTED_EXITS:
        raise SystemExit(f"the demo exited {code}, which is not a documented outcome")
    return text


def test_total() -> int:
    out = run(sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q")
    match = re.search(r"^(\d+) tests? collected", out, re.MULTILINE)
    if not match:
        raise SystemExit(f"could not read a collection total from:\n{out[-400:]}")
    return int(match.group(1))


def python_range() -> str:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    versions = sorted(set(re.findall(r'"(3\.\d+)"', workflow)), key=lambda v: int(v[2:]))
    if not versions:
        raise SystemExit("no Python versions found in the CI matrix")
    return f"{versions[0]} to {versions[-1]}"


def release() -> str:
    from questz import __version__

    tag = f"v{__version__}"
    described = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"], capture_output=True, text=True, cwd=ROOT
    )
    if described.returncode == 0 and described.stdout.strip() != tag:
        raise SystemExit(
            f"the newest tag is {described.stdout.strip()} but the version is {__version__}"
        )
    return tag


def capture_screenshot() -> dict[str, object]:
    """Regenerate the committed evidence screenshot, and record where it painted.

    THIS IS THE ONE COMMITTED ARTEFACT THAT HAD NO COMMAND BEHIND IT. It turns out to be
    genuine: this reproduces the committed file byte for byte. That was not knowable before,
    which is the whole complaint, and it only reproduces at the viewport the frame was taken
    at, so the viewport is named above rather than left to whatever the default is that year.

    The geometry travels with the image because that is what makes the image checkable on
    another machine. Comparing the file to a fresh render would be comparing font rendering
    across operating systems, which is the reason this suite compares no screenshots; measuring
    the boxes on the reader's own layout instead has the same problem one step later. Boxes
    recorded by the run that painted them do not.

    Opt in, so the rest of this script stays browserless: the card's numbers and its terminal
    block need no browser, no network and no third party site, and that is worth keeping.
    """
    from playwright.sync_api import sync_playwright

    # examples/ ships with the checkout rather than the wheel, the same way `questz demo`
    # reaches it.
    sys.path.insert(0, str(ROOT / "examples"))
    import pricewatch

    from questz.canary import load
    from questz.driver import MASK_COLOUR, PlaywrightDriver
    from questz.testsite import serve

    contract = load(ROOT / "examples" / "contracts" / "items.json")
    server, base_url = serve("v1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as engine:
            browser = engine.chromium.launch()
            try:
                page = browser.new_page(viewport=VIEWPORT)
                driver = PlaywrightDriver(page)
                driver.goto(f"{base_url}/login.html")
                driver.fill(pricewatch.USERNAME_FIELD, pricewatch.DEMO_USER)
                driver.fill(pricewatch.PASSWORD_FIELD, pricewatch.DEMO_PASSWORD)
                driver.screenshot(SCREENSHOT, mask=contract.secret_selectors)
                boxes = []
                for selector in contract.secret_selectors:
                    box = page.locator(selector).bounding_box()
                    if box is None:
                        raise SystemExit(f"{selector} renders nothing, so it masked nothing")
                    boxes.append({"selector": selector, **box})
                ratio = page.evaluate("() => window.devicePixelRatio")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
    width, height = struct.unpack(">II", SCREENSHOT.read_bytes()[16:24])
    return {
        "colour": MASK_COLOUR,
        "device_pixel_ratio": ratio,
        "height": height,
        "masked": boxes,
        "width": width,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture the demo output and the card's numbers.")
    parser.add_argument(
        "--screenshot",
        action="store_true",
        help="also regenerate docs/evidence-login-masked.png, which needs a browser",
    )
    args = parser.parse_args(argv)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if args.screenshot:
        recorded = capture_screenshot()
        (EVIDENCE / "screenshot.json").write_text(
            json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {SCREENSHOT} and {EVIDENCE / 'screenshot.json'}")
    output = capture_demo()
    if "/Users/" in output or "/var/folders/" in output:
        raise SystemExit("the demo output carries a machine specific path, refusing")
    (EVIDENCE / "demo.txt").write_text(output, encoding="utf-8")

    run_id = os.environ.get("GITHUB_RUN_ID")
    slug = os.environ.get("GITHUB_REPOSITORY", "PNX89/QUESTZ")
    facts = {
        "tests": test_total(),
        "python": python_range(),
        "release": release(),
        "captured": datetime.date.today().isoformat(),
        "runUrl": f"https://github.com/{slug}/actions/runs/{run_id}" if run_id else None,
    }
    (EVIDENCE / "facts.json").write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {EVIDENCE / 'demo.txt'} ({len(output.splitlines())} lines)")
    print(f"wrote {EVIDENCE / 'facts.json'} {facts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
