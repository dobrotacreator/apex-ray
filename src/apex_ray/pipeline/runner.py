import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from apex_ray.analyzers import run_analyzers
from apex_ray.classify import classify_diff
from apex_ray.context import build_context_packs
from apex_ray.diff import parse_unified_diff
from apex_ray.discovery import discover_project_with_files
from apex_ray.findings import (
    active_verifications,
    finding_decision_identity,
    findings_are_duplicates,
    findings_share_reviewer_origin,
    merge_finding_reviewer_provenance,
    payload_fingerprint,
    retain_finding_reviewer_provenance,
    reviewer_origin_pack_ids,
    reviewer_origins_are_explicit,
    unresolved_verification_candidate_pack_ids,
    unresolved_verifications,
    verification_candidate_counts,
    verification_decisions_match,
    verification_is_terminally_replaced,
    verification_subject_matches_any,
)
from apex_ray.llm import (
    LLMProvider,
    LLMProviderError,
    LLMRouteCircuitBreaker,
    review_config_for_pack,
    review_context_packs,
    verify_findings,
)
from apex_ray.llm.prompts import ReviewPromptCache
from apex_ray.llm.routing import config_for_profile_or_model, verification_config_for_finding
from apex_ray.models import (
    ChangedFile,
    ContextPack,
    DiffSummary,
    Finding,
    FindingVerification,
    LLMConfig,
    LLMContextSelection,
    LLMCoverageMode,
    LLMRun,
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
    resolved_reviewers = effective_reviewers(config.reviewers, reviewer_ids) if reviewer_ids is not None else None
    reviewer_scope_ids = [reviewer.id for reviewer in resolved_reviewers] if resolved_reviewers is not None else None
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
        timeout_seconds=config.analyzer.timeout_seconds,
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
        reviewers = resolved_reviewers if resolved_reviewers is not None else effective_reviewers(config.reviewers)
        try:
            for reviewer in reviewers:
                reviewer_config = llm_config_for_reviewer(config.llm, reviewer)
                rendered_prompts: ReviewPromptCache = {}
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
                    rendered_prompts=rendered_prompts,
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
                    rendered_prompts=rendered_prompts,
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
                    rendered_prompts=rendered_prompts,
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
                        verification.finding
                        for verification in active_verifications(verifications)
                        if verification.approved
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
        reviewer_scope_ids=reviewer_scope_ids,
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
    rendered_prompts: ReviewPromptCache | None = None,
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
        rendered_prompts=rendered_prompts,
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
    # Preserve the pre-reviewer stage contract for the default/single-reviewer
    # projection; reviewer_selections carries provenance without changing it.
    if len(reviewer_selections) == 1:
        stages = [stage.model_copy() for selection in reviewer_selections.values() for stage in selection.stages]
    else:
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
    prior_reviewers = effective_reviewers(report.config.reviewers)
    prior_reviewer_ids = {reviewer.id for reviewer in prior_reviewers}
    current_reviewer_ids = {reviewer.id for reviewer in configured_reviewers}
    historical_reviewer_ids = {
        *(run.reviewer_id for run in report.llm_runs),
        *(verification.reviewer_id for verification in report.verifications),
        *report.reviewer_selections,
    }
    reactivated_reviewer_ids = current_reviewer_ids.difference(prior_reviewer_ids).intersection(historical_reviewer_ids)
    retired_reviewer_ids = prior_reviewer_ids.difference(current_reviewer_ids)
    current_reviewers_by_id = {reviewer.id: reviewer for reviewer in configured_reviewers}
    current_reviewer_pack_ids = {
        reviewer.id: {pack.id for pack in report.context_packs if reviewer_matches_pack(reviewer, pack)}
        for reviewer in configured_reviewers
    }
    prior_reviewer_pack_ids = {
        reviewer.id: {pack.id for pack in report.context_packs if reviewer_matches_pack(reviewer, pack)}
        for reviewer in prior_reviewers
    }
    expanded_reviewer_scope_pack_ids = {
        reviewer_id: current_reviewer_pack_ids[reviewer_id].difference(prior_reviewer_pack_ids.get(reviewer_id, set()))
        for reviewer_id in current_reviewer_ids.intersection(prior_reviewer_ids)
    }
    current_findings = _findings_in_current_reviewer_scope(
        report.findings,
        current_reviewer_pack_ids,
    )
    reviewer_provenance_changed = current_findings != report.findings
    reviewer_set_changed = current_reviewer_ids != prior_reviewer_ids
    prior_reviewers_by_id = {reviewer.id: reviewer for reviewer in prior_reviewers}
    changed_reviewer_behavior_ids = {
        reviewer_id
        for reviewer_id in current_reviewer_ids.intersection(prior_reviewer_ids)
        if _reviewer_review_behavior_changed(
            report,
            report.config,
            effective_config,
            prior_reviewers_by_id[reviewer_id],
            current_reviewers_by_id[reviewer_id],
        )
    } | reactivated_reviewer_ids
    changed_reviewer_verification_ids = {
        reviewer_id
        for reviewer_id in current_reviewer_ids.intersection(prior_reviewer_ids)
        if _reviewer_verification_behavior_changed(
            report,
            report.config,
            effective_config,
            prior_reviewers_by_id[reviewer_id],
            current_reviewers_by_id[reviewer_id],
        )
    }
    llm_runs = [
        *report.llm_runs,
        *_reviewer_config_reset_runs(
            report.llm_runs,
            changed_reviewer_behavior_ids | retired_reviewer_ids,
        ),
        *_reviewer_scope_expansion_reset_runs(
            report.llm_runs,
            {
                reviewer_id: pack_ids
                for reviewer_id, pack_ids in expanded_reviewer_scope_pack_ids.items()
                if reviewer_id not in changed_reviewer_behavior_ids
            },
        ),
        *_reviewer_verification_reset_runs(
            report.llm_runs,
            changed_reviewer_verification_ids,
        ),
    ]
    verification_history = _retire_verifications_outside_current_reviewer_scope(
        _supersede_reviewer_verifications(
            report.verifications,
            changed_reviewer_verification_ids | changed_reviewer_behavior_ids,
        ),
        current_reviewer_pack_ids,
        report.llm_runs,
    )
    if reviewer_provenance_changed:
        current_findings = _apply_active_verifications_to_findings(
            current_findings,
            verification_history,
        )
    missing_verification_pack_ids = _missing_verification_pack_ids(
        llm_runs,
        effective_config,
        current_reviewers_by_id,
        current_findings,
        verification_history,
    )
    recoverable_verification_pack_ids: dict[str, set[str]] = {}
    for (candidate_reviewer_id, candidate_pack_id), count in verification_candidate_counts(
        current_findings,
        verification_history,
    ).items():
        if count:
            recoverable_verification_pack_ids.setdefault(candidate_reviewer_id, set()).add(candidate_pack_id)
    verification_refresh_reviewer_ids = changed_reviewer_verification_ids | _reviewers_with_missing_verification(
        llm_runs,
        effective_config,
        current_reviewers_by_id,
        current_findings,
        verification_history,
    )
    effective_run_states = reduce_llm_pack_run_states(llm_runs)
    effective_review_depths = _effective_review_depths_by_reviewer(llm_runs)
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
    reviewer_scope_ids = None
    if requested_reviewer_ids is not None and report.reviewer_scope_ids is not None:
        reviewer_scope_ids = list(
            dict.fromkeys(
                [
                    *(reviewer_id for reviewer_id in report.reviewer_scope_ids if reviewer_id in current_reviewer_ids),
                    *(reviewer.id for reviewer in reviewers if reviewer is not None),
                ]
            )
        )
    candidate_packs = select_continuation_context_packs(
        report,
        residual_priorities=residual_priorities,
        slices=slices,
        pack_ids=pack_ids,
        only_unreviewed=only_unreviewed and reviewers == [None],
    )
    verification_candidate_packs = select_continuation_context_packs(
        report,
        residual_priorities=residual_priorities,
        slices=slices,
        pack_ids=pack_ids,
        only_unreviewed=False,
    )
    reviewer_selections = {
        reviewer_id: selection
        for reviewer_id, selection in report.reviewer_selections.items()
        if reviewer_id in current_reviewer_ids
    }
    for persisted_reviewer_id, persisted_selection in list(reviewer_selections.items()):
        persisted_reviewer = current_reviewers_by_id[persisted_reviewer_id]
        reviewer_selections[persisted_reviewer_id] = _rebase_reviewer_selection_scope(
            persisted_selection,
            [pack for pack in report.context_packs if reviewer_matches_pack(persisted_reviewer, pack)],
            effective_review_depths.get(persisted_reviewer_id),
        )
    persisted_scope_ids = (
        current_reviewer_ids
        if report.reviewer_scope_ids is None
        else {reviewer_id for reviewer_id in report.reviewer_scope_ids if reviewer_id in current_reviewer_ids}
    )
    for persisted_reviewer_id in sorted(persisted_scope_ids):
        persisted_reviewer = current_reviewers_by_id.get(persisted_reviewer_id)
        if persisted_reviewer is None or persisted_reviewer_id in reviewer_selections:
            continue
        reviewer_selections[persisted_reviewer_id] = _rebase_reviewer_selection_scope(
            None,
            [pack for pack in report.context_packs if reviewer_matches_pack(persisted_reviewer, pack)],
            effective_review_depths.get(persisted_reviewer_id),
        )
    scoped_packs: list[tuple[ReviewerConfig | None, list[ContextPack]]] = []
    for reviewer in reviewers:
        reviewer_context_packs = (
            [pack for pack in report.context_packs if reviewer_matches_pack(reviewer, pack)]
            if reviewer is not None
            else list(report.context_packs)
        )
        reviewer_pack_scope_ids = {pack.id for pack in reviewer_context_packs}
        matching = [pack for pack in candidate_packs if pack.id in reviewer_pack_scope_ids]
        if reviewer is not None:
            reviewer_selections[reviewer.id] = _rebase_reviewer_selection_scope(
                reviewer_selections.get(reviewer.id),
                reviewer_context_packs,
                effective_review_depths.get(reviewer.id),
            )
        if reviewer is not None and reviewer.id not in changed_reviewer_behavior_ids and only_unreviewed:
            reviewed_ids = {
                state.context_pack_id
                for state in effective_run_states.values()
                if state.reviewer_id == reviewer.id and state.review is not None and state.review.status == "ok"
            }
            retry_ids = {pack.id for pack in matching}.difference(reviewed_ids)
            retry_ids.update(
                _reviewer_failed_retry_pack_ids(
                    llm_runs,
                    reviewer.id,
                )
            )
            retry_ids.update(
                missing_verification_pack_ids.get(reviewer.id, set()).difference(
                    recoverable_verification_pack_ids.get(reviewer.id, set())
                )
            )
            matching = [pack for pack in matching if pack.id in retry_ids]
        scoped_packs.append((reviewer, matching))
    eligible_pack_reviews = sum(len(packs) for _reviewer, packs in scoped_packs)
    eligible_scoped_packs = scoped_packs
    archived_priority_by_pack_id = {
        status.context_pack_id: status.priority
        for status in report.llm_coverage.pack_statuses
        if status.priority is not None
    }
    scoped_packs = _limit_continuation_pack_reviews(
        scoped_packs,
        report.diff.files,
        max_pack_reviews=max_pack_reviews,
        priority_by_pack_id=archived_priority_by_pack_id,
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
            include_unmatched_context_packs=reviewer_scope_ids is None,
        )
        if reviewer_scoped
        else report.llm_selection
    )
    active_reviewers_by_id: dict[str, ReviewerConfig | None] = (
        {"general": None}
        if reviewers == [None]
        else {reviewer.id: reviewer for reviewer in reviewers if reviewer is not None}
    )
    verification_scope_pack_ids = {pack.id for pack in verification_candidate_packs}
    selected_review_pack_ids_by_reviewer = {
        (reviewer.id if reviewer is not None else "general"): {pack.id for pack in reviewer_packs}
        for reviewer, reviewer_packs in scoped_packs
    }
    carried_verification_findings = {
        reviewer_id: _carried_findings_for_reviewer(
            report,
            reviewer_id,
            reviewer,
            report_findings=current_findings,
            allowed_pack_ids=(
                verification_scope_pack_ids
                if reviewer_id in changed_reviewer_verification_ids
                else verification_scope_pack_ids.intersection(missing_verification_pack_ids.get(reviewer_id, set()))
            ),
            verifications=verification_history,
            include_resolved=reviewer_id in changed_reviewer_verification_ids,
        )
        for reviewer_id, reviewer in active_reviewers_by_id.items()
        if reviewer_id in verification_refresh_reviewer_ids and reviewer_id not in changed_reviewer_behavior_ids
    }

    def carried_finding_is_being_refreshed(
        reviewer_id: str,
        finding: Finding,
    ) -> bool:
        selected_review_pack_ids = selected_review_pack_ids_by_reviewer.get(
            reviewer_id,
            set(),
        )
        if reviewer_origins_are_explicit(finding, reviewer_id):
            return bool(reviewer_origin_pack_ids(finding, reviewer_id).intersection(selected_review_pack_ids))
        if not any(
            finding_decision_identity(report_finding) == finding_decision_identity(finding)
            and reviewer_id in (report_finding.reviewer_ids or ["general"])
            for report_finding in current_findings
        ):
            return False
        if finding.context_pack_id in selected_review_pack_ids:
            return True
        # Legacy reports lack durable origins. If their canonical pack falls
        # outside the reviewer's current scope, defer cross-pack verification
        # while any reviewer pack is being refreshed.
        return bool(selected_review_pack_ids) and finding.context_pack_id not in current_reviewer_pack_ids.get(
            reviewer_id,
            set(),
        )

    carried_verification_findings = {
        reviewer_id: [
            finding for finding in carried_findings if not carried_finding_is_being_refreshed(reviewer_id, finding)
        ]
        for reviewer_id, carried_findings in carried_verification_findings.items()
    }
    eligible_verification_scoped_packs = [
        (
            active_reviewers_by_id[reviewer_id],
            [
                pack
                for pack in verification_candidate_packs
                if any(finding.context_pack_id == pack.id for finding in carried_findings)
                and pack.id not in selected_review_pack_ids_by_reviewer.get(reviewer_id, set())
            ],
        )
        for reviewer_id, carried_findings in carried_verification_findings.items()
    ]
    eligible_verification_pack_reviews = sum(len(packs) for _reviewer, packs in eligible_verification_scoped_packs)
    remaining_pack_reviews = None if max_pack_reviews is None else max(0, max_pack_reviews - selected_pack_reviews)
    if remaining_pack_reviews == 0:
        verification_scoped_packs = [(reviewer, []) for reviewer, _packs in eligible_verification_scoped_packs]
    else:
        verification_scoped_packs = _limit_continuation_pack_reviews(
            eligible_verification_scoped_packs,
            report.diff.files,
            max_pack_reviews=remaining_pack_reviews,
            priority_by_pack_id=archived_priority_by_pack_id,
        )
    selected_verification_pack_ids_by_reviewer = {
        (reviewer.id if reviewer is not None else "general"): {pack.id for pack in reviewer_packs}
        for reviewer, reviewer_packs in verification_scoped_packs
    }
    carried_verification_findings = {
        reviewer_id: [
            finding
            for finding in carried_findings
            if finding.context_pack_id
            in (
                selected_review_pack_ids_by_reviewer.get(reviewer_id, set())
                | selected_verification_pack_ids_by_reviewer.get(reviewer_id, set())
            )
        ]
        for reviewer_id, carried_findings in carried_verification_findings.items()
    }
    selected_verification_pack_reviews = sum(len(packs) for _reviewer, packs in verification_scoped_packs)
    has_carried_verification_work = any(carried_verification_findings.values())
    selection_message = (
        f"selected {len(selected_packs)} continuation context pack(s) "
        f"across {selected_pack_reviews} review and "
        f"{selected_verification_pack_reviews} verification assignment(s)"
    )
    deferred_pack_reviews = (
        eligible_pack_reviews
        + eligible_verification_pack_reviews
        - selected_pack_reviews
        - selected_verification_pack_reviews
    )
    if deferred_pack_reviews:
        selection_message += f"; deferred {deferred_pack_reviews} assignment(s) at the configured follow-up cap"
    progress.event(selection_message, force=True)
    if not selected_packs and not (effective_config.llm.enabled and has_carried_verification_work):
        return (
            build_report(
                report.project,
                effective_config,
                report.diff,
                analyzer_results=report.analyzer_results,
                context_packs=report.context_packs,
                findings=current_findings,
                verifications=verification_history,
                llm_runs=llm_runs,
                llm_selection=rebased_llm_selection,
                reviewer_selections=reviewer_selections,
                reviewer_scope_ids=reviewer_scope_ids,
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
                findings=current_findings,
                verifications=verification_history,
                llm_runs=llm_runs,
                llm_selection=rebased_llm_selection,
                reviewer_selections=reviewer_selections,
                reviewer_scope_ids=reviewer_scope_ids,
            ),
            selected_packs,
        )

    verifications = list(verification_history)
    prior_verification_count = len(verifications)
    approved_new_findings: list[Finding] = []
    refreshed_review_pack_ids: dict[str, set[str]] = {}
    protected_snapshot_verification_ids: set[int] = set()
    retried_snapshot_ids: set[str] = set()
    review_snapshot_ids_by_reviewer_pack: dict[tuple[str, str], str] = {}
    route_circuit = LLMRouteCircuitBreaker()
    for reviewer, reviewer_packs in scoped_packs:
        if not reviewer_packs:
            continue
        llm_config = (
            llm_config_for_reviewer(effective_config.llm, reviewer) if reviewer is not None else effective_config.llm
        )
        active_reviewer_id = reviewer.id if reviewer is not None else "general"
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
        reviewer_run_states = reduce_llm_pack_run_states(review_runs)
        successful_review_pack_ids = {
            state.context_pack_id
            for state in reviewer_run_states.values()
            if state.review is not None and state.review.status == "ok"
        }
        candidate_finding_pack_ids = {finding.context_pack_id for finding in new_findings}
        reviewer_findings = new_findings
        verifier_runs: list[LLMRun] = []
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
            snapshot_ids_by_pack_id = {
                pack_id: _review_snapshot_id(
                    reviewer_id=active_reviewer_id,
                    context_pack_id=pack_id,
                    run_count=len(llm_runs),
                    verification_count=len(verifications),
                )
                for pack_id in candidate_finding_pack_ids
            }
            review_snapshot_ids_by_reviewer_pack.update(
                {(active_reviewer_id, pack_id): snapshot_id for pack_id, snapshot_id in snapshot_ids_by_pack_id.items()}
            )
            new_verifications = [
                verification.model_copy(
                    update={"review_snapshot_id": snapshot_ids_by_pack_id.get(verification.finding.context_pack_id)}
                )
                for verification in new_verifications
            ]
            verifications = _append_verification_history(
                verifications,
                new_verifications,
            )
            llm_runs.extend(verifier_runs)
        successful_snapshot_pack_ids = set(successful_review_pack_ids)
        if llm_config.verify:
            verify_runs_by_pack_id = {
                pack_id: [run for run in verifier_runs if run.context_pack_id == pack_id]
                for pack_id in candidate_finding_pack_ids
            }
            successful_snapshot_pack_ids = {
                pack_id
                for pack_id in successful_review_pack_ids
                if pack_id not in candidate_finding_pack_ids
                or (
                    verify_runs_by_pack_id.get(pack_id)
                    and all(run.status == "ok" for run in verify_runs_by_pack_id[pack_id])
                )
            }
        refreshed_review_pack_ids.setdefault(active_reviewer_id, set()).update(successful_snapshot_pack_ids)
        approved_new_findings.extend(reviewer_findings)

        if reviewer is not None:
            reviewer_context_packs = [pack for pack in report.context_packs if reviewer_matches_pack(reviewer, pack)]
            reviewer_selections[reviewer.id] = _merge_continuation_selection(
                reviewer_selections.get(reviewer.id),
                reviewer_context_packs,
                reviewer_packs,
                review_depth=review_depth,
            )

    for reviewer_id, carried_findings in carried_verification_findings.items():
        reviewer = active_reviewers_by_id[reviewer_id]
        refreshed_findings = [
            verification.finding
            for verification in verifications[prior_verification_count:]
            if verification.reviewer_id == reviewer_id
        ]
        pending_findings = [
            finding for finding in carried_findings if not verification_subject_matches_any(finding, refreshed_findings)
        ]
        if not pending_findings:
            continue
        llm_runs.extend(
            _verification_retry_reset_runs(
                llm_runs,
                reviewer_id,
                {finding.context_pack_id for finding in pending_findings},
            )
        )
        llm_config = (
            llm_config_for_reviewer(effective_config.llm, reviewer) if reviewer is not None else effective_config.llm
        )
        _approved_findings, new_verifications, verifier_runs = verify_findings(
            pending_findings,
            report.context_packs,
            llm_config,
            root,
            provider=provider,
            progress=progress,
            reviewer=reviewer,
            circuit_breaker=route_circuit,
        )
        unresolved_before_retry = unresolved_verifications(verifications)
        new_verifications = [
            verification.model_copy(
                update={
                    "review_snapshot_id": _retry_snapshot_id_for_finding(
                        unresolved_before_retry,
                        reviewer_id,
                        verification.finding,
                    )
                    or review_snapshot_ids_by_reviewer_pack.get(
                        (
                            reviewer_id,
                            verification.finding.context_pack_id,
                        )
                    )
                }
            )
            for verification in new_verifications
        ]
        retried_snapshot_ids.update(
            verification.review_snapshot_id
            for verification in new_verifications
            if verification.review_snapshot_id is not None
        )
        verifications = _append_verification_history(
            verifications,
            new_verifications,
        )
        llm_runs.extend(verifier_runs)
        failed_pack_ids = {run.context_pack_id for run in verifier_runs if run.status != "ok"}
        refreshed_review_pack_ids.get(reviewer_id, set()).difference_update(failed_pack_ids)

    completed_snapshot_pack_ids, completed_snapshot_verification_ids = _completed_retried_review_snapshots(
        verifications,
        retried_snapshot_ids,
    )
    protected_snapshot_verification_ids.update(completed_snapshot_verification_ids)
    for completed_reviewer_id, completed_pack_ids in completed_snapshot_pack_ids.items():
        refreshed_review_pack_ids.setdefault(completed_reviewer_id, set()).update(completed_pack_ids)
    verifications = _supersede_refreshed_snapshot_verifications(
        verifications,
        prior_verification_count=prior_verification_count,
        refreshed_review_pack_ids=refreshed_review_pack_ids,
        protected_verification_ids=protected_snapshot_verification_ids,
    )
    carried_findings = _remove_refreshed_reviewer_findings(
        current_findings,
        refreshed_review_pack_ids,
    )
    findings = _apply_active_verifications_to_findings(
        [*carried_findings, *approved_new_findings],
        verifications,
    )
    if reviewer_scoped:
        llm_selection = _merge_reviewer_context_selections(
            report.context_packs,
            reviewer_selections,
            include_unmatched_context_packs=reviewer_scope_ids is None,
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
            reviewer_scope_ids=reviewer_scope_ids,
        ),
        selected_packs,
    )


