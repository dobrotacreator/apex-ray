import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from apex_ray.analyzers import run_analyzers
from apex_ray.classify import classify_diff
from apex_ray.context import build_context_packs
from apex_ray.diff import parse_unified_diff
from apex_ray.discovery import discover_project_with_files
from apex_ray.llm import (
    LLMProvider,
    LLMProviderError,
    LLMRouteCircuitBreaker,
    review_config_for_pack,
    review_context_packs,
    verify_findings,
)
from apex_ray.models import (
    ChangedFile,
    ContextPack,
    DiffSummary,
    Finding,
    LLMConfig,
    LLMContextSelection,
    LLMCoverageMode,
    ReviewConfig,
    ReviewerConfig,
    ReviewReport,
    TargetMode,
)
from apex_ray.pipeline.findings import consolidate_findings
from apex_ray.pipeline.selection import merge_continuation_selection as _merge_continuation_selection
from apex_ray.pipeline.selection import (
    plan_llm_context_selection,
    select_continuation_context_packs,
    select_llm_context_packs,
)
from apex_ray.progress import NoopProgress, ProgressSink
from apex_ray.report import build_report
from apex_ray.report.run_state import reduce_llm_pack_run_states
from apex_ray.reviewers import (
    effective_reviewers,
    llm_config_for_reviewer,
    pack_for_reviewer,
    reviewer_matches_pack,
)


