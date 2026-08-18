"""The command line surface.

The exit code is the product. A scheduler has to page differently for "the site changed"
and "the site is down", so those are 1 and 3, never a shared non zero.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC
from pathlib import Path
from types import ModuleType
from typing import cast

from questz import __version__
from questz.cache import atomic_write_bytes
from questz.canary import (
    DECIMAL_SEPARATORS,
    DEFAULT_FAIL_ON,
    FieldRule,
    FieldShape,
    SelectorRule,
    check_html,
    record,
    run,
)
from questz.canary import load as load_contract
from questz.canary import save as save_contract
from questz.clock import SYSTEM_CLOCK, Clock, FakeClock
from questz.driver import PlaywrightDriver
from questz.journal import read_run, render_report
from questz.testsite import VARIANTS, serve
from questz.types import SEVERITY_ORDER, QuestzError, Severity

__all__ = [
    "BROWSER_HINT",
    "EXIT_BLOCKED",
    "EXIT_DRIFT",
    "EXIT_OK",
    "EXIT_USAGE",
    "STATUS_EXIT",
    "main",
]

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3

STATUS_EXIT: dict[str, int] = {
    "OK": EXIT_OK,
    "DRIFT": EXIT_DRIFT,
    "UNAVAILABLE": EXIT_BLOCKED,
    "BLOCKED": EXIT_BLOCKED,
}

_SHAPES: tuple[str, ...] = ("decimal", "integer", "text", "enum")
_SCENARIOS = ("happy", "drift", "degrade")
BROWSER_HINT = (
    "this path needs a browser: run 'uv sync --extra playwright'"
    " then 'uv run playwright install --only-shell chromium'"
)


class _Usage(QuestzError):
    """Anything the operator fixes by editing the command line."""


def _clock(args: argparse.Namespace) -> Clock:
    return FakeClock() if getattr(args, "deterministic", False) else SYSTEM_CLOCK


def _stamp(clock: Clock) -> str:
    return clock.now().astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # A ValueError, not an OSError. Every "unreadable file" handler that lists only
        # OSError lets this one out as a traceback carrying the wrong exit code.
        raise _Usage(f"{label} {path}: not valid UTF-8, {exc.reason} at byte {exc.start}") from None
    except OSError as exc:
        raise _Usage(f"{label} {path}: {exc.strerror or exc}") from None


def _write_json(path: Path, payload: object) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write_bytes(path, body)


def _parse_selector(text: str) -> str | SelectorRule:
    """SEL, SEL:MIN or SEL:MIN:MAX. A bare selector is pinned to the count observed here.

    The supported CSS subset has no pseudo classes, so a colon is always a separator.
    """
    parts = text.split(":")
    if len(parts) == 1:
        return parts[0]
    if len(parts) > 3:
        raise _Usage(f"--selector {text!r} should be SEL, SEL:MIN or SEL:MIN:MAX")
    maximum = parts[2] if len(parts) == 3 else ""
    try:
        return SelectorRule(
            selector=parts[0],
            min_count=int(parts[1]),
            max_count=int(maximum) if maximum else None,
        )
    except ValueError:
        raise _Usage(f"--selector {text!r} has a count that is not a whole number") from None


def _parse_field(text: str) -> FieldRule:
    parts = text.split(":")
    if len(parts) not in (3, 4):
        raise _Usage(f"--field {text!r} should be NAME:SEL:SHAPE, or NAME:SEL:enum:VALUE,VALUE")
    name, selector, shape = parts[0], parts[1], parts[2]
    allowed = tuple(value for value in parts[3].split(",") if value) if len(parts) == 4 else ()
    if shape not in _SHAPES:
        raise _Usage(f"--field {text!r}: unknown shape {shape!r}, expected one of {list(_SHAPES)}")
    if shape == "enum" and not allowed:
        raise _Usage(f"--field {text!r}: shape 'enum' needs its values, NAME:SEL:enum:VALUE,VALUE")
    return FieldRule(name=name, selector=selector, shape=cast(FieldShape, shape), allowed=allowed)


@contextmanager
def _browser(timeout_ms: int) -> Iterator[PlaywrightDriver]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise _Usage(BROWSER_HINT) from None
    with sync_playwright() as engine:
        try:
            browser = engine.chromium.launch()
        except Exception as exc:
            # An installed package with no downloaded browser is an operator fix, not a bug.
            raise _Usage(f"{str(exc).splitlines()[0]}; {BROWSER_HINT}") from None
        try:
            yield PlaywrightDriver(browser.new_page(), default_timeout_ms=timeout_ms)
        finally:
            browser.close()


def _cmd_check(args: argparse.Namespace) -> int:
    contract = load_contract(args.contract)
    fail_on = cast(Severity, args.fail_on)
    if args.html is not None:
        html = _read_text(args.html, "html file")
        report = check_html(html, contract, target=str(args.html), fail_on=fail_on)
    else:
        with _browser(args.timeout_ms) as driver:
            report = run(
                driver,
                replace(contract, url=args.url),
                timeout_ms=args.timeout_ms,
                fail_on=fail_on,
            )
    print(report.to_text())
    if args.json_out is not None:
        _write_json(
            args.json_out,
            {
                **report.to_dict(),
                "generated_at": _stamp(_clock(args)),
                "questz_version": __version__,
            },
        )
    return STATUS_EXIT[report.status]


def _cmd_record(args: argparse.Namespace) -> int:
    if args.html is not None:
        html = _read_text(args.html, "html file")
        url = args.html.resolve().as_uri()
    else:
        with _browser(args.timeout_ms) as driver:
            status = driver.goto(args.url)
            if not 200 <= status <= 299:
                raise _Usage(f"{args.url} answered HTTP {status}, nothing was recorded")
            if not driver.wait_for(args.ready_when, timeout_ms=args.timeout_ms):
                # A baseline taken from a page that never finished is worse than no baseline.
                raise _Usage(
                    f"{args.ready_when!r} never appeared on {args.url}, nothing was recorded"
                )
            html = driver.html()
        url = args.url
    contract = record(
        html,
        name=args.name,
        url=url,
        container=args.container,
        ready_when=args.ready_when,
        required=[_parse_selector(text) for text in args.selector],
        fields=[_parse_field(text) for text in args.fields],
        secret_selectors=tuple(args.secret),
        decimal_separator=args.decimal_separator,
    )
    save_contract(contract, args.out)
    counts = ", ".join(f"{rule.selector} {rule.min_count}" for rule in contract.required)
    print(f"recorded {contract.name} v{contract.version} to {args.out}")
    print(f"url:      {url}")
    print(f"counts:   {counts or 'none declared'}")
    print("counts are pinned to what this page had. Widen the ranges by hand before committing.")
    if args.html is not None:
        print("url names the file this was recorded from. Point it at the live page.")
    return EXIT_OK


def _cmd_report(args: argparse.Namespace) -> int:
    # No `exists()` pre-check: it answers a different question from "can this be read", and
    # a directory passes it. read_run raises one readable QuestzError for every case.
    record_ = read_run(args.run)
    print(render_report(record_))
    if args.json_out is not None:
        _write_json(
            args.json_out,
            {"run_id": record_.run_id, "entries": [asdict(entry) for entry in record_.entries]},
        )
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    server, url = serve(args.variant, port=args.port)
    # serve_forever never returns, so the banner has to be flushed or a caller reading this
    # through a pipe sees nothing until the process is killed.
    print(f"serving questz/testsite/{args.variant} at {url}")
    print("stop with ctrl-c", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        server.server_close()
    return EXIT_OK


def _examples_module(name: str) -> ModuleType:
    """examples/ ships with the git checkout, not inside the wheel."""
    directory = Path(__file__).resolve().parent.parent / "examples"
    if not (directory / f"{name}.py").is_file():
        raise _Usage(
            f"examples/{name}.py is not next to the installed package,"
            " so the demo needs the git checkout: clone the repo and run 'uv sync'"
        )
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return importlib.import_module(name)


def _cmd_demo(args: argparse.Namespace) -> int:
    scenarios = _examples_module("scenarios")
    return int(
        scenarios.run_scenario(
            args.scenario, out=args.out, deterministic=args.deterministic, seed=args.seed
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="questz",
        description="Check that a page still matches its contract before a job acts on it.",
        epilog="exit codes: 0 OK, 1 DRIFT, 2 usage or argument error, 3 UNAVAILABLE or BLOCKED",
    )
    parser.add_argument("--version", action="version", version=f"questz {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    canary = commands.add_parser("canary", help="record and check page contracts")
    canary_commands = canary.add_subparsers(dest="canary_command", required=True)

    check = canary_commands.add_parser("check", help="check a page against a contract")
    check.add_argument("--contract", required=True, type=Path)
    source = check.add_mutually_exclusive_group(required=True)
    source.add_argument("--html", type=Path, help="check a file, no browser needed")
    source.add_argument("--url", help="drive a real browser to this url")
    check.add_argument("--json", dest="json_out", type=Path, help="also write the report as JSON")
    check.add_argument(
        "--fail-on",
        dest="fail_on",
        type=str.upper,
        default=DEFAULT_FAIL_ON,
        choices=list(SEVERITY_ORDER),
        help="lowest severity that makes this a stop; findings below it are still printed"
        f" (default {DEFAULT_FAIL_ON.lower()})",
    )
    check.add_argument("--timeout-ms", type=int, default=5000, dest="timeout_ms")
    check.add_argument("--deterministic", action="store_true", help="fixed clock in the JSON")
    check.set_defaults(handler=_cmd_check)

    recorder = canary_commands.add_parser("record", help="derive a contract from a good page")
    recorder_source = recorder.add_mutually_exclusive_group(required=True)
    recorder_source.add_argument("--html", type=Path)
    recorder_source.add_argument("--url")
    recorder.add_argument("--name", required=True)
    recorder.add_argument("--container", required=True, metavar="SEL")
    recorder.add_argument("--ready-when", required=True, dest="ready_when", metavar="SEL")
    recorder.add_argument("--out", required=True, type=Path)
    recorder.add_argument("--selector", action="append", default=[], metavar="SEL:MIN:MAX")
    recorder.add_argument(
        "--field", action="append", default=[], dest="fields", metavar="NAME:SEL:SHAPE"
    )
    recorder.add_argument("--secret", action="append", default=[], metavar="SEL")
    recorder.add_argument(
        "--decimal-separator",
        dest="decimal_separator",
        choices=DECIMAL_SEPARATORS,
        help="which character this target uses as the decimal point;"
        " without it a value like '1.234' is refused rather than guessed",
    )
    recorder.add_argument("--timeout-ms", type=int, default=5000, dest="timeout_ms")
    recorder.set_defaults(handler=_cmd_record)

    report = commands.add_parser("report", help="render a run journal")
    report.add_argument("run", type=Path, metavar="RUN.jsonl")
    report.add_argument("--json", dest="json_out", type=Path)
    report.set_defaults(handler=_cmd_report)

    server = commands.add_parser("serve", help="serve the bundled target on loopback")
    server.add_argument("--variant", default="v1", choices=VARIANTS)
    server.add_argument("--port", type=int, default=0, help="0 asks the OS for a free port")
    server.set_defaults(handler=_cmd_serve)

    demo = commands.add_parser("demo", help="run a scenario end to end in a browser")
    demo.add_argument("--scenario", default="happy", choices=_SCENARIOS)
    demo.add_argument("--out", type=Path, default=Path("artifacts"))
    demo.add_argument("--deterministic", action="store_true")
    demo.add_argument("--seed", type=int, default=0)
    demo.set_defaults(handler=_cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except QuestzError as exc:
        print(f"questz: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