def _limit_continuation_pack_reviews(
    scoped_packs: list[tuple[ReviewerConfig | None, list[ContextPack]]],
    changed_files: list[ChangedFile],
    *,
    max_pack_reviews: int | None,
    priority_by_pack_id: dict[str, str] | None = None,
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
                priority_by_pack_id=priority_by_pack_id,
            ),
        )
        for index, (reviewer, packs) in enumerate(scoped_packs)
    ]


def _reviewer_failed_retry_pack_ids(
    runs: list[LLMRun],
    reviewer_id: str,
) -> set[str]:
    states = reduce_llm_pack_run_states(runs)
    return {
        state.context_pack_id
        for (state_reviewer_id, _pack_id), state in states.items()
        if state_reviewer_id == reviewer_id and state.review is not None and state.review.status != "ok"
    }


def _reviewer_behavior_config(reviewer: ReviewerConfig) -> dict[str, object]:
    return {
        "name": reviewer.name,
        "focus": reviewer.focus,
        "instructions": reviewer.instructions,
        "profile": reviewer.profile,
        "review_depth": reviewer.review_depth,
    }


def _reviewer_review_behavior_changed(
    report: ReviewReport,
    prior_config: ReviewConfig,
    current_config: ReviewConfig,
    prior_reviewer: ReviewerConfig,
    current_reviewer: ReviewerConfig,
) -> bool:
    if _reviewer_behavior_config(prior_reviewer) != _reviewer_behavior_config(current_reviewer):
        return True
    return _review_behavior_for_report(
        report,
        prior_reviewer.id,
        llm_config_for_reviewer(prior_config.llm, prior_reviewer),
    ) != _review_behavior_for_report(
        report,
        current_reviewer.id,
        llm_config_for_reviewer(current_config.llm, current_reviewer),
    )


