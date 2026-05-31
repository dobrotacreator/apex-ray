import time
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from apex_ray import git
from apex_ray.cli.common import ensure_distinct_outputs
from apex_ray.config import ConfigError, load_config
from apex_ray.gates import evaluate_pre_push_gate, render_pre_push_gate_stdout
from apex_ray.llm import LLMProviderError
from apex_ray.models import ReviewReport, TargetMode
from apex_ray.pipeline import continue_review_from_report, run_review_pipeline
from apex_ray.report import render_html, render_markdown
from apex_ray.report.coverage import continue_command_for_pack
from apex_ray.telemetry import TelemetryError, append_review_telemetry

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

    gate_config = review_config.gates.pre_push
    if not gate_config.enabled:
        typer.echo("APEX RAY GATE: DISABLED")
        raise typer.Exit()

    output = _resolve_output_path(root, output)
    json_output = _resolve_output_path(root, json_output)
    html_output = _resolve_output_path(root, html_output) if html_output is not None else None
    ensure_distinct_outputs(output, json_output, html_output)
    if telemetry and no_telemetry:
        raise typer.BadParameter("Use only one of --telemetry or --no-telemetry.")
    if telemetry_path is not None and no_telemetry:
        raise typer.BadParameter("Use --telemetry-path only when telemetry is enabled.")

    previous_report = _load_previous_report(json_output)
    target_base = base or review_config.base
    started_monotonic = time.monotonic()
    try:
        diff_text = _load_base_diff(root, target_base)
        report = run_review_pipeline(
            root,
            diff_text,
            TargetMode.BASE,
            review_config,
            base=target_base,
            config_path=config_path,
        )
        if gate_config.auto_followup_p0 and report.llm_coverage.partial_severity == "critical":
            report, selected_packs = continue_review_from_report(
                report,
                repo_root=root,
                config=review_config,
                residual_priorities={"p0"},
                only_unreviewed=True,
                review_depth="deep",
            )
            if selected_packs:
                typer.echo(f"Auto-followup reviewed {len(selected_packs)} residual P0 context pack(s).")
    except LLMProviderError as exc:
        raise typer.BadParameter(str(exc)) from exc
    duration_ms = round((time.monotonic() - started_monotonic) * 1000)

    _set_continue_commands(report, json_output)
    previous_decision = evaluate_pre_push_gate(previous_report, gate_config) if previous_report else None
    decision = evaluate_pre_push_gate(report, gate_config)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(report), encoding="utf-8")
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    if html_output is not None:
        html_output.parent.mkdir(parents=True, exist_ok=True)
        html_output.write_text(render_html(report), encoding="utf-8")

    telemetry_enabled = (
        review_config.telemetry.enabled or telemetry or telemetry_path is not None
    ) and not no_telemetry
    effective_telemetry_path = telemetry_path or Path(review_config.telemetry.path)
    if not effective_telemetry_path.is_absolute():
        effective_telemetry_path = root / effective_telemetry_path
    if telemetry_enabled:
        try:
            append_review_telemetry(
                report,
                effective_telemetry_path,
                source_repo=root,
                duration_ms=duration_ms,
                output_path=output,
                json_output_path=json_output,
                html_output_path=html_output,
            )
        except TelemetryError as exc:
            raise typer.BadParameter(str(exc)) from exc

    typer.echo(
        render_pre_push_gate_stdout(
            report,
            decision,
            markdown_path=output,
            json_path=json_output,
            base=target_base,
            config=gate_config,
            previous_decision=previous_decision,
        ),
        nl=False,
    )
    if telemetry_enabled:
        typer.echo(f"Appended telemetry: {effective_telemetry_path}")
    if decision.blocked:
        raise typer.Exit(code=1)


def _resolve_output_path(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _load_base_diff(root: Path, base: str) -> str:
    if not git.is_git_repo(root):
        raise typer.BadParameter("Current directory is not a git repository.")
    try:
        return git.diff_base(root, base)
    except git.GitError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _load_previous_report(path: Path) -> ReviewReport | None:
    if not path.exists():
        return None
    try:
        return ReviewReport.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError, ValidationError:
        return None


def _set_continue_commands(report: ReviewReport, json_output: Path) -> None:
    for todo in report.llm_coverage.coverage_todos:
        todo.suggested_command = continue_command_for_pack(todo.context_pack_id, str(json_output))
