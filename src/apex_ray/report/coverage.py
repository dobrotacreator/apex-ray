from collections import Counter
from shlex import quote
from typing import Literal

from apex_ray.findings import (
    active_verifications,
    reviewer_origin_pack_ids,
    reviewer_origins_are_explicit,
    unresolved_verification_candidate_pack_ids,
    verification_candidates_by_reviewer_pack,
)
from apex_ray.llm.usage import aggregate_actual_usage
from apex_ray.models import (
    ContextPack,
    Finding,
    FindingVerification,
    LLMContextSelection,
    LLMCoverageSummary,
    LLMCoverageTodo,
    LLMPackReviewStatus,
    LLMResidualRiskSummary,
    LLMReviewerCoverageSummary,
    LLMRouteSummary,
    LLMRun,
    ReviewConfig,
)
from apex_ray.report.coverage_breakdown import (
    _build_file_coverage,
    _build_slice_coverage,
    _coverage_ratio,
    _is_high_risk_pack,
    _pack_review_slice,
    _pack_risk_by_severity,
    _pack_scope,
    _pack_symbol_names,
    _source_line_coverage_ratio,
    pack_residual_priority,
)
from apex_ray.report.coverage_breakdown import (
    _format_pack_symbols as _format_pack_symbols,
)
from apex_ray.report.coverage_breakdown import (
    _line_range_count as _line_range_count,
)
from apex_ray.report.run_state import (
    EffectiveLLMPackRunState,
    reduce_llm_pack_run_states,
)
from apex_ray.reviewers import reviewer_matches_pack