def _review_behavior_for_report(
    report: ReviewReport,
    reviewer_id: str,
    config: LLMConfig,
) -> list[tuple[str, tuple[object, ...]]]:
    packs_by_id = {pack.id: pack for pack in report.context_packs}
    behavior: list[tuple[str, tuple[object, ...]]] = []
    for state in reduce_llm_pack_run_states(report.llm_runs).values():
        if state.reviewer_id != reviewer_id or state.review is None:
            continue
        pack = packs_by_id.get(state.context_pack_id)
        if pack is None:
            continue
        route_input = config.model_copy(deep=True)
        route_input.review_depth = "shallow" if state.review.kind == "review_shallow" else "deep"
        fallback_profile = route_input.routing.escalated_review_profile
        if (state.review.route_reason or "").startswith("fallback:") and fallback_profile:
            route_config, _profile, _reason = config_for_profile_or_model(
                route_input,
                fallback_profile,
                state.review.route_reason or "fallback",
            )
        else:
            route_config, _profile, _reason = review_config_for_pack(route_input, pack)
        behavior.append((pack.id, _effective_llm_route_behavior(route_config)))
    return sorted(behavior)


def _reviewer_verification_behavior_changed(
    report: ReviewReport,
    prior_config: ReviewConfig,
    current_config: ReviewConfig,
    prior_reviewer: ReviewerConfig,
    current_reviewer: ReviewerConfig,
) -> bool:
    prior_llm = llm_config_for_reviewer(prior_config.llm, prior_reviewer)
    current_llm = llm_config_for_reviewer(current_config.llm, current_reviewer)
    if not current_llm.verify:
        return False
    if not prior_llm.verify:
        return True
    prior_routes = _verification_behavior_for_report(
        report,
        prior_reviewer.id,
        prior_llm,
    )
    current_routes = _verification_behavior_for_report(
        report,
        current_reviewer.id,
        current_llm,
    )
    if prior_routes or current_routes:
        return prior_routes != current_routes
    return _verification_behavior_config(prior_llm) != _verification_behavior_config(current_llm)


