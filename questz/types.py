"""Shared value types and errors. This module imports nothing from its siblings."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

__all__ = [
    "SEVERITY_ORDER",
    "CacheError",
    "CanaryStatus",
    "CircuitOpenError",
    "ContractError",
    "DriftReport",
    "Finding",
    "FindingKind",
    "JournalSink",
    "PermanentError",
    "QuestzError",
    "Severity",
    "TransientError",
]

Severity = Literal["CRITICAL", "MAJOR", "WARNING"]
CanaryStatus = Literal["OK", "DRIFT", "UNAVAILABLE", "BLOCKED"]
FindingKind = Literal[
    "missing_selector",
    "count_out_of_range",
    "field_shape",
    "structure_added",
    "structure_moved",
    "structure_removed",
]

SEVERITY_ORDER: dict[Severity, int] = {"CRITICAL": 3, "MAJOR": 2, "WARNING": 1}

_DIFF_PREVIEW_LINES = 24


class QuestzError(Exception):
    """Base class for every error this package raises deliberately."""


class ContractError(QuestzError):
    """A contract file is malformed, or a contract selector is outside the supported subset."""


class CacheError(QuestzError):
    """A cache entry could not be read back."""


class CircuitOpenError(QuestzError):
    """The breaker refused the call before the action ran."""


class TransientError(QuestzError):
    """Retryable. Callers map driver and network faults onto this."""


class PermanentError(QuestzError):
    """Never retried, however many attempts the policy allows."""


class JournalSink(Protocol):
    """The narrow write side of the journal, so event producers never import `questz.journal`."""

    def event(
        self,
        name: str,
        *,
        level: str = "INFO",
        step: str | None = None,
        duration_ms: float | None = None,
        artifact: str | None = None,
        **payload: Any,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Finding:
    kind: FindingKind
    severity: Severity
    selector: str | None
    expected: str
    found: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DriftReport:
    status: CanaryStatus
    target: str
    contract_name: str
    contract_version: int
    findings: tuple[Finding, ...]
    signature_expected: str
    signature_observed: str
    signature_diff: tuple[str, ...]
    observed_counts: tuple[tuple[str, int], ...]
    reason: str = ""
    # The severity that had to be reached for this to be a stop. Recorded rather than
    # assumed, because a report read a week later has to say what it was judged against.
    fail_on: Severity = "WARNING"

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    @property
    def max_severity(self) -> Severity | None:
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=SEVERITY_ORDER.__getitem__)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target,
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "fail_on": self.fail_on,
            "findings": [f.to_dict() for f in self.findings],
            "max_severity": self.max_severity,
            "signature_expected": self.signature_expected,
            "signature_observed": self.signature_observed,
            "signature_diff": list(self.signature_diff),
            "observed_counts": [[path, count] for path, count in self.observed_counts],
            "reason": self.reason,
        }

    def to_text(self) -> str:
        lines = [
            f"questz canary: {self.status}",
            f"contract: {self.contract_name} v{self.contract_version}",
            f"target:   {self.target}",
        ]
        if self.reason:
            lines.append(f"reason:   {self.reason}")
        lines.append("")
        lines.append(f"findings ({len(self.findings)}):")
        for finding in self.findings:
            anchor = finding.selector or ""
            lines.append(f"  {finding.severity:<9} {finding.kind:<18} {anchor}".rstrip())
            lines.append(f"  {'':<9} {'':<18} expected {finding.expected}, found {finding.found}")
            if finding.detail:
                lines.append(f"  {'':<9} {'':<18} {finding.detail}")
        if not self.findings:
            lines.append("  none")
        elif self.status == "OK":
            lines.append(
                f"  nothing reaches --fail-on {self.fail_on.lower()}, so this is not a stop"
            )
        lines.append("")
        if self.signature_expected == self.signature_observed:
            lines.append(f"signature: {self.signature_observed[:12]} matches the baseline")
        else:
            lines.append(
                f"signature: baseline {self.signature_expected[:12]}"
                f" observed {self.signature_observed[:12]}"
            )
        # The leaf node reads better here; to_dict keeps the full path.
        counts = ", ".join(
            f"{path.rsplit('>', 1)[-1]}={count}" for path, count in self.observed_counts
        )
        lines.append(f"counts:    {counts if counts else 'none'}")
        if self.signature_diff:
            lines.append("")
            lines.append("structure diff:")
            preview = self.signature_diff[:_DIFF_PREVIEW_LINES]
            lines.extend(f"  {line}" for line in preview)
            hidden = len(self.signature_diff) - len(preview)
            if hidden:
                lines.append(f"  ... {hidden} more diff lines")
        return "\n".join(lines)
