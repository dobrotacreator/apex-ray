import time
from pathlib import Path
from typing import Annotated

import typer

from apex_ray import git
from apex_ray.cli.common import (
    ensure_apex_ignore_for_outputs,
    ensure_distinct_outputs,
    resolve_output_path,
    warn_outdated_agent_artifacts,
)
from apex_ray.config import ConfigError, load_config
from apex_ray.gate_retry import (
    CarriedFinding,
    CoverageDebt,
    any_relevant_path_changed,
    build_pre_push_state,
    changed_paths,
    check_incremental_eligibility,
    config_fingerprint,
    coverage_debt_from_decision,
    current_blocking_findings,
    dedupe_carried_findings,
    load_pre_push_state,
    resolve_state_path,
    stale_carried_finding_reason,
    write_pre_push_state,
)
from apex_ray.gates import PrePushGateDecision, PrePushRetrySummary, evaluate_pre_push_gate, render_pre_push_gate_stdout
from apex_ray.llm import LLMProviderError
from apex_ray.llm.providers import provider_from_config
from apex_ray.local_data import LocalDataPathError, resolve_config_path, resolve_runtime_config_paths
from apex_ray.models import ReviewReport, TargetMode
from apex_ray.pipeline import continue_review_from_report, run_review_pipeline
from apex_ray.progress import NoopProgress, ProgressSink, StreamProgress, progress_enabled
from apex_ray.report import (
    ReportArtifact,
    ReviewReportLoadError,
    archive_report_artifacts,
    load_review_report,
    render_html,
    render_markdown,
)
from apex_ray.report.coverage import continue_command_for_pack
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

gate_app = typer.Typer(help="Run configured Apex Ray quality gates.")


@gate_app.command("pre-push")
def pre_push(
    base: Annotated[str | None, typer.Option("--base", help="Base ref for git diff <base>...HEAD.")] = None,
    output: Annotated[Path, typer.Option("--output", help="Markdown report path.")] = Path(
        ".apex-ray/reports/pre-push.md"
    ),
    json_output: Annotated[Path, typer.Option("--json", help="JSON report path.")] = Path(
        ".apex-ray/reports/pre-push.json"
    ),
    html_output: Annotated[Path | None, typer.Option("--html", help="Optional HTML report path.")] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Path to config file.")] = None,
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
    root = git.repo_root(Path.cwd()) or Path.cwd()
    try:
        review_config, config_path = load_config(root, config)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        review_config = resolve_runtime_config_paths(root, review_config)
    except LocalDataPathError as exc:
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
    state_path = resolve_state_path(root, gate_config)
    retry_state = None
    retry_summary: PrePushRetrySummary | None = None
    current_head = ""
    merge_base_sha = ""
    config_hash = ""
    incremental_mode = False
    incremental_fallback_reason = ""
    if gate_config.incremental_retry.enabled:
        try:
            current_head = git.rev_parse(root, "HEAD")
            merge_base_sha = git.merge_base(root, target_base, "HEAD")
        except git.GitError as exc:
            raise typer.BadParameter(str(exc)) from exc
        config_hash = config_fingerprint(review_config, gate_config)
        retry_state = load_pre_push_state(state_path)
        previous_head_exists = bool(retry_state and git.object_exists(root, retry_state.head_sha))
        eligibility = check_incremental_eligibility(
            retry_state,
            repo_root=root,
            base_ref=target_base,
            merge_base_sha=merge_base_sha,
            config_hash=config_hash,
            previous_head_exists=previous_head_exists,
        )
        incremental_mode = eligibility.eligible
        incremental_fallback_reason = eligibility.reason

    started_monotonic = time.monotonic()
    try:
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
        )
        if gate_config.auto_followup_p0 and report.llm_coverage.partial_severity == "critical":
            report, selected_packs = continue_review_from_report(
                report,
                repo_root=root,
                config=review_config,
                residual_priorities={"p0"},
                only_unreviewed=True,
                review_depth="deep",
                progress=progress,
            )
            if selected_packs:
                progress.event(f"auto-followup reviewed {len(selected_packs)} residual P0 context pack(s)", force=True)
    except LLMProviderError as exc:
        raise typer.BadParameter(str(exc)) from exc
    duration_ms = round((time.monotonic() - started_monotonic) * 1000)

    progress.event("writing reports", force=True)
    _set_continue_commands(report, json_output)
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
    if incremental_mode and retry_state is not None:
        active_carried_findings, resolved_carried_count = _resolve_incremental_carried_findings(
            retry_state.active_findings,
            report,
            repo_root=root,
            config=review_config,
            progress=progress,
        )
        if retry_state.coverage_debt.quality_gate_failed or retry_state.coverage_debt.partial_blocked:
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
    if gate_config.incremental_retry.enabled:
        retry_summary = PrePushRetrySummary(
            mode="incremental" if incremental_mode else "full",
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
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown_text, encoding="utf-8")
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json_text, encoding="utf-8")
    if html_output is not None:
        html_output.parent.mkdir(parents=True, exist_ok=True)
        html_output.write_text(html_text or "", encoding="utf-8")

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
                head_sha=current_head,
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


def _set_continue_commands(report: ReviewReport, json_output: Path) -> None:
    for todo in report.llm_coverage.coverage_todos:
        todo.suggested_command = continue_command_for_pack(todo.context_pack_id, str(json_output))


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
) -> tuple[list[CarriedFinding], int]:
    changed = changed_paths(report)
    unchanged_active: list[CarriedFinding] = []
    needs_resolution: list[CarriedFinding] = []
    stale_resolved_count = 0
    for carried in carried_findings:
        relevant = set(carried.relevant_files or [carried.finding.file])
        if changed and any_relevant_path_changed(relevant, changed):
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
                        "resolution_reason": "No relevant files changed since the previous gate attempt.",
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
            _uncertain_carried_finding(carried, "Relevant files changed, but LLM resolution is disabled.")
            for carried in carried_findings
        ]
    progress.event(f"resolving {len(carried_findings)} carried finding(s)", force=True)
    provider = provider_from_config(config.llm)
    unresolved: list[CarriedFinding] = []
    for carried in carried_findings:
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
    carried_findings = [carried.finding for carried in active_carried_findings]
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