def _verification_behavior_for_report(
    report: ReviewReport,
    reviewer_id: str,
    config: LLMConfig,
) -> list[tuple[tuple[object, ...], tuple[object, ...]]]:
    packs_by_id = {pack.id: pack for pack in report.context_packs}
    candidates = {
        finding_decision_identity(finding): finding
        for finding in [
            *[
                finding
                for finding in report.findings
                if reviewer_id in finding.reviewer_ids or (reviewer_id == "general" and not finding.reviewer_ids)
            ],
            *[
                verification.finding
                for verification in report.verifications
                if verification.reviewer_id == reviewer_id and not verification_is_terminally_replaced(verification)
            ],
        ]
        if finding.context_pack_id in packs_by_id
    }
    behavior = []
    for identity, finding in candidates.items():
        route_config, _profile, _reason = verification_config_for_finding(
            config,
            finding,
            packs_by_id[finding.context_pack_id],
        )
        behavior.append((identity, _effective_llm_route_behavior(route_config)))
    return sorted(behavior, key=lambda item: repr(item[0]))


def _effective_llm_route_behavior(config: LLMConfig) -> tuple[object, ...]:
    provider = str(config.provider)
    return (
        provider,
        config.model,
        str(config.effort) if config.effort is not None else None,
        config.timeout_seconds,
        config.codex_path if provider == "codex_cli" else None,
        config.claude_path if provider == "claude_code_cli" else None,
        (config.api.model_dump(mode="json") if provider not in {"codex_cli", "claude_code_cli", "fake"} else None),
    )


