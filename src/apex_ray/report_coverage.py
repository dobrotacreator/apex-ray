from collections import Counter
from collections.abc import Iterable
from typing import Literal

from apex_ray.models import (
    ContextPack,
    FileKind,
    LLMContextSelection,
    LLMCoverageSummary,
    LLMCoverageTodo,
    LLMFileCoverageSummary,
    LLMPackReviewStatus,
    LLMResidualRiskSummary,
    LLMRouteSummary,
    LLMRun,
    LLMSliceCoverageSummary,
    ReviewConfig,
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
