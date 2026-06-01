from pathlib import Path
from typing import Literal

from apex_ray.analyzers import AnalyzerError, run_typescript_analyzer
from apex_ray.classify import classify_diff
from apex_ray.context import build_context_packs
from apex_ray.diff import parse_unified_diff
from apex_ray.discovery import discover_project
from apex_ray.llm import (
    LLMProvider,
    LLMProviderError,
    review_context_packs,
    verify_findings,
)
from apex_ray.models import (
    ContextPack,
    DiffSummary,
    ReviewConfig,
    ReviewReport,
    TargetMode,
)
from apex_ray.pipeline.findings import consolidate_findings
from apex_ray.pipeline.selection import merge_continuation_selection as _merge_continuation_selection
from apex_ray.pipeline.selection import (
    plan_llm_context_selection,
    select_continuation_context_packs,
)
from apex_ray.progress import NoopProgress, ProgressSink
from apex_ray.report import build_report


def run_review_pipeline(
    repo_root: Path,
    diff_text: str,
    target_mode: TargetMode,
    config: ReviewConfig,
    base: str | None = None,
    config_path: Path | None = None,
    provider: LLMProvider | None = None,
    progress: ProgressSink | None = None,
) -> ReviewReport:
    progress = progress or NoopProgress()
    progress.event("parsing diff", force=True)
    diff_summary = parse_unified_diff(diff_text, target_mode=target_mode, base=base)
    diff_summary = classify_diff(diff_summary, config.ignore)
    apply_language_filter(diff_summary, config.languages)
    progress.event(
        f"parsed diff: {diff_summary.stats.files_changed} file(s), "
        f"+{diff_summary.stats.additions}/-{diff_summary.stats.deletions}, "
        f"{diff_summary.stats.ignored_files} ignored",
        force=True,
    )

    progress.event("discovering project", force=True)
    project = discover_project(repo_root, ignored_patterns=config.ignore, config_path=config_path)
    analyzer_results = []
    fallback_reasons_by_path: dict[str, str] = {}
    try:
        progress.event(f"running TypeScript analyzer on {len(diff_summary.files)} changed file(s)", force=True)
        ts_result = run_typescript_analyzer(repo_root, diff_summary.files, config.analyzer)
        if ts_result:
            analyzer_results.append(ts_result)
            for failed_path in ts_result.failed_files:
                fallback_reasons_by_path[failed_path] = (
                    "TypeScript analyzer shard failed; using diff-only fallback context."
                )
            progress.event(
                f"TypeScript analyzer completed: {sum(len(file.symbols) for file in ts_result.files)} symbol(s), "
                f"{len(ts_result.failed_files)} failed file(s)",
                force=True,
            )
        else:
            progress.event("TypeScript analyzer skipped", force=True)
    except AnalyzerError as exc:
        warning = str(exc)
        diff_summary.warnings.append(warning)
        for changed_file in diff_summary.files:
            fallback_reasons_by_path[changed_file.path] = f"TypeScript analyzer unavailable: {warning}"
        progress.event(f"TypeScript analyzer unavailable: {warning}", force=True)

    progress.event("building context packs", force=True)
    context_packs = build_context_packs(
        analyzer_results,
        diff_summary.files,
        config,
        repo_root=repo_root,
        fallback_reasons_by_path=fallback_reasons_by_path,
    )
    progress.event(f"built {len(context_packs)} context pack(s)", force=True)
    findings = []
    verifications = []
    llm_runs = []
    llm_selection = None
    if config.llm.enabled:
        progress.event("planning LLM context selection", force=True)
        llm_selection = plan_llm_context_selection(
            context_packs,
            diff_summary.files,
            max_packs=config.llm.max_packs,
            max_deep_packs=config.llm.max_deep_packs,
            max_input_tokens=config.llm.max_input_tokens,
            max_pack_chars=config.context.max_pack_chars,
            coverage_mode=config.llm.coverage_mode,
        )
        deep_selected_ids = set(llm_selection.deep_selected_context_pack_ids)
        shallow_selected_ids = set(llm_selection.shallow_selected_context_pack_ids)
        selected_ids = deep_selected_ids | shallow_selected_ids
        deep_context_packs = [pack for pack in context_packs if pack.id in deep_selected_ids]
        shallow_context_packs = [pack for pack in context_packs if pack.id in shallow_selected_ids]
        llm_context_packs = [pack for pack in context_packs if pack.id in selected_ids]
        progress.event(
            f"selected {len(llm_context_packs)} LLM context pack(s): "
            f"{len(deep_context_packs)} deep, {len(shallow_context_packs)} shallow, "
            f"{len(context_packs) - len(llm_context_packs)} unreviewed",
            force=True,
        )
        capped_pack_ids = [
            pack_id
            for pack_id, reason in llm_selection.skipped_context_pack_reasons.items()
            if reason == "not selected by LLM pack cap"
        ]
        if capped_pack_ids:
            diff_summary.warnings.append(
                f"LLM review limited to {len(llm_context_packs)} of {len(context_packs)} context packs "
                f"by review.llm.max_packs={config.llm.max_packs}."
            )
        token_capped_pack_ids = [
            pack_id
            for pack_id, reason in llm_selection.skipped_context_pack_reasons.items()
            if reason == "not selected by LLM token budget"
        ]
        if token_capped_pack_ids:
            diff_summary.warnings.append(
                f"LLM review left {len(token_capped_pack_ids)} context pack(s) unreviewed "
                f"by review.llm.max_input_tokens={config.llm.max_input_tokens}."
            )
        over_budget_pack_ids = llm_selection.over_budget_context_pack_ids
        if over_budget_pack_ids:
            diff_summary.warnings.append(
                "Skipped LLM review for over-budget context pack(s): " + ", ".join(over_budget_pack_ids)
            )
        if not llm_context_packs:
            if over_budget_pack_ids:
                diff_summary.warnings.append("LLM review requested, but all context packs were over budget.")
            elif token_capped_pack_ids:
                diff_summary.warnings.append("LLM review requested, but the token budget selected no context packs.")
            else:
                diff_summary.warnings.append("LLM review requested, but no context packs were generated.")
        else:
            try:
                shallow_findings, shallow_runs = review_context_packs(
                    shallow_context_packs,
                    config.llm,
                    repo_root,
                    provider=provider,
                    review_depth="shallow",
                    progress=progress,
                )
                deep_findings, deep_runs = review_context_packs(
                    deep_context_packs,
                    config.llm,
                    repo_root,
                    provider=provider,
                    review_depth="deep",
                    progress=progress,
                )
                findings = consolidate_findings([*shallow_findings, *deep_findings])
                llm_runs = [*shallow_runs, *deep_runs]
                if config.llm.verify and findings:
                    progress.event(f"verifying {len(findings)} finding(s)", force=True)
                    findings, verifications, verifier_runs = verify_findings(
                        findings,
                        context_packs,
                        config.llm,
                        repo_root,
                        provider=provider,
                        progress=progress,
                    )
                    llm_runs.extend(verifier_runs)
                findings = consolidate_findings(findings)
            except LLMProviderError:
                raise
    else:
        progress.event("LLM review disabled", force=True)

    progress.event("building report", force=True)
    return build_report(
        project,
        config,
        diff_summary,
        analyzer_results=analyzer_results,
        context_packs=context_packs,
        findings=findings,
        verifications=verifications,
        llm_runs=llm_runs,
        llm_selection=llm_selection,
    )


