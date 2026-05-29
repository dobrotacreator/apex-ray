import re
from typing import Any

import yaml

from apex_ray.pr_eval_models import (
    GreptileFinding,
    PrEvalTelemetryEntry,
    PullRequestEvalRunReport,
    PullRequestEvalRunResult,
)

DEFAULT_MEMORY_BODY_CHARS = 4000


def render_pr_eval_report(report: PullRequestEvalRunReport) -> str:
    lines = [
        "# Apex Ray PR Replay Eval",
        "",
        f"- Cases: `{report.total}`",
        f"- Passed: `{report.passed}`",
        f"- Failed: `{report.failed}`",
        f"- Partial: `{report.partial}`",
        f"- Timed out: `{report.timed_out}`",
        f"- Quarantined: `{report.quarantined}`",
        f"- Skipped: `{report.skipped}`",
        f"- Greptile findings: `{report.greptile_findings_total}`",
        f"- Matched Greptile findings: `{report.matched_greptile_findings_total}`",
        f"- Missed Greptile findings: `{report.missed_greptile_findings_total}`",
        f"- Extra Apex findings: `{report.extra_apex_findings_total}`",
        f"- Triaged extra true positives: `{report.triaged_extra_true_positives_total}`",
        f"- Triaged extra false positives: `{report.triaged_extra_false_positives_total}`",
        f"- Triaged extra duplicates: `{report.triaged_extra_duplicates_total}`",
        f"- Triaged extra not actionable: `{report.triaged_extra_not_actionable_total}`",
        f"- Triaged extra unknown: `{report.triaged_extra_unknown_total}`",
        f"- Estimated input tokens: `~{report.estimated_input_tokens_total}`",
        "",
        "## Cases",
        "",
    ]
    if not report.cases:
        lines.append("No PR eval cases were run.")
        lines.append("")
        return "\n".join(lines)
    for case in report.cases:
        lines.append(f"### PR #{case.number}: {case.title}")
        lines.append("")
        lines.append(f"- Status: `{case.status}`")
        lines.append(f"- Gate: `{'pass' if case.passed else 'fail'}`")
        if not case.scored:
            lines.append("- Scored: `false`")
        if case.error_message:
            lines.append(f"- Error: {case.error_message}")
        lines.append(f"- URL: {case.url}")
        lines.append(f"- Greptile findings: `{case.greptile_findings_count}`")
        if case.ignored_greptile_findings:
            lines.append(f"- Ignored Greptile findings by labels: `{case.ignored_greptile_findings}`")
        lines.append(f"- Matched/missed: `{case.matched_greptile_findings}` / `{case.missed_greptile_findings}`")
        lines.append(f"- Apex findings: `{case.apex_findings_count}`")
        lines.append(f"- Extra Apex findings: `{case.extra_apex_findings}`")
        lines.append(f"- Coverage partial severity: `{case.coverage_partial_severity}`")
        lines.append(f"- Coverage gate: `{case.coverage_quality_gate_status}`")
        if case.labels_path:
            lines.append(f"- Labels: `{case.labels_path}`")
        if case.extra_apex_findings:
            lines.append(
                "- Extra triage: "
                f"`{case.triaged_extra_true_positives}` true positive / "
                f"`{case.triaged_extra_false_positives}` false positive / "
                f"`{case.triaged_extra_duplicates}` duplicate / "
                f"`{case.triaged_extra_not_actionable}` not actionable / "
                f"`{case.triaged_extra_unknown}` unknown"
            )
        lines.append(f"- Context packs: `{case.context_packs_count}`")
        lines.append(
            f"- Reviewed/unreviewed context packs: `{case.reviewed_context_packs_count}` / "
            f"`{case.unreviewed_context_packs_count}`"
        )
        lines.append(
            f"- Residual P0/P1 packs: `{case.residual_p0_context_packs_count}` / "
            f"`{case.residual_p1_context_packs_count}`"
        )
        lines.append(
            f"- LLM coverage: `{case.llm_coverage_ratio:.1%}`, source lines "
            f"`{case.source_changed_line_coverage_ratio:.1%}`, high-risk `{case.high_risk_coverage_ratio:.1%}`"
        )
        lines.append(f"- LLM runs: `{case.llm_runs_count}`")
        lines.append(
            f"- Failed LLM review/verifier runs: `{case.failed_llm_review_runs_count}` / "
            f"`{case.failed_llm_verify_runs_count}`"
        )
        lines.append(f"- LLM duration: `{case.llm_duration_ms}ms`")
        lines.append(f"- Estimated input: `{case.llm_input_chars}` chars (`~{case.llm_estimated_input_tokens}` tokens)")
        if case.matches:
            lines.append("- Greptile match details:")
            for match in case.matches:
                marker = "matched" if match.matched else "missed"
                location = _format_location(match.greptile_finding.file, match.greptile_finding.line)
                matched = f" -> {match.matched_apex_title}" if match.matched_apex_title else ""
                lines.append(
                    f"  - `{marker}` `{match.greptile_finding.severity or 'n/a'}` "
                    f"{match.greptile_finding.title} at `{location}`{matched}"
                )
        if case.extra_findings:
            lines.append("- Extra Apex findings:")
            for finding in case.extra_findings:
                location = _format_location(finding.file, finding.line)
                lines.append(f"  - `{finding.severity}` {finding.title} at `{location}`")
        if case.warnings:
            lines.append("- Warnings:")
            for warning in case.warnings:
                lines.append(f"  - {warning}")
        lines.append("")
    return "\n".join(lines)


