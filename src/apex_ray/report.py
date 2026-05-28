from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from html import escape
from typing import Literal

from apex_ray import __version__
from apex_ray.models import (
    AnalyzerResult,
    ContextPack,
    DiffSummary,
    FileKind,
    Finding,
    FindingVerification,
    LLMContextSelection,
    LLMCoverageSummary,
    LLMCoverageTodo,
    LLMFileCoverageSummary,
    LLMPackReviewStatus,
    LLMResidualRiskSummary,
    LLMRouteSummary,
    LLMRun,
    LLMSliceCoverageSummary,
    MemorySummary,
    ProjectProfile,
    ReportSummary,
    ReviewConfig,
    ReviewReport,
)


def build_report(
    project: ProjectProfile,
    config: ReviewConfig,
    diff: DiffSummary,
    analyzer_results: list[AnalyzerResult] | None = None,
    context_packs: list[ContextPack] | None = None,
    findings: list[Finding] | None = None,
    verifications: list[FindingVerification] | None = None,
    llm_runs: list[LLMRun] | None = None,
    llm_selection: LLMContextSelection | None = None,
) -> ReviewReport:
    files_by_kind = Counter(file.file_kind for file in diff.files)
    files_by_language = Counter(file.language for file in diff.files)
    risk_by_severity = Counter(
        signal.severity for file in diff.files for signal in file.risk_signals if not file.is_ignored
    )
    actual_context_packs = context_packs or []
    actual_llm_runs = llm_runs or []
    return ReviewReport(
        project=project,
        config=config,
        diff=diff,
        summary=ReportSummary(
            files_by_kind=dict(sorted(files_by_kind.items())),
            files_by_language=dict(sorted(files_by_language.items())),
            risk_by_severity=dict(sorted(risk_by_severity.items())),
            total_risk_signals=sum(risk_by_severity.values()),
        ),
        llm_selection=llm_selection,
        llm_coverage=_build_llm_coverage(config, actual_context_packs, actual_llm_runs, llm_selection),
        memory_summary=_build_memory_summary(config, actual_context_packs),
        rules=[
            *config.rules,
            *[f"{rule.id}: {rule.title or rule.id} ({rule.severity}, {rule.mode})" for rule in config.rule_definitions],
        ],
        analyzer_results=analyzer_results or [],
        context_packs=actual_context_packs,
        findings=findings or [],
        verifications=verifications or [],
        llm_runs=actual_llm_runs,
        generated_at=datetime.now(UTC),
        version=__version__,
    )