def run_review_pipeline(
    repo_root: Path,
    diff_text: str,
    target_mode: TargetMode,
    config: ReviewConfig,
    base: str | None = None,
    config_path: Path | None = None,
    provider: LLMProvider | None = None,
    progress: ProgressSink | None = None,
    reviewer_ids: list[str] | None = None,
) -> ReviewReport:
    pipeline_started = time.monotonic()
    stage_durations_ms: dict[str, int] = {}
    progress = progress or NoopProgress()
    stage_started = time.monotonic()
    progress.event("parsing diff", force=True)
    diff_summary = parse_unified_diff(diff_text, target_mode=target_mode, base=base)
    diff_summary = classify_diff(diff_summary, config.ignore, config.risk)
    apply_language_filter(diff_summary, config.languages)
    progress.event(
        f"parsed diff: {diff_summary.stats.files_changed} file(s), "
        f"+{diff_summary.stats.additions}/-{diff_summary.stats.deletions}, "
        f"{diff_summary.stats.ignored_files} ignored",
        force=True,
    )
    stage_durations_ms["diff"] = _elapsed_ms(stage_started)

    stage_started = time.monotonic()
    progress.event("discovering project", force=True)
    project, project_files = discover_project_with_files(
        repo_root,
        ignored_patterns=config.ignore,
        config_path=config_path,
    )
    analyzer_project_files = (
        project_files if Path(project.root).resolve(strict=False) == repo_root.resolve(strict=False) else None
    )
    stage_durations_ms["discovery"] = _elapsed_ms(stage_started)
    stage_started = time.monotonic()
    progress.event("running analyzers", force=True)
    analyzer_run = run_analyzers(
        repo_root,
        diff_summary.files,
        config.analyzer,
        ignored_patterns=config.ignore,
        project_files=analyzer_project_files,
    )
    analyzer_results = analyzer_run.results
    fallback_reasons_by_path = analyzer_run.fallback_reasons_by_path
    diff_summary.warnings.extend(analyzer_run.warnings)
    for backend_run in analyzer_run.backend_runs:
        if backend_run.changed_files_count == 0:
            progress.event(f"{backend_run.display_name} analyzer skipped", force=True)
            continue
        if backend_run.warning:
            progress.event(backend_run.warning, force=True)
            continue
        if backend_run.result is None:
            progress.event(f"{backend_run.display_name} analyzer skipped", force=True)
            continue
        progress.event(
            f"{backend_run.display_name} analyzer completed: "
            f"{sum(len(file.symbols) for file in backend_run.result.files)} symbol(s), "
            f"{len(backend_run.result.failed_files)} failed file(s)",
            force=True,
        )
    stage_durations_ms["analyzers"] = _elapsed_ms(stage_started)

    stage_started = time.monotonic()
    progress.event("building context packs", force=True)
    context_packs = build_context_packs(
        analyzer_results,
        diff_summary.files,
        config,
        repo_root=repo_root,
        fallback_reasons_by_path=fallback_reasons_by_path,
    )
    progress.event(f"built {len(context_packs)} context pack(s)", force=True)
    stage_durations_ms["context"] = _elapsed_ms(stage_started)
    findings = []
    verifications = []
    llm_runs = []
    llm_selection = None
    reviewer_selections: dict[str, LLMContextSelection] = {}
    route_circuit = LLMRouteCircuitBreaker()
    stage_started = time.monotonic()
    if config.llm.enabled:
        reviewers = effective_reviewers(config.reviewers, reviewer_ids)
        try:
            for reviewer in reviewers:
                reviewer_config = llm_config_for_reviewer(config.llm, reviewer)
                scoped_context_packs = [pack for pack in context_packs if reviewer_matches_pack(reviewer, pack)]
                progress.event(
                    f"planning LLM context selection for reviewer {reviewer.id}",
                    force=True,
                )
                selection = _plan_reviewer_context_selection(
                    scoped_context_packs,
                    diff_summary,
                    reviewer_config,
                    reviewer,
                    max_pack_chars=config.context.max_pack_chars,
                )
                reviewer_selections[reviewer.id] = selection
                deep_selected_ids = set(selection.deep_selected_context_pack_ids)
                shallow_selected_ids = set(selection.shallow_selected_context_pack_ids)
                selected_ids = deep_selected_ids | shallow_selected_ids
                deep_context_packs = [pack for pack in scoped_context_packs if pack.id in deep_selected_ids]
                shallow_context_packs = [pack for pack in scoped_context_packs if pack.id in shallow_selected_ids]
                llm_context_packs = [pack for pack in scoped_context_packs if pack.id in selected_ids]
                progress.event(
                    f"reviewer {reviewer.id}: selected {len(llm_context_packs)} of "
                    f"{len(scoped_context_packs)} matching context pack(s): "
                    f"{len(deep_context_packs)} deep, {len(shallow_context_packs)} shallow",
                    force=True,
                )
                _append_selection_warnings(
                    diff_summary,
                    selection,
                    reviewer_config,
                    reviewer,
                    matching_pack_count=len(scoped_context_packs),
                )
                if not llm_context_packs:
                    continue

                shallow_findings, shallow_runs = review_context_packs(
                    shallow_context_packs,
                    reviewer_config,
                    repo_root,
                    provider=provider,
                    review_depth="shallow",
                    progress=progress,
                    reviewer=reviewer,
                    circuit_breaker=route_circuit,
                )
                deep_findings, deep_runs = review_context_packs(
                    deep_context_packs,
                    reviewer_config,
                    repo_root,
                    provider=provider,
                    review_depth="deep",
                    progress=progress,
                    reviewer=reviewer,
                    circuit_breaker=route_circuit,
                )
                reviewer_findings = consolidate_findings([*shallow_findings, *deep_findings])
                llm_runs.extend([*shallow_runs, *deep_runs])
                if reviewer_config.verify and reviewer_findings:
                    progress.event(
                        f"reviewer {reviewer.id}: verifying {len(reviewer_findings)} finding(s)",
                        force=True,
                    )
                    reviewer_findings, reviewer_verifications, verifier_runs = verify_findings(
                        reviewer_findings,
                        context_packs,
                        reviewer_config,
                        repo_root,
                        provider=provider,
                        progress=progress,
                        reviewer=reviewer,
                        circuit_breaker=route_circuit,
                    )
                    verifications.extend(reviewer_verifications)
                    llm_runs.extend(verifier_runs)
                findings = consolidate_findings(
                    [*findings, *reviewer_findings],
                    preferred_findings=[
                        verification.finding for verification in verifications if verification.approved
                    ],
                )
        except LLMProviderError:
            raise
        llm_selection = _merge_reviewer_context_selections(
            context_packs,
            reviewer_selections,
            include_unmatched_context_packs=reviewer_ids is None,
        )
    else:
        progress.event("LLM review disabled", force=True)
    stage_durations_ms["llm"] = _elapsed_ms(stage_started)

    stage_started = time.monotonic()
    progress.event("building report", force=True)
    report = build_report(
        project,
        config,
        diff_summary,
        analyzer_results=analyzer_results,
        context_packs=context_packs,
        findings=findings,
        verifications=verifications,
        llm_runs=llm_runs,
        llm_selection=llm_selection,
        reviewer_selections=reviewer_selections,
        stage_durations_ms=stage_durations_ms,
    )
    stage_durations_ms["report"] = _elapsed_ms(stage_started)
    stage_durations_ms["total"] = _elapsed_ms(pipeline_started)
    report.stage_durations_ms = dict(stage_durations_ms)
    return report