def _build_llm_coverage(
    config: ReviewConfig,
    context_packs: list[ContextPack],
    llm_runs: list[LLMRun],
    llm_selection: LLMContextSelection | None = None,
    reviewer_selections: dict[str, LLMContextSelection] | None = None,
    *,
    reviewer_scope_ids: list[str] | None = None,
    findings: list[Finding] | None = None,
    verifications: list[FindingVerification] | None = None,
) -> LLMCoverageSummary:
    all_context_packs = list(context_packs)
    all_context_pack_ids = {pack.id for pack in context_packs}
    if llm_selection is not None:
        scoped_pack_ids = set(llm_selection.total_context_pack_ids)
        context_packs = [pack for pack in context_packs if pack.id in scoped_pack_ids]
    review_runs = [run for run in llm_runs if run.kind in {"review", "review_shallow"}]
    verify_runs = [run for run in llm_runs if run.kind == "verify"]
    execution_runs = [
        run
        for run in llm_runs
        if run.kind
        in {
            "review",
            "review_shallow",
            "verify",
        }
    ]
    failed_review_runs = [run for run in review_runs if run.status != "ok"]
    failed_verify_runs = [run for run in verify_runs if run.status != "ok"]
    effective_run_states = reduce_llm_pack_run_states(llm_runs)
    reviewer_scope = set(reviewer_scope_ids) if reviewer_scope_ids is not None else None
    current_reviewer_selections = {
        reviewer_id: selection
        for reviewer_id, selection in (reviewer_selections or {}).items()
        if reviewer_scope is None or reviewer_id in reviewer_scope
    }
    configured_reviewer_pack_ids = (
        {
            reviewer.id: (
                {pack.id for pack in context_packs if reviewer_matches_pack(reviewer, pack)}
                if reviewer.enabled
                else set()
            )
            for reviewer in config.reviewers
            if reviewer_scope is None or reviewer.id in reviewer_scope
        }
        if config.reviewers
        else None
    )
    scoped_context_pack_ids = {pack.id for pack in context_packs}
    historical_review_keys = {
        (run.reviewer_id, run.context_pack_id) for run in llm_runs if run.kind in {"review", "review_shallow"}
    }

    def verification_key_in_scope(
        key: tuple[str, str],
        candidates: list[Finding],
    ) -> bool:
        reviewer_id, context_pack_id = key
        verifier_only_or_unattempted = key not in historical_review_keys
        selection = current_reviewer_selections.get(reviewer_id)
        if selection is not None:
            reviewer_pack_ids = set(selection.total_context_pack_ids)
        elif configured_reviewer_pack_ids is not None:
            if reviewer_id not in configured_reviewer_pack_ids:
                return False
            reviewer_pack_ids = configured_reviewer_pack_ids[reviewer_id]
        elif current_reviewer_selections:
            return False
        elif reviewer_id == "general":
            reviewer_pack_ids = scoped_context_pack_ids
        else:
            return False
        if context_pack_id in reviewer_pack_ids:
            return True
        explicit_candidates = [
            candidate for candidate in candidates if reviewer_origins_are_explicit(candidate, reviewer_id)
        ]
        if any(
            reviewer_origin_pack_ids(candidate, reviewer_id).intersection(reviewer_pack_ids)
            for candidate in explicit_candidates
        ):
            return True
        return len(explicit_candidates) != len(candidates) and verifier_only_or_unattempted

    candidate_findings = verification_candidates_by_reviewer_pack(
        findings or [],
        verifications or [],
    )
    verification_candidates = {
        key: len(candidates)
        for key, candidates in candidate_findings.items()
        if key[1] in all_context_pack_ids and verification_key_in_scope(key, candidates)
    }
    scoped_effective_run_states = {
        key: state
        for key, state in effective_run_states.items()
        if _run_state_in_reviewer_scope(
            state,
            current_reviewer_selections,
            configured_reviewer_pack_ids,
            scoped_context_pack_ids,
            verification_candidate_keys=set(verification_candidates),
        )
    }
    active_verification_counts: dict[tuple[str, str], int] = {}
    for verification in active_verifications(verifications or []):
        key = (verification.reviewer_id, verification.finding.context_pack_id)
        if key in verification_candidates:
            active_verification_counts[key] = active_verification_counts.get(key, 0) + 1
    unresolved_verification_pack_ids = {
        key
        for key in unresolved_verification_candidate_pack_ids(
            findings or [],
            verifications or [],
        )
        if key in verification_candidates
    }
    effective_review_runs = [state.review for state in scoped_effective_run_states.values() if state.review is not None]
    effective_verify_runs = [
        run
        for state in scoped_effective_run_states.values()
        if _reviewer_verify_enabled(config, state.reviewer_id)
        for run in state.verify_runs
    ]
    successful_review_runs = [run for run in effective_review_runs if run.status == "ok"]
    active_failed_review_runs = [run for run in effective_review_runs if run.status != "ok"]
    active_failed_verify_runs = [run for run in effective_verify_runs if run.status != "ok"]
    reviewer_coverage = _build_reviewer_coverage(
        config,
        context_packs,
        current_reviewer_selections,
        scoped_effective_run_states,
        reviewer_scope=reviewer_scope,
        active_verification_counts=active_verification_counts,
        verification_candidate_counts=verification_candidates,
        unresolved_verification_pack_ids=unresolved_verification_pack_ids,
    )
    effective_verify_enabled = (
        any(reviewer.verify_enabled for reviewer in reviewer_coverage) if reviewer_coverage else config.llm.verify
    )
    reviewed_ids = {run.context_pack_id for run in successful_review_runs}
    deep_reviewed_ids = {
        run.context_pack_id for run in effective_review_runs if run.kind == "review" and run.status == "ok"
    }
    shallow_reviewed_ids = {
        run.context_pack_id for run in effective_review_runs if run.kind == "review_shallow" and run.status == "ok"
    }
    failed_review_by_pack_id = {run.context_pack_id: run for run in active_failed_review_runs}
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
        failed_review_runs=len(active_failed_review_runs),
        failed_verify_runs=len(active_failed_verify_runs),
        failed_review_status_counts=dict(sorted(Counter(run.status for run in active_failed_review_runs).items())),
        failed_verify_status_counts=dict(sorted(Counter(run.status for run in active_failed_verify_runs).items())),
        unreviewed_count=len(unreviewed_pack_ids),
    )
    required_reviewer_failures = [
        reason
        for reviewer in reviewer_coverage
        if reviewer.required and reviewer.status == "fail"
        for reason in reviewer.reasons
    ]
    if required_reviewer_failures:
        quality_gate_status = "fail"
        quality_gate_reasons = [*quality_gate_reasons, *required_reviewer_failures]
        partial_severity = "critical"
        partial_reasons = [*partial_reasons, *required_reviewer_failures]
    unresolved_general_pack_ids = {
        pack_id for reviewer_id, pack_id in unresolved_verification_pack_ids if reviewer_id == "general"
    }
    if (
        config.llm.enabled
        and not config.reviewers
        and _reviewer_verify_enabled(config, "general")
        and unresolved_general_pack_ids
    ):
        unresolved_general_reason = (
            "General review has unresolved verification subjects in "
            f"{len(unresolved_general_pack_ids)} context pack(s)."
        )
        quality_gate_status = "fail"
        quality_gate_reasons = [*quality_gate_reasons, unresolved_general_reason]
        partial_severity = "critical"
        partial_reasons = [*partial_reasons, unresolved_general_reason]
    pack_statuses = _build_pack_statuses(
        context_packs,
        reviewed_ids,
        deep_reviewed_ids,
        shallow_reviewed_ids,
        unreviewed_reasons,
        residual_risks,
        failed_review_by_pack_id,
    )
    reviewer_coverage_todos = _build_reviewer_coverage_todos(
        reviewer_coverage,
        all_context_packs,
        scoped_effective_run_states,
        active_verification_counts,
        verification_candidates,
        unresolved_verification_pack_ids,
    )
    reviewer_debt_pack_ids = {todo.context_pack_id for todo in reviewer_coverage_todos}
    coverage_todos = [
        *(
            todo
            for todo in _build_coverage_todos(residual_risks, context_packs)
            if todo.context_pack_id not in reviewer_debt_pack_ids
        ),
        *reviewer_coverage_todos,
    ]
    coverage_todos.sort(
        key=lambda todo: (
            {"p0": 0, "p1": 1, "p2": 2}.get(todo.priority, 9),
            todo.reviewer_id or "",
            todo.file,
            todo.context_pack_id,
        )
    )

    routes: dict[tuple[str, str, str, str | None, str | None, str | None, str | None, str], LLMRouteSummary] = {}
    for run in execution_runs:
        cache_hits = _run_cache_hits(run)
        cache_misses = _run_cache_misses(run)
        key = (
            run.kind,
            run.reviewer_id,
            run.provider,
            run.model,
            run.effort,
            run.profile,
            run.route_reason,
            run.status,
        )
        route = routes.get(key)
        if route is None:
            route = LLMRouteSummary(
                kind=run.kind,
                reviewer_id=run.reviewer_id,
                provider=run.provider,
                model=run.model,
                effort=run.effort,
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
        route.actual_input_tokens += run.actual_input_tokens
        route.actual_cached_input_tokens += run.actual_cached_input_tokens
        route.actual_output_tokens += run.actual_output_tokens
        route.actual_reasoning_output_tokens += run.actual_reasoning_output_tokens
        route.actual_total_tokens += run.actual_total_tokens
        route.actual_cache_read_input_tokens += run.actual_cache_read_input_tokens
        route.actual_cache_creation_input_tokens += run.actual_cache_creation_input_tokens
        route.estimated_saved_input_tokens += run.estimated_saved_input_tokens
        if run.estimated_cost_usd is not None:
            route.estimated_cost_usd = round((route.estimated_cost_usd or 0.0) + run.estimated_cost_usd, 6)
        if run.usage_source and run.usage_source not in route.usage_sources:
            route.usage_sources.append(run.usage_source)
        route.cache_hits += cache_hits
        route.cache_misses += cache_misses
        route.errors += 1 if run.error else 0

    usage_totals = aggregate_actual_usage(execution_runs)
    return LLMCoverageSummary(
        enabled=config.llm.enabled,
        verify_enabled=effective_verify_enabled,
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
        reviewers=reviewer_coverage,
        cluster_context_packs=sum(1 for pack in context_packs if _pack_scope(pack) == "cluster"),
        file_context_packs=sum(1 for pack in context_packs if _pack_scope(pack) == "file"),
        symbol_context_packs=sum(1 for pack in context_packs if _pack_scope(pack) == "symbol"),
        reviewed_files=sorted({pack.file for pack in context_packs if pack.id in reviewed_ids}),
        unreviewed_files=sorted({pack.file for pack in context_packs if pack.id not in reviewed_ids}),
        review_runs=len(review_runs),
        verify_runs=len(verify_runs),
        failed_review_runs=len(failed_review_runs),
        failed_verify_runs=len(failed_verify_runs),
        run_status_counts=dict(sorted(Counter(run.status for run in execution_runs).items())),
        total_duration_ms=sum(run.duration_ms for run in execution_runs),
        input_chars=sum(run.input_chars for run in execution_runs),
        estimated_input_tokens=sum(run.estimated_input_tokens for run in execution_runs),
        **usage_totals,
        cache_hits=sum(_run_cache_hits(run) for run in execution_runs),
        cache_misses=sum(_run_cache_misses(run) for run in execution_runs),
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


def _build_reviewer_coverage(
    config: ReviewConfig,
    context_packs: list[ContextPack],
    reviewer_selections: dict[str, LLMContextSelection],
    effective_run_states: dict[tuple[str, str], EffectiveLLMPackRunState],
    *,
    reviewer_scope: set[str] | None = None,
    active_verification_counts: dict[tuple[str, str], int] | None = None,
    verification_candidate_counts: dict[tuple[str, str], int] | None = None,
    unresolved_verification_pack_ids: set[tuple[str, str]] | None = None,
) -> list[LLMReviewerCoverageSummary]:
    active_verification_counts = active_verification_counts or {}
    verification_candidate_counts = verification_candidate_counts or {}
    unresolved_verification_pack_ids = unresolved_verification_pack_ids or set()
    configured = {reviewer.id: reviewer for reviewer in config.reviewers}
    enabled_configured_ids = {
        reviewer.id
        for reviewer in config.reviewers
        if reviewer.enabled and (reviewer_scope is None or reviewer.id in reviewer_scope)
    }
    reviewer_ids = set(reviewer_selections) | {reviewer_id for reviewer_id, _pack_id in effective_run_states}
    reviewer_ids.update(enabled_configured_ids)
    summaries: list[LLMReviewerCoverageSummary] = []
    for reviewer_id in sorted(reviewer_ids):
        selection = reviewer_selections.get(reviewer_id)
        reviewer = configured.get(reviewer_id)
        required = reviewer.required if reviewer is not None and reviewer.enabled else False
        verify_enabled = reviewer.verify if reviewer is not None and reviewer.verify is not None else config.llm.verify
        reviewer_states = [
            state
            for (state_reviewer_id, _pack_id), state in effective_run_states.items()
            if state_reviewer_id == reviewer_id
        ]
        effective_review_runs = [state.review for state in reviewer_states if state.review is not None]
        effective_verify_runs = (
            [run for state in reviewer_states for run in state.verify_runs] if verify_enabled else []
        )
        reviewed = {
            run.context_pack_id
            for run in effective_review_runs
            if run.status == "ok" and run.context_pack_id is not None
        }
        if selection is not None:
            matching_ids = list(selection.total_context_pack_ids)
            selected_ids = list(selection.selected_context_pack_ids)
        else:
            reviewer_state_ids = {state.context_pack_id for state in reviewer_states}
            if reviewer is not None and reviewer.enabled:
                matching_ids = [pack.id for pack in context_packs if reviewer_matches_pack(reviewer, pack)]
                selected_ids = list(matching_ids)
            else:
                matching_ids = [pack.id for pack in context_packs if pack.id in reviewer_state_ids]
                selected_ids = list(matching_ids)
        reviewed_ids = [pack_id for pack_id in matching_ids if pack_id in reviewed]
        missing_ids = [pack_id for pack_id in selected_ids if pack_id not in reviewed]
        reviewer_candidate_total = sum(
            count
            for (candidate_reviewer_id, _pack_id), count in verification_candidate_counts.items()
            if candidate_reviewer_id == reviewer_id
        )
        missing_verification_ids = sorted(
            {
                *(
                    pack_id
                    for candidate_reviewer_id, pack_id in unresolved_verification_pack_ids
                    if verify_enabled and candidate_reviewer_id == reviewer_id
                ),
                *(
                    state.context_pack_id
                    for state in reviewer_states
                    if verify_enabled
                    and state.context_pack_id in matching_ids
                    and state.review is not None
                    and state.review.status == "ok"
                    and state.review.findings_count > 0
                    and not any(run.status == "ok" for run in state.verify_runs)
                    and reviewer_candidate_total == 0
                ),
            }
        )
        active_failed_review_runs = [run for run in effective_review_runs if run.status != "ok"]
        active_failed_verify_runs = [run for run in effective_verify_runs if run.status != "ok"]
        effective_runs = [*effective_review_runs, *effective_verify_runs]
        reasons: list[str] = []
        if matching_ids and not selected_ids:
            reasons.append(
                f"{'Required reviewer' if required else 'Reviewer'} {reviewer_id} selected no "
                f"context packs from {len(matching_ids)} matching pack(s)."
            )
        elif missing_ids:
            reasons.append(
                f"{'Required reviewer' if required else 'Reviewer'} {reviewer_id} did not review "
                f"{len(missing_ids)} of {len(selected_ids)} selected context pack(s)."
            )
        if active_failed_verify_runs:
            reasons.append(
                f"{'Required reviewer' if required else 'Reviewer'} {reviewer_id} had "
                f"{len(active_failed_verify_runs)} failed verification run(s)."
            )
        if missing_verification_ids:
            reasons.append(
                f"{'Required reviewer' if required else 'Reviewer'} {reviewer_id} has "
                f"{len(missing_verification_ids)} reviewed pack(s) with unresolved verification subjects."
            )
        status: Literal["not_applicable", "pass", "warn", "fail"]
        if not config.llm.enabled:
            reasons = []
            status = "not_applicable"
        elif not matching_ids:
            status = "not_applicable"
        elif reasons:
            status = "fail" if required else "warn"
        else:
            status = "pass"
        usage = aggregate_actual_usage(effective_runs)
        summaries.append(
            LLMReviewerCoverageSummary(
                reviewer_id=reviewer_id,
                required=required,
                verify_enabled=verify_enabled,
                status=status,
                reasons=reasons,
                matching_context_packs=len(matching_ids),
                selected_context_packs=len(selected_ids),
                reviewed_context_packs=len(reviewed_ids),
                failed_review_runs=len(active_failed_review_runs),
                failed_verify_runs=len(active_failed_verify_runs),
                matching_context_pack_ids=matching_ids,
                selected_context_pack_ids=selected_ids,
                reviewed_context_pack_ids=reviewed_ids,
                estimated_input_tokens=sum(run.estimated_input_tokens for run in effective_runs),
                actual_total_tokens=usage["actual_total_tokens"],
                estimated_cost_usd=usage["estimated_cost_usd"],
            )
        )
    return summaries


def _reviewer_verify_enabled(config: ReviewConfig, reviewer_id: str) -> bool:
    reviewer = next(
        (candidate for candidate in config.reviewers if candidate.id == reviewer_id),
        None,
    )
    if reviewer is not None and reviewer.verify is not None:
        return reviewer.verify
    return config.llm.verify


def _run_state_in_reviewer_scope(
    state: EffectiveLLMPackRunState,
    reviewer_selections: dict[str, LLMContextSelection],
    configured_reviewer_pack_ids: dict[str, set[str]] | None,
    scoped_context_pack_ids: set[str],
    *,
    verification_candidate_keys: set[tuple[str, str]],
) -> bool:
    verification_candidate_state = (
        bool(state.verify_runs) and (state.reviewer_id, state.context_pack_id) in verification_candidate_keys
    )
    if state.context_pack_id not in scoped_context_pack_ids and not verification_candidate_state:
        return False
    selection = reviewer_selections.get(state.reviewer_id)
    if selection is not None:
        return state.context_pack_id in selection.total_context_pack_ids or verification_candidate_state
    if configured_reviewer_pack_ids is not None:
        return state.context_pack_id in configured_reviewer_pack_ids.get(
            state.reviewer_id,
            set(),
        ) or (state.reviewer_id in configured_reviewer_pack_ids and verification_candidate_state)
    if reviewer_selections:
        return False
    return True


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
    failed_review_status_counts: dict[str, int] | None = None,
    failed_verify_status_counts: dict[str, int] | None = None,
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
        reasons.append(_format_failed_run_reason("review", failed_review_runs, failed_review_status_counts))
    if failed_verify_runs:
        reasons.append(_format_failed_run_reason("verifier", failed_verify_runs, failed_verify_status_counts))
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


def _format_failed_run_reason(kind: str, total: int, status_counts: dict[str, int] | None) -> str:
    details = ", ".join(f"{status}: {count}" for status, count in (status_counts or {}).items())
    suffix = f" ({details})" if details else ""
    return f"{total} {kind} run(s) failed{suffix}"


def _residual_risk_summary(pack: ContextPack, reason: str) -> LLMResidualRiskSummary:
    risk_by_severity = _pack_risk_by_severity(pack)
    rule_modes = Counter(str(rule.mode) for rule in pack.rule_matches)
    rule_severities = Counter(str(rule.severity) for rule in pack.rule_matches)
    priority = pack_residual_priority(pack)
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
                suggested_command=continue_command_for_pack(pack.id),
                estimated_chars=pack.stats.estimated_chars,
                changed_lines=pack.changed_lines,
                changed_symbols=_pack_symbol_names([pack]),
            )
        )
    return todos