def _verification_behavior_config(config: LLMConfig) -> dict[str, object]:
    return {
        "provider": config.provider,
        "model": config.model,
        "effort": config.effort,
        "codex_path": config.codex_path,
        "claude_path": config.claude_path,
        "api": config.api.model_dump(),
        "profiles": {profile_id: profile.model_dump() for profile_id, profile in sorted(config.profiles.items())},
        "routing": {
            "verify_profile": config.routing.verify_profile,
            "escalated_verify_profile": config.routing.escalated_verify_profile,
            "escalate_verify_when": config.routing.escalate_verify_when.model_dump(),
        },
    }


def _reviewer_config_reset_runs(
    runs: list[LLMRun],
    changed_reviewer_ids: set[str],
) -> list[LLMRun]:
    if not changed_reviewer_ids:
        return []
    states = reduce_llm_pack_run_states(runs)
    return [
        LLMRun(
            kind="review_reset",
            provider="apex-ray",
            reviewer_id=reviewer_id,
            context_pack_id=context_pack_id,
            status="reviewer_config_changed",
            duration_ms=0,
        )
        for reviewer_id, context_pack_id in sorted(states)
        if reviewer_id in changed_reviewer_ids
    ]


def _reviewer_scope_expansion_reset_runs(
    runs: list[LLMRun],
    expanded_pack_ids_by_reviewer: dict[str, set[str]],
) -> list[LLMRun]:
    states = reduce_llm_pack_run_states(runs)
    return [
        LLMRun(
            kind="review_reset",
            provider="apex-ray",
            reviewer_id=reviewer_id,
            context_pack_id=context_pack_id,
            status="reviewer_scope_reactivated",
            duration_ms=0,
        )
        for reviewer_id, expanded_pack_ids in sorted(expanded_pack_ids_by_reviewer.items())
        for context_pack_id in sorted(expanded_pack_ids)
        if (state := states.get((reviewer_id, context_pack_id))) is not None
        and state.review is not None
        and state.review.findings_count > 0
    ]


def _reviewer_verification_reset_runs(
    runs: list[LLMRun],
    changed_reviewer_ids: set[str],
) -> list[LLMRun]:
    if not changed_reviewer_ids:
        return []
    states = reduce_llm_pack_run_states(runs)
    return [
        LLMRun(
            kind="verify_reset",
            provider="apex-ray",
            reviewer_id=reviewer_id,
            context_pack_id=context_pack_id,
            status="reviewer_verification_changed",
            duration_ms=0,
        )
        for reviewer_id, context_pack_id in sorted(states)
        if reviewer_id in changed_reviewer_ids
    ]


def _verification_retry_reset_runs(
    runs: list[LLMRun],
    reviewer_id: str,
    context_pack_ids: set[str],
) -> list[LLMRun]:
    states = reduce_llm_pack_run_states(runs)
    reset_runs: list[LLMRun] = []
    for context_pack_id in sorted(context_pack_ids):
        state = states.get((reviewer_id, context_pack_id))
        if state is None or not any(run.status != "ok" for run in state.verify_runs):
            continue
        reset_runs.append(
            LLMRun(
                kind="verify_reset",
                provider="apex-ray",
                reviewer_id=reviewer_id,
                context_pack_id=context_pack_id,
                status="verification_retry",
                duration_ms=0,
            )
        )
    return reset_runs


def _reviewers_with_missing_verification(
    runs: list[LLMRun],
    config: ReviewConfig,
    reviewers_by_id: dict[str, ReviewerConfig],
    findings: list[Finding],
    verifications: list[FindingVerification],
) -> set[str]:
    return set(
        _missing_verification_pack_ids(
            runs,
            config,
            reviewers_by_id,
            findings,
            verifications,
        )
    )


def _missing_verification_pack_ids(
    runs: list[LLMRun],
    config: ReviewConfig,
    reviewers_by_id: dict[str, ReviewerConfig],
    findings: list[Finding],
    verifications: list[FindingVerification],
) -> dict[str, set[str]]:
    states = reduce_llm_pack_run_states(runs)
    candidate_counts = verification_candidate_counts(findings, verifications)
    candidate_totals = {
        reviewer_id: sum(
            count
            for (candidate_reviewer_id, _pack_id), count in candidate_counts.items()
            if candidate_reviewer_id == reviewer_id
        )
        for reviewer_id in reviewers_by_id
    }
    unresolved_verification_pack_ids = unresolved_verification_candidate_pack_ids(
        findings,
        verifications,
    )
    missing: dict[str, set[str]] = {}
    for reviewer_id, reviewer in reviewers_by_id.items():
        if not llm_config_for_reviewer(config.llm, reviewer).verify:
            continue
        reviewer_missing: set[str] = set()
        for (candidate_reviewer_id, context_pack_id), candidate_count in candidate_counts.items():
            if candidate_reviewer_id != reviewer_id or not candidate_count:
                continue
            state = states.get((reviewer_id, context_pack_id))
            verify_runs = [] if state is None else state.verify_runs
            if (reviewer_id, context_pack_id) in unresolved_verification_pack_ids or any(
                run.status != "ok" for run in verify_runs
            ):
                reviewer_missing.add(context_pack_id)
        reviewer_missing.update(
            state.context_pack_id
            for state in states.values()
            if state.reviewer_id == reviewer_id
            and state.review is not None
            and state.review.status == "ok"
            and state.review.findings_count > 0
            and not any(run.status == "ok" for run in state.verify_runs)
            and candidate_totals.get(reviewer_id, 0) == 0
        )
        if reviewer_missing:
            missing[reviewer_id] = reviewer_missing
    return missing


