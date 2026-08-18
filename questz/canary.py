"""The two layer check: a selector and field contract that carries severity, plus a
diffable structural signature. Either layer alone misses a whole class of change."""

from __future__ import annotations

import difflib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from questz.driver import PageDriver
from questz.normalize import Element, diff, normalize_tree, parse, select, walk
from questz.types import (
    SEVERITY_ORDER,
    ContractError,
    DriftReport,
    Finding,
    JournalSink,
    Severity,
)

__all__ = [
    "CONTRACT_FORMAT",
    "Contract",
    "FieldRule",
    "SelectorRule",
    "check_html",
    "load",
    "parse_decimal",
    "record",
    "run",
    "save",
]

CONTRACT_FORMAT = 1

FieldShape = Literal["decimal", "integer", "text", "enum"]
_SHAPES: tuple[str, ...] = ("decimal", "integer", "text", "enum")
_CONTRACT_KEYS = (
    "baseline",
    "container",
    "fields",
    "format",
    "name",
    "ready_when",
    "required",
    "secret_selectors",
    "signature",
    "url",
    "version",
)
_DETAIL_NODES = 4


@dataclass(frozen=True, slots=True)
class SelectorRule:
    selector: str
    min_count: int = 1
    max_count: int | None = None
    # Severity of a selector that matched nothing. An in-range violation is always MAJOR.
    severity: Severity = "CRITICAL"


@dataclass(frozen=True, slots=True)
class FieldRule:
    name: str
    selector: str
    shape: FieldShape
    allowed: tuple[str, ...] = ()
    severity: Severity = "CRITICAL"


@dataclass(frozen=True, slots=True)
class Contract:
    name: str
    version: int
    url: str
    container: str
    ready_when: str
    required: tuple[SelectorRule, ...]
    fields: tuple[FieldRule, ...]
    secret_selectors: tuple[str, ...]
    signature: str
    baseline: tuple[str, ...]


def _contract_dict(contract: Contract) -> dict[str, Any]:
    return {
        "baseline": list(contract.baseline),
        "container": contract.container,
        "fields": [
            {
                "allowed": list(rule.allowed),
                "name": rule.name,
                "selector": rule.selector,
                "severity": rule.severity,
                "shape": rule.shape,
            }
            for rule in contract.fields
        ],
        "format": CONTRACT_FORMAT,
        "name": contract.name,
        "ready_when": contract.ready_when,
        "required": [
            {
                "max_count": rule.max_count,
                "min_count": rule.min_count,
                "selector": rule.selector,
                "severity": rule.severity,
            }
            for rule in contract.required
        ],
        "secret_selectors": list(contract.secret_selectors),
        "signature": contract.signature,
        "url": contract.url,
        "version": contract.version,
    }


