from dataclasses import dataclass
from pathlib import Path

from apex_ray.models import Finding, PrePushGateConfig, ReviewReport

_FINDING_SEVERITY_RANK = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
_PARTIAL_SEVERITY_RANK = {
    "none": 0,
    "minor": 1,
    "major": 2,
    "critical": 3,
}


@dataclass(frozen=True)
class PrePushGateDecision:
    blocked: bool
    reasons: list[str]
    blocking_findings: list[Finding]
    quality_gate_failed: bool = False
    partial_blocked: bool = False


def evaluate_pre_push_gate(report: ReviewReport, config: PrePushGateConfig) -> PrePushGateDecision:
    blocking_findings = _blocking_findings(report, config)
    reasons: list[str] = []
    if blocking_findings and config.min_finding_severity is not None:
        reasons.append(f"Blocking findings: {len(blocking_findings)} >= {config.min_finding_severity!s}")

    quality_gate_failed = config.fail_on_quality_gate and report.llm_coverage.quality_gate_status == "fail"
    if quality_gate_failed:
        details = "; ".join(report.llm_coverage.quality_gate_reasons)
        reasons.append(f"Coverage quality gate failed{f': {details}' if details else ''}")

    partial_blocked = _partial_severity_blocks(
        report.llm_coverage.partial_severity,
        config.fail_on_partial_severity,
    )
    if partial_blocked:
        details = "; ".join(report.llm_coverage.partial_reasons)
        reasons.append(f"Partial coverage is {report.llm_coverage.partial_severity}{f': {details}' if details else ''}")

    return PrePushGateDecision(
        blocked=bool(reasons),
        reasons=reasons,
        blocking_findings=blocking_findings,
        quality_gate_failed=quality_gate_failed,
        partial_blocked=partial_blocked,
    )


def render_pre_push_gate_stdout(
    report: ReviewReport,
    decision: PrePushGateDecision,
    *,
    markdown_path: Path,
    json_path: Path,
    base: str,
    config: PrePushGateConfig,
    previous_decision: PrePushGateDecision | None = None,
) -> str:
    title = "APEX RAY GATE: BLOCKED" if decision.blocked else "APEX RAY GATE: PASSED"
    lines = [
        title,
        "",
        f"Target: base {base}...HEAD",
        f"Report: {markdown_path}",
        f"JSON: {json_path}",
        "",
    ]
    if previous_decision is not None:
        lines.extend(_render_delta(decision, previous_decision))
        lines.append("")
    if decision.reasons:
        lines.append("Reasons:")
        lines.extend(f"- {reason}" for reason in decision.reasons)
        lines.append("")
    if config.stdout_format == "compact":
        lines.append(f"Blocking findings: {len(decision.blocking_findings)}")
        if decision.blocked:
            lines.append("Run the report command above for full details.")
        return "\n".join(lines).rstrip() + "\n"
    if decision.blocking_findings:
        lines.extend(_render_findings(decision.blocking_findings, config.max_stdout_findings))
    if decision.partial_blocked and report.llm_coverage.coverage_todos:
        lines.extend(_render_continuations(report))
    if decision.blocked:
        lines.append("After fixing, commit the changes and run git push again.")
    else:
        lines.append(f"Findings: {len(report.findings)}")
        lines.append(f"Coverage gate: {report.llm_coverage.quality_gate_status}")
        lines.append(f"Partial severity: {report.llm_coverage.partial_severity}")
    return "\n".join(lines).rstrip() + "\n"


def _blocking_findings(report: ReviewReport, config: PrePushGateConfig) -> list[Finding]:
    if config.min_finding_severity is None:
        return []
    findings = _eligible_findings(report, config)
    threshold = _FINDING_SEVERITY_RANK[str(config.min_finding_severity)]
    return [finding for finding in findings if _FINDING_SEVERITY_RANK.get(str(finding.severity), 0) >= threshold]


def _eligible_findings(report: ReviewReport, config: PrePushGateConfig) -> list[Finding]:
    if not config.require_verified_findings:
        return report.findings
    approved = {
        _finding_identity(verification.finding) for verification in report.verifications if verification.approved
    }
    if not approved:
        return []
    return [finding for finding in report.findings if _finding_identity(finding) in approved]


def _partial_severity_blocks(
    current: str,
    threshold: str | None,
) -> bool:
    if threshold in {None, "none"}:
        return False
    return _PARTIAL_SEVERITY_RANK.get(current, 0) >= _PARTIAL_SEVERITY_RANK[str(threshold)]


def _finding_identity(finding: object) -> tuple[object, ...]:
    return (
        getattr(finding, "title", ""),
        getattr(finding, "file", ""),
        getattr(finding, "line", None),
        getattr(finding, "failure_mode", ""),
        getattr(finding, "evidence", ""),
    )


def _finding_fingerprint(finding: Finding) -> tuple[object, ...]:
    return (
        str(finding.severity),
        finding.title,
        finding.file,
        finding.line,
        finding.failure_mode,
    )


def _render_delta(current: PrePushGateDecision, previous: PrePushGateDecision) -> list[str]:
    current_keys = {_finding_fingerprint(finding) for finding in current.blocking_findings}
    previous_keys = {_finding_fingerprint(finding) for finding in previous.blocking_findings}
    return [
        "Delta since previous pre-push report:",
        f"- New blocking findings: {len(current_keys - previous_keys)}",
        f"- Still blocking findings: {len(current_keys & previous_keys)}",
        f"- Resolved blocking findings: {len(previous_keys - current_keys)}",
    ]


def _render_findings(findings: list[Finding], max_findings: int) -> list[str]:
    visible = findings[:max_findings]
    lines = [f"Blocking findings shown: {len(visible)}/{len(findings)}", ""]
    for index, finding in enumerate(visible, start=1):
        location = finding.file if finding.line is None else f"{finding.file}:{finding.line}"
        lines.extend(
            [
                f"{index}. [{finding.severity}] {finding.title}",
                f"   {location}",
                f"   Failure: {_one_line(finding.failure_mode)}",
                f"   Evidence: {_one_line(finding.evidence)}",
                f"   Fix: {_one_line(finding.suggested_fix)}",
            ]
        )
        if finding.suggested_test:
            lines.append(f"   Test: {_one_line(finding.suggested_test)}")
        if finding.context_pack_id:
            lines.append(f"   Pack: {finding.context_pack_id}")
        lines.append("")
    if len(findings) > len(visible):
        lines.append(f"... {len(findings) - len(visible)} more blocking finding(s) in the report.")
        lines.append("")
    return lines


def _render_continuations(report: ReviewReport) -> list[str]:
    lines = ["Coverage continuation commands:"]
    for todo in report.llm_coverage.coverage_todos[:3]:
        if todo.suggested_command:
            lines.append(f"- {todo.suggested_command}")
    if len(report.llm_coverage.coverage_todos) > 3:
        lines.append(f"- ... {len(report.llm_coverage.coverage_todos) - 3} more in the report.")
    lines.append("")
    return lines


def _one_line(value: str, max_chars: int = 220) -> str:
    compact = " ".join(value.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."
