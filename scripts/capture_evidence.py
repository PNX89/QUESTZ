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
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "evidence"
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


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
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