def _plan_reviewer_context_selection(
    context_packs: list[ContextPack],
    diff_summary: DiffSummary,
    llm_config: LLMConfig,
    reviewer: ReviewerConfig,
    *,
    max_pack_chars: int,
) -> LLMContextSelection:
    budgeted_context_packs = [pack_for_reviewer(pack, reviewer) for pack in context_packs]
    coverage_mode = llm_config.coverage_mode
    max_deep_packs = llm_config.max_deep_packs
    if reviewer.review_depth == "deep":
        coverage_mode = LLMCoverageMode.FAST
    elif reviewer.review_depth == "shallow":
        coverage_mode = LLMCoverageMode.BALANCED
        max_deep_packs = 0
    return plan_llm_context_selection(
        budgeted_context_packs,
        diff_summary.files,
        max_packs=llm_config.max_packs,
        max_deep_packs=max_deep_packs,
        max_input_tokens=llm_config.max_input_tokens,
        max_pack_chars=max_pack_chars,
        coverage_mode=coverage_mode,
        provider=lambda pack: review_config_for_pack(llm_config, pack)[0].provider,
    )


def _append_selection_warnings(
    diff_summary: DiffSummary,
    selection: LLMContextSelection,
    llm_config: LLMConfig,
    reviewer: ReviewerConfig,
    *,
    matching_pack_count: int,
) -> None:
    prefix = f"Reviewer {reviewer.id}"
    if matching_pack_count == 0:
        diff_summary.warnings.append(f"{prefix} matched no context packs.")
        return
    capped_pack_ids = [
        pack_id
        for pack_id, reason in selection.skipped_context_pack_reasons.items()
        if reason == "not selected by LLM pack cap"
    ]
    if capped_pack_ids:
        diff_summary.warnings.append(
            f"{prefix} limited review to {len(selection.selected_context_pack_ids)} of "
            f"{matching_pack_count} matching context packs by max_packs={llm_config.max_packs}."
        )
    token_capped_pack_ids = [
        pack_id
        for pack_id, reason in selection.skipped_context_pack_reasons.items()
        if reason == "not selected by LLM token budget"
    ]
    if token_capped_pack_ids:
        diff_summary.warnings.append(
            f"{prefix} left {len(token_capped_pack_ids)} context pack(s) unreviewed "
            f"by max_input_tokens={llm_config.max_input_tokens}."
        )
    if selection.over_budget_context_pack_ids:
        diff_summary.warnings.append(
            f"{prefix} skipped over-budget context pack(s): " + ", ".join(selection.over_budget_context_pack_ids)
        )
    if selection.selected_context_pack_ids:
        return
    if selection.over_budget_context_pack_ids:
        diff_summary.warnings.append(f"{prefix} could not review any pack because all were over budget.")
    elif token_capped_pack_ids:
        diff_summary.warnings.append(f"{prefix} token budget selected no context packs.")
    else:
        diff_summary.warnings.append(f"{prefix} selected no context packs.")


def _merge_reviewer_context_selections(
    context_packs: list[ContextPack],
    reviewer_selections: dict[str, LLMContextSelection],
    *,
    include_unmatched_context_packs: bool = True,
) -> LLMContextSelection:
    all_ids = [pack.id for pack in context_packs]
    matching_ids = {
        pack_id for selection in reviewer_selections.values() for pack_id in selection.total_context_pack_ids
    }
    total_ids = (
        all_ids if include_unmatched_context_packs else [pack_id for pack_id in all_ids if pack_id in matching_ids]
    )
    selected = {
        pack_id for selection in reviewer_selections.values() for pack_id in selection.selected_context_pack_ids
    }
    deep = {
        pack_id for selection in reviewer_selections.values() for pack_id in selection.deep_selected_context_pack_ids
    }
    shallow = {
        pack_id for selection in reviewer_selections.values() for pack_id in selection.shallow_selected_context_pack_ids
    } - deep
    unselected = [pack_id for pack_id in total_ids if pack_id not in selected]
    over_budget: list[str] = []
    over_token_budget: list[str] = []
    skipped_reasons: dict[str, str] = {}
    for pack_id in unselected:
        relevant = {
            reviewer_id: selection
            for reviewer_id, selection in reviewer_selections.items()
            if pack_id in selection.total_context_pack_ids
        }
        if not relevant:
            skipped_reasons[pack_id] = "not matched by any enabled reviewer"
            continue
        if all(pack_id in selection.over_budget_context_pack_ids for selection in relevant.values()):
            over_budget.append(pack_id)
        if all(pack_id in selection.over_token_budget_context_pack_ids for selection in relevant.values()):
            over_token_budget.append(pack_id)
        reasons = sorted(
            {selection.skipped_context_pack_reasons.get(pack_id, "not selected") for selection in relevant.values()}
        )
        skipped_reasons[pack_id] = "; ".join(reasons)
    stages = [
        stage.model_copy(update={"stage": f"{reviewer_id}:{stage.stage}"})
        for reviewer_id, selection in reviewer_selections.items()
        for stage in selection.stages
    ]
    return LLMContextSelection(
        total_context_pack_ids=total_ids,
        selected_context_pack_ids=[pack_id for pack_id in total_ids if pack_id in selected],
        deep_selected_context_pack_ids=[pack_id for pack_id in total_ids if pack_id in deep],
        shallow_selected_context_pack_ids=[pack_id for pack_id in total_ids if pack_id in shallow],
        unselected_context_pack_ids=unselected,
        over_budget_context_pack_ids=over_budget,
        over_token_budget_context_pack_ids=over_token_budget,
        skipped_context_pack_reasons=skipped_reasons,
        stages=stages,
    )