def _effective_review_depths_by_reviewer(
    runs: list[LLMRun],
) -> dict[str, dict[str, Literal["deep", "shallow"]]]:
    depths: dict[str, dict[str, Literal["deep", "shallow"]]] = {}
    for state in reduce_llm_pack_run_states(runs).values():
        if state.review is None:
            continue
        depths.setdefault(state.reviewer_id, {})[state.context_pack_id] = (
            "shallow" if state.review.kind == "review_shallow" else "deep"
        )
    return depths


def _carried_findings_for_reviewer(
    report: ReviewReport,
    reviewer_id: str,
    reviewer: ReviewerConfig | None,
    *,
    report_findings: list[Finding],
    allowed_pack_ids: set[str],
    verifications: list[FindingVerification],
    include_resolved: bool,
) -> list[Finding]:
    reviewer_review_pack_ids = {
        run.context_pack_id
        for run in report.llm_runs
        if run.reviewer_id == reviewer_id and run.kind in {"review", "review_shallow"}
    }
    matching_pack_ids = {
        pack.id for pack in report.context_packs if reviewer is None or reviewer_matches_pack(reviewer, pack)
    }

    def finding_matches_reviewer_scope(finding: Finding) -> bool:
        if finding.context_pack_id not in allowed_pack_ids:
            return False
        if reviewer_origins_are_explicit(finding, reviewer_id):
            return bool(reviewer_origin_pack_ids(finding, reviewer_id).intersection(matching_pack_ids))
        return finding.context_pack_id in matching_pack_ids or finding.context_pack_id not in reviewer_review_pack_ids

    report_findings = [
        finding
        for finding in report_findings
        if finding_matches_reviewer_scope(finding)
        and (reviewer_id in finding.reviewer_ids or (reviewer_id == "general" and not finding.reviewer_ids))
    ]
    historical_findings = [
        verification.finding
        for verification in verifications
        if verification.reviewer_id == reviewer_id
        and finding_matches_reviewer_scope(verification.finding)
        and not verification_is_terminally_replaced(verification)
    ]
    active_decisions = [
        verification
        for verification in active_verifications(verifications)
        if verification.reviewer_id == reviewer_id and finding_matches_reviewer_scope(verification.finding)
    ]
    unresolved_decisions = [
        verification
        for verification in unresolved_verifications(verifications)
        if verification.reviewer_id == reviewer_id and finding_matches_reviewer_scope(verification.finding)
    ]
    latest_pending_snapshot_by_pack_id: dict[str, str] = {}
    for verification in unresolved_decisions:
        if verification.review_snapshot_id is not None:
            latest_pending_snapshot_by_pack_id[verification.finding.context_pack_id] = verification.review_snapshot_id
    retry_evidence_by_pack_id: dict[str, list[Finding]] = {}
    for verification in unresolved_decisions:
        pending_snapshot_id = latest_pending_snapshot_by_pack_id.get(verification.finding.context_pack_id)
        if pending_snapshot_id is not None and verification.review_snapshot_id != pending_snapshot_id:
            continue
        retry_evidence_by_pack_id.setdefault(
            verification.finding.context_pack_id,
            [],
        ).append(verification.finding)
    candidates = [*report_findings, *historical_findings]
    candidates = [
        finding
        for finding in candidates
        if finding.context_pack_id not in latest_pending_snapshot_by_pack_id
        and (
            include_resolved
            or not (retry_evidence := retry_evidence_by_pack_id.get(finding.context_pack_id))
            or verification_subject_matches_any(finding, retry_evidence)
            or not any(verification_subject_matches_any(finding, [decision.finding]) for decision in active_decisions)
        )
    ]
    normalized = [retain_finding_reviewer_provenance(finding, [reviewer_id]) for finding in candidates]
    consolidated = _consolidate_findings_by_context_pack(normalized)
    pending: list[Finding] = []
    pending_identities: set[tuple[object, ...]] = set()
    for context_pack_id in latest_pending_snapshot_by_pack_id:
        for finding in retry_evidence_by_pack_id.get(context_pack_id, []):
            identity = finding_decision_identity(finding)
            if identity in pending_identities:
                continue
            pending.append(retain_finding_reviewer_provenance(finding, [reviewer_id]))
            pending_identities.add(identity)
    return [*consolidated, *pending]


def _findings_in_current_reviewer_scope(
    findings: list[Finding],
    current_reviewer_pack_ids: dict[str, set[str]],
) -> list[Finding]:
    retained: list[Finding] = []
    for finding in findings:
        provenance = finding.reviewer_ids or ["general"]
        current_provenance: list[str] = []
        scoped_origins: dict[str, set[str]] = {}
        for reviewer_id in provenance:
            reviewer_pack_ids = current_reviewer_pack_ids.get(reviewer_id)
            if reviewer_pack_ids is None:
                continue
            if reviewer_origins_are_explicit(finding, reviewer_id):
                origin_pack_ids = reviewer_origin_pack_ids(finding, reviewer_id).intersection(reviewer_pack_ids)
                if not origin_pack_ids:
                    continue
                scoped_origins[reviewer_id] = origin_pack_ids
                current_provenance.append(reviewer_id)
                continue
            # Reports without explicit origin metadata predate durable
            # reviewer-pack provenance. Keep their established display
            # behavior and let verification/candidate scope use the canonical
            # pack plus historical run fallback.
            current_provenance.append(reviewer_id)
        if not current_provenance:
            continue
        retained.append(
            retain_finding_reviewer_provenance(
                finding,
                current_provenance,
                origin_pack_ids=scoped_origins,
            )
        )
    return retained