def _build_reviewer_coverage_todos(
    reviewer_coverage: list[LLMReviewerCoverageSummary],
    context_packs: list[ContextPack],
    effective_run_states: dict[tuple[str, str], EffectiveLLMPackRunState],
    active_verification_counts: dict[tuple[str, str], int],
    verification_candidate_counts: dict[tuple[str, str], int],
    unresolved_verification_pack_ids: set[tuple[str, str]],
) -> list[LLMCoverageTodo]:
    packs_by_id = {pack.id: pack for pack in context_packs}
    priority_rank = {"p0": 0, "p1": 1, "p2": 2}
    todos: list[LLMCoverageTodo] = []
    for reviewer in reviewer_coverage:
        if reviewer.status == "not_applicable":
            continue
        reviewer_states = {
            pack_id: state
            for (reviewer_id, pack_id), state in effective_run_states.items()
            if reviewer_id == reviewer.reviewer_id
        }
        reviewer_candidate_total = sum(
            count
            for (candidate_reviewer_id, _pack_id), count in verification_candidate_counts.items()
            if candidate_reviewer_id == reviewer.reviewer_id
        )
        failed_verify_ids = {
            pack_id
            for pack_id, state in reviewer_states.items()
            if reviewer.verify_enabled and any(run.status != "ok" for run in state.verify_runs)
        }
        missing_verify_ids = {
            pack_id
            for candidate_reviewer_id, pack_id in unresolved_verification_pack_ids
            if reviewer.verify_enabled and candidate_reviewer_id == reviewer.reviewer_id
        }
        missing_verify_ids.update(
            {
                pack_id
                for pack_id, state in reviewer_states.items()
                if reviewer.verify_enabled
                and state.review is not None
                and state.review.status == "ok"
                and state.review.findings_count > 0
                and (
                    (reviewer.reviewer_id, pack_id) in unresolved_verification_pack_ids
                    or (not any(run.status == "ok" for run in state.verify_runs) and reviewer_candidate_total == 0)
                )
            }
        )
        debt_ids = set(reviewer.selected_context_pack_ids).difference(reviewer.reviewed_context_pack_ids)
        if reviewer.matching_context_pack_ids and not reviewer.selected_context_pack_ids:
            debt_ids.update(reviewer.matching_context_pack_ids)
        debt_ids.update(failed_verify_ids)
        debt_ids.update(missing_verify_ids)
        for pack_id in debt_ids:
            pack = packs_by_id.get(pack_id)
            if pack is None:
                continue
            reason = (
                f"Reviewer {reviewer.reviewer_id} has an active failed verification run."
                if pack_id in failed_verify_ids
                else (
                    f"Reviewer {reviewer.reviewer_id} has unresolved verification subjects."
                    if pack_id in missing_verify_ids
                    else f"Reviewer {reviewer.reviewer_id} did not complete its selected review."
                )
            )
            todos.append(
                LLMCoverageTodo(
                    context_pack_id=pack.id,
                    file=pack.file,
                    reviewer_id=reviewer.reviewer_id,
                    file_kind=pack.file_kind,
                    priority=pack_residual_priority(pack),
                    slice=_pack_review_slice(pack),
                    reason=reason,
                    suggested_command=continue_command_for_pack(
                        pack.id,
                        reviewer_id=reviewer.reviewer_id,
                    ),
                    estimated_chars=pack.stats.estimated_chars,
                    changed_lines=pack.changed_lines,
                    changed_symbols=_pack_symbol_names([pack]),
                )
            )
    return sorted(
        todos,
        key=lambda todo: (
            priority_rank.get(todo.priority, 9),
            todo.reviewer_id or "",
            todo.file,
            todo.context_pack_id,
        ),
    )


def continue_command_for_pack(
    pack_id: str,
    report_path: str = "<report.json>",
    reviewer_id: str | None = None,
    *,
    json_output_path: str | None = None,
) -> str:
    command = f"apex-ray review --continue-from {quote(report_path)} --only-pack {quote(pack_id)} --llm"
    if reviewer_id is not None:
        command += f" --reviewer {quote(reviewer_id)}"
    if json_output_path is not None:
        command += f" --json {quote(json_output_path)}"
    return command