def continue_review_from_report(
    report: ReviewReport,
    *,
    repo_root: Path | None = None,
    config: ReviewConfig | None = None,
    residual_priorities: set[str] | None = None,
    slices: set[str] | None = None,
    pack_ids: set[str] | None = None,
    only_unreviewed: bool = True,
    review_depth: Literal["deep", "shallow"] = "deep",
    provider: LLMProvider | None = None,
    progress: ProgressSink | None = None,
) -> tuple[ReviewReport, list[ContextPack]]:
    progress = progress or NoopProgress()
    effective_config = config.model_copy(deep=True) if config is not None else report.config.model_copy(deep=True)
    root = repo_root or Path(report.project.root)
    selected_packs = select_continuation_context_packs(
        report,
        residual_priorities=residual_priorities,
        slices=slices,
        pack_ids=pack_ids,
        only_unreviewed=only_unreviewed,
    )
    progress.event(f"selected {len(selected_packs)} continuation context pack(s)", force=True)
    if not selected_packs:
        return (
            build_report(
                report.project,
                effective_config,
                report.diff,
                analyzer_results=report.analyzer_results,
                context_packs=report.context_packs,
                findings=report.findings,
                verifications=report.verifications,
                llm_runs=report.llm_runs,
                llm_selection=report.llm_selection,
            ),
            [],
        )

    if not effective_config.llm.enabled:
        diff_summary = report.diff.model_copy(deep=True)
        warning = "LLM review is disabled; pass --llm or enable review.llm.enabled to review continuation packs."
        if warning not in diff_summary.warnings:
            diff_summary.warnings.append(warning)
        return (
            build_report(
                report.project,
                effective_config,
                diff_summary,
                analyzer_results=report.analyzer_results,
                context_packs=report.context_packs,
                findings=report.findings,
                verifications=report.verifications,
                llm_runs=report.llm_runs,
                llm_selection=report.llm_selection,
            ),
            selected_packs,
        )

    new_findings, review_runs = review_context_packs(
        selected_packs,
        effective_config.llm,
        root,
        provider=provider,
        review_depth=review_depth,
        progress=progress,
    )
    llm_runs = [*report.llm_runs, *review_runs]
    verifications = list(report.verifications)
    approved_new_findings = new_findings
    if effective_config.llm.verify and new_findings:
        approved_new_findings, new_verifications, verifier_runs = verify_findings(
            new_findings,
            report.context_packs,
            effective_config.llm,
            root,
            provider=provider,
            progress=progress,
        )
        verifications.extend(new_verifications)
        llm_runs.extend(verifier_runs)
    findings = consolidate_findings([*report.findings, *approved_new_findings])
    llm_selection = _merge_continuation_selection(
        report.llm_selection,
        report.context_packs,
        selected_packs,
        review_depth=review_depth,
    )
    return (
        build_report(
            report.project,
            effective_config,
            report.diff,
            analyzer_results=report.analyzer_results,
            context_packs=report.context_packs,
            findings=findings,
            verifications=verifications,
            llm_runs=llm_runs,
            llm_selection=llm_selection,
        ),
        selected_packs,
    )


def apply_language_filter(diff_summary: DiffSummary, languages: list[str]) -> None:
    if not languages:
        return
    allowed = set(languages)
    for file in diff_summary.files:
        if file.is_ignored or file.language in allowed:
            continue
        file.is_ignored = True
        file.ignore_reason = f"Language not enabled: {file.language}"
        file.risk_signals = []
        for hunk in file.hunks:
            hunk.risk_signals = []
    diff_summary.stats.ignored_files = sum(1 for file in diff_summary.files if file.is_ignored)