def _elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


def continue_review_from_report(
    report: ReviewReport,
    *,
    repo_root: Path | None = None,
    config: ReviewConfig | None = None,
    residual_priorities: set[str] | None = None,
    slices: set[str] | None = None,
    pack_ids: set[str] | None = None,
    only_unreviewed: bool = True,
    max_pack_reviews: int | None = None,
    review_depth: Literal["deep", "shallow"] = "deep",
    reviewer_id: str | None = None,
    reviewer_ids: list[str] | None = None,
    provider: LLMProvider | None = None,
    progress: ProgressSink | None = None,
) -> tuple[ReviewReport, list[ContextPack]]:
    progress = progress or NoopProgress()
    effective_config = config.model_copy(deep=True) if config is not None else report.config.model_copy(deep=True)
    root = repo_root or Path(report.project.root)
    if reviewer_id is not None and reviewer_ids is not None:
        raise ValueError("Use reviewer_id or reviewer_ids, not both.")
    requested_reviewer_ids = reviewer_ids
    if reviewer_id is not None:
        requested_reviewer_ids = [reviewer_id]
    configured_reviewers = effective_reviewers(effective_config.reviewers)
    prior_reviewer_ids = {reviewer.id for reviewer in effective_reviewers(report.config.reviewers)}
    current_reviewer_ids = {reviewer.id for reviewer in configured_reviewers}
    reviewer_set_changed = current_reviewer_ids != prior_reviewer_ids
    reviewers: Sequence[ReviewerConfig | None]
    if requested_reviewer_ids is not None:
        reviewers = effective_reviewers(
            effective_config.reviewers,
            requested_reviewer_ids,
        )
    elif effective_config.reviewers or reviewer_set_changed or report.reviewer_selections:
        reviewers = configured_reviewers
    else:
        reviewers = [None]
    candidate_packs = select_continuation_context_packs(
        report,
        residual_priorities=residual_priorities,
        slices=slices,
        pack_ids=pack_ids,
        only_unreviewed=only_unreviewed and reviewers == [None],
    )
    reviewer_coverage = {reviewer.reviewer_id: reviewer for reviewer in report.llm_coverage.reviewers}
    reviewer_selections = {
        reviewer_id: selection
        for reviewer_id, selection in report.reviewer_selections.items()
        if reviewer_id in current_reviewer_ids
    }
    scoped_packs: list[tuple[ReviewerConfig | None, list[ContextPack]]] = []
    for reviewer in reviewers:
        reviewer_context_packs = (
            [pack for pack in report.context_packs if reviewer_matches_pack(reviewer, pack)]
            if reviewer is not None
            else list(report.context_packs)
        )
        reviewer_scope_ids = {pack.id for pack in reviewer_context_packs}
        matching = [pack for pack in candidate_packs if pack.id in reviewer_scope_ids]
        if reviewer is not None:
            reviewer_selections[reviewer.id] = _rebase_reviewer_selection_scope(
                reviewer_selections.get(reviewer.id),
                reviewer_context_packs,
            )
        if reviewer is not None and only_unreviewed:
            prior_coverage = reviewer_coverage.get(reviewer.id)
            if prior_coverage is not None:
                retry_ids = {pack.id for pack in matching}.difference(prior_coverage.reviewed_context_pack_ids)
                retry_ids.update(
                    _reviewer_failed_retry_pack_ids(
                        report,
                        reviewer.id,
                        verify_enabled=llm_config_for_reviewer(
                            effective_config.llm,
                            reviewer,
                        ).verify,
                    )
                )
                matching = [pack for pack in matching if pack.id in retry_ids]
        scoped_packs.append((reviewer, matching))
    eligible_pack_reviews = sum(len(packs) for _reviewer, packs in scoped_packs)
    eligible_scoped_packs = scoped_packs
    scoped_packs = _limit_continuation_pack_reviews(
        scoped_packs,
        report.diff.files,
        max_pack_reviews=max_pack_reviews,
    )
    selected_pack_reviews = sum(len(packs) for _reviewer, packs in scoped_packs)
    if selected_pack_reviews < eligible_pack_reviews:
        selected_ids_by_reviewer = {
            reviewer.id: {pack.id for pack in reviewer_packs}
            for reviewer, reviewer_packs in scoped_packs
            if reviewer is not None
        }
        for reviewer, eligible_packs in eligible_scoped_packs:
            if reviewer is None:
                continue
            selected_reviewer_ids = selected_ids_by_reviewer.get(reviewer.id, set())
            deferred_packs = [pack for pack in eligible_packs if pack.id not in selected_reviewer_ids]
            if not deferred_packs:
                continue
            reviewer_context_packs = [pack for pack in report.context_packs if reviewer_matches_pack(reviewer, pack)]
            reviewer_selections[reviewer.id] = _merge_continuation_selection(
                reviewer_selections.get(reviewer.id),
                reviewer_context_packs,
                deferred_packs,
                review_depth=review_depth,
            )
    selected_ids = {pack.id for _reviewer, reviewer_packs in scoped_packs for pack in reviewer_packs}
    selected_packs = [pack for pack in candidate_packs if pack.id in selected_ids]
    reviewer_scoped = reviewers != [None]
    rebased_llm_selection = (
        _merge_reviewer_context_selections(
            report.context_packs,
            reviewer_selections,
            include_unmatched_context_packs=requested_reviewer_ids is None,
        )
        if reviewer_scoped
        else report.llm_selection
    )
    selection_message = (
        f"selected {len(selected_packs)} continuation context pack(s) "
        f"across {selected_pack_reviews} reviewer-pack assignment(s)"
    )
    deferred_pack_reviews = eligible_pack_reviews - selected_pack_reviews
    if deferred_pack_reviews:
        selection_message += f"; deferred {deferred_pack_reviews} assignment(s) at the configured follow-up cap"
    progress.event(selection_message, force=True)
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
                llm_selection=rebased_llm_selection,
                reviewer_selections=reviewer_selections,
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
                llm_selection=rebased_llm_selection,
                reviewer_selections=reviewer_selections,
            ),
            selected_packs,
        )

    llm_runs = list(report.llm_runs)
    verifications = list(report.verifications)
    approved_new_findings: list[Finding] = []
    route_circuit = LLMRouteCircuitBreaker()
    for reviewer, reviewer_packs in scoped_packs:
        if not reviewer_packs:
            continue
        llm_config = (
            llm_config_for_reviewer(effective_config.llm, reviewer) if reviewer is not None else effective_config.llm
        )
        new_findings, review_runs = review_context_packs(
            reviewer_packs,
            llm_config,
            root,
            provider=provider,
            review_depth=review_depth,
            progress=progress,
            reviewer=reviewer,
            circuit_breaker=route_circuit,
        )
        llm_runs.extend(review_runs)
        reviewer_findings = new_findings
        if llm_config.verify and new_findings:
            reviewer_findings, new_verifications, verifier_runs = verify_findings(
                new_findings,
                report.context_packs,
                llm_config,
                root,
                provider=provider,
                progress=progress,
                reviewer=reviewer,
                circuit_breaker=route_circuit,
            )
            verifications.extend(new_verifications)
            llm_runs.extend(verifier_runs)
        approved_new_findings.extend(reviewer_findings)

        if reviewer is not None:
            reviewer_context_packs = [pack for pack in report.context_packs if reviewer_matches_pack(reviewer, pack)]
            reviewer_selections[reviewer.id] = _merge_continuation_selection(
                reviewer_selections.get(reviewer.id),
                reviewer_context_packs,
                reviewer_packs,
                review_depth=review_depth,
            )

    findings = consolidate_findings(
        [*report.findings, *approved_new_findings],
        preferred_findings=[verification.finding for verification in verifications if verification.approved],
    )
    if reviewer_scoped:
        llm_selection = _merge_reviewer_context_selections(
            report.context_packs,
            reviewer_selections,
            include_unmatched_context_packs=requested_reviewer_ids is None,
        )
    else:
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
            reviewer_selections=reviewer_selections,
        ),
        selected_packs,
    )