def _retire_verifications_outside_current_reviewer_scope(
    verifications: list[FindingVerification],
    current_reviewer_pack_ids: dict[str, set[str]],
    llm_runs: list[LLMRun],
) -> list[FindingVerification]:
    verifier_only_pack_ids = {
        (state.reviewer_id, state.context_pack_id)
        for state in reduce_llm_pack_run_states(llm_runs).values()
        if state.review is None and state.verify_runs
    }
    retained: list[FindingVerification] = []
    for verification in verifications:
        reviewer_pack_ids = current_reviewer_pack_ids.get(verification.reviewer_id)
        scoped_origins: set[str] | None = None
        if reviewer_pack_ids is not None and reviewer_origins_are_explicit(
            verification.finding,
            verification.reviewer_id,
        ):
            scoped_origins = reviewer_origin_pack_ids(
                verification.finding,
                verification.reviewer_id,
            ).intersection(reviewer_pack_ids)
            in_scope = bool(scoped_origins)
        else:
            in_scope = reviewer_pack_ids is not None and (
                not verification.finding.context_pack_id
                or verification.finding.context_pack_id in reviewer_pack_ids
                or (
                    verification.reviewer_id,
                    verification.finding.context_pack_id,
                )
                in verifier_only_pack_ids
            )
        if in_scope:
            retained.append(
                verification.model_copy(
                    update={
                        "finding": retain_finding_reviewer_provenance(
                            verification.finding,
                            [verification.reviewer_id],
                            origin_pack_ids=(
                                {verification.reviewer_id: scoped_origins} if scoped_origins is not None else None
                            ),
                        )
                    }
                )
                if scoped_origins is not None
                else verification
            )
            continue
        retained.append(
            verification.model_copy(
                update={
                    "superseded": True,
                    "superseded_reason": ("Replaced by the current reviewer configuration scope."),
                }
            )
        )
    return retained


def _supersede_reviewer_verifications(
    verifications: list[FindingVerification],
    reviewer_ids: set[str],
) -> list[FindingVerification]:
    if not reviewer_ids:
        return list(verifications)
    return [
        (
            verification.model_copy(
                update={
                    "superseded": True,
                    "superseded_reason": "Reviewer review or verification configuration changed.",
                }
            )
            if verification.reviewer_id in reviewer_ids and not verification.superseded
            else verification
        )
        for verification in verifications
    ]


def _append_verification_history(
    existing: list[FindingVerification],
    new: list[FindingVerification],
) -> list[FindingVerification]:
    effective_new = [verification for verification in new if not verification.superseded]
    updated = [
        (
            verification.model_copy(
                update={
                    "superseded": True,
                    "superseded_reason": "Replaced by a newer successful verification decision.",
                }
            )
            if not verification.superseded
            and any(
                candidate.reviewer_id == verification.reviewer_id
                and verification_decisions_match(verification, candidate)
                for candidate in effective_new
            )
            else verification
        )
        for verification in existing
    ]
    return [*updated, *new]


def _review_snapshot_id(
    *,
    reviewer_id: str,
    context_pack_id: str,
    run_count: int,
    verification_count: int,
) -> str:
    fingerprint = payload_fingerprint(
        {
            "reviewer_id": reviewer_id,
            "context_pack_id": context_pack_id,
            "run_count": run_count,
            "verification_count": verification_count,
        }
    )
    return f"snapshot-{fingerprint[:20]}"


def _retry_snapshot_id_for_finding(
    unresolved: list[FindingVerification],
    reviewer_id: str,
    finding: Finding,
) -> str | None:
    identity = finding_decision_identity(finding)
    for verification in reversed(unresolved):
        if (
            verification.reviewer_id == reviewer_id
            and verification.review_snapshot_id is not None
            and finding_decision_identity(verification.finding) == identity
        ):
            return verification.review_snapshot_id
    return None


def _completed_retried_review_snapshots(
    verifications: list[FindingVerification],
    retried_snapshot_ids: set[str],
) -> tuple[dict[str, set[str]], set[int]]:
    completed_pack_ids: dict[str, set[str]] = {}
    protected_verification_ids: set[int] = set()
    for snapshot_id in retried_snapshot_ids:
        snapshot_history = [
            verification for verification in verifications if verification.review_snapshot_id == snapshot_id
        ]
        if not snapshot_history:
            continue
        reviewer_ids = {verification.reviewer_id for verification in snapshot_history}
        context_pack_ids = {
            verification.finding.context_pack_id
            for verification in snapshot_history
            if verification.finding.context_pack_id
        }
        if len(reviewer_ids) != 1 or len(context_pack_ids) != 1:
            continue
        latest_decisions_by_subject: dict[tuple[object, ...], FindingVerification] = {}
        for verification in snapshot_history:
            latest_decisions_by_subject[finding_decision_identity(verification.finding)] = verification
        active_snapshot = list(latest_decisions_by_subject.values())
        if any(verification.superseded for verification in active_snapshot):
            continue
        reviewer_id = next(iter(reviewer_ids))
        context_pack_id = next(iter(context_pack_ids))
        completed_pack_ids.setdefault(reviewer_id, set()).add(context_pack_id)
        protected_verification_ids.update(id(verification) for verification in active_snapshot)
    return completed_pack_ids, protected_verification_ids


def _supersede_refreshed_snapshot_verifications(
    verifications: list[FindingVerification],
    *,
    prior_verification_count: int,
    refreshed_review_pack_ids: dict[str, set[str]],
    protected_verification_ids: set[int] | None = None,
) -> list[FindingVerification]:
    protected_verification_ids = protected_verification_ids or set()
    updated: list[FindingVerification] = []
    for index, verification in enumerate(verifications):
        refreshed_pack_ids = refreshed_review_pack_ids.get(verification.reviewer_id, set())
        if (
            index >= prior_verification_count
            or id(verification) in protected_verification_ids
            or not refreshed_pack_ids
        ):
            updated.append(verification)
            continue
        if reviewer_origins_are_explicit(verification.finding, verification.reviewer_id):
            current_origins = reviewer_origin_pack_ids(
                verification.finding,
                verification.reviewer_id,
            )
            remaining_origins = current_origins.difference(refreshed_pack_ids)
            if remaining_origins == current_origins:
                updated.append(verification)
                continue
            if remaining_origins:
                updated.append(
                    verification.model_copy(
                        update={
                            "finding": retain_finding_reviewer_provenance(
                                verification.finding,
                                [verification.reviewer_id],
                                origin_pack_ids={
                                    verification.reviewer_id: remaining_origins,
                                },
                            )
                        }
                    )
                )
                continue
        elif verification.finding.context_pack_id not in refreshed_pack_ids:
            updated.append(verification)
            continue
        updated.append(
            verification.model_copy(
                update={
                    "superseded": True,
                    "superseded_reason": "Replaced by a newer successful reviewer-pack snapshot.",
                }
            )
        )
    return updated


def _remove_refreshed_reviewer_findings(
    findings: list[Finding],
    refreshed_review_pack_ids: dict[str, set[str]],
) -> list[Finding]:
    retained: list[Finding] = []
    for finding in findings:
        provenance = finding.reviewer_ids or ["general"]
        retained_provenance: list[str] = []
        retained_origins: dict[str, set[str]] = {}
        for reviewer_id in provenance:
            refreshed_pack_ids = refreshed_review_pack_ids.get(reviewer_id, set())
            if not refreshed_pack_ids:
                retained_provenance.append(reviewer_id)
                continue
            if reviewer_origins_are_explicit(finding, reviewer_id):
                remaining_origins = reviewer_origin_pack_ids(finding, reviewer_id).difference(refreshed_pack_ids)
                if not remaining_origins:
                    continue
                retained_origins[reviewer_id] = remaining_origins
                retained_provenance.append(reviewer_id)
                continue
            if finding.context_pack_id not in refreshed_pack_ids:
                retained_provenance.append(reviewer_id)
        if not retained_provenance:
            continue
        retained.append(
            retain_finding_reviewer_provenance(
                finding,
                retained_provenance,
                origin_pack_ids=retained_origins,
            )
        )
    return retained


