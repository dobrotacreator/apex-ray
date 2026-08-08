import time
from pathlib import Path
from typing import Annotated

import typer

from apex_ray import __version__, git
from apex_ray.cli.common import (
    atomic_write_text,
    ensure_apex_ignore_for_outputs,
    ensure_distinct_outputs,
    resolve_output_path,
    warn_outdated_agent_artifacts,
)
from apex_ray.config import ConfigError, load_config
from apex_ray.discovery import DiscoveryError, discover_repo_root
from apex_ray.findings import finding_fingerprint
from apex_ray.gate_retry import (
    CarriedFinding,
    CoverageDebt,
    PrePushGateState,
    any_relevant_path_changed,
    build_pre_push_state,
    changed_paths,
    check_incremental_eligibility,
    config_fingerprint,
    context_pack_fingerprint,
    coverage_debt_from_decision,
    current_blocking_findings,
    dedupe_carried_findings,
    load_pre_push_state,
    resolve_state_path,
    review_report_fingerprint,
    stale_carried_finding_reason,
    write_pre_push_state,
)
from apex_ray.gates import PrePushGateDecision, PrePushRetrySummary, evaluate_pre_push_gate, render_pre_push_gate_stdout
from apex_ray.llm import LLMProviderError
from apex_ray.llm.prompts import resolution_diff_warnings_incomplete
from apex_ray.llm.providers import provider_from_config
from apex_ray.local_data import LocalDataPathError, resolve_config_path, resolve_runtime_config_paths
from apex_ray.models import Finding, PrePushGateConfig, ReviewReport, TargetMode
from apex_ray.pipeline import continue_review_from_report, run_review_pipeline
from apex_ray.pipeline.snapshot import ReviewInputSnapshotError, validate_review_input_snapshot
from apex_ray.progress import NoopProgress, ProgressSink, StreamProgress, progress_enabled
from apex_ray.report import (
    ReportArtifact,
    ReviewReportLoadError,
    archive_report_artifacts,
    load_review_report,
    render_html,
    render_markdown,
)
from apex_ray.report.coverage import set_continue_commands
from apex_ray.report.coverage_breakdown import pack_residual_priority
from apex_ray.resolution import (
    context_pack_resolution_paths,
    novel_resolution_file_is_reviewable,
)
from apex_ray.reviewers import ReviewerConfigError, effective_reviewers
from apex_ray.telemetry import TelemetryError, append_review_telemetry
from apex_ray.triage import (
    StaleSuppression,
    SuppressedFinding,
    append_triage_events,
    apply_suppressions,
    finding_candidate,
    finding_candidates_for_report,
    load_triage_state,
    prune_triage_state,
    render_triage_snapshot,
    write_triage_state,
)
from apex_ray.version_lock import VersionLockError, assert_version_lock

gate_app = typer.Typer(help="Run configured Apex Ray quality gates.")
_RESOLUTION_CALL_BUDGET_REASON = "Resolution call budget exhausted for this retry; the finding remains blocking."


def _coverage_followup_policy(
    config: PrePushGateConfig,
    report: ReviewReport | None,
) -> tuple[bool, set[str], int]:
    if config.auto_followup is None:
        return (
            config.auto_followup_p0,
            {"p0"} if config.auto_followup_p0 else set(),
            config.auto_followup_p0_max_pack_reviews,
        )
    threshold = config.fail_on_partial_severity
    priorities = (
        {
            "critical": {"p0"},
            "major": {"p0", "p1"},
            "minor": {"p0", "p1", "p2"},
            "none": set(),
        }[threshold]
        if threshold is not None
        else set()
    )
    if report is not None:
        blocking_pack_ids = _coverage_followup_blocking_pack_ids(config, report)
        priorities.update(
            todo.priority for todo in report.llm_coverage.coverage_todos if todo.context_pack_id in blocking_pack_ids
        )
        priorities.update(
            status.priority
            for status in report.llm_coverage.pack_statuses
            if status.context_pack_id in blocking_pack_ids and status.priority is not None
        )
        archived_priorities = {
            risk.context_pack_id: risk.priority for risk in report.llm_coverage.residual_risk_context_packs
        }
        priorities.update(
            pack_residual_priority(pack, archived_priorities.get(pack.id))
            for pack in report.context_packs
            if pack.id in blocking_pack_ids
        )
    enabled = config.auto_followup and bool(priorities)
    return (
        enabled,
        priorities if enabled else set(),
        config.auto_followup_max_pack_reviews or config.auto_followup_p0_max_pack_reviews,
    )


def _coverage_followup_force_pack_ids(
    config: PrePushGateConfig,
    report: ReviewReport,
) -> set[str]:
    if config.auto_followup is None:
        return set()
    blocking_pack_ids = _coverage_followup_blocking_pack_ids(config, report)
    return blocking_pack_ids.intersection(report.llm_coverage.shallow_only_high_risk_context_pack_ids)


def _coverage_followup_reviewer_ids(
    report: ReviewReport,
    blocking_pack_ids: set[str],
    *,
    force_review_pack_ids: set[str] | None = None,
    requested_reviewer_ids: list[str] | None = None,
) -> list[str] | None:
    forced_ids = set(force_review_pack_ids or ())
    failed_required_ids = {
        reviewer.reviewer_id
        for reviewer in report.llm_coverage.reviewers
        if reviewer.required and reviewer.status == "fail"
    }
    blocking_assignment_todos = [
        todo
        for todo in report.llm_coverage.coverage_todos
        if todo.context_pack_id in blocking_pack_ids and todo.reviewer_id in failed_required_ids
    ]
    if (
        blocking_pack_ids
        and blocking_pack_ids == forced_ids
        and (requested_reviewer_ids is not None or not blocking_assignment_todos)
    ):
        depth_requested_reviewer_ids = requested_reviewer_ids
        if requested_reviewer_ids is not None and blocking_assignment_todos:
            blocking_assignment_reviewer_ids = {
                todo.reviewer_id for todo in blocking_assignment_todos if todo.reviewer_id is not None
            }
            requested_blocking_reviewer_ids = [
                reviewer_id
                for reviewer_id in dict.fromkeys(requested_reviewer_ids)
                if reviewer_id in blocking_assignment_reviewer_ids
            ]
            if requested_blocking_reviewer_ids:
                depth_requested_reviewer_ids = requested_blocking_reviewer_ids
        depth_reviewer_id = _coverage_followup_depth_reviewer_id(
            report,
            forced_ids,
            requested_reviewer_ids=depth_requested_reviewer_ids,
        )
        if depth_reviewer_id is not None:
            return [depth_reviewer_id]
    if requested_reviewer_ids is not None:
        return list(dict.fromkeys(requested_reviewer_ids))

    if not failed_required_ids:
        return None
    if not blocking_assignment_todos or blocking_pack_ids.difference(
        todo.context_pack_id for todo in blocking_assignment_todos
    ):
        return None
    configured_ids = {reviewer.id for reviewer in report.config.reviewers}
    reviewer_ids = {
        todo.reviewer_id
        for todo in blocking_assignment_todos
        if todo.reviewer_id is not None and todo.reviewer_id in configured_ids
    }
    if not reviewer_ids:
        return None
    ordered_ids = [
        reviewer.reviewer_id for reviewer in report.llm_coverage.reviewers if reviewer.reviewer_id in reviewer_ids
    ]
    return ordered_ids or sorted(reviewer_ids)