def save(contract: Contract, path: Path) -> None:
    """JSON, not YAML: the core has no third party dependencies, and `tomllib` cannot write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_contract_dict(contract), indent=2, sort_keys=True) + "\n", "utf-8")


def load(path: Path) -> Contract:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ContractError(f"contract file not found: {path}") from None
    except UnicodeDecodeError as exc:
        # UnicodeDecodeError is a ValueError, so an `except OSError` above it catches
        # nothing. Letting it escape here would exit 1, and 1 means DRIFT.
        raise ContractError(
            f"{path} is not valid UTF-8: {exc.reason} at byte {exc.start}"
        ) from None
    except OSError as exc:
        raise ContractError(f"contract file {path}: {exc.strerror or exc}") from None
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ContractError(f"{path}: a contract must be a JSON object")
    for key in _CONTRACT_KEYS:
        if key not in raw:
            raise ContractError(f"{path}: contract is missing the key {key!r}")
    if raw["format"] != CONTRACT_FORMAT:
        raise ContractError(
            f"{path}: contract format {raw['format']!r} is not supported,"
            f" this build reads format {CONTRACT_FORMAT}"
        )
    return Contract(
        name=str(raw["name"]),
        version=int(raw["version"]),
        url=str(raw["url"]),
        container=str(raw["container"]),
        ready_when=str(raw["ready_when"]),
        required=tuple(_selector_rule(entry, path) for entry in raw["required"]),
        fields=tuple(_field_rule(entry, path) for entry in raw["fields"]),
        secret_selectors=tuple(str(entry) for entry in raw["secret_selectors"]),
        signature=str(raw["signature"]),
        baseline=tuple(str(entry) for entry in raw["baseline"]),
    )


def _severity(raw: Any, path: Path) -> Severity:
    if raw not in SEVERITY_ORDER:
        raise ContractError(
            f"{path}: unknown severity {raw!r}, expected one of {list(SEVERITY_ORDER)}"
        )
    return raw


def _selector_rule(raw: Any, path: Path) -> SelectorRule:
    if not isinstance(raw, dict) or "selector" not in raw:
        raise ContractError(f"{path}: every entry in 'required' needs a 'selector' key")
    return SelectorRule(
        selector=str(raw["selector"]),
        min_count=int(raw.get("min_count", 1)),
        max_count=None if raw.get("max_count") is None else int(raw["max_count"]),
        severity=_severity(raw.get("severity", "CRITICAL"), path),
    )


def _field_rule(raw: Any, path: Path) -> FieldRule:
    if not isinstance(raw, dict):
        raise ContractError(f"{path}: every entry in 'fields' must be a JSON object")
    for key in ("name", "selector", "shape"):
        if key not in raw:
            raise ContractError(f"{path}: field entry is missing the key {key!r}")
    shape = raw["shape"]
    if shape not in _SHAPES:
        raise ContractError(
            f"{path}: unknown field shape {shape!r} for field {raw['name']!r},"
            f" expected one of {list(_SHAPES)}"
        )
    allowed = tuple(str(entry) for entry in raw.get("allowed", ()))
    if shape == "enum" and not allowed:
        raise ContractError(
            f"{path}: field {raw['name']!r} has shape 'enum' but no 'allowed' values"
        )
    return FieldRule(
        name=str(raw["name"]),
        selector=str(raw["selector"]),
        shape=shape,
        allowed=allowed,
        severity=_severity(raw.get("severity", "CRITICAL"), path),
    )


def record(
    html: str,
    *,
    name: str,
    url: str,
    container: str,
    ready_when: str,
    required: Sequence[str | SelectorRule],
    fields: Sequence[FieldRule] = (),
    secret_selectors: Sequence[str] = (),
    version: int = 1,
) -> Contract:
    """Derive a contract from a page that is known good.

    A bare selector string is pinned to the count observed here, so the human widens the
    range by hand before committing. A `SelectorRule` is taken exactly as declared.
    """
    root = parse(html)
    if not select(root, container):
        raise ContractError(f"container {container!r} matches nothing in the recorded html")
    rules: list[SelectorRule] = []
    for entry in required:
        if isinstance(entry, SelectorRule):
            rules.append(entry)
            continue
        observed = len(select(root, entry))
        rules.append(SelectorRule(selector=entry, min_count=observed, max_count=observed))
    structure = normalize_tree(root, container=container)
    return Contract(
        name=name,
        version=version,
        url=url,
        container=container,
        ready_when=ready_when,
        required=tuple(rules),
        fields=tuple(fields),
        secret_selectors=tuple(secret_selectors),
        signature=structure.signature,
        baseline=structure.lines,
    )


def _range_text(rule: SelectorRule) -> str:
    if rule.max_count is None:
        return f"{rule.min_count} or more"
    return f"{rule.min_count} to {rule.max_count}"


def parse_decimal(text: str) -> Decimal | None:
    """The one parser both sides use. A contract that validates a price with different
    rules from the job that reads it is a contract that proves nothing."""
    cleaned = "".join(char for char in text.strip() if char.isdigit() or char in ".,+-")
    if not cleaned or not cleaned.lstrip("+-")[:1].isdigit():
        return None
    if cleaned.count(".") > 1 and "," not in cleaned:
        cleaned = cleaned.replace(".", "")
    elif cleaned.count(",") > 1 and "." not in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "." in cleaned and "," in cleaned:
        group = "," if cleaned.rfind(".") > cleaned.rfind(",") else "."
        cleaned = cleaned.replace(group, "")
    cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _shape_ok(rule: FieldRule, text: str) -> bool:
    value = text.strip()
    if rule.shape == "text":
        return bool(value)
    if rule.shape == "enum":
        return value in rule.allowed
    number = parse_decimal(value)
    if number is None:
        return False
    if rule.shape == "integer":
        return number == number.to_integral_value()
    return True


def _shape_text(rule: FieldRule) -> str:
    if rule.shape == "enum":
        return f"one of {list(rule.allowed)}"
    return f"a {rule.shape} value"


def _node_detail(lines: Sequence[str]) -> str:
    rendered: list[str] = []
    for line in lines[:_DETAIL_NODES]:
        parts = line.split("|")
        tag, attrs = parts[1], parts[2]
        rendered.append(f"{tag}[{attrs}]" if attrs else tag)
    if len(lines) > _DETAIL_NODES:
        rendered.append(f"and {len(lines) - _DETAIL_NODES} more")
    return ", ".join(rendered)


def _selector_findings(root: Element, contract: Contract) -> list[Finding]:
    findings: list[Finding] = []
    for rule in contract.required:
        count = len(select(root, rule.selector))
        if count == 0 and rule.min_count >= 1:
            findings.append(
                Finding("missing_selector", rule.severity, rule.selector, _range_text(rule), "0")
            )
        elif count < rule.min_count or (rule.max_count is not None and count > rule.max_count):
            findings.append(
                Finding("count_out_of_range", "MAJOR", rule.selector, _range_text(rule), str(count))
            )
    return findings


def _field_findings(root: Element, contract: Contract) -> list[Finding]:
    findings: list[Finding] = []
    for rule in contract.fields:
        elements = select(root, rule.selector)
        if not elements:
            findings.append(
                Finding(
                    "missing_selector",
                    rule.severity,
                    rule.selector,
                    _shape_text(rule),
                    "0 elements",
                    detail=f"field {rule.name!r}",
                )
            )
            continue
        bad = [element.text for element in elements if not _shape_ok(rule, element.text)]
        if bad:
            findings.append(
                Finding(
                    "field_shape",
                    rule.severity,
                    rule.selector,
                    _shape_text(rule),
                    repr(bad[0]),
                    detail=f"field {rule.name!r}, {len(bad)} of {len(elements)} values fail",
                )
            )
    return findings


def _structure_findings(baseline: Sequence[str], observed: Sequence[str]) -> list[Finding]:
    matcher = difflib.SequenceMatcher(a=list(baseline), b=list(observed), autojunk=False)
    removed: list[str] = []
    added: list[str] = []
    for tag, start_a, end_a, start_b, end_b in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(baseline[start_a:end_a])
        if tag in ("replace", "insert"):
            added.extend(observed[start_b:end_b])
    findings: list[Finding] = []
    if removed:
        findings.append(
            Finding(
                "structure_removed",
                "MAJOR",
                None,
                "every baseline node still present",
                f"{len(removed)} gone",
                detail=_node_detail(removed),
            )
        )
    if added:
        findings.append(
            Finding(
                "structure_added",
                "WARNING",
                None,
                "no nodes outside the baseline",
                f"{len(added)} new",
                detail=_node_detail(added),
            )
        )
    return findings


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """The same missing selector reached from a selector rule and from a field rule is one
    problem, so it is reported once, with the selector rule's count range."""
    seen: set[tuple[str, str | None]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (finding.kind, finding.selector)
        if finding.selector is not None and key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def _top_level_testids(root: Element) -> list[str]:
    values: list[str] = []
    for node in walk(root):
        value = node.attrs.get("data-testid")
        if not value:
            continue
        ancestor = node.parent
        while ancestor is not None and not ancestor.attrs.get("data-testid"):
            ancestor = ancestor.parent
        if ancestor is None and value not in values:
            values.append(value)
    return values


def check_html(html: str, contract: Contract, *, target: str = "") -> DriftReport:
    root = parse(html)
    structure = normalize_tree(root, container=contract.container)
    if not select(root, contract.container):
        served = ", ".join(_top_level_testids(root)) or "none"
        absent = f"container {contract.container!r} is absent"
        return DriftReport(
            status="BLOCKED",
            target=target,
            contract_name=contract.name,
            contract_version=contract.version,
            findings=(),
            signature_expected=contract.signature,
            signature_observed=structure.signature,
            signature_diff=(),
            observed_counts=(),
            reason=f"{absent}; data-testid served instead: {served}",
        )
    findings = _dedupe(
        _selector_findings(root, contract)
        + _field_findings(root, contract)
        + _structure_findings(contract.baseline, structure.lines)
    )
    findings.sort(key=lambda finding: -SEVERITY_ORDER[finding.severity])
    return DriftReport(
        status="DRIFT" if findings else "OK",
        target=target,
        contract_name=contract.name,
        contract_version=contract.version,
        findings=tuple(findings),
        signature_expected=contract.signature,
        signature_observed=structure.signature,
        signature_diff=diff(contract.baseline, structure.lines),
        observed_counts=structure.counts,
    )


def _unavailable(contract: Contract, reason: str) -> DriftReport:
    return DriftReport(
        status="UNAVAILABLE",
        target=contract.url,
        contract_name=contract.name,
        contract_version=contract.version,
        findings=(),
        signature_expected=contract.signature,
        signature_observed="",
        signature_diff=(),
        observed_counts=(),
        reason=reason,
    )


def _record_result(journal: JournalSink | None, report: DriftReport) -> None:
    if journal is None:
        return
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


def run(
    driver: PageDriver,
    contract: Contract,
    *,
    timeout_ms: int = 5000,
    journal: JournalSink | None = None,
) -> DriftReport:
    """Fails closed four different ways, because "the site changed" and "the site is down"
    need different pager behaviour."""
    try:
        status_code = driver.goto(contract.url)
    except Exception as exc:
        report = _unavailable(contract, f"navigation failed: {type(exc).__name__}: {exc}")
        _record_result(journal, report)
        return report
    if not 200 <= status_code <= 299:
        report = _unavailable(contract, f"HTTP {status_code}")
        _record_result(journal, report)
        return report
    ready = driver.wait_for(contract.ready_when, timeout_ms=timeout_ms)
    report = check_html(driver.html(), contract, target=contract.url)
    if not ready:
        if report.status == "BLOCKED":
            never = f"readiness selector {contract.ready_when!r} never appeared"
            report = replace(report, reason=f"{never}; {report.reason}")
        elif any(finding.selector == contract.ready_when for finding in report.findings):
            # The contract rules already name that selector; saying it twice adds nothing.
            report = replace(report, status="DRIFT")
        else:
            timed_out = Finding(
                "missing_selector",
                "MAJOR",
                contract.ready_when,
                f"present within {timeout_ms} ms",
                "not ready",
                detail="readiness selector did not appear",
            )
            findings = sorted(
                (timed_out, *report.findings), key=lambda finding: -SEVERITY_ORDER[finding.severity]
            )
            report = replace(report, status="DRIFT", findings=tuple(findings))
    _record_result(journal, report)
    return report