def _apply_active_verifications_to_findings(
    findings: list[Finding],
    verifications: list[FindingVerification],
) -> list[Finding]:
    current_verifications = active_verifications(verifications)
    approved_findings = [
        retain_finding_reviewer_provenance(
            verification.finding,
            [verification.reviewer_id],
        )
        for verification in current_verifications
        if verification.approved
    ]

    def retain_provenance(finding: Finding, reviewer_id: str) -> bool:
        exact_decisions = [
            verification
            for verification in current_verifications
            if verification.reviewer_id == reviewer_id
            and finding_decision_identity(finding) == finding_decision_identity(verification.finding)
        ]
        if exact_decisions:
            return any(verification.approved for verification in exact_decisions)
        matching_decisions = [
            verification
            for verification in current_verifications
            if verification.reviewer_id == reviewer_id
            and verification_subject_matches_any(finding, [verification.finding])
        ]
        if matching_decisions:
            return any(verification.approved for verification in matching_decisions)
        has_cross_pack_decision = any(
            verification.reviewer_id == reviewer_id
            and verification.finding.context_pack_id != finding.context_pack_id
            and findings_are_duplicates(finding, verification.finding)
            and (
                findings_share_reviewer_origin(
                    finding,
                    verification.finding,
                    reviewer_id,
                )
                or (not reviewer_origins_are_explicit(finding, reviewer_id) and len(finding.reviewer_ids) > 1)
            )
            for verification in current_verifications
        )
        if has_cross_pack_decision:
            has_distinct_report_candidate = any(
                candidate is not finding
                and reviewer_id in (candidate.reviewer_ids or ["general"])
                and findings_are_duplicates(finding, candidate)
                for candidate in findings
            )
            return has_distinct_report_candidate
        return True

    effective_findings: list[Finding] = []
    for finding in [*findings, *approved_findings]:
        provenance = finding.reviewer_ids or ["general"]
        retained_provenance = [reviewer_id for reviewer_id in provenance if retain_provenance(finding, reviewer_id)]
        if not retained_provenance:
            continue
        effective_findings.append(retain_finding_reviewer_provenance(finding, retained_provenance))
    per_pack_findings = _consolidate_findings_by_context_pack(
        effective_findings,
        preferred_findings=[verification.finding for verification in current_verifications if verification.approved],
    )
    return _consolidate_cross_reviewer_findings(
        per_pack_findings,
        preferred_findings=[verification.finding for verification in current_verifications if verification.approved],
    )


def _consolidate_findings_by_context_pack(
    findings: list[Finding],
    *,
    preferred_findings: list[Finding] | None = None,
) -> list[Finding]:
    pack_order = list(dict.fromkeys(finding.context_pack_id for finding in findings))
    return [
        finding
        for context_pack_id in pack_order
        for finding in consolidate_findings(
            [candidate for candidate in findings if candidate.context_pack_id == context_pack_id],
            preferred_findings=preferred_findings or [],
        )
    ]


def _consolidate_cross_reviewer_findings(
    findings: list[Finding],
    *,
    preferred_findings: list[Finding] | None = None,
) -> list[Finding]:
    consolidated: list[Finding] = []
    preferred_identities = {finding_decision_identity(finding) for finding in (preferred_findings or [])}
    for finding in findings:
        finding_reviewers = set(finding.reviewer_ids or ["general"])
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(consolidated)
                if (
                    finding_reviewers.isdisjoint(existing.reviewer_ids or ["general"])
                    and findings_are_duplicates(existing, finding)
                )
                or _findings_have_exact_cross_pack_identity(existing, finding)
            ),
            None,
        )
        if duplicate_index is None:
            consolidated.append(finding)
            continue
        existing = consolidated[duplicate_index]
        if _findings_have_exact_cross_pack_identity(existing, finding):
            preferred = (
                finding
                if finding_decision_identity(finding) in preferred_identities
                and finding_decision_identity(existing) not in preferred_identities
                else existing
            )
            consolidated[duplicate_index] = merge_finding_reviewer_provenance(
                preferred,
                [existing, finding],
            )
            continue
        consolidated[duplicate_index] = consolidate_findings(
            [existing, finding],
            preferred_findings=preferred_findings or [],
        )[0]
    return consolidated


def _findings_have_exact_cross_pack_identity(
    left: Finding,
    right: Finding,
) -> bool:
    return (
        left.context_pack_id != right.context_pack_id
        and finding_decision_identity(left)[:-1] == finding_decision_identity(right)[:-1]
    )


def _rebase_reviewer_selection_scope(
    existing: LLMContextSelection | None,
    context_packs: list[ContextPack],
    effective_review_depths: dict[str, Literal["deep", "shallow"]] | None = None,
) -> LLMContextSelection:
    total_ids = [pack.id for pack in context_packs]
    effective_review_depths = effective_review_depths or {}
    total_id_set = set(total_ids)
    existing_selected_ids = set(existing.selected_context_pack_ids if existing is not None else [])
    selected_ids = [
        pack_id for pack_id in total_ids if pack_id in existing_selected_ids or pack_id in effective_review_depths
    ]
    selected_id_set = set(selected_ids)
    unselected_ids = [pack_id for pack_id in total_ids if pack_id not in selected_id_set]
    deep_selected_ids = [
        pack_id
        for pack_id in selected_ids
        if (
            effective_review_depths.get(pack_id) == "deep"
            or (
                pack_id not in effective_review_depths
                and existing is not None
                and pack_id in existing.deep_selected_context_pack_ids
            )
        )
    ]
    shallow_selected_ids = [
        pack_id
        for pack_id in selected_ids
        if (
            effective_review_depths.get(pack_id) == "shallow"
            or (
                pack_id not in effective_review_depths
                and existing is not None
                and pack_id in existing.shallow_selected_context_pack_ids
            )
        )
    ]
    if existing is None:
        return LLMContextSelection(
            total_context_pack_ids=total_ids,
            selected_context_pack_ids=selected_ids,
            deep_selected_context_pack_ids=deep_selected_ids,
            shallow_selected_context_pack_ids=shallow_selected_ids,
            unselected_context_pack_ids=unselected_ids,
        )
    return existing.model_copy(
        update={
            "total_context_pack_ids": total_ids,
            "selected_context_pack_ids": selected_ids,
            "deep_selected_context_pack_ids": deep_selected_ids,
            "shallow_selected_context_pack_ids": shallow_selected_ids,
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
