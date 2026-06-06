from pathlib import Path
from typing import Annotated

import typer

from apex_ray import git
from apex_ray.config import ConfigError, load_config
from apex_ray.memory import memory_suggestions_from_report
from apex_ray.report import ReviewReportLoadError, load_review_report

memory_app = typer.Typer(help="Inspect and maintain repo-committed Apex Ray memory.")


@memory_app.command("lint")
def memory_lint(
    config: Annotated[Path | None, typer.Option("--config", help="Path to config file.")] = None,
) -> None:
    """Load configured memory cards and report validation errors."""
    root = git.repo_root(Path.cwd()) or Path.cwd()
    try:
        review_config, config_path = load_config(root, config)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo("Apex Ray memory")
    typer.echo(f"- Config: {config_path or 'not found'}")
    typer.echo(f"- Enabled: {str(review_config.memory.enabled).lower()}")
    typer.echo(f"- Paths: {', '.join(review_config.memory.paths) or 'none'}")
    typer.echo(f"- Loaded cards: {len(review_config.memory_definitions)}")
    for card in review_config.memory_definitions:
        source = card.source_path or "inline"
        typer.echo(f"  - {card.id} ({card.kind}, applies_to={card.applies_to or 'default'}) from {source}")


@memory_app.command("suggest")
def memory_suggest(
    from_report: Annotated[Path, typer.Option("--from-report", help="Apex Ray review JSON report.")],
    output: Annotated[Path | None, typer.Option("--output", help="Optional markdown output path.")] = None,
    include_unverified: Annotated[
        bool,
        typer.Option(
            "--include-unverified",
            help="Draft suggestions from unverified findings. Default uses approved verifier findings only.",
        ),
    ] = False,
) -> None:
    """Draft curated memory cards from a review JSON report."""
    try:
        report = load_review_report(from_report)
    except OSError as exc:
        raise typer.BadParameter(f"Unable to read report {from_report}: {exc}") from exc
    except ReviewReportLoadError as exc:
        raise typer.BadParameter(str(exc)) from exc

    suggestions = memory_suggestions_from_report(report, include_unverified=include_unverified)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(suggestions, encoding="utf-8")
        typer.echo(f"Wrote {output}")
    else:
        typer.echo(suggestions)
