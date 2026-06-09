from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, cast

import typer

from apex_ray import git
from apex_ray.cli.common import ensure_apex_ignore_for_outputs
from apex_ray.config import ConfigError, load_config
from apex_ray.findings import finding_fingerprint
from apex_ray.local_data import LocalDataPathError, resolve_runtime_config_paths
from apex_ray.report import ReviewReportLoadError, load_review_report
from apex_ray.triage import (
    SuppressionVerdict,
    add_or_replace_suppression,
    append_triage_events,
    create_suppression,
    finding_snapshot,
    load_triage_state,
    prune_triage_state,
    remove_suppressions,
    write_triage_state,
)

findings_app = typer.Typer(help="Inspect and triage Apex Ray findings.")


@findings_app.command("list")
def list_findings(
    from_report: Annotated[Path, typer.Option("--from-report", help="Apex Ray review JSON report.")],
) -> None:
    """Print finding fingerprints from a review report."""
    report = _load_report(from_report)
    if not report.findings:
        typer.echo("No findings.")
        return
    typer.echo("Apex Ray findings")
    for finding in report.findings:
        location = finding.file if finding.line is None else f"{finding.file}:{finding.line}"
        typer.echo(f"- {finding_fingerprint(finding)} [{finding.severity}] {finding.title}")
        typer.echo(f"  Location: {location}")
        if finding.context_pack_id:
            typer.echo(f"  Context pack: {finding.context_pack_id}")


@findings_app.command("suppress")
def suppress_finding(
    finding_id: Annotated[str, typer.Argument(help="Finding fingerprint from `apex-ray findings list`.")],
    from_report: Annotated[Path, typer.Option("--from-report", help="Apex Ray review JSON report.")],
    reason: Annotated[str, typer.Option("--reason", help="Why this finding is safe to suppress locally.")],
    verdict: Annotated[str, typer.Option("--verdict", help="Local triage verdict.")] = "false_positive",
    expires: Annotated[
        str | None,
        typer.Option("--expires", help="Expiry such as 7d, 30d, never, or an ISO timestamp."),
    ] = None,
) -> None:
    """Suppress one finding locally for matching future gate runs."""
    root, config = _load_runtime_config()
    if not config.triage.enabled:
        raise typer.BadParameter("Finding triage is disabled by review.triage.enabled=false.")
    if verdict not in {"false_positive", "not_actionable", "duplicate", "accepted_risk"}:
        raise typer.BadParameter(
            "Unsupported verdict. Use false_positive, not_actionable, duplicate, or accepted_risk."
        )
    report = _load_report(from_report)
    packs_by_id = {pack.id: pack for pack in report.context_packs}
    matches = [finding for finding in report.findings if finding_fingerprint(finding) == finding_id]
    if not matches:
        raise typer.BadParameter(f"Finding id not found in report: {finding_id}")
    if len(matches) > 1:
        raise typer.BadParameter(f"Finding id is ambiguous in report: {finding_id}")
    finding = matches[0]
    snapshot = finding_snapshot(finding, packs_by_id.get(finding.context_pack_id))
    expires_at = _parse_expiry(expires, default_days=config.triage.default_expiry_days)
    state_path = Path(config.triage.state_path)
    events_path = Path(config.triage.events_path)
    ensure_apex_ignore_for_outputs(root, state_path, events_path)
    state = load_triage_state(state_path)
    suppression, event = create_suppression(
        snapshot=snapshot,
        reason=reason,
        config=config.triage,
        verdict=cast(SuppressionVerdict, verdict),
        expires_at=expires_at,
        target_base_ref=report.diff.base,
        report_path=from_report,
    )
    state = add_or_replace_suppression(state, suppression)
    pruned = prune_triage_state(state, config.triage)
    write_triage_state(state_path, pruned.state)
    append_triage_events(events_path, [event, *pruned.events], retention_days=config.triage.events_retention_days)
    typer.echo(f"Suppressed {finding_id} as {verdict}: {suppression.id}")
    if suppression.expires_at:
        typer.echo(f"Expires: {suppression.expires_at.isoformat()}")