def _coverage_followup_depth_reviewer_id(
    report: ReviewReport,
    force_review_pack_ids: set[str],
    *,
    requested_reviewer_ids: list[str] | None,
) -> str | None:
    if not force_review_pack_ids:
        return None
    configured_order = [reviewer.id for reviewer in report.config.reviewers if reviewer.enabled]
    candidate_order = (
        list(dict.fromkeys(requested_reviewer_ids)) if requested_reviewer_ids is not None else configured_order
    )
    if not candidate_order:
        return None
    order_by_id = {reviewer_id: index for index, reviewer_id in enumerate(candidate_order)}
    summaries = {
        reviewer.reviewer_id: reviewer
        for reviewer in report.llm_coverage.reviewers
        if reviewer.reviewer_id in order_by_id
        and force_review_pack_ids.intersection(reviewer.matching_context_pack_ids)
    }
    if not summaries:
        return None
    return min(
        summaries,
        key=lambda reviewer_id: (
            -len(force_review_pack_ids.intersection(summaries[reviewer_id].matching_context_pack_ids)),
            not summaries[reviewer_id].required,
            order_by_id[reviewer_id],
        ),
    )


def _coverage_followup_blocking_pack_ids(
    config: PrePushGateConfig,
    report: ReviewReport,
) -> set[str]:
    if config.auto_followup is None or not config.auto_followup:
        return set()
    decision = evaluate_pre_push_gate(report, config)
    if not decision.quality_gate_failed and not decision.partial_blocked:
        return set()

    coverage = report.llm_coverage
    priorities_for_partial_threshold = {
        "critical": {"p0"},
        "major": {"p0", "p1"},
        "minor": {"p0", "p1", "p2"},
        "none": set(),
        None: set(),
    }[config.fail_on_partial_severity]
    blocking_ids: set[str] = set()
    if decision.partial_blocked:
        blocking_ids.update(
            residual.context_pack_id
            for residual in coverage.residual_risk_context_packs
            if residual.priority in priorities_for_partial_threshold
        )
        blocking_ids.update(
            status.context_pack_id for status in coverage.pack_statuses if status.status.startswith("failed_")
        )
        failed_reviewers = {reviewer.reviewer_id for reviewer in coverage.reviewers if reviewer.status == "fail"}
        failed_reviewers.update(
            reviewer.reviewer_id
            for reviewer in coverage.reviewers
            if reviewer.failed_review_runs or reviewer.failed_verify_runs
        )
        blocking_ids.update(
            todo.context_pack_id for todo in coverage.coverage_todos if todo.reviewer_id in failed_reviewers
        )
        blocking_ids.update(coverage.shallow_only_high_risk_context_pack_ids)

    if decision.quality_gate_failed:
        blocking_ids.update(coverage.residual_risk_p0_context_pack_ids)
        if (
            report.config.llm.min_source_line_coverage
            and coverage.source_changed_line_coverage_ratio < report.config.llm.min_source_line_coverage
        ):
            blocking_ids.update(
                residual.context_pack_id
                for residual in coverage.residual_risk_context_packs
                if str(residual.file_kind) == "source"
            )
        if (
            report.config.llm.min_high_risk_coverage
            and coverage.high_risk_coverage_ratio < report.config.llm.min_high_risk_coverage
        ):
            unreviewed_ids = set(coverage.unreviewed_context_pack_ids)
            blocking_ids.update(
                todo.context_pack_id
                for todo in coverage.coverage_todos
                if todo.context_pack_id in unreviewed_ids and todo.slice == "high_risk"
            )
        blocking_ids.update(coverage.shallow_only_high_risk_context_pack_ids)
        failed_required_reviewers = {
            reviewer.reviewer_id for reviewer in coverage.reviewers if reviewer.required and reviewer.status == "fail"
        }
        blocking_ids.update(
            todo.context_pack_id for todo in coverage.coverage_todos if todo.reviewer_id in failed_required_reviewers
        )
        if not report.config.reviewers:
            general_has_unresolved_verification = any(
                reviewer.reviewer_id == "general"
                and any("verification" in reason.casefold() for reason in reviewer.reasons)
                for reviewer in coverage.reviewers
            )
            if general_has_unresolved_verification:
                blocking_ids.update(
                    todo.context_pack_id
                    for todo in coverage.coverage_todos
                    if todo.reviewer_id == "general" and "verification" in todo.reason.casefold()
                )
    return blocking_ids