def render_markdown(report: ReviewReport) -> str:
    lines: list[str] = [
        "# Apex Ray Review",
        "",
        "> Static classification and context extraction. CI, tests, linters, typecheckers, and scanners were not run.",
        "",
        "## Project",
        "",
        f"- Root: `{report.project.root}`",
        f"- Git repository: `{str(report.project.is_git_repo).lower()}`",
        f"- Config: `{report.project.config_path or 'not found'}`",
        f"- Detected languages: {_format_list(report.project.detected_languages)}",
        f"- Package managers: {_format_list(report.project.package_managers)}",
        f"- Framework hints: {_format_list(report.project.framework_hints)}",
        "",
        "## Diff Summary",
        "",
        f"- Target mode: `{report.diff.target_mode}`",
        f"- Base: `{report.diff.base or 'n/a'}`",
        f"- Files changed: `{report.diff.stats.files_changed}`",
        f"- Additions: `{report.diff.stats.additions}`",
        f"- Deletions: `{report.diff.stats.deletions}`",
        f"- Ignored files: `{report.diff.stats.ignored_files}`",
        "",
        "## Changed Files",
        "",
    ]

    if not report.diff.files:
        lines.append("No changed files detected.")
        lines.append("")
    else:
        for file in report.diff.files:
            ignored = " ignored" if file.is_ignored else ""
            lines.append(
                f"- `{file.path}` - {file.status}, {file.file_kind}, {file.language}, "
                f"+{file.additions}/-{file.deletions}{ignored}"
            )
            if file.ignore_reason:
                lines.append(f"  - Ignore reason: {file.ignore_reason}")
        lines.append("")

    lines.extend(["## Risk Signals", ""])
    active_signals = [signal for file in report.diff.files if not file.is_ignored for signal in file.risk_signals]
    if not active_signals:
        lines.append("No risk signals detected.")
        lines.append("")
    else:
        for severity in ("high", "medium", "low"):
            matching = [signal for signal in active_signals if signal.severity == severity]
            if not matching:
                continue
            lines.append(f"### {severity.title()}")
            lines.append("")
            for signal in matching:
                location = f"{signal.file}:{signal.line}" if signal.line else signal.file
                lines.append(f"- `{signal.kind}` at `{location}` - {signal.reason}")
            lines.append("")

    lines.extend(["## Project Rules", ""])
    if report.rules:
        for rule in report.rules:
            lines.append(f"- {rule}")
    else:
        lines.append("No project rules configured.")
    lines.append("")

    lines.extend(["## Memory", ""])
    memory = report.memory_summary
    if not memory.enabled:
        lines.append("Repo memory is disabled.")
    else:
        lines.append(f"- Loaded cards: `{memory.loaded_cards}`")
        lines.append(f"- Matched cards: `{memory.matched_cards}`")
        lines.append(f"- Applied cards: `{memory.applied_cards}`")
        lines.append(f"- Omitted cards: `{memory.omitted_cards}`")
        lines.append(f"- Prompt budget used: `{memory.total_prompt_chars}` chars")
        if memory.applied_card_ids:
            lines.append(f"- Applied card ids: {_format_list(memory.applied_card_ids[:20])}")
        if memory.omitted_card_reasons:
            lines.append("- Omitted card reasons:")
            for card_id, reason in list(memory.omitted_card_reasons.items())[:20]:
                lines.append(f"  - `{card_id}` - {reason}")
    lines.append("")

    lines.extend(["## Context Packs", ""])
    if not report.context_packs:
        lines.append("No context packs generated.")
        lines.append("")
    else:
        for pack in report.context_packs:
            symbol = _format_pack_symbols(pack)
            refs = len(pack.references)
            contracts = len(pack.contracts)
            metadata = len(pack.metadata)
            tests = len(pack.related_tests)
            rules = len(pack.rule_matches)
            memory_cards = len(pack.memory_matches)
            truncated = ", truncated" if pack.stats.truncated else ""
            lines.append(
                f"- `{pack.id}` - {symbol}, references: `{refs}`, contracts: `{contracts}`, "
                f"metadata: `{metadata}`, related tests: `{tests}`, rules: `{rules}`, "
                f"memory: `{memory_cards}`, "
                f"estimated chars: `{pack.stats.estimated_chars}`{truncated}"
            )
        lines.append("")

    lines.extend(["## Context Budget", ""])
    if not report.context_packs:
        lines.append("No context packs generated.")
        lines.append("")
    else:
        truncated = [pack for pack in report.context_packs if pack.stats.truncated]
        lines.append(f"- Packs: `{len(report.context_packs)}`")
        lines.append(f"- Truncated packs: `{len(truncated)}`")
        largest = sorted(report.context_packs, key=lambda pack: pack.stats.estimated_chars, reverse=True)[:5]
        lines.append("- Largest packs:")
        for pack in largest:
            lines.append(
                f"  - `{pack.id}` - `{pack.stats.estimated_chars}` chars, "
                f"diff lines: `{pack.stats.diff_lines}`, changed lines: `{pack.stats.changed_snippet_lines}`, "
                f"reference lines: `{pack.stats.reference_snippet_lines}`, "
                f"callee lines: `{pack.stats.callee_snippet_lines}`, "
                f"contract lines: `{pack.stats.contract_snippet_lines}`, "
                f"metadata lines: `{pack.stats.metadata_snippet_lines}`, "
                f"test lines: `{pack.stats.related_test_snippet_lines}`, "
                f"memory: `{pack.stats.memory_cards}` cards / `{pack.stats.memory_chars}` chars"
            )
        if truncated:
            lines.append("- Truncation notes:")
            for pack in truncated:
                notes = "; ".join(_summarize_notes(pack.stats.truncation_notes))
                lines.append(f"  - `{pack.id}` - {notes}")
        lines.append("")

    lines.extend(["## LLM Coverage", ""])
    coverage = report.llm_coverage
    if not coverage.enabled:
        lines.append("LLM review was not enabled.")
        lines.append("")
    else:
        lines.append(f"- Review context packs: `{coverage.reviewed_context_packs}` of `{coverage.total_context_packs}`")
        lines.append(
            f"- Deep/shallow reviewed packs: `{coverage.deep_reviewed_context_packs}` / "
            f"`{coverage.shallow_reviewed_context_packs}`"
        )
        lines.append(f"- Unreviewed context packs: `{coverage.unreviewed_context_packs}`")
        lines.append(f"- Coverage ratio: `{coverage.coverage_ratio:.1%}`")
        lines.append(f"- Source changed-line coverage: `{coverage.source_changed_line_coverage_ratio:.1%}`")
        lines.append(f"- High-risk coverage: `{coverage.high_risk_coverage_ratio:.1%}`")
        lines.append(f"- Coverage quality gate: `{coverage.quality_gate_status}`")
        lines.append(f"- Partial severity: `{coverage.partial_severity}`")
        lines.append(f"- Residual P0 packs: `{len(coverage.residual_risk_p0_context_pack_ids)}`")
        lines.append(f"- Residual P1 packs: `{len(coverage.residual_risk_p1_context_pack_ids)}`")
        lines.append(f"- Failed review runs: `{coverage.failed_review_runs}`")
        lines.append(f"- Failed verifier runs: `{coverage.failed_verify_runs}`")
        lines.append(f"- Max packs: `{coverage.max_packs}`")
        if coverage.max_deep_packs:
            lines.append(f"- Max deep packs: `{coverage.max_deep_packs}`")
        if coverage.max_input_tokens:
            lines.append(f"- Max input tokens: `~{coverage.max_input_tokens}`")
        lines.append(f"- Over-budget packs: `{len(coverage.over_budget_context_pack_ids)}`")
        lines.append(f"- Over-token-budget packs: `{len(coverage.over_token_budget_context_pack_ids)}`")
        lines.append(f"- Truncated packs: `{len(coverage.truncated_context_pack_ids)}`")
        lines.append(
            f"- Cluster/file packs: `{coverage.cluster_context_packs}` cluster / "
            f"`{coverage.file_context_packs}` file / `{coverage.symbol_context_packs}` symbol"
        )
        lines.append(f"- Cache hits: `{coverage.cache_hits}`")
        lines.append(f"- Cache misses: `{coverage.cache_misses}`")
        lines.append(f"- LLM duration: `{coverage.total_duration_ms}ms`")
        lines.append(
            f"- Estimated LLM input: `{coverage.input_chars}` chars (`~{coverage.estimated_input_tokens}` tokens)"
        )
        if coverage.routes:
            lines.append("- Model routes:")
            for route in coverage.routes:
                profile = f", profile: `{route.profile}`" if route.profile else ""
                model = f", model: `{route.model}`" if route.model else ""
                reason = f", route: `{route.route_reason}`" if route.route_reason else ""
                lines.append(
                    f"  - {route.kind}/{route.provider}, status: `{route.status}`{profile}{model}{reason}, "
                    f"runs: `{route.runs}`, findings: `{route.findings_count}`, "
                    f"input: `{route.input_chars}` chars (`~{route.estimated_input_tokens}` tokens), "
                    f"cache: `{route.cache_hits}` hits / `{route.cache_misses}` misses, "
                    f"duration: `{route.duration_ms}ms`"
                )
        if coverage.unreviewed_context_pack_ids:
            lines.append("- Unreviewed pack ids:")
            for pack_id in coverage.unreviewed_context_pack_ids[:20]:
                reason = _unreviewed_pack_reason(pack_id, coverage)
                lines.append(f"  - `{pack_id}` - {reason}")
            if len(coverage.unreviewed_context_pack_ids) > 20:
                lines.append(f"  - ... `{len(coverage.unreviewed_context_pack_ids) - 20}` more")
        if coverage.quality_gate_reasons:
            lines.append("- Quality gate reasons:")
            for reason in coverage.quality_gate_reasons:
                lines.append(f"  - {reason}")
        if coverage.partial_reasons:
            lines.append("- Partial reasons:")
            for reason in coverage.partial_reasons:
                lines.append(f"  - {reason}")
        if coverage.coverage_todos:
            lines.append("- Coverage todo:")
            for todo in coverage.coverage_todos[:20]:
                symbols = f", symbols: `{_format_list(todo.changed_symbols[:5])}`" if todo.changed_symbols else ""
                lines.append(f"  - `{todo.priority}` `{todo.slice}` `{todo.context_pack_id}` - {todo.reason}{symbols}")
                lines.append(f"    - Continue: `{todo.suggested_command}`")
            if len(coverage.coverage_todos) > 20:
                lines.append(f"  - ... `{len(coverage.coverage_todos) - 20}` more")
        if coverage.file_coverage:
            lines.append("- File coverage:")
            for file_coverage in coverage.file_coverage[:20]:
                residual = file_coverage.residual_priority or "none"
                reviewed_lines = _line_range_count(file_coverage.reviewed_changed_lines)
                unreviewed_lines = _line_range_count(file_coverage.unreviewed_changed_lines)
                lines.append(
                    f"  - `{file_coverage.file}` - reviewed "
                    f"`{file_coverage.reviewed_context_packs}/{file_coverage.total_context_packs}`, "
                    f"residual: `{residual}`, cluster/file/symbol: "
                    f"`{file_coverage.cluster_context_packs}`/`{file_coverage.file_context_packs}`/"
                    f"`{file_coverage.symbol_context_packs}`, changed lines: "
                    f"`{reviewed_lines}` reviewed / `{unreviewed_lines}` unreviewed"
                )
            if len(coverage.file_coverage) > 20:
                lines.append(f"  - ... `{len(coverage.file_coverage) - 20}` more")
        if coverage.slice_coverage:
            lines.append("- Slice coverage:")
            for slice_coverage in coverage.slice_coverage:
                residual = slice_coverage.residual_priority or "none"
                lines.append(
                    f"  - `{slice_coverage.slice}` - reviewed "
                    f"`{slice_coverage.reviewed_context_packs}/{slice_coverage.total_context_packs}`, "
                    f"deep/shallow: `{slice_coverage.deep_reviewed_context_packs}`/"
                    f"`{slice_coverage.shallow_reviewed_context_packs}`, high-risk: "
                    f"`{slice_coverage.reviewed_high_risk_context_packs}/"
                    f"{slice_coverage.high_risk_context_packs}`, residual: `{residual}`"
                )
        lines.append("")

    lines.extend(["## Findings", ""])
    if not report.findings:
        if report.llm_runs:
            lines.append("No LLM findings reported.")
        else:
            lines.append("LLM review was not run.")
        lines.append("")
    else:
        for severity in ("critical", "high", "medium", "low"):
            matching = [finding for finding in report.findings if finding.severity == severity]
            if not matching:
                continue
            lines.append(f"### {severity.title()}")
            lines.append("")
            for finding in matching:
                location = f"{finding.file}:{finding.line}" if finding.line else finding.file
                lines.append(f"- {finding.title} (`{finding.confidence}` confidence) at `{location}`")
                if finding.context_pack_id:
                    lines.append(f"  - Context pack: `{finding.context_pack_id}`")
                lines.append(f"  - Failure mode: {finding.failure_mode}")
                lines.append(f"  - Evidence: {finding.evidence}")
                lines.append(f"  - Suggested fix: {finding.suggested_fix}")
                lines.append(f"  - Suggested test: {finding.suggested_test}")
            lines.append("")

    lines.extend(["## Verifier", ""])
    if not report.verifications:
        lines.append("Verifier was not run.")
        lines.append("")
    else:
        approved = [verification for verification in report.verifications if verification.approved]
        rejected = [verification for verification in report.verifications if not verification.approved]
        lines.append(f"- Approved: `{len(approved)}`")
        lines.append(f"- Rejected: `{len(rejected)}`")
        if rejected:
            lines.append("")
            lines.append("### Rejected")
            lines.append("")
            for verification in rejected:
                lines.append(f"- {verification.finding.title} (`{verification.confidence}` confidence)")
                lines.append(f"  - Reason: {verification.reason}")
        lines.append("")

    lines.extend(["## LLM Runs", ""])
    if not report.llm_runs:
        lines.append("No LLM runs.")
    else:
        review_pack_ids = {run.context_pack_id for run in report.llm_runs if run.kind in {"review", "review_shallow"}}
        skipped_packs = max(0, len(report.context_packs) - len(review_pack_ids))
        lines.append(f"- Review context packs: `{len(review_pack_ids)}` of `{len(report.context_packs)}`")
        if skipped_packs:
            lines.append(
                f"- Skipped context packs: `{skipped_packs}` "
                "(test-only packs are kept as context and skipped when source packs are reviewed)"
            )
        lines.append("")
        for run in report.llm_runs:
            suffix = f", error: {run.error}" if run.error else ""
            cache = _format_run_cache(run)
            profile = f", profile: `{run.profile}`" if run.profile else ""
            model = f", model: `{run.model}`" if run.model else ""
            route = f", route: `{run.route_reason}`" if run.route_reason else ""
            lines.append(
                f"- `{run.context_pack_id}` - {run.kind}, {run.provider}, status: `{run.status}`, "
                f"prompt: `{run.prompt_version or 'unknown'}`, findings: `{run.findings_count}`, "
                f"cache: `{cache}`{profile}{model}{route}, "
                f"input: `{run.input_chars}` chars (`~{run.estimated_input_tokens}` tokens), "
                f"duration: `{run.duration_ms}ms`{suffix}"
            )
    lines.append("")

    lines.extend(["## Warnings", ""])
    warnings = [*report.diff.warnings]
    for analyzer_result in report.analyzer_results:
        warnings.extend(analyzer_result.warnings)

    if warnings:
        for warning in warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("No warnings.")
    lines.append("")

    return "\n".join(lines)