@findings_app.command("unsuppress")
def unsuppress_finding(
    selector: Annotated[str, typer.Argument(help="Suppression id or finding fingerprint.")],
) -> None:
    """Remove local active suppressions."""
    root, config = _load_runtime_config()
    state_path = Path(config.triage.state_path)
    events_path = Path(config.triage.events_path)
    ensure_apex_ignore_for_outputs(root, state_path, events_path)
    state = load_triage_state(state_path)
    next_state, events = remove_suppressions(state, selector)
    if not events:
        typer.echo(f"No active suppression matched {selector}.")
        return
    write_triage_state(state_path, next_state)
    append_triage_events(events_path, events, retention_days=config.triage.events_retention_days)
    typer.echo(f"Removed {len(events)} suppression(s).")


@findings_app.command("suppressions")
def list_suppressions() -> None:
    """List active local suppressions."""
    root, config = _load_runtime_config()
    state_path = Path(config.triage.state_path)
    events_path = Path(config.triage.events_path)
    state = load_triage_state(state_path)
    result = prune_triage_state(state, config.triage)
    if result.events or result.state.suppressions != state.suppressions:
        ensure_apex_ignore_for_outputs(root, state_path, events_path)
        write_triage_state(state_path, result.state)
        append_triage_events(events_path, result.events, retention_days=config.triage.events_retention_days)
    if not result.state.suppressions:
        typer.echo("No active suppressions.")
        return
    typer.echo("Active Apex Ray suppressions")
    for suppression in result.state.suppressions:
        location = suppression.file if suppression.line is None else f"{suppression.file}:{suppression.line}"
        expires = suppression.expires_at.isoformat() if suppression.expires_at else "never"
        typer.echo(f"- {suppression.id} {suppression.finding_fingerprint} [{suppression.severity}]")
        typer.echo(f"  {suppression.title}")
        typer.echo(f"  Location: {location}")
        typer.echo(f"  Verdict: {suppression.verdict}; expires: {expires}; matches: {suppression.match_count}")
        typer.echo(f"  Reason: {suppression.reason}")


@findings_app.command("prune")
def prune_findings() -> None:
    """Prune expired and over-limit local suppressions."""
    root, config = _load_runtime_config()
    state_path = Path(config.triage.state_path)
    events_path = Path(config.triage.events_path)
    ensure_apex_ignore_for_outputs(root, state_path, events_path)
    state = load_triage_state(state_path)
    result = prune_triage_state(state, config.triage)
    write_triage_state(state_path, result.state)
    append_triage_events(events_path, result.events, retention_days=config.triage.events_retention_days)
    typer.echo(f"Active suppressions: {len(result.state.suppressions)}")
    typer.echo(f"Expired suppressions: {result.expired_count}")
    typer.echo(f"Pruned suppressions: {result.pruned_count}")


def _load_runtime_config():
    root = git.repo_root(Path.cwd()) or Path.cwd()
    try:
        review_config, _ = load_config(root)
        return root, resolve_runtime_config_paths(root, review_config)
    except (ConfigError, LocalDataPathError) as exc:
        raise typer.BadParameter(str(exc)) from exc


def _load_report(path: Path):
    try:
        return load_review_report(path)
    except OSError as exc:
        raise typer.BadParameter(f"Unable to read report {path}: {exc}") from exc
    except ReviewReportLoadError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _parse_expiry(value: str | None, *, default_days: int) -> datetime | None:
    if value is None:
        return datetime.now(UTC) + timedelta(days=default_days)
    normalized = value.strip().lower()
    if normalized in {"never", "none"}:
        return None
    if normalized.endswith("d") and normalized[:-1].isdigit():
        days = int(normalized[:-1])
        if days <= 0:
            raise typer.BadParameter("--expires must be positive.")
        return datetime.now(UTC) + timedelta(days=days)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter("--expires must be like 7d, 30d, never, or an ISO timestamp.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
