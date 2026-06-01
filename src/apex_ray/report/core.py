from collections import Counter
from datetime import UTC, datetime

from apex_ray import __version__
from apex_ray.models import (
    AnalyzerResult,
    ContextPack,
    DiffSummary,
    Finding,
    FindingVerification,
    LLMContextSelection,
    LLMRun,
    MemorySummary,
    ProjectProfile,
    ReportSummary,
    ReviewConfig,
    ReviewReport,
)
from apex_ray.report.coverage import (
    _build_llm_coverage,
    _format_pack_symbols,
    _format_run_cache,
    _line_range_count,
    _unreviewed_pack_reason,
)
from apex_ray.report.formatting import format_list as _format_list
from apex_ray.report.formatting import summarize_notes as _summarize_notes


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
    ]

    _append_findings_section(lines, report)

    lines.extend(["## Changed Files", ""])

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
        if coverage.actual_total_tokens:
            lines.append(
                f"- Provider-reported LLM tokens: `{coverage.actual_total_tokens}` total "
                f"(`{coverage.actual_input_tokens}` input, `{coverage.actual_cached_input_tokens}` cached input, "
                f"`{coverage.actual_output_tokens}` output, `{coverage.actual_reasoning_output_tokens}` reasoning)"
            )
        if coverage.estimated_saved_input_tokens:
            lines.append(f"- Estimated cache-saved input: `~{coverage.estimated_saved_input_tokens}` tokens")
        if coverage.estimated_cost_usd is not None:
            lines.append(f"- Estimated provider cost: `${coverage.estimated_cost_usd:.6f}`")
        if coverage.usage_sources:
            lines.append(f"- Usage sources: `{', '.join(coverage.usage_sources)}`")
        if coverage.routes:
            lines.append("- Model routes:")
            for route in coverage.routes:
                profile = f", profile: `{route.profile}`" if route.profile else ""
                model = f", model: `{route.model}`" if route.model else ""
                effort = f", effort: `{route.effort}`" if route.effort else ""
                reason = f", route: `{route.route_reason}`" if route.route_reason else ""
                actual = f", actual: `{route.actual_total_tokens}` tokens" if route.actual_total_tokens else ""
                saved = (
                    f", saved: `~{route.estimated_saved_input_tokens}` tokens"
                    if route.estimated_saved_input_tokens
                    else ""
                )
                lines.append(
                    f"  - {route.kind}/{route.provider}, status: `{route.status}`{profile}{model}{effort}{reason}, "
                    f"runs: `{route.runs}`, findings: `{route.findings_count}`, "
                    f"input: `{route.input_chars}` chars (`~{route.estimated_input_tokens}` tokens), "
                    f"cache: `{route.cache_hits}` hits / `{route.cache_misses}` misses{actual}{saved}, "
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
            effort = f", effort: `{run.effort}`" if run.effort else ""
            route = f", route: `{run.route_reason}`" if run.route_reason else ""
            actual = f", actual: `{run.actual_total_tokens}` tokens" if run.actual_total_tokens else ""
            saved = f", saved: `~{run.estimated_saved_input_tokens}` tokens" if run.estimated_saved_input_tokens else ""
            lines.append(
                f"- `{run.context_pack_id}` - {run.kind}, {run.provider}, status: `{run.status}`, "
                f"prompt: `{run.prompt_version or 'unknown'}`, findings: `{run.findings_count}`, "
                f"cache: `{cache}`{profile}{model}{effort}{route}, "
                f"input: `{run.input_chars}` chars (`~{run.estimated_input_tokens}` tokens), "
                f"duration: `{run.duration_ms}ms`{actual}{saved}{suffix}"
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


def _append_findings_section(lines: list[str], report: ReviewReport) -> None:
    lines.extend(["## Findings", ""])
    if not report.findings:
        if report.llm_runs:
            lines.append("No LLM findings reported.")
        else:
            lines.append("LLM review was not run.")
        lines.append("")
        return
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