def render_html(report: ReviewReport) -> str:
    warnings = [*report.diff.warnings]
    for analyzer_result in report.analyzer_results:
        warnings.extend(analyzer_result.warnings)
    coverage = report.llm_coverage
    memory = report.memory_summary
    findings_html = "\n".join(_finding_html(finding) for finding in report.findings) or "<p>No findings.</p>"
    memory_html = _html_list(
        [
            f"<code>{escape(card_id)}</code> - {escape(reason)}"
            for card_id, reason in list(memory.omitted_card_reasons.items())[:50]
        ],
        empty="No omitted memory cards.",
    )
    unreviewed_html = _html_list(
        [
            f"<code>{escape(pack_id)}</code> - {escape(_unreviewed_pack_reason(pack_id, coverage))}"
            for pack_id in coverage.unreviewed_context_pack_ids[:50]
        ],
        empty="No unreviewed context packs.",
    )
    routes_html = _html_list(
        [
            (
                f"{escape(route.kind)}/{escape(route.provider)} "
                f"<code>{escape(route.profile or route.model or 'default')}</code> "
                f"({escape(route.status)}): {route.runs} runs, "
                f"{route.findings_count} findings, ~{route.estimated_input_tokens} input tokens, "
                f"{route.cache_hits} cache hits / {route.cache_misses} misses"
            )
            for route in coverage.routes
        ],
        empty="No model routes recorded.",
    )
    slices_html = _html_list(
        [
            (
                f"<code>{escape(item.slice)}</code>: "
                f"{item.reviewed_context_packs}/{item.total_context_packs} reviewed, "
                f"{item.deep_reviewed_context_packs}/{item.shallow_reviewed_context_packs} deep/shallow, "
                f"{item.reviewed_high_risk_context_packs}/{item.high_risk_context_packs} high-risk, "
                f"residual {escape(item.residual_priority or 'none')}"
            )
            for item in coverage.slice_coverage
        ],
        empty="No slice coverage recorded.",
    )
    warnings_html = _html_list([escape(warning) for warning in warnings], empty="No warnings.")
    context_rows = "\n".join(
        "<tr>"
        f"<td><code>{escape(pack.id)}</code></td>"
        f"<td>{escape(pack.file)}</td>"
        f"<td>{pack.stats.estimated_chars}</td>"
        f"<td>{pack.stats.memory_cards}</td>"
        f"<td>{'yes' if pack.stats.truncated else 'no'}</td>"
        f"<td>{escape('; '.join(_summarize_notes(pack.stats.truncation_notes)))}</td>"
        "</tr>"
        for pack in sorted(report.context_packs, key=lambda item: item.stats.estimated_chars, reverse=True)[:50]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Apex Ray Review</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2937; }}
    main {{ max-width: 1180px; margin: 0 auto; }}
    h1, h2 {{ color: #111827; }}
    code {{ background: #f3f4f6; padding: 1px 4px; border-radius: 4px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 16px 0; }}
    .metric {{ border: 1px solid #d1d5db; border-radius: 8px; padding: 12px; background: #fff; }}
    .metric strong {{ display: block; font-size: 24px; color: #111827; }}
    .finding {{ border-left: 4px solid #b91c1c; padding: 10px 12px; background: #fff7ed; margin: 12px 0; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f9fafb; }}
    details {{ border: 1px solid #d1d5db; border-radius: 8px; padding: 10px 12px; margin: 12px 0; }}
    summary {{ cursor: pointer; font-weight: 600; }}
  </style>
</head>
<body>
<main>
  <h1>Apex Ray Review</h1>
  <div class="grid">
    <div class="metric"><span>Files changed</span><strong>{report.diff.stats.files_changed}</strong></div>
    <div class="metric"><span>Findings</span><strong>{len(report.findings)}</strong></div>
    <div class="metric"><span>Context packs</span><strong>{len(report.context_packs)}</strong></div>
    <div class="metric"><span>LLM reviewed packs</span><strong>{coverage.reviewed_context_packs}/{coverage.total_context_packs}</strong></div>
    <div class="metric"><span>Deep / shallow packs</span><strong>{coverage.deep_reviewed_context_packs}/{coverage.shallow_reviewed_context_packs}</strong></div>
    <div class="metric"><span>Memory cards</span><strong>{memory.applied_cards}/{memory.loaded_cards}</strong></div>
    <div class="metric"><span>Coverage gate</span><strong>{escape(coverage.quality_gate_status)}</strong></div>
    <div class="metric"><span>Estimated input tokens</span><strong>~{coverage.estimated_input_tokens}</strong></div>
    <div class="metric"><span>LLM duration</span><strong>{coverage.total_duration_ms}ms</strong></div>
  </div>

  <h2>Findings</h2>
  {findings_html}

  <h2>LLM Coverage</h2>
  <ul>
    <li>Enabled: <code>{str(coverage.enabled).lower()}</code></li>
    <li>Coverage mode: <code>{escape(str(coverage.coverage_mode))}</code></li>
    <li>Review runs: <code>{coverage.review_runs}</code>; verifier runs: <code>{coverage.verify_runs}</code></li>
    <li>Source changed-line coverage: <code>{coverage.source_changed_line_coverage_ratio:.1%}</code></li>
    <li>High-risk coverage: <code>{coverage.high_risk_coverage_ratio:.1%}</code></li>
    <li>Cache: <code>{coverage.cache_hits}</code> hits / <code>{coverage.cache_misses}</code> misses</li>
    <li>Over-budget packs: <code>{len(coverage.over_budget_context_pack_ids)}</code></li>
    <li>Over-token-budget packs: <code>{len(coverage.over_token_budget_context_pack_ids)}</code></li>
    <li>Truncated packs: <code>{len(coverage.truncated_context_pack_ids)}</code></li>
    <li>Residual P0/P1 packs: <code>{len(coverage.residual_risk_p0_context_pack_ids)}</code> / <code>{len(coverage.residual_risk_p1_context_pack_ids)}</code></li>
    <li>Cluster/file/symbol packs: <code>{coverage.cluster_context_packs}</code> / <code>{coverage.file_context_packs}</code> / <code>{coverage.symbol_context_packs}</code></li>
  </ul>
  <details open><summary>Model Routes</summary>{routes_html}</details>
  <details><summary>Slice Coverage</summary>{slices_html}</details>
  <details><summary>Unreviewed Context Packs</summary>{unreviewed_html}</details>

  <h2>Memory</h2>
  <ul>
    <li>Enabled: <code>{str(memory.enabled).lower()}</code></li>
    <li>Loaded cards: <code>{memory.loaded_cards}</code></li>
    <li>Matched/applied/omitted: <code>{memory.matched_cards}</code> / <code>{memory.applied_cards}</code> / <code>{memory.omitted_cards}</code></li>
    <li>Prompt chars: <code>{memory.total_prompt_chars}</code></li>
  </ul>
  <details><summary>Omitted Memory Cards</summary>{memory_html}</details>

  <h2>Context Budget</h2>
  <table>
    <thead><tr><th>Pack</th><th>File</th><th>Chars</th><th>Memory</th><th>Truncated</th><th>Notes</th></tr></thead>
    <tbody>{context_rows or '<tr><td colspan="6">No context packs.</td></tr>'}</tbody>
  </table>

  <h2>Warnings</h2>
  {warnings_html}
</main>
</body>
</html>
"""


def _finding_html(finding: Finding) -> str:
    location = f"{finding.file}:{finding.line}" if finding.line else finding.file
    return (
        '<article class="finding">'
        f"<h3>{escape(finding.title)}</h3>"
        f"<p><strong>{escape(str(finding.severity)).title()}</strong> "
        f"at <code>{escape(location)}</code>, confidence <code>{escape(str(finding.confidence))}</code></p>"
        f"<p><strong>Failure mode:</strong> {escape(finding.failure_mode)}</p>"
        f"<p><strong>Evidence:</strong> {escape(finding.evidence)}</p>"
        f"<p><strong>Suggested fix:</strong> {escape(finding.suggested_fix)}</p>"
        f"<p><strong>Suggested test:</strong> {escape(finding.suggested_test)}</p>"
        f"<p><strong>Context pack:</strong> <code>{escape(finding.context_pack_id or 'n/a')}</code></p>"
        "</article>"
    )


def _html_list(items: list[str], *, empty: str) -> str:
    if not items:
        return f"<p>{escape(empty)}</p>"
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _summarize_notes(notes: list[str], limit: int = 8) -> list[str]:
    if not notes:
        return []
    counts = Counter(notes)
    ordered = []
    seen = set()
    for note in notes:
        if note in seen:
            continue
        seen.add(note)
        count = counts[note]
        ordered.append(f"{note} (x{count})" if count > 1 else note)
    if len(ordered) <= limit:
        return ordered
    hidden = len(ordered) - limit
    return [*ordered[:limit], f"... {hidden} more note types"]


def _format_list(values: list[str]) -> str:
    if not values:
        return "`none`"
    return ", ".join(f"`{value}`" for value in values)


def _build_memory_summary(config: ReviewConfig, context_packs: list[ContextPack]) -> MemorySummary:
    applied_ids: list[str] = []
    seen_applied_ids: set[str] = set()
    omitted_reasons: dict[str, str] = {}
    applied_count = 0
    omitted_count = 0
    prompt_chars = 0

    for pack in context_packs:
        applied_count += len(pack.memory_matches)
        omitted_count += len(pack.memory_omissions)
        prompt_chars += sum(match.prompt_chars for match in pack.memory_matches)
        for match in pack.memory_matches:
            if match.id not in seen_applied_ids:
                seen_applied_ids.add(match.id)
                applied_ids.append(match.id)
        for omission in pack.memory_omissions:
            omitted_reasons.setdefault(omission.id, omission.reason)

    return MemorySummary(
        enabled=config.memory.enabled,
        loaded_cards=len(config.memory_definitions),
        matched_cards=applied_count + omitted_count,
        applied_cards=applied_count,
        omitted_cards=omitted_count,
        applied_card_ids=applied_ids,
        omitted_card_reasons=omitted_reasons,
        total_prompt_chars=prompt_chars,
    )


def _build_llm_coverage(
    config: ReviewConfig,
    context_packs: list[ContextPack],
    llm_runs: list[LLMRun],
    llm_selection: LLMContextSelection | None = None,
) -> LLMCoverageSummary:
    review_runs = [run for run in llm_runs if run.kind in {"review", "review_shallow"}]
    deep_review_runs = [run for run in llm_runs if run.kind == "review"]
    shallow_review_runs = [run for run in llm_runs if run.kind == "review_shallow"]
    verify_runs = [run for run in llm_runs if run.kind == "verify"]
    successful_review_runs = [run for run in review_runs if run.status == "ok"]
    failed_review_runs = [run for run in review_runs if run.status != "ok"]
    failed_verify_runs = [run for run in verify_runs if run.status != "ok"]
    reviewed_ids = {run.context_pack_id for run in successful_review_runs}
    deep_reviewed_ids = {run.context_pack_id for run in deep_review_runs if run.status == "ok"}
    shallow_reviewed_ids = {run.context_pack_id for run in shallow_review_runs if run.status == "ok"}
    failed_review_by_pack_id = {run.context_pack_id: run for run in failed_review_runs}
    reviewed_pack_ids = [pack.id for pack in context_packs if pack.id in reviewed_ids]
    unreviewed_pack_ids = [pack.id for pack in context_packs if pack.id not in reviewed_ids]
    over_budget_pack_ids = [
        pack.id for pack in context_packs if pack.stats.estimated_chars > config.context.max_pack_chars
    ]
    if llm_selection is not None:
        over_budget_pack_ids = llm_selection.over_budget_context_pack_ids
    over_token_budget_pack_ids = llm_selection.over_token_budget_context_pack_ids if llm_selection is not None else []
    truncated_pack_ids = [pack.id for pack in context_packs if pack.stats.truncated]
    unreviewed_reasons = {}
    for pack_id in unreviewed_pack_ids:
        failed_run = failed_review_by_pack_id.get(pack_id)
        if failed_run is not None:
            unreviewed_reasons[pack_id] = failed_run.status
        else:
            unreviewed_reasons[pack_id] = _coverage_unreviewed_pack_reason(
                pack_id,
                enabled=config.llm.enabled,
                total_context_packs=len(context_packs),
                max_packs=config.llm.max_packs,
                over_budget_pack_ids=over_budget_pack_ids,
                llm_selection=llm_selection,
            )
    residual_risks = [
        _residual_risk_summary(pack, unreviewed_reasons[pack.id])
        for pack in context_packs
        if pack.id in unreviewed_reasons
    ]
    residual_p0_ids = [risk.context_pack_id for risk in residual_risks if risk.priority == "p0"]
    residual_p1_ids = [risk.context_pack_id for risk in residual_risks if risk.priority == "p1"]
    file_coverage = _build_file_coverage(context_packs, reviewed_ids, set(over_budget_pack_ids), residual_risks)
    slice_coverage = _build_slice_coverage(
        context_packs,
        reviewed_ids,
        deep_reviewed_ids,
        shallow_reviewed_ids,
        residual_risks,
    )
    source_line_coverage_ratio = _source_line_coverage_ratio(file_coverage)
    high_risk_ids = [pack.id for pack in context_packs if _is_high_risk_pack(pack)]
    reviewed_high_risk_ids = [pack_id for pack_id in high_risk_ids if pack_id in reviewed_ids]
    shallow_only_high_risk_ids = [
        pack_id for pack_id in high_risk_ids if pack_id in shallow_reviewed_ids and pack_id not in deep_reviewed_ids
    ]
    high_risk_coverage_ratio = (
        _coverage_ratio(len(reviewed_high_risk_ids), len(high_risk_ids)) if high_risk_ids else 1.0
    )
    quality_gate_status, quality_gate_reasons = _coverage_quality_gate(
        enabled=config.llm.enabled,
        total_context_packs=len(context_packs),
        coverage_ratio=_coverage_ratio(len(reviewed_pack_ids), len(context_packs)),
        source_line_coverage_ratio=source_line_coverage_ratio,
        high_risk_coverage_ratio=high_risk_coverage_ratio,
        min_source_line_coverage=config.llm.min_source_line_coverage,
        min_high_risk_coverage=config.llm.min_high_risk_coverage,
        residual_p0_count=len(residual_p0_ids),
        residual_p1_count=len(residual_p1_ids),
        shallow_only_high_risk_count=len(shallow_only_high_risk_ids),
        unreviewed_count=len(unreviewed_pack_ids),
    )
    partial_severity, partial_reasons = _coverage_partial_severity(
        enabled=config.llm.enabled,
        total_context_packs=len(context_packs),
        coverage_ratio=_coverage_ratio(len(reviewed_pack_ids), len(context_packs)),
        source_line_coverage_ratio=source_line_coverage_ratio,
        high_risk_coverage_ratio=high_risk_coverage_ratio,
        residual_p0_count=len(residual_p0_ids),
        residual_p1_count=len(residual_p1_ids),
        shallow_only_high_risk_count=len(shallow_only_high_risk_ids),
        failed_review_runs=len(failed_review_runs),
        failed_verify_runs=len(failed_verify_runs),
        unreviewed_count=len(unreviewed_pack_ids),
    )
    pack_statuses = _build_pack_statuses(
        context_packs,
        reviewed_ids,
        deep_reviewed_ids,
        shallow_reviewed_ids,
        unreviewed_reasons,
        residual_risks,
        failed_review_by_pack_id,
    )
    coverage_todos = _build_coverage_todos(residual_risks, context_packs)

    routes: dict[tuple[str, str, str | None, str | None, str | None, str], LLMRouteSummary] = {}
    for run in llm_runs:
        cache_hits = _run_cache_hits(run)
        cache_misses = _run_cache_misses(run)
        key = (run.kind, run.provider, run.model, run.profile, run.route_reason, run.status)
        route = routes.get(key)
        if route is None:
            route = LLMRouteSummary(
                kind=run.kind,
                provider=run.provider,
                model=run.model,
                profile=run.profile,
                route_reason=run.route_reason,
                status=run.status,
            )
            routes[key] = route
        route.runs += 1
        route.findings_count += run.findings_count
        route.duration_ms += run.duration_ms
        route.input_chars += run.input_chars
        route.estimated_input_tokens += run.estimated_input_tokens
        route.cache_hits += cache_hits
        route.cache_misses += cache_misses
        route.errors += 1 if run.error else 0

    return LLMCoverageSummary(
        enabled=config.llm.enabled,
        verify_enabled=config.llm.verify,
        max_packs=config.llm.max_packs,
        coverage_mode=config.llm.coverage_mode,
        max_deep_packs=config.llm.max_deep_packs,
        max_input_tokens=config.llm.max_input_tokens,
        total_context_packs=len(context_packs),
        reviewed_context_packs=len(reviewed_pack_ids),
        unreviewed_context_packs=len(unreviewed_pack_ids),
        coverage_ratio=_coverage_ratio(len(reviewed_pack_ids), len(context_packs)),
        source_changed_line_coverage_ratio=source_line_coverage_ratio,
        high_risk_coverage_ratio=high_risk_coverage_ratio,
        high_risk_context_packs=len(high_risk_ids),
        reviewed_high_risk_context_packs=len(reviewed_high_risk_ids),
        shallow_only_high_risk_context_pack_ids=shallow_only_high_risk_ids,
        quality_gate_status=quality_gate_status,
        quality_gate_reasons=quality_gate_reasons,
        partial_severity=partial_severity,
        partial_reasons=partial_reasons,
        reviewed_context_pack_ids=reviewed_pack_ids,
        unreviewed_context_pack_ids=unreviewed_pack_ids,
        unreviewed_context_pack_reasons=unreviewed_reasons,
        pack_statuses=pack_statuses,
        coverage_todos=coverage_todos,
        over_budget_context_pack_ids=over_budget_pack_ids,
        over_token_budget_context_pack_ids=over_token_budget_pack_ids,
        truncated_context_pack_ids=truncated_pack_ids,
        deep_selected_context_pack_ids=(
            llm_selection.deep_selected_context_pack_ids if llm_selection is not None else reviewed_pack_ids
        ),
        shallow_selected_context_pack_ids=(
            llm_selection.shallow_selected_context_pack_ids if llm_selection is not None else []
        ),
        deep_reviewed_context_pack_ids=[pack.id for pack in context_packs if pack.id in deep_reviewed_ids],
        shallow_reviewed_context_pack_ids=[pack.id for pack in context_packs if pack.id in shallow_reviewed_ids],
        deep_reviewed_context_packs=len(deep_reviewed_ids),
        shallow_reviewed_context_packs=len(shallow_reviewed_ids),
        residual_risk_p0_context_pack_ids=residual_p0_ids,
        residual_risk_p1_context_pack_ids=residual_p1_ids,
        residual_risk_context_packs=residual_risks,
        file_coverage=file_coverage,
        slice_coverage=slice_coverage,
        cluster_context_packs=sum(1 for pack in context_packs if _pack_scope(pack) == "cluster"),
        file_context_packs=sum(1 for pack in context_packs if _pack_scope(pack) == "file"),
        symbol_context_packs=sum(1 for pack in context_packs if _pack_scope(pack) == "symbol"),
        reviewed_files=sorted({pack.file for pack in context_packs if pack.id in reviewed_ids}),
        unreviewed_files=sorted({pack.file for pack in context_packs if pack.id not in reviewed_ids}),
        review_runs=len(review_runs),
        verify_runs=len(verify_runs),
        failed_review_runs=len(failed_review_runs),
        failed_verify_runs=len(failed_verify_runs),
        run_status_counts=dict(sorted(Counter(run.status for run in llm_runs).items())),
        total_duration_ms=sum(run.duration_ms for run in llm_runs),
        input_chars=sum(run.input_chars for run in llm_runs),
        estimated_input_tokens=sum(run.estimated_input_tokens for run in llm_runs),
        cache_hits=sum(_run_cache_hits(run) for run in llm_runs),
        cache_misses=sum(_run_cache_misses(run) for run in llm_runs),
        routes=sorted(
            routes.values(),
            key=lambda route: (
                route.kind,
                route.provider,
                route.profile or "",
                route.model or "",
                route.route_reason or "",
                route.status,
            ),
        ),
    )


def _run_cache_hits(run: LLMRun) -> int:
    if run.cache_hits or run.cache_misses:
        return run.cache_hits
    return 1 if run.cache_hit else 0


def _run_cache_misses(run: LLMRun) -> int:
    if run.cache_hits or run.cache_misses:
        return run.cache_misses
    return 1 if run.cache_key and not run.cache_hit else 0


def _format_run_cache(run: LLMRun) -> str:
    if run.cache_hits or run.cache_misses:
        return f"{run.cache_hits} hit / {run.cache_misses} miss"
    return "hit" if run.cache_hit else "miss" if run.cache_key else "off"


def _unreviewed_pack_reason(pack_id: str, coverage: LLMCoverageSummary) -> str:
    return coverage.unreviewed_context_pack_reasons.get(pack_id, "no review run recorded")


def _coverage_unreviewed_pack_reason(
    pack_id: str,
    *,
    enabled: bool,
    total_context_packs: int,
    max_packs: int,
    over_budget_pack_ids: list[str],
    llm_selection: LLMContextSelection | None = None,
) -> str:
    if llm_selection is not None and pack_id in llm_selection.skipped_context_pack_reasons:
        return llm_selection.skipped_context_pack_reasons[pack_id]
    if not enabled:
        return "LLM review disabled"
    if pack_id in over_budget_pack_ids:
        return "over context budget"
    if total_context_packs > max_packs:
        return "not selected by LLM pack cap or later filtering"
    return "no review run recorded"


def _coverage_ratio(reviewed_context_packs: int, total_context_packs: int) -> float:
    if total_context_packs == 0:
        return 0.0
    return round(reviewed_context_packs / total_context_packs, 4)


def _coverage_quality_gate(
    *,
    enabled: bool,
    total_context_packs: int,
    coverage_ratio: float,
    source_line_coverage_ratio: float,
    high_risk_coverage_ratio: float,
    min_source_line_coverage: float,
    min_high_risk_coverage: float,
    residual_p0_count: int,
    residual_p1_count: int,
    shallow_only_high_risk_count: int,
    unreviewed_count: int,
) -> tuple[str, list[str]]:
    if not enabled:
        return "disabled", ["LLM review disabled"]
    if total_context_packs == 0:
        return "pass", []
    reasons = []
    if residual_p0_count:
        reasons.append(f"Unreviewed P0 residual risk: {residual_p0_count} context pack(s)")
    if residual_p1_count:
        reasons.append(f"Unreviewed P1 residual risk: {residual_p1_count} context pack(s)")
    if shallow_only_high_risk_count:
        reasons.append(f"High-risk packs reviewed only shallowly: {shallow_only_high_risk_count}")
    gate_failures = []
    if min_source_line_coverage and source_line_coverage_ratio < min_source_line_coverage:
        gate_failures.append(
            f"Source changed-line coverage below threshold: "
            f"{source_line_coverage_ratio:.1%} < {min_source_line_coverage:.1%}"
        )
    if min_high_risk_coverage and high_risk_coverage_ratio < min_high_risk_coverage:
        gate_failures.append(
            f"High-risk coverage below threshold: {high_risk_coverage_ratio:.1%} < {min_high_risk_coverage:.1%}"
        )
    reasons.extend(gate_failures)
    if unreviewed_count and not reasons:
        reasons.append(f"Unreviewed context packs: {unreviewed_count}")
    if residual_p0_count or gate_failures:
        return "fail", reasons
    if reasons or coverage_ratio < 1.0:
        return "warn", reasons or [f"LLM coverage ratio below 100%: {coverage_ratio:.1%}"]
    return "pass", []


def _coverage_partial_severity(
    *,
    enabled: bool,
    total_context_packs: int,
    coverage_ratio: float,
    source_line_coverage_ratio: float,
    high_risk_coverage_ratio: float,
    residual_p0_count: int,
    residual_p1_count: int,
    shallow_only_high_risk_count: int,
    failed_review_runs: int,
    failed_verify_runs: int,
    unreviewed_count: int,
) -> tuple[Literal["none", "minor", "major", "critical"], list[str]]:
    if not enabled or total_context_packs == 0:
        return "none", []
    reasons: list[str] = []
    if residual_p0_count:
        reasons.append(f"{residual_p0_count} unreviewed P0 context pack(s)")
    if residual_p1_count:
        reasons.append(f"{residual_p1_count} unreviewed P1 context pack(s)")
    if shallow_only_high_risk_count:
        reasons.append(f"{shallow_only_high_risk_count} high-risk context pack(s) only reviewed shallowly")
    if failed_review_runs:
        reasons.append(f"{failed_review_runs} review run(s) failed")
    if failed_verify_runs:
        reasons.append(f"{failed_verify_runs} verifier run(s) failed")
    if coverage_ratio < 1.0 and not reasons:
        reasons.append(f"{unreviewed_count} context pack(s) unreviewed")

    if residual_p0_count or high_risk_coverage_ratio < 1.0:
        return "critical", reasons
    if failed_review_runs or failed_verify_runs:
        return "major", reasons
    if residual_p1_count or shallow_only_high_risk_count or source_line_coverage_ratio < 1.0:
        return "major", reasons
    if coverage_ratio < 1.0:
        return "minor", reasons
    return "none", []


def _residual_risk_summary(pack: ContextPack, reason: str) -> LLMResidualRiskSummary:
    risk_by_severity = _pack_risk_by_severity(pack)
    rule_modes = Counter(str(rule.mode) for rule in pack.rule_matches)
    rule_severities = Counter(str(rule.severity) for rule in pack.rule_matches)
    priority = _residual_priority(pack, risk_by_severity, rule_modes, rule_severities)
    return LLMResidualRiskSummary(
        context_pack_id=pack.id,
        file=pack.file,
        file_kind=pack.file_kind,
        priority=priority,
        reason=reason,
        risk_by_severity=dict(sorted(risk_by_severity.items())),
        rule_modes=dict(sorted(rule_modes.items())),
        rule_severities=dict(sorted(rule_severities.items())),
        estimated_chars=pack.stats.estimated_chars,
        truncated=pack.stats.truncated,
    )


def _residual_priority(
    pack: ContextPack,
    risk_by_severity: Counter[str],
    rule_modes: Counter[str],
    rule_severities: Counter[str],
) -> str:
    if (
        risk_by_severity.get("high", 0)
        or rule_modes.get("strict", 0)
        or rule_severities.get("critical", 0)
        or rule_severities.get("high", 0)
    ):
        return "p0"
    if (
        risk_by_severity.get("medium", 0)
        or pack.file_kind in {FileKind.SOURCE, FileKind.SCHEMA, FileKind.MIGRATION, FileKind.CONFIG}
        or pack.stats.truncated
    ):
        return "p1"
    return "p2"


def _build_pack_statuses(
    context_packs: list[ContextPack],
    reviewed_ids: set[str],
    deep_reviewed_ids: set[str],
    shallow_reviewed_ids: set[str],
    unreviewed_reasons: dict[str, str],
    residual_risks: list[LLMResidualRiskSummary],
    failed_review_by_pack_id: dict[str, LLMRun],
) -> list[LLMPackReviewStatus]:
    residual_by_pack_id = {risk.context_pack_id: risk for risk in residual_risks}
    statuses: list[LLMPackReviewStatus] = []
    for pack in context_packs:
        review_depth: Literal["deep", "shallow"] | None = None
        reason = ""
        error = None
        if pack.id in deep_reviewed_ids:
            status = "reviewed_deep"
            review_depth = "deep"
        elif pack.id in shallow_reviewed_ids:
            status = "reviewed_shallow"
            review_depth = "shallow"
        elif pack.id in failed_review_by_pack_id:
            failed_run = failed_review_by_pack_id[pack.id]
            status = failed_run.status
            reason = failed_run.status
            error = failed_run.error
        else:
            reason = unreviewed_reasons.get(pack.id, "no review run recorded")
            status = _pack_status_for_unreviewed_reason(reason)
        residual = residual_by_pack_id.get(pack.id)
        statuses.append(
            LLMPackReviewStatus(
                context_pack_id=pack.id,
                file=pack.file,
                file_kind=pack.file_kind,
                status=status,
                priority=residual.priority if residual else None,
                slice=_pack_review_slice(pack),
                reason=reason,
                review_depth=review_depth,
                estimated_chars=pack.stats.estimated_chars,
                changed_lines=pack.changed_lines,
                changed_symbols=_pack_symbol_names([pack]),
                error=error,
            )
        )
    return statuses


def _pack_status_for_unreviewed_reason(reason: str) -> str:
    if reason == "over context budget":
        return "skipped_context_too_large"
    if reason == "not selected by LLM token budget":
        return "skipped_token_budget"
    if reason == "not selected by LLM pack cap":
        return "skipped_pack_cap"
    if reason == "LLM review disabled":
        return "skipped_llm_disabled"
    return "unreviewed"


def _build_coverage_todos(
    residual_risks: list[LLMResidualRiskSummary],
    context_packs: list[ContextPack],
) -> list[LLMCoverageTodo]:
    packs_by_id = {pack.id: pack for pack in context_packs}
    priority_rank = {"p0": 0, "p1": 1, "p2": 2}
    ordered = sorted(
        residual_risks,
        key=lambda risk: (
            priority_rank.get(risk.priority, 9),
            -risk.estimated_chars,
            risk.file,
            risk.context_pack_id,
        ),
    )
    todos = []
    for risk in ordered:
        pack = packs_by_id.get(risk.context_pack_id)
        if pack is None:
            continue
        todos.append(
            LLMCoverageTodo(
                context_pack_id=pack.id,
                file=pack.file,
                file_kind=pack.file_kind,
                priority=risk.priority,
                slice=_pack_review_slice(pack),
                reason=risk.reason,
                suggested_command=_continue_command_for_pack(pack.id),
                estimated_chars=pack.stats.estimated_chars,
                changed_lines=pack.changed_lines,
                changed_symbols=_pack_symbol_names([pack]),
            )
        )
    return todos


def _continue_command_for_pack(pack_id: str) -> str:
    safe_id = pack_id.replace("'", "'\"'\"'")
    return f"apex-ray review --continue-from <report.json> --only-pack '{safe_id}' --llm"


def _build_file_coverage(
    context_packs: list[ContextPack],
    reviewed_ids: set[str],
    over_budget_ids: set[str],
    residual_risks: list[LLMResidualRiskSummary],
) -> list[LLMFileCoverageSummary]:
    residual_by_pack_id = {risk.context_pack_id: risk for risk in residual_risks}
    file_order: list[str] = []
    packs_by_file: dict[str, list[ContextPack]] = {}
    for pack in context_packs:
        if pack.file not in packs_by_file:
            packs_by_file[pack.file] = []
            file_order.append(pack.file)
        packs_by_file[pack.file].append(pack)

    summaries = []
    for file in file_order:
        packs = packs_by_file[file]
        reviewed_pack_ids = [pack.id for pack in packs if pack.id in reviewed_ids]
        unreviewed_pack_ids = [pack.id for pack in packs if pack.id not in reviewed_ids]
        residual_priority = _highest_residual_priority(
            residual_by_pack_id[pack_id].priority for pack_id in unreviewed_pack_ids if pack_id in residual_by_pack_id
        )
        risk_by_severity: Counter[str] = Counter()
        for pack in packs:
            risk_by_severity.update(_pack_risk_by_severity(pack))
        reviewed_packs = [pack for pack in packs if pack.id in reviewed_ids]
        unreviewed_packs = [pack for pack in packs if pack.id not in reviewed_ids]
        reviewed_changed_lines = _merge_line_ranges(range_ for pack in reviewed_packs for range_ in pack.changed_lines)
        unreviewed_changed_lines = _subtract_line_ranges(
            _merge_line_ranges(range_ for pack in unreviewed_packs for range_ in pack.changed_lines),
            reviewed_changed_lines,
        )
        reviewed_changed_symbols = _pack_symbol_names(reviewed_packs)
        reviewed_symbol_names = set(reviewed_changed_symbols)
        unreviewed_changed_symbols = [
            name for name in _pack_symbol_names(unreviewed_packs) if name not in reviewed_symbol_names
        ]
        summaries.append(
            LLMFileCoverageSummary(
                file=file,
                file_kind=packs[0].file_kind,
                total_context_packs=len(packs),
                reviewed_context_packs=len(reviewed_pack_ids),
                unreviewed_context_packs=len(unreviewed_pack_ids),
                cluster_context_packs=sum(1 for pack in packs if _pack_scope(pack) == "cluster"),
                file_context_packs=sum(1 for pack in packs if _pack_scope(pack) == "file"),
                symbol_context_packs=sum(1 for pack in packs if _pack_scope(pack) == "symbol"),
                over_budget_context_packs=sum(1 for pack in packs if pack.id in over_budget_ids),
                truncated_context_packs=sum(1 for pack in packs if pack.stats.truncated),
                risk_by_severity=dict(sorted(risk_by_severity.items())),
                residual_priority=residual_priority,
                reviewed_changed_lines=reviewed_changed_lines,
                unreviewed_changed_lines=unreviewed_changed_lines,
                reviewed_changed_symbols=reviewed_changed_symbols,
                unreviewed_changed_symbols=unreviewed_changed_symbols,
                reviewed_context_pack_ids=reviewed_pack_ids,
                unreviewed_context_pack_ids=unreviewed_pack_ids,
            )
        )
    return summaries


def _build_slice_coverage(
    context_packs: list[ContextPack],
    reviewed_ids: set[str],
    deep_reviewed_ids: set[str],
    shallow_reviewed_ids: set[str],
    residual_risks: list[LLMResidualRiskSummary],
) -> list[LLMSliceCoverageSummary]:
    residual_by_pack_id = {risk.context_pack_id: risk for risk in residual_risks}
    slice_order: list[str] = []
    packs_by_slice: dict[str, list[ContextPack]] = {}
    for pack in context_packs:
        slice_name = _pack_review_slice(pack)
        if slice_name not in packs_by_slice:
            packs_by_slice[slice_name] = []
            slice_order.append(slice_name)
        packs_by_slice[slice_name].append(pack)

    summaries: list[LLMSliceCoverageSummary] = []
    for slice_name in sorted(slice_order, key=_slice_sort_key):
        packs = packs_by_slice[slice_name]
        reviewed_pack_ids = [pack.id for pack in packs if pack.id in reviewed_ids]
        unreviewed_pack_ids = [pack.id for pack in packs if pack.id not in reviewed_ids]
        high_risk_pack_ids = [pack.id for pack in packs if _is_high_risk_pack(pack)]
        residual_priority = _highest_residual_priority(
            residual_by_pack_id[pack_id].priority for pack_id in unreviewed_pack_ids if pack_id in residual_by_pack_id
        )
        summaries.append(
            LLMSliceCoverageSummary(
                slice=slice_name,
                total_context_packs=len(packs),
                reviewed_context_packs=len(reviewed_pack_ids),
                unreviewed_context_packs=len(unreviewed_pack_ids),
                deep_reviewed_context_packs=sum(1 for pack in packs if pack.id in deep_reviewed_ids),
                shallow_reviewed_context_packs=sum(1 for pack in packs if pack.id in shallow_reviewed_ids),
                high_risk_context_packs=len(high_risk_pack_ids),
                reviewed_high_risk_context_packs=sum(1 for pack_id in high_risk_pack_ids if pack_id in reviewed_ids),
                residual_priority=residual_priority,
                reviewed_context_pack_ids=reviewed_pack_ids,
                unreviewed_context_pack_ids=unreviewed_pack_ids,
            )
        )
    return summaries


def _pack_review_slice(pack: ContextPack) -> str:
    if _is_high_risk_pack(pack):
        return "high_risk"
    if pack.file_kind in {FileKind.SCHEMA, FileKind.CONFIG, FileKind.MIGRATION, FileKind.DEPENDENCY}:
        return "contracts_config"
    if pack.file_kind == FileKind.SOURCE:
        return "source"
    if pack.file_kind == FileKind.TEST:
        return "tests"
    if pack.file_kind == FileKind.DOCS:
        return "docs"
    return "other"


def _slice_sort_key(slice_name: str) -> tuple[int, str]:
    order = {
        "high_risk": 0,
        "contracts_config": 1,
        "source": 2,
        "tests": 3,
        "docs": 4,
        "other": 5,
    }
    return (order.get(slice_name, 99), slice_name)


def _source_line_coverage_ratio(file_coverage: list[LLMFileCoverageSummary]) -> float:
    reviewed = 0
    total = 0
    for summary in file_coverage:
        if summary.file_kind != FileKind.SOURCE:
            continue
        reviewed_lines = _line_range_count(summary.reviewed_changed_lines)
        unreviewed_lines = _line_range_count(summary.unreviewed_changed_lines)
        reviewed += reviewed_lines
        total += reviewed_lines + unreviewed_lines
    if total == 0:
        return 1.0
    return _coverage_ratio(reviewed, total)


def _is_high_risk_pack(pack: ContextPack) -> bool:
    if any(str(signal.severity) == "high" for signal in pack.risk_signals):
        return True
    if any(str(rule.mode) == "strict" for rule in pack.rule_matches):
        return True
    return any(str(rule.severity) in {"critical", "high"} for rule in pack.rule_matches)


def _pack_risk_by_severity(pack: ContextPack) -> Counter[str]:
    return Counter(str(signal.severity) for signal in pack.risk_signals)


def _merge_line_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((min(start, end), max(start, end)) for start, end in ranges if start > 0 and end > 0)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end + 1:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _line_range_count(ranges: list[tuple[int, int]]) -> int:
    return sum(max(0, end - start + 1) for start, end in ranges)


def _subtract_line_ranges(
    ranges: list[tuple[int, int]],
    covered_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    remaining: list[tuple[int, int]] = []
    for start, end in ranges:
        fragments = [(start, end)]
        for covered_start, covered_end in covered_ranges:
            next_fragments: list[tuple[int, int]] = []
            for fragment_start, fragment_end in fragments:
                if covered_end < fragment_start or covered_start > fragment_end:
                    next_fragments.append((fragment_start, fragment_end))
                    continue
                if fragment_start < covered_start:
                    next_fragments.append((fragment_start, covered_start - 1))
                if covered_end < fragment_end:
                    next_fragments.append((covered_end + 1, fragment_end))
            fragments = next_fragments
            if not fragments:
                break
        remaining.extend(fragments)
    return _merge_line_ranges(remaining)


def _pack_symbol_names(packs: list[ContextPack]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for pack in packs:
        symbols = pack.symbols or ([pack.symbol] if pack.symbol is not None else [])
        for symbol in symbols:
            if symbol is None or symbol.name in seen:
                continue
            seen.add(symbol.name)
            names.append(symbol.name)
    return names


def _highest_residual_priority(priorities: Iterable[str]) -> str | None:
    priority_order = {"p0": 3, "p1": 2, "p2": 1}
    highest = None
    for priority in priorities:
        if highest is None or priority_order.get(str(priority), 0) > priority_order.get(highest, 0):
            highest = str(priority)
    return highest


def _pack_scope(pack: ContextPack) -> str:
    if "#cluster:" in pack.id or len(pack.symbols) > 1:
        return "cluster"
    if pack.symbol is not None or pack.symbols:
        return "symbol"
    return "file"


def _format_pack_symbols(pack: ContextPack) -> str:
    if pack.symbols:
        names = ", ".join(f"{symbol.kind} `{symbol.name}`" for symbol in pack.symbols)
        return names
    if pack.symbol:
        return f"{pack.symbol.kind} `{pack.symbol.name}`"
    return "file-level context"