def memory_suggestions_from_pr_eval_report(report: PullRequestEvalRunReport) -> str:
    lines = [
        "# Apex Ray Memory Suggestions From PR Replay",
        "",
        "Review these suggestions before committing them under `.apex-ray/memory/`.",
        "They are generated from missed first-pass Greptile findings and should be edited into stable project knowledge.",
        "",
    ]
    missed = [(case, match.greptile_finding) for case in report.cases for match in case.matches if not match.matched]
    if not missed:
        lines.append("No missed Greptile findings were available for memory suggestions.")
        lines.append("")
        return "\n".join(lines)

    seen: set[str] = set()
    for case, finding in missed:
        base_slug = _slugify(f"greptile-pr-{case.number}-{finding.title}") or f"greptile-pr-{case.number}"
        slug = _dedupe_slug(base_slug, seen)
        severity = _memory_severity_from_greptile(finding.severity)
        title = finding.title or "Historical Greptile finding"
        frontmatter: dict[str, Any] = {
            "id": slug,
            "title": title,
            "kind": "bug_pattern",
            "severity": severity,
        }
        if finding.file:
            frontmatter["paths"] = [finding.file]
        triggers = _memory_triggers_from_finding(finding)
        if triggers:
            frontmatter["triggers"] = {"text": triggers}
        body = _memory_body_from_greptile_finding(case, finding)
        lines.extend(
            [
                f"## {title}",
                "",
                "```md",
                "---",
                yaml.safe_dump(frontmatter, sort_keys=False).strip(),
                "---",
                body,
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def render_pr_eval_telemetry_summary(entries: list[PrEvalTelemetryEntry]) -> str:
    lines = ["# Apex Ray PR Eval Telemetry", ""]
    if not entries:
        lines.append("No telemetry entries found.")
        lines.append("")
        return "\n".join(lines)
    latest = entries[-1]
    latest_failed_llm_runs = sum(
        case.failed_llm_review_runs_count + case.failed_llm_verify_runs_count for case in latest.cases
    )
    latest_unreviewed_packs = sum(case.unreviewed_context_packs_count for case in latest.cases)
    latest_llm_duration_ms = sum(case.llm_duration_ms for case in latest.cases)
    lines.extend(
        [
            f"- Runs: `{len(entries)}`",
            f"- Latest run: `{latest.created_at}` (`{latest.run_id}`)",
            f"- Latest cases: `{latest.total}`",
            f"- Latest matched Greptile findings: `{latest.matched_greptile_findings_total}/{latest.greptile_findings_total}`",
            f"- Latest missed Greptile findings: `{latest.missed_greptile_findings_total}`",
            f"- Latest extra Apex findings: `{latest.extra_apex_findings_total}`",
            f"- Latest triaged extra true positives: `{latest.triaged_extra_true_positives_total}`",
            f"- Latest triaged extra false positives: `{latest.triaged_extra_false_positives_total}`",
            f"- Latest triaged extra duplicates: `{latest.triaged_extra_duplicates_total}`",
            f"- Latest triaged extra not actionable: `{latest.triaged_extra_not_actionable_total}`",
            f"- Latest estimated input tokens: `~{latest.estimated_input_tokens_total}`",
            f"- Latest LLM duration: `{latest_llm_duration_ms}ms`",
            f"- Latest unreviewed context packs: `{latest_unreviewed_packs}`",
            f"- Latest failed LLM runs: `{latest_failed_llm_runs}`",
            "",
            "## Recent Runs",
            "",
        ]
    )
    for entry in entries[-20:]:
        lines.append(
            f"- `{entry.created_at}` `{entry.run_id}` - cases `{entry.total}`, "
            f"matched `{entry.matched_greptile_findings_total}/{entry.greptile_findings_total}`, "
            f"missed `{entry.missed_greptile_findings_total}`, extra `{entry.extra_apex_findings_total}`, "
            f"tokens `~{entry.estimated_input_tokens_total}`, "
            f"unreviewed packs `{sum(case.unreviewed_context_packs_count for case in entry.cases)}`, "
            f"failed LLM runs `{sum(case.failed_llm_review_runs_count + case.failed_llm_verify_runs_count for case in entry.cases)}`"
        )
    lines.append("")
    return "\n".join(lines)


def _memory_body_from_greptile_finding(case: PullRequestEvalRunResult, finding: GreptileFinding) -> str:
    body = _clean_greptile_body(finding.body)
    location = _format_location(finding.file, finding.line)
    parts = [
        f"Historical Greptile first-pass finding from PR #{case.number}: {case.title}",
        f"PR: {case.url}",
        f"Location: {location}",
        "",
        f"Pattern to check: {body or finding.title}",
        "",
        "When similar code changes, verify the diff preserves the invariant and includes a regression test.",
    ]
    return "\n".join(parts)


def _clean_greptile_body(value: str) -> str:
    text = _strip_prompt_details(value)
    text = re.sub(r"!\[[^\]]*]\([^)]+\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return _trim_body(text)


def _strip_prompt_details(body: str) -> str:
    return re.split(r"\n\s*<details\b", body, maxsplit=1, flags=re.IGNORECASE)[0]


def _trim_body(body: str) -> str:
    return _clean_text(body)[:DEFAULT_MEMORY_BODY_CHARS]


def _clean_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    return text


def _memory_severity_from_greptile(severity: str | None) -> str:
    normalized = (severity or "").upper()
    if normalized in {"P0", "P1"}:
        return "high"
    if normalized == "P2":
        return "medium"
    return "low" if normalized == "P3" else "medium"


def _memory_triggers_from_finding(finding: GreptileFinding) -> list[str]:
    text = " ".join([finding.title, finding.body])
    code_terms = []
    for term in re.findall(r"`([^`\n]{3,80})`", text):
        cleaned = _clean_text(term)
        if cleaned and cleaned not in code_terms:
            code_terms.append(cleaned)
    return code_terms[:8]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:96].strip("-")


def _dedupe_slug(slug: str, seen: set[str]) -> str:
    if slug not in seen:
        seen.add(slug)
        return slug
    suffix = 2
    while f"{slug}-{suffix}" in seen:
        suffix += 1
    deduped = f"{slug}-{suffix}"
    seen.add(deduped)
    return deduped


def _format_location(file: str | None, line: int | None) -> str:
    if not file:
        return "n/a"
    return f"{file}:{line}" if line else file