def _limit_continuation_pack_reviews(
    scoped_packs: list[tuple[ReviewerConfig | None, list[ContextPack]]],
    changed_files: list[ChangedFile],
    *,
    max_pack_reviews: int | None,
) -> list[tuple[ReviewerConfig | None, list[ContextPack]]]:
    if max_pack_reviews is None:
        return scoped_packs
    if max_pack_reviews <= 0:
        raise ValueError("max_pack_reviews must be greater than zero")
    if sum(len(packs) for _reviewer, packs in scoped_packs) <= max_pack_reviews:
        return scoped_packs

    reviewer_budgets = [0 for _item in scoped_packs]

    def reviewer_allocation_key(index: int) -> tuple[bool, str]:
        reviewer = scoped_packs[index][0]
        if reviewer is None:
            return True, ""
        return not reviewer.required, reviewer.id

    reviewer_order = sorted(range(len(scoped_packs)), key=reviewer_allocation_key)
    selected_count = 0
    while selected_count < max_pack_reviews:
        selected_in_round = False
        for index in reviewer_order:
            if selected_count >= max_pack_reviews:
                break
            reviewer_capacity = len(scoped_packs[index][1])
            if reviewer_budgets[index] >= reviewer_capacity:
                continue
            reviewer_budgets[index] += 1
            selected_count += 1
            selected_in_round = True
        if not selected_in_round:
            break

    return [
        (
            reviewer,
            select_llm_context_packs(
                packs,
                changed_files,
                max_packs=reviewer_budgets[index],
            ),
        )
        for index, (reviewer, packs) in enumerate(scoped_packs)
    ]