@gate_app.command("pre-push")
def pre_push(
    base: Annotated[
        str | None,
        typer.Option(
            "--base",
            envvar=["APEX_RAY_BASE", "TURBO_SCM_BASE"],
            help="Base ref for git diff <base>...HEAD; CLI, hook environment, then review.base.",
        ),
    ] = None,
    output: Annotated[Path, typer.Option("--output", help="Markdown report path.")] = Path(
        ".apex-ray/reports/pre-push.md"
    ),
    json_output: Annotated[Path, typer.Option("--json", help="JSON report path.")] = Path(
        ".apex-ray/reports/pre-push.json"
    ),
    html_output: Annotated[Path | None, typer.Option("--html", help="Optional HTML report path.")] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Path to config file.")] = None,
    reviewer: Annotated[
        list[str] | None,
        typer.Option("--reviewer", help="Run only this configured reviewer. May be repeated."),
    ] = None,
    telemetry: Annotated[
        bool,
        typer.Option("--telemetry", help="Append this gate run to review telemetry JSONL."),
    ] = False,
    no_telemetry: Annotated[
        bool,
        typer.Option("--no-telemetry", help="Disable configured local review telemetry for this gate run."),
    ] = False,
    telemetry_path: Annotated[
        Path | None,
        typer.Option("--telemetry-path", help="Review telemetry JSONL path."),
    ] = None,
) -> None:
    """Run the configured pre-push review gate and block on policy failures."""
    try:
        root = discover_repo_root(Path.cwd())
        lock_status = assert_version_lock(root, runtime_version=__version__)
        review_config, config_path = load_config(root, config)
    except (ConfigError, DiscoveryError, VersionLockError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    report_config = review_config.model_copy(deep=True)
    try:
        review_config = resolve_runtime_config_paths(root, review_config)
    except LocalDataPathError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        if reviewer:
            effective_reviewers(review_config.reviewers, reviewer)
    except ReviewerConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc

    gate_config = review_config.gates.pre_push
    if not gate_config.enabled:
        typer.echo("APEX RAY GATE: DISABLED")
        raise typer.Exit()
    warn_outdated_agent_artifacts(root)
    progress = _progress_for_gate(gate_config)

    output = resolve_output_path(root, output)
    json_output = resolve_output_path(root, json_output)
    html_output = resolve_output_path(root, html_output) if html_output is not None else None
    ensure_distinct_outputs(output, json_output, html_output)
    if telemetry and no_telemetry:
        raise typer.BadParameter("Use only one of --telemetry or --no-telemetry.")
    if telemetry_path is not None and no_telemetry:
        raise typer.BadParameter("Use --telemetry-path only when telemetry is enabled.")

    previous_report = _load_previous_report(json_output)
    target_base = base or review_config.base
    if gate_config.fetch_base:
        progress.event(f"preparing review base {target_base}", force=True)
        try:
            target_base = git.resolve_pre_push_base(root, target_base)
        except (git.GitError, git.GitRemoteRefError) as exc:
            raise typer.BadParameter(str(exc)) from exc
    state_path = resolve_state_path(root, gate_config)
    retry_state = None
    retry_summary: PrePushRetrySummary | None = None
    current_head = ""
    merge_base_sha = ""
    config_hash = ""
    incremental_mode = False
    incremental_fallback_reason = ""
    resumed_coverage_retry = False
    resumed_resolution_retry = False
    coverage_retry_has_pending_delta = False
    coverage_report_fallback = False
    followup_no_eligible_reason: str | None = None
    if gate_config.incremental_retry.enabled:
        try:
            current_head = git.rev_parse(root, "HEAD")
            merge_base_sha = git.merge_base(root, target_base, "HEAD")
        except git.GitError as exc:
            raise typer.BadParameter(str(exc)) from exc
        config_hash = config_fingerprint(report_config, gate_config, reviewer_ids=reviewer)
        retry_state = load_pre_push_state(state_path)
        previous_head_exists = bool(retry_state and git.object_exists(root, retry_state.head_sha))
        try:
            previous_head_is_ancestor = bool(
                retry_state and previous_head_exists and git.is_ancestor(root, retry_state.head_sha, current_head)
            )
        except git.GitError as exc:
            raise typer.BadParameter(str(exc)) from exc
        eligibility = check_incremental_eligibility(
            retry_state,
            repo_root=root,
            base_ref=target_base,
            merge_base_sha=merge_base_sha,
            config_hash=config_hash,
            previous_head_exists=previous_head_exists,
            previous_head_is_ancestor=previous_head_is_ancestor,
        )
        incremental_mode = eligibility.eligible
        incremental_fallback_reason = eligibility.reason

    started_monotonic = time.monotonic()
    try:
        retry_coverage_report = _retry_coverage_report(
            retry_state,
            previous_report,
            json_output=json_output,
            config_hash=config_hash,
            gate_config=gate_config,
            reviewer_ids=reviewer,
        )
        retry_resolution_report = _retry_resolution_report(
            retry_state,
            previous_report,
            current_head=current_head,
            json_output=json_output,
            config_hash=config_hash,
            gate_config=gate_config,
            reviewer_ids=reviewer,
        )
        if (
            incremental_mode
            and retry_state is not None
            and (retry_state.coverage_debt.quality_gate_failed or retry_state.coverage_debt.partial_blocked)
            and retry_coverage_report is None
        ):
            incremental_mode = False
            coverage_report_fallback = True
            incremental_fallback_reason = "previous coverage-debt report is unavailable or no longer matches state"
        if incremental_mode and retry_state is not None and retry_coverage_report is not None:
            report = retry_coverage_report
            resumed_coverage_retry = True
            coverage_retry_has_pending_delta = current_head != retry_state.head_sha
            progress.event("resuming bounded coverage from the previous pre-push report", force=True)
        elif incremental_mode and retry_state is not None and retry_resolution_report is not None:
            report = retry_resolution_report
            resumed_resolution_retry = True
            progress.event(
                "resuming bounded carried-finding resolution from the previous pre-push report",
                force=True,
            )
        else:
            if incremental_mode and retry_state is not None:
                progress.event(f"reading diff {retry_state.head_sha}..HEAD", force=True)
                diff_text = _load_range_diff(root, retry_state.head_sha, "HEAD")
                target_mode = TargetMode.PATCH
                report_base = f"{retry_state.head_sha}..HEAD"
            else:
                progress.event(f"reading diff {target_base}...HEAD", force=True)
                diff_text = _load_base_diff(root, target_base)
                target_mode = TargetMode.BASE
                report_base = target_base
            report = run_review_pipeline(
                root,
                diff_text,
                target_mode,
                review_config,
                base=report_base,
                config_path=config_path,
                progress=progress,
                reviewer_ids=reviewer,
                snapshot_range_start_ref=(
                    retry_state.head_sha if incremental_mode and retry_state is not None else None
                ),
            )
        followup_enabled, followup_priorities, followup_max_pack_reviews = _coverage_followup_policy(
            gate_config,
            report,
        )
        followup_decision = evaluate_pre_push_gate(report, gate_config)
        generalized_coverage_blocked = gate_config.auto_followup is not None and (
            followup_decision.quality_gate_failed or followup_decision.partial_blocked
        )
        should_attempt_followup = followup_enabled and (
            generalized_coverage_blocked
            if gate_config.auto_followup is not None
            else report.llm_coverage.partial_severity == "critical"
        )
        followup_selected_packs = []
        followup_runs = []
        if should_attempt_followup:
            blocking_pack_ids = _coverage_followup_blocking_pack_ids(gate_config, report)
            force_review_pack_ids = _coverage_followup_force_pack_ids(gate_config, report)
            followup_reviewer_ids = _coverage_followup_reviewer_ids(
                report,
                blocking_pack_ids,
                force_review_pack_ids=force_review_pack_ids,
                requested_reviewer_ids=reviewer,
            )
            llm_runs_before = len(report.llm_runs)
            report, followup_selected_packs = continue_review_from_report(
                report,
                repo_root=root,
                config=review_config,
                residual_priorities=followup_priorities,
                pack_ids=blocking_pack_ids or None,
                only_unreviewed=True,
                force_review_pack_ids=force_review_pack_ids,
                max_pack_reviews=followup_max_pack_reviews,
                review_depth="deep",
                progress=progress,
                reviewer_ids=followup_reviewer_ids,
            )
            followup_runs = report.llm_runs[llm_runs_before:]
            followup_calls = len(followup_runs)
            if followup_calls:
                priority_label = "/".join(sorted(followup_priorities))
                followed_pack_count = len({run.context_pack_id for run in followup_runs})
                progress.event(
                    f"auto-followup executed {followup_calls} residual {priority_label} review/verification call(s) "
                    f"across {followed_pack_count} context pack(s)",
                    force=True,
                )
            elif gate_config.auto_followup is not None:
                followup_no_eligible_reason = (
                    "Auto-followup made no progress: no review or verification call was executed for the "
                    "blocking coverage debt. Inspect coverage_todos in the JSON report and run one of its "
                    "suggested_command values; if no todo is eligible, fix the provider or reviewer "
                    "configuration and run git push again."
                )
        elif gate_config.auto_followup and generalized_coverage_blocked:
            followup_no_eligible_reason = (
                "Auto-followup could not start: the blocking coverage debt has no eligible context-pack "
                "priority in the report. Inspect coverage_todos in the JSON report and run one of its "
                "suggested_command values; if no todo is present, fix the provider or reviewer configuration "
                "and run git push again."
            )
        if (
            resumed_coverage_retry
            and coverage_retry_has_pending_delta
            and not followup_selected_packs
            and not followup_runs
        ):
            incremental_fallback_reason = "coverage retry made no progress while newer commits were pending"
            progress.event(
                f"{incremental_fallback_reason}; refreshing the full current review",
                force=True,
            )
            # The saved coverage debt belongs to the prior full range. A
            # delta-only report cannot authoritatively replace that scope.
            diff_text = _load_base_diff(root, target_base)
            report = run_review_pipeline(
                root,
                diff_text,
                TargetMode.BASE,
                review_config,
                base=target_base,
                config_path=config_path,
                progress=progress,
                reviewer_ids=reviewer,
            )
            followup_no_eligible_reason = None
            resumed_coverage_retry = False
            coverage_retry_has_pending_delta = False
            incremental_mode = False
            coverage_report_fallback = True
        if report.input_snapshot is not None and not resumed_coverage_retry and not resumed_resolution_retry:
            validate_review_input_snapshot(
                report.input_snapshot,
                root,
                expected_target_mode=report.diff.target_mode,
                expected_base_ref=report.diff.base,
            )
    except (DiscoveryError, LLMProviderError, ReviewInputSnapshotError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    duration_ms = round((time.monotonic() - started_monotonic) * 1000)
    report.config = report_config

    progress.event("writing reports", force=True)
    set_continue_commands(report, str(json_output), launcher_version=lock_status.locked_version)
    previous_decision = evaluate_pre_push_gate(previous_report, gate_config) if previous_report else None
    current_decision = evaluate_pre_push_gate(report, gate_config)
    suppressed_findings: list[SuppressedFinding] = []
    stale_suppression_details: list[StaleSuppression] = []
    stale_suppressions = 0
    expired_suppressions = 0
    pruned_suppressions = 0
    triage_state = None
    triage_events = []
    triage_state_path = Path(review_config.triage.state_path)
    triage_events_path = Path(review_config.triage.events_path)
    if review_config.triage.enabled:
        triage_state = load_triage_state(triage_state_path)
        pruned = prune_triage_state(triage_state, review_config.triage)
        triage_state = pruned.state
        triage_events.extend(pruned.events)
        expired_suppressions += pruned.expired_count
        pruned_suppressions += pruned.pruned_count
        current_triage = apply_suppressions(
            finding_candidates_for_report(report, current_decision.blocking_findings),
            triage_state,
            # Suppressions scope to the pre-push target base, not the incremental
            # retry diff range, so a user's explicit triage keeps applying across
            # repeated push attempts. The context-pack fingerprint still prevents
            # stale suppressions from hiding changed evidence.
            target_base_ref=target_base,
        )
        triage_state = current_triage.state
        triage_events.extend(current_triage.events)
        stale_suppressions += current_triage.stale_count
        suppressed_findings.extend(current_triage.suppressed_findings)
        stale_suppression_details.extend(current_triage.stale_suppressions)
        current_decision = _replace_blocking_findings(current_decision, current_triage.remaining_findings, gate_config)
    active_carried_findings: list[CarriedFinding] = []
    resolved_carried_count = 0
    carried_coverage_debt = CoverageDebt()
    if (incremental_mode or coverage_report_fallback) and retry_state is not None:
        active_carried_findings, resolved_carried_count = _resolve_incremental_carried_findings(
            retry_state.active_findings,
            report,
            repo_root=root,
            config=review_config,
            progress=progress,
            current_blocking_findings=current_decision.blocking_findings,
            only_budget_deferred=resumed_resolution_retry,
        )
        if (
            incremental_mode
            and not resumed_coverage_retry
            and (retry_state.coverage_debt.quality_gate_failed or retry_state.coverage_debt.partial_blocked)
        ):
            carried_coverage_debt = retry_state.coverage_debt
    if review_config.triage.enabled and active_carried_findings and triage_state is not None:
        carried_triage = apply_suppressions(
            [finding_candidate(carried.finding, carried.context_pack) for carried in active_carried_findings],
            triage_state,
            target_base_ref=target_base,
        )
        remaining_carried_ids = {id(finding) for finding in carried_triage.remaining_findings}
        active_carried_findings = [
            carried for carried in active_carried_findings if id(carried.finding) in remaining_carried_ids
        ]
        triage_state = carried_triage.state
        triage_events.extend(carried_triage.events)
        stale_suppressions += carried_triage.stale_count
        suppressed_findings.extend(carried_triage.suppressed_findings)
        stale_suppression_details.extend(carried_triage.stale_suppressions)
    current_coverage_debt = coverage_debt_from_decision(
        report,
        quality_gate_failed=current_decision.quality_gate_failed,
        partial_blocked=current_decision.partial_blocked,
        reasons=current_decision.reasons,
    )
    decision = _combine_incremental_decision(current_decision, active_carried_findings, carried_coverage_debt)
    # A productive retry may clear the saved coverage debt, but it must not
    # authorize commits newer than the report snapshot. Keep the old state HEAD
    # and fail closed here; the next attempt reviews old_head..HEAD.
    if coverage_retry_has_pending_delta:
        decision = _block_for_pending_incremental_delta(decision)
    if gate_config.incremental_retry.enabled:
        retry_summary = PrePushRetrySummary(
            mode=(
                "coverage-resume"
                if resumed_coverage_retry
                else "resolution-resume"
                if resumed_resolution_retry
                else "incremental"
                if incremental_mode
                else "full"
            ),
            fallback_reason="" if incremental_mode else incremental_fallback_reason,
            new_blocking_findings=len(current_decision.blocking_findings),
            still_blocking_carried_findings=sum(
                1 for carried in active_carried_findings if carried.status == "still_present"
            ),
            uncertain_carried_findings=sum(1 for carried in active_carried_findings if carried.status == "uncertain"),
            resolved_carried_findings=resolved_carried_count,
            carried_coverage_reasons=carried_coverage_debt.reasons,
        )

    markdown_text = render_markdown(report)
    if retry_summary is not None or stale_suppression_details:
        markdown_text = _prepend_gate_summary_markdown(
            markdown_text,
            decision,
            retry_summary=retry_summary,
            stale_suppressions=stale_suppression_details,
        )
    json_text = report.model_dump_json(indent=2)
    html_text = render_html(report) if html_output is not None else None
    ensure_apex_ignore_for_outputs(root, output, json_output, html_output)
    atomic_write_text(output, markdown_text)
    atomic_write_text(json_output, json_text)
    if html_output is not None:
        atomic_write_text(html_output, html_text or "")

    artifacts = [
        ReportArtifact(output, markdown_text),
        ReportArtifact(json_output, json_text),
    ]
    if html_output is not None and html_text is not None:
        artifacts.append(ReportArtifact(html_output, html_text))
    if review_config.triage.enabled:
        artifacts.append(
            ReportArtifact(
                Path("pre-push-triage.json"),
                render_triage_snapshot(
                    suppressed_findings=suppressed_findings,
                    stale_suppressions=stale_suppression_details,
                    active_suppressions=triage_state.suppressions if triage_state is not None else [],
                    stale_count=stale_suppressions,
                    expired_count=expired_suppressions,
                    pruned_count=pruned_suppressions,
                ),
            )
        )
    archive_path = archive_report_artifacts(
        root,
        review_config.reports,
        artifacts,
        created_at=report.generated_at,
    )

    if (
        review_config.triage.enabled
        and triage_state is not None
        and (triage_state_path.exists() or triage_state.suppressions or triage_events)
    ):
        ensure_apex_ignore_for_outputs(root, triage_state_path, triage_events_path)
        write_triage_state(triage_state_path, triage_state)
        append_triage_events(
            triage_events_path,
            triage_events,
            retention_days=review_config.triage.events_retention_days,
        )

    if gate_config.incremental_retry.enabled:
        state_coverage_debt = _merge_coverage_debt(carried_coverage_debt, current_coverage_debt)
        state_findings = dedupe_carried_findings(
            [
                *active_carried_findings,
                *current_blocking_findings(report, current_decision.blocking_findings, report_path=output),
            ]
        )
        write_pre_push_state(
            state_path,
            build_pre_push_state(
                repo_root=root,
                base_ref=target_base,
                merge_base_sha=merge_base_sha,
                head_sha=retry_state.head_sha if resumed_coverage_retry and retry_state is not None else current_head,
                config_hash=config_hash,
                report=report,
                report_path=output,
                json_path=json_output,
                active_findings=state_findings,
                coverage_debt=state_coverage_debt,
            ),
        )

    telemetry_enabled = (
        review_config.telemetry.enabled or telemetry or telemetry_path is not None
    ) and not no_telemetry
    try:
        effective_telemetry_path = resolve_config_path(
            root,
            review_config.local_data,
            telemetry_path or review_config.telemetry.path,
        )
    except LocalDataPathError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if telemetry_enabled:
        try:
            progress.event("appending telemetry", force=True)
            append_review_telemetry(
                report,
                effective_telemetry_path,
                source_repo=root,
                duration_ms=duration_ms,
                output_path=output,
                json_output_path=json_output,
                html_output_path=html_output,
                triage_counts={
                    "triage_suppressed_findings_count": len(suppressed_findings),
                    "triage_stale_suppressions_count": stale_suppressions,
                    "triage_expired_suppressions_count": expired_suppressions,
                    "triage_pruned_suppressions_count": pruned_suppressions,
                    "triage_active_suppressions_count": len(triage_state.suppressions)
                    if triage_state is not None
                    else 0,
                },
            )
        except TelemetryError as exc:
            raise typer.BadParameter(str(exc)) from exc

    progress.event("evaluating pre-push gate", force=True)
    typer.echo(
        render_pre_push_gate_stdout(
            report,
            decision,
            markdown_path=output,
            json_path=json_output,
            base=target_base,
            config=gate_config,
            previous_decision=previous_decision,
            retry_summary=retry_summary,
            suppressed_findings=suppressed_findings,
            stale_suppression_details=stale_suppression_details,
            stale_suppressions=stale_suppressions,
            expired_suppressions=expired_suppressions,
            pruned_suppressions=pruned_suppressions,
        ),
        nl=False,
    )
    if followup_no_eligible_reason is not None:
        typer.echo(followup_no_eligible_reason)
    if telemetry_enabled:
        typer.echo(f"Appended telemetry: {effective_telemetry_path}")
    if archive_path:
        typer.echo(f"Archived report: {archive_path}")
    if decision.blocked:
        raise typer.Exit(code=1)


def _load_base_diff(root: Path, base: str) -> str:
    if not git.is_git_repo(root):
        raise typer.BadParameter("Current directory is not a git repository.")
    try:
        return git.diff_base(root, base)
    except git.GitError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _load_range_diff(root: Path, old_ref: str, new_ref: str) -> str:
    if not git.is_git_repo(root):
        raise typer.BadParameter("Current directory is not a git repository.")
    try:
        return git.diff_range(root, old_ref, new_ref)
    except git.GitError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _load_previous_report(path: Path) -> ReviewReport | None:
    if not path.exists():
        return None
    try:
        return load_review_report(path)
    except OSError, ReviewReportLoadError:
        return None


def _retry_coverage_report(
    state: PrePushGateState | None,
    report: ReviewReport | None,
    *,
    json_output: Path,
    config_hash: str,
    gate_config: PrePushGateConfig,
    reviewer_ids: list[str] | None,
) -> ReviewReport | None:
    if state is None or report is None:
        return None
    debt = state.coverage_debt
    if not debt.quality_gate_failed and not debt.partial_blocked:
        return None
    if Path(report.project.root) != Path(state.repo_root):
        return None
    try:
        if Path(state.json_path).resolve() != json_output.resolve():
            return None
    except (OSError, RuntimeError):  # fmt: skip
        return None
    if not _coverage_retry_snapshot_matches_state(state, report):
        return None
    if report.generated_at < state.generated_at:
        return None
    try:
        report_config_hash = config_fingerprint(report.config, gate_config, reviewer_ids=reviewer_ids)
    except ReviewerConfigError:
        return None
    if report_config_hash != config_hash:
        return None
    expected_fingerprints = state.context_pack_fingerprints
    if not expected_fingerprints:
        return None
    actual_fingerprints = {
        pack.id: context_pack_fingerprint(pack.model_dump(mode="json")) for pack in report.context_packs
    }
    if actual_fingerprints != expected_fingerprints:
        return None
    previous_reviewed = set(state.reviewed_context_pack_ids)
    current_reviewed = set(report.llm_coverage.reviewed_context_pack_ids)
    if not previous_reviewed.issubset(current_reviewed):
        return None
    return report


def _coverage_retry_snapshot_matches_state(state: PrePushGateState, report: ReviewReport) -> bool:
    snapshot = report.input_snapshot
    if snapshot is None:
        return False
    if (
        not state.input_snapshot_fingerprint
        or context_pack_fingerprint(snapshot.model_dump(mode="json")) != state.input_snapshot_fingerprint
    ):
        return False
    target_mode = TargetMode(snapshot.target_mode)
    if target_mode != TargetMode(report.diff.target_mode):
        return False
    if snapshot.base_ref != report.diff.base or snapshot.head_sha != state.head_sha:
        return False
    if target_mode == TargetMode.BASE:
        return (
            snapshot.base_ref == state.base_ref
            and snapshot.merge_base_sha == state.merge_base_sha
            and snapshot.range_start_sha is None
        )
    if target_mode == TargetMode.PATCH and snapshot.range_start_sha is not None:
        return snapshot.merge_base_sha is None and report.diff.base == f"{snapshot.range_start_sha}..HEAD"
    return False


def _retry_resolution_report(
    state: PrePushGateState | None,
    report: ReviewReport | None,
    *,
    current_head: str,
    json_output: Path,
    config_hash: str,
    gate_config: PrePushGateConfig,
    reviewer_ids: list[str] | None,
) -> ReviewReport | None:
    if state is None or report is None or current_head != state.head_sha:
        return None
    deferred_findings = [
        carried for carried in state.active_findings if carried.resolution_reason == _RESOLUTION_CALL_BUDGET_REASON
    ]
    if not deferred_findings:
        return None
    if state.config_fingerprint != config_hash:
        return None
    try:
        state_json_path = Path(state.json_path).resolve()
        current_json_path = json_output.resolve()
    except (OSError, RuntimeError):  # fmt: skip
        return None
    if state_json_path != current_json_path:
        return None
    if Path(report.project.root) != Path(state.repo_root):
        return None
    # Resolution retries only reuse the exact report snapshot that produced the
    # current state. Unlike manual coverage continuation, a newer timestamp is
    # not a trustworthy match for this evidence-bearing delta.
    if report.generated_at != state.generated_at:
        return None
    try:
        report_config_hash = config_fingerprint(report.config, gate_config, reviewer_ids=reviewer_ids)
    except ReviewerConfigError:
        return None
    if report_config_hash != config_hash:
        return None
    if not state.report_fingerprint or review_report_fingerprint(report) != state.report_fingerprint:
        return None
    actual_fingerprints = {
        pack.id: context_pack_fingerprint(pack.model_dump(mode="json")) for pack in report.context_packs
    }
    if actual_fingerprints != state.context_pack_fingerprints:
        return None
    if set(report.llm_coverage.reviewed_context_pack_ids) != set(state.reviewed_context_pack_ids):
        return None

    changed = changed_paths(report)
    current_blocking_fingerprints = {
        finding_fingerprint(finding) for finding in evaluate_pre_push_gate(report, gate_config).blocking_findings
    }
    if not any(
        _reviewed_pack_omits_carried_finding(carried, report, current_blocking_fingerprints)
        or _delta_may_contain_resolution_evidence(carried, report, changed)
        for carried in deferred_findings
    ):
        return None
    return report


def _block_for_pending_incremental_delta(decision: PrePushGateDecision) -> PrePushGateDecision:
    return PrePushGateDecision(
        blocked=True,
        reasons=[
            *decision.reasons,
            "New commits are pending review after the carried coverage retry; run the gate again.",
        ],
        blocking_findings=decision.blocking_findings,
        quality_gate_failed=decision.quality_gate_failed,
        partial_blocked=decision.partial_blocked,
    )


def _replace_blocking_findings(
    decision: PrePushGateDecision,
    blocking_findings,
    gate_config,
) -> PrePushGateDecision:
    reasons = [reason for reason in decision.reasons if not reason.startswith("Blocking findings:")]
    if blocking_findings and gate_config.min_finding_severity is not None:
        reasons.insert(0, f"Blocking findings: {len(blocking_findings)} >= {gate_config.min_finding_severity!s}")
    return PrePushGateDecision(
        blocked=bool(reasons),
        reasons=reasons,
        blocking_findings=list(blocking_findings),
        quality_gate_failed=decision.quality_gate_failed,
        partial_blocked=decision.partial_blocked,
    )


def _progress_for_gate(config) -> ProgressSink:
    if not progress_enabled(config.progress):
        return NoopProgress()
    return StreamProgress(interval_seconds=config.progress_interval_seconds)


def _resolve_incremental_carried_findings(
    carried_findings: list[CarriedFinding],
    report: ReviewReport,
    *,
    repo_root: Path,
    config,
    progress: ProgressSink,
    current_blocking_findings: list[Finding] | None = None,
    only_budget_deferred: bool = False,
) -> tuple[list[CarriedFinding], int]:
    changed = changed_paths(report)
    fingerprint_source = current_blocking_findings if current_blocking_findings is not None else report.findings
    current_blocking_fingerprints = {finding_fingerprint(finding) for finding in fingerprint_source}
    unchanged_active: list[CarriedFinding] = []
    needs_resolution: list[CarriedFinding] = []
    stale_resolved_count = 0
    for carried in carried_findings:
        if finding_fingerprint(carried.finding) in current_blocking_fingerprints:
            unchanged_active.append(
                carried.model_copy(
                    update={
                        "status": "still_present",
                        "resolution_reason": "The finding remains in the current review report.",
                    }
                )
            )
            continue
        if only_budget_deferred and carried.resolution_reason != _RESOLUTION_CALL_BUDGET_REASON:
            # This is the exact same evidence snapshot that already produced a
            # resolver outcome for this finding. Preserve that outcome while
            # rotating only calls that were previously skipped at the budget.
            unchanged_active.append(carried)
            continue
        if _reviewed_pack_omits_carried_finding(carried, report, current_blocking_fingerprints):
            needs_resolution.append(carried)
            continue
        if _delta_may_contain_resolution_evidence(carried, report, changed):
            needs_resolution.append(carried)
        else:
            stale_reason = stale_carried_finding_reason(carried, repo_root)
            if stale_reason is not None:
                stale_resolved_count += 1
                progress.event(f"dropping stale carried finding: {stale_reason}", force=True)
                continue
            unchanged_active.append(
                carried.model_copy(
                    update={
                        "status": "still_present",
                        "resolution_reason": (
                            "No relevant resolution surface changed since the previous gate attempt."
                        ),
                    }
                )
            )
    unresolved = resolve_carried_findings(
        needs_resolution,
        report,
        repo_root=repo_root,
        config=config,
        progress=progress,
    )
    return (
        dedupe_carried_findings([*unchanged_active, *unresolved]),
        len(needs_resolution) - len(unresolved) + stale_resolved_count,
    )


def _delta_may_contain_resolution_evidence(
    carried: CarriedFinding,
    report: ReviewReport,
    changed: set[str],
) -> bool:
    if not changed:
        return False
    relevant = set(carried.relevant_files or [carried.finding.file])
    if any_relevant_path_changed(relevant, changed):
        return True

    # A fix can introduce a registration, migration, provider, or test file
    # that could not have appeared in the historical relevance set. Keep that
    # fallback bounded to novel reviewable files so an unrelated edit to an
    # existing file does not re-resolve every carried finding.
    if any(novel_resolution_file_is_reviewable(changed_file) for changed_file in report.diff.files):
        return True

    # Existing files can also become newly relevant when the current analyzer
    # graph links them back to a historical resolution surface.
    for pack in report.context_packs:
        if pack.file not in changed:
            continue
        linked_paths = context_pack_resolution_paths(pack)
        if any_relevant_path_changed(relevant, linked_paths):
            return True
    return False


def _reviewed_pack_omits_carried_finding(
    carried: CarriedFinding,
    report: ReviewReport,
    current_blocking_fingerprints: set[str],
) -> bool:
    pack_ids = {
        pack_id
        for pack_id in [
            carried.finding.context_pack_id,
            carried.context_pack.id if carried.context_pack else "",
        ]
        if pack_id
    }
    if not pack_ids:
        return False
    if not pack_ids.intersection(report.llm_coverage.reviewed_context_pack_ids):
        return False
    return finding_fingerprint(carried.finding) not in current_blocking_fingerprints


def resolve_carried_findings(
    carried_findings: list[CarriedFinding],
    report: ReviewReport,
    *,
    repo_root: Path,
    config,
    progress: ProgressSink,
) -> list[CarriedFinding]:
    if not carried_findings:
        return []
    if not config.llm.enabled:
        progress.event(f"marking {len(carried_findings)} carried finding(s) uncertain; LLM disabled", force=True)
        return [
            _uncertain_carried_finding(
                carried,
                "Current delta may contain resolution evidence, but LLM resolution is disabled.",
            )
            for carried in carried_findings
        ]
    if resolution_diff_warnings_incomplete(report):
        progress.event(
            f"marking {len(carried_findings)} carried finding(s) uncertain; diff warning evidence is incomplete",
            force=True,
        )
        return [
            _uncertain_carried_finding(
                carried,
                "Current diff warnings were omitted or truncated; resolution evidence is incomplete.",
            )
            for carried in carried_findings
        ]
    call_limit = config.gates.pre_push.incremental_retry.max_resolution_calls_per_retry
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    selected_indexes = {
        index
        for index, _carried in sorted(
            enumerate(carried_findings),
            key=lambda item: (
                severity_order.get(str(item[1].finding.severity), 4),
                0 if item[1].resolution_reason == _RESOLUTION_CALL_BUDGET_REASON else 1,
                item[0],
            ),
        )[:call_limit]
    }
    deferred_count = len(carried_findings) - len(selected_indexes)
    progress.event(
        f"resolving {len(selected_indexes)} carried finding(s)"
        f"{f'; deferring {deferred_count} at the retry call limit' if deferred_count else ''}",
        force=True,
    )
    provider = provider_from_config(config.llm)
    unresolved: list[CarriedFinding] = []
    for index, carried in enumerate(carried_findings):
        if index not in selected_indexes:
            unresolved.append(
                _uncertain_carried_finding(
                    carried,
                    _RESOLUTION_CALL_BUDGET_REASON,
                )
            )
            continue
        try:
            resolution = provider.resolve_finding(carried.finding, carried.context_pack, report, repo_root)
        except Exception as exc:
            unresolved.append(_uncertain_carried_finding(carried, f"Resolution verifier failed: {exc}"))
            continue
        if resolution.status == "resolved":
            continue
        unresolved.append(
            carried.model_copy(
                update={
                    "status": str(resolution.status),
                    "resolution_reason": resolution.reason,
                    "resolution_confidence": resolution.confidence,
                }
            )
        )
    return unresolved


def _combine_incremental_decision(
    current: PrePushGateDecision,
    active_carried_findings: list[CarriedFinding],
    carried_coverage_debt: CoverageDebt,
) -> PrePushGateDecision:
    reasons = list(current.reasons)
    seen_fingerprints = {finding_fingerprint(finding) for finding in current.blocking_findings}
    carried_findings: list[Finding] = []
    for carried in active_carried_findings:
        fingerprint = finding_fingerprint(carried.finding)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        carried_findings.append(carried.finding)
    if carried_findings:
        reasons.append(f"Carried blocking findings: {len(carried_findings)}")
    if carried_coverage_debt.quality_gate_failed or carried_coverage_debt.partial_blocked:
        details = "; ".join(carried_coverage_debt.reasons)
        reasons.append(f"Carried coverage debt{f': {details}' if details else ''}")
    return PrePushGateDecision(
        blocked=bool(reasons),
        reasons=reasons,
        blocking_findings=[*current.blocking_findings, *carried_findings],
        quality_gate_failed=current.quality_gate_failed or carried_coverage_debt.quality_gate_failed,
        partial_blocked=current.partial_blocked or carried_coverage_debt.partial_blocked,
    )


def _uncertain_carried_finding(carried: CarriedFinding, reason: str) -> CarriedFinding:
    return carried.model_copy(
        update={
            "status": "uncertain",
            "resolution_reason": reason,
            "resolution_confidence": "low",
        }
    )


def _merge_coverage_debt(carried: CoverageDebt, current: CoverageDebt) -> CoverageDebt:
    quality_gate_failed = carried.quality_gate_failed or current.quality_gate_failed
    partial_blocked = carried.partial_blocked or current.partial_blocked
    reasons = [*carried.reasons, *[reason for reason in current.reasons if reason not in carried.reasons]]
    return CoverageDebt(
        quality_gate_failed=quality_gate_failed,
        partial_blocked=partial_blocked,
        reasons=reasons if quality_gate_failed or partial_blocked else [],
        partial_severity=current.partial_severity if current.partial_severity != "none" else carried.partial_severity,
        quality_gate_status=(
            current.quality_gate_status if current.quality_gate_status != "pass" else carried.quality_gate_status
        ),
    )


def _prepend_gate_summary_markdown(
    markdown_text: str,
    decision: PrePushGateDecision,
    *,
    retry_summary: PrePushRetrySummary | None,
    stale_suppressions: list[StaleSuppression],
) -> str:
    title = "blocked" if decision.blocked else "passed"
    lines = [
        "## Pre-Push Gate",
        "",
        f"- Decision: `{title}`",
    ]
    if retry_summary is not None:
        lines.extend(
            [
                f"- Mode: `{retry_summary.mode}`",
                f"- New blocking findings: `{retry_summary.new_blocking_findings}`",
                f"- Still blocking carried findings: `{retry_summary.still_blocking_carried_findings}`",
                f"- Uncertain carried findings: `{retry_summary.uncertain_carried_findings}`",
                f"- Resolved carried findings: `{retry_summary.resolved_carried_findings}`",
            ]
        )
        if retry_summary.fallback_reason:
            lines.append(f"- Fallback reason: `{retry_summary.fallback_reason}`")
        if retry_summary.carried_coverage_reasons:
            lines.append(f"- Carried coverage debt: `{len(retry_summary.carried_coverage_reasons)}`")
    if stale_suppressions:
        lines.extend(["", "### Local Triage", ""])
        lines.append(f"- Stale suppressions requiring review: `{len(stale_suppressions)}`")
        for item in stale_suppressions[:10]:
            location = (
                item.snapshot.file if item.snapshot.line is None else f"{item.snapshot.file}:{item.snapshot.line}"
            )
            lines.append(
                f"- `{item.snapshot.fingerprint}` `{item.suppression.id}` "
                f"`{item.snapshot.severity}` {item.snapshot.title} at `{location}`"
            )
            lines.append(f"  Prior reason: {_markdown_one_line(item.suppression.reason)}")
            lines.append(f"  Stale reason: {_markdown_one_line(item.reason)}")
        if len(stale_suppressions) > 10:
            lines.append(f"- ... `{len(stale_suppressions) - 10}` more stale suppression(s).")
        lines.append(
            "- Re-check stale findings before suppressing again; if a finding is still objectively false positive, "
            "create a fresh local suppression from the current report with a new concrete reason."
        )
    lines.append("")
    return "\n".join(lines) + markdown_text


def _markdown_one_line(value: str, max_chars: int = 220) -> str:
    compact = " ".join(value.split())
    if len(compact) > max_chars:
        compact = compact[: max_chars - 3].rstrip() + "..."
    return compact.replace("|", "\\|")
