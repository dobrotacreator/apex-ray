from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from apex_ray.llm import LLMProvider
from apex_ray.models import CoverageStopReason, ReviewConfig, ReviewReport
from apex_ray.pipeline.runner import continue_review_from_report
from apex_ray.pipeline.snapshot import ReviewInputSnapshotError, validate_review_input_snapshot
from apex_ray.progress import ProgressSink
from apex_ray.reviewers import ReviewerConfigError, effective_reviewers


@dataclass(frozen=True, slots=True)
class CoverageDrainResult:
    report: ReviewReport
    complete: bool
    batches: int
    stop_reason: CoverageStopReason


class CoverageScopeError(ValueError):
    """Raised when a bounded completion reviewer scope is invalid or ambiguous."""


def resolve_completion_reviewer_scope(
    config: ReviewConfig,
    requested_reviewer_ids: list[str] | None,
) -> list[str] | None:
    """Resolve the one bounded completion scope shared by CLI and CI enforcement."""

    try:
        if requested_reviewer_ids:
            return [reviewer.id for reviewer in effective_reviewers(config.reviewers, requested_reviewer_ids)]
        reviewers = effective_reviewers(config.reviewers)
    except ReviewerConfigError as exc:
        raise CoverageScopeError(str(exc)) from exc
    if not config.reviewers:
        return None
    required = [reviewer.id for reviewer in reviewers if reviewer.required]
    if len(required) == 1:
        return required
    if len(reviewers) == 1:
        return [reviewers[0].id]
    raise CoverageScopeError(
        "Coverage completion with multiple configured reviewers requires --reviewer, "
        "unless exactly one reviewer is marked required."
    )


def continue_review_until_complete(
    report: ReviewReport,
    *,
    repo_root: Path,
    config: ReviewConfig,
    reviewer_ids: list[str] | None,
    batch_size: int,
    max_batches: int,
    provider: LLMProvider | None = None,
    progress: ProgressSink | None = None,
    on_batch: Callable[[ReviewReport, int], None] | None = None,
) -> CoverageDrainResult:
    """Drain one explicit reviewer scope in bounded, progress-checked batches."""

    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    if max_batches <= 0:
        raise ValueError("max_batches must be greater than zero")
    current = report.model_copy(deep=True)
    current.coverage_completion = None
    _validate_current_snapshot(current, repo_root)
    if _completion_scope_needs_rebase(current, config=config, reviewer_ids=reviewer_ids):
        current, _selected_packs = continue_review_from_report(
            current,
            repo_root=repo_root,
            config=config,
            pack_ids=set(),
            only_unreviewed=True,
            reviewer_ids=reviewer_ids,
            provider=provider,
            progress=progress,
        )
        _validate_current_snapshot(current, repo_root)
    if coverage_is_complete(current, reviewer_ids=reviewer_ids):
        return CoverageDrainResult(current, True, 0, "complete")

    for batch in range(1, max_batches + 1):
        _validate_current_snapshot(current, repo_root)
        pending_ids = _pending_context_pack_ids(current, reviewer_ids=reviewer_ids)
        if not pending_ids:
            return CoverageDrainResult(current, False, batch - 1, "no_eligible_work")
        before = _coverage_progress_key(current, reviewer_ids=reviewer_ids)
        force_review_pack_ids = set(pending_ids).intersection(
            current.llm_coverage.shallow_only_high_risk_context_pack_ids
        )
        selected_pack_ids = set(pending_ids)
        batch_reviewer_ids = reviewer_ids
        depth_upgrade = _single_reviewer_depth_upgrade(
            current,
            pending_ids=pending_ids,
            force_review_pack_ids=force_review_pack_ids,
            reviewer_ids=reviewer_ids,
            batch_size=batch_size,
        )
        if depth_upgrade is not None:
            selected_pack_ids, batch_reviewer_ids = depth_upgrade
            force_review_pack_ids.intersection_update(selected_pack_ids)
        current, _selected_packs = continue_review_from_report(
            current,
            repo_root=repo_root,
            config=config,
            pack_ids=selected_pack_ids,
            only_unreviewed=True,
            force_review_pack_ids=force_review_pack_ids,
            max_pack_reviews=batch_size,
            review_depth="deep",
            reviewer_ids=batch_reviewer_ids,
            provider=provider,
            progress=progress,
        )
        _validate_current_snapshot(current, repo_root)
        if on_batch is not None:
            on_batch(current, batch)
        if coverage_is_complete(current, reviewer_ids=reviewer_ids):
            return CoverageDrainResult(current, True, batch, "complete")
        after = _coverage_progress_key(current, reviewer_ids=reviewer_ids)
        if after <= before:
            return CoverageDrainResult(current, False, batch, "no_progress")
    return CoverageDrainResult(current, False, max_batches, "max_batches")


def coverage_is_complete(
    report: ReviewReport,
    *,
    reviewer_ids: list[str] | None,
) -> bool:
    coverage = report.llm_coverage
    if reviewer_ids is None:
        return coverage.completion_status == "complete"
    if not coverage.enabled:
        return False
    summaries = {summary.reviewer_id: summary for summary in coverage.reviewers}
    matching_ids: set[str] = set()
    for reviewer_id in reviewer_ids:
        summary = summaries.get(reviewer_id)
        if summary is None:
            return False
        matching_ids.update(summary.matching_context_pack_ids)
        if summary.reviewed_context_packs != summary.matching_context_packs:
            return False
        if summary.status not in {"pass", "not_applicable"}:
            return False
    if matching_ids.intersection(coverage.shallow_only_high_risk_context_pack_ids):
        return False
    return True