def _reviewer_failed_retry_pack_ids(
    report: ReviewReport,
    reviewer_id: str,
    *,
    verify_enabled: bool,
) -> set[str]:
    states = reduce_llm_pack_run_states(report.llm_runs)
    return {
        state.context_pack_id
        for (state_reviewer_id, _pack_id), state in states.items()
        if state_reviewer_id == reviewer_id
        and (
            (state.review is not None and state.review.status != "ok")
            or (verify_enabled and any(run.status != "ok" for run in state.verify_runs))
        )
    }


def _rebase_reviewer_selection_scope(
    existing: LLMContextSelection | None,
    context_packs: list[ContextPack],
) -> LLMContextSelection:
    total_ids = [pack.id for pack in context_packs]
    if existing is None:
        return LLMContextSelection(
            total_context_pack_ids=total_ids,
            unselected_context_pack_ids=total_ids,
        )
    total_id_set = set(total_ids)
    selected_ids = [pack_id for pack_id in total_ids if pack_id in existing.selected_context_pack_ids]
    selected_id_set = set(selected_ids)
    unselected_ids = [pack_id for pack_id in total_ids if pack_id not in selected_id_set]
    return existing.model_copy(
        update={
            "total_context_pack_ids": total_ids,
            "selected_context_pack_ids": selected_ids,
            "deep_selected_context_pack_ids": [
                pack_id for pack_id in total_ids if pack_id in existing.deep_selected_context_pack_ids
            ],
            "shallow_selected_context_pack_ids": [
                pack_id for pack_id in total_ids if pack_id in existing.shallow_selected_context_pack_ids
            ],
            "unselected_context_pack_ids": unselected_ids,
            "over_budget_context_pack_ids": [
                pack_id
                for pack_id in existing.over_budget_context_pack_ids
                if pack_id in total_id_set and pack_id not in selected_id_set
            ],
            "over_token_budget_context_pack_ids": [
                pack_id
                for pack_id in existing.over_token_budget_context_pack_ids
                if pack_id in total_id_set and pack_id not in selected_id_set
            ],
            "skipped_context_pack_reasons": {
                pack_id: reason
                for pack_id, reason in existing.skipped_context_pack_reasons.items()
                if pack_id in unselected_ids
            },
        }
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