def _pending_context_pack_ids(
    report: ReviewReport,
    *,
    reviewer_ids: list[str] | None,
) -> list[str]:
    coverage = report.llm_coverage
    target_reviewers = set(reviewer_ids or [])
    summaries = [
        summary for summary in coverage.reviewers if not target_reviewers or summary.reviewer_id in target_reviewers
    ]
    matching_scope_ids = {pack_id for summary in summaries for pack_id in summary.matching_context_pack_ids}
    if target_reviewers:
        pending = set(coverage.shallow_only_high_risk_context_pack_ids).intersection(matching_scope_ids)
    else:
        pending = {
            *coverage.unreviewed_context_pack_ids,
            *coverage.shallow_only_high_risk_context_pack_ids,
        }
    for summary in summaries:
        pending.update(set(summary.matching_context_pack_ids).difference(summary.reviewed_context_pack_ids))
    reviewed_unique = set(coverage.reviewed_context_pack_ids)
    for todo in coverage.coverage_todos:
        if target_reviewers and todo.reviewer_id not in {None, *target_reviewers}:
            continue
        if target_reviewers and todo.context_pack_id not in matching_scope_ids:
            continue
        verification_debt = "verif" in todo.reason.casefold()
        if todo.context_pack_id not in reviewed_unique or todo.reviewer_id is not None or verification_debt:
            pending.add(todo.context_pack_id)

    priority_by_pack_id = {
        residual.context_pack_id: residual.priority for residual in coverage.residual_risk_context_packs
    }
    for todo in coverage.coverage_todos:
        current = priority_by_pack_id.get(todo.context_pack_id)
        if current is None or _priority_rank(todo.priority) < _priority_rank(current):
            priority_by_pack_id[todo.context_pack_id] = todo.priority
    order_by_pack_id = {pack.id: index for index, pack in enumerate(report.context_packs)}
    return sorted(
        pending,
        key=lambda pack_id: (
            _priority_rank(priority_by_pack_id.get(pack_id, "p2")),
            order_by_pack_id.get(pack_id, len(order_by_pack_id)),
            pack_id,
        ),
    )


def _coverage_progress_key(
    report: ReviewReport,
    *,
    reviewer_ids: list[str] | None,
) -> tuple[int, int, int, int]:
    coverage = report.llm_coverage
    target_reviewers = set(reviewer_ids or [])
    reviewer_assignments = sum(
        summary.reviewed_context_packs
        for summary in coverage.reviewers
        if not target_reviewers or summary.reviewer_id in target_reviewers
    )
    deep_high_risk = coverage.reviewed_high_risk_context_packs - len(coverage.shallow_only_high_risk_context_pack_ids)
    resolved_todos = -len(_pending_context_pack_ids(report, reviewer_ids=reviewer_ids))
    return (
        coverage.reviewed_context_packs,
        reviewer_assignments,
        deep_high_risk,
        resolved_todos,
    )


def _priority_rank(priority: str) -> int:
    return {"p0": 0, "p1": 1, "p2": 2}.get(priority, 9)


def _validate_current_snapshot(report: ReviewReport, repo_root: Path) -> None:
    if report.input_snapshot is None:
        raise ReviewInputSnapshotError(
            "coverage completion requires a report with a review-input snapshot; run a fresh review"
        )
    validate_review_input_snapshot(
        report.input_snapshot,
        repo_root,
        expected_target_mode=report.diff.target_mode,
        expected_base_ref=report.diff.base,
    )


def _completion_scope_needs_rebase(
    report: ReviewReport,
    *,
    config: ReviewConfig,
    reviewer_ids: list[str] | None,
) -> bool:
    if report.config != config:
        return True
    if reviewer_ids is None:
        return False
    requested = set(reviewer_ids)
    summary_ids = {summary.reviewer_id for summary in report.llm_coverage.reviewers}
    if not requested.issubset(summary_ids):
        return True
    return report.reviewer_scope_ids is not None and not requested.issubset(report.reviewer_scope_ids)


def _single_reviewer_depth_upgrade(
    report: ReviewReport,
    *,
    pending_ids: list[str],
    force_review_pack_ids: set[str],
    reviewer_ids: list[str] | None,
    batch_size: int,
) -> tuple[set[str], list[str]] | None:
    """Assign global depth-only debt once when reviewer scopes overlap."""

    if not force_review_pack_ids or reviewer_ids is None or len(reviewer_ids) < 2:
        return None
    summaries = {summary.reviewer_id: summary for summary in report.llm_coverage.reviewers}
    for pack_id in pending_ids:
        if pack_id not in force_review_pack_ids:
            continue
        matching = [
            summaries[reviewer_id]
            for reviewer_id in reviewer_ids
            if reviewer_id in summaries and pack_id in summaries[reviewer_id].matching_context_pack_ids
        ]
        if not matching or any(pack_id not in summary.reviewed_context_pack_ids for summary in matching):
            continue
        selected_reviewer_id = matching[0].reviewer_id
        selected_pack_ids = [
            candidate_id
            for candidate_id in pending_ids
            if candidate_id in force_review_pack_ids
            and candidate_id in summaries[selected_reviewer_id].matching_context_pack_ids
        ][:batch_size]
        return set(selected_pack_ids), [selected_reviewer_id]
    return None
