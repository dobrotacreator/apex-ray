import shutil
import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from apex_ray import __version__, git
from apex_ray.analyzers import go_analyzer_runtime_dir, typescript_analyzer_script
from apex_ray.cli.benchmark import register_benchmark_commands
from apex_ray.cli.common import ensure_apex_ignore_for_outputs, ensure_distinct_outputs, resolve_output_path
from apex_ray.cli.eval import eval_app
from apex_ray.cli.findings import findings_app
from apex_ray.cli.gate import gate_app
from apex_ray.cli.memory import memory_app
from apex_ray.config import ConfigError, find_local_config, init_project, load_config
from apex_ray.discovery import discover_project
from apex_ray.invocation import ReviewOverrides, apply_review_overrides
from apex_ray.llm import LLMProviderError
from apex_ray.local_data import LOCAL_DATA_TOKEN, LocalDataPathError, resolve_config_path, resolve_runtime_config_paths
from apex_ray.models import LLMCoverageMode, LLMProviderName, ReviewReport, TargetMode
from apex_ray.pipeline import continue_review_from_report, run_review_pipeline
from apex_ray.report import (
    ReportArtifact,
    ReviewReportLoadError,
    archive_report_artifacts,
    load_review_report,
    render_html,
    render_markdown,
)
from apex_ray.report.coverage import continue_command_for_pack
from apex_ray.telemetry import (
    TelemetryError,
    append_review_telemetry,
    load_review_telemetry,
    render_review_telemetry_summary,
)

app = typer.Typer(
    help="Local CLI-first code review engine.",
    invoke_without_command=True,
    no_args_is_help=True,
)
app.add_typer(memory_app, name="memory")
app.add_typer(findings_app, name="findings")
app.add_typer(eval_app, name="eval")
app.add_typer(gate_app, name="gate")
register_benchmark_commands(app)


class InitHookMode(StrEnum):
    LEFTHOOK = "lefthook"
    GIT = "git"
    NONE = "none"


class InitAgentFilesMode(StrEnum):
    BOTH = "both"
    CODEX = "codex"
    CLAUDE = "claude"
    NONE = "none"


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", help="Show version and exit.")] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def init(
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", "--force", help="Overwrite Apex Ray-managed setup files when safe."),
    ] = False,
    hooks: Annotated[
        InitHookMode,
        typer.Option("--hooks", help="Hook setup mode: lefthook, git, or none."),
    ] = InitHookMode.LEFTHOOK,
    agent_files: Annotated[
        InitAgentFilesMode,
        typer.Option("--agent-files", help="Agent instruction files: both, codex, claude, or none."),
    ] = InitAgentFilesMode.BOTH,
    agent_skill: Annotated[
        bool,
        typer.Option("--agent-skill/--no-agent-skill", help="Add Apex Ray project skill files for selected agents."),
    ] = True,
    update_gitignore: Annotated[
        bool,
        typer.Option(
            "--update-gitignore/--no-update-gitignore",
            help="Deprecated compatibility flag. Emits a warning; root .gitignore is not modified.",
        ),
    ] = False,
) -> None:
    """Create project Apex Ray config, ignores, hooks, and agent instructions."""
    root = git.repo_root(Path.cwd()) or Path.cwd()
    try:
        paths = init_project(
            root,
            overwrite=overwrite,
            update_gitignore=update_gitignore,
            hooks=hooks.value,
            agent_files=agent_files.value,
            agent_skill=agent_skill,
        )
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Apex Ray ready: {root}")
    for path in paths:
        typer.echo(f"- {path}")
    for message in _init_next_steps(hooks.value):
        typer.echo(message)


def _init_next_steps(hooks: str) -> list[str]:
    messages = [
        "Next: inspect and commit Apex Ray setup files before reviewing application changes.",
    ]
    if hooks == "lefthook":
        if shutil.which("lefthook") is None:
            messages.append("Hook note: install Lefthook, then run `lefthook install` to activate pre-push review.")
        else:
            messages.append("Hook note: run `lefthook install` to activate pre-push review.")
    if hooks in {"lefthook", "git"}:
        messages.append("Hook note: generated hooks call `apex-ray`; ensure it is available on PATH for Git hooks.")
    return messages


@app.command()
def doctor(
    config: Annotated[Path | None, typer.Option("--config", help="Path to config file.")] = None,
) -> None:
    """Check local Apex Ray prerequisites and project discovery."""
    root = git.repo_root(Path.cwd()) or Path.cwd()
    try:
        review_config, config_path = load_config(root, config)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc

    project = discover_project(root, ignored_patterns=review_config.ignore)
    typer.echo("Apex Ray doctor")
    typer.echo(f"- Version: {__version__}")
    typer.echo("- Python runtime: 3.14 required")
    typer.echo(f"- Git available: {str(git.git_available()).lower()}")
    typer.echo(f"- Git repository: {str(project.is_git_repo).lower()}")
    typer.echo(f"- Repository root: {project.root}")
    typer.echo(f"- Config: {config_path or 'not found'}")
    typer.echo(f"- Local config: {find_local_config(root) or 'not found'}")
    typer.echo(f"- Detected languages: {', '.join(project.detected_languages) or 'none'}")
    typer.echo(f"- Package managers: {', '.join(project.package_managers) or 'none'}")
    typer.echo(f"- Framework hints: {', '.join(project.framework_hints) or 'none'}")
    analyzer_script = typescript_analyzer_script(review_config.analyzer, root)
    go_runtime = go_analyzer_runtime_dir()
    typer.echo("- Python analyzer: built in")
    typer.echo(f"- Python analyzer available: {str(_python_analyzer_available()).lower()}")
    typer.echo(f"- Go available: {str(shutil.which('go') is not None).lower()}")
    typer.echo(f"- Go analyzer: {go_runtime}")
    typer.echo(f"- Go analyzer available: {str(go_runtime.exists()).lower()}")
    typer.echo(f"- Node available: {str(shutil.which('node') is not None).lower()}")
    typer.echo(f"- TypeScript analyzer: {analyzer_script}")
    typer.echo(f"- TypeScript analyzer built: {str(analyzer_script.exists()).lower()}")


def _python_analyzer_available() -> bool:
    try:
        from apex_ray.analyzers.python import run_python_analyzer
    except Exception:
        return False
    return callable(run_python_analyzer)


@app.command("telemetry-summary")
def review_telemetry_summary(
    telemetry_path: Annotated[
        Path | None,
        typer.Option("--telemetry-path", help="Review telemetry JSONL path."),
    ] = None,
) -> None:
    """Summarize long-lived local review telemetry."""
    root = git.repo_root(Path.cwd()) or Path.cwd()
    if telemetry_path is not None and LOCAL_DATA_TOKEN not in str(telemetry_path):
        path = telemetry_path if telemetry_path.is_absolute() else root / telemetry_path
    else:
        try:
            review_config, _ = load_config(root)
            path = resolve_config_path(root, review_config.local_data, telemetry_path or review_config.telemetry.path)
        except (ConfigError, LocalDataPathError) as exc:
            raise typer.BadParameter(str(exc)) from exc
    try:
        entries = load_review_telemetry(path)
    except TelemetryError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(render_review_telemetry_summary(entries))


@app.command()
def review(
    base: Annotated[str | None, typer.Option("--base", help="Base ref for git diff <base>...HEAD.")] = None,
    staged: Annotated[bool, typer.Option("--staged", help="Review staged changes.")] = False,
    worktree: Annotated[bool, typer.Option("--worktree", help="Review unstaged worktree changes.")] = False,
    diff_file: Annotated[Path | None, typer.Option("--diff", help="Review a supplied unified diff file.")] = None,
    continue_from: Annotated[
        Path | None,
        typer.Option("--continue-from", help="Continue an existing Apex Ray JSON report by reviewing skipped packs."),
    ] = None,
    only_unreviewed: Annotated[
        bool,
        typer.Option("--only-unreviewed/--include-reviewed", help="Limit continuation to currently unreviewed packs."),
    ] = True,
    only_pack: Annotated[
        list[str] | None,
        typer.Option("--only-pack", help="Continue only a specific context pack id. May be repeated."),
    ] = None,
    residual_priority: Annotated[
        list[str] | None,
        typer.Option("--residual-priority", help="Continue only residual priority p0, p1, or p2. May be repeated."),
    ] = None,
    only_slice: Annotated[
        list[str] | None,
        typer.Option("--only-slice", help="Continue only a review slice such as high_risk, source, tests, docs."),
    ] = None,
    continue_review_depth: Annotated[
        str,
        typer.Option("--continue-review-depth", help="Continuation review depth: deep or shallow."),
    ] = "deep",
    auto_followup: Annotated[
        bool,
        typer.Option("--auto-followup", help="After the first pass, automatically review unreviewed P0 packs."),
    ] = False,
    output: Annotated[Path, typer.Option("--output", help="Markdown report path.")] = Path(
        ".apex-ray/reports/review.md"
    ),
    json_output: Annotated[Path, typer.Option("--json", help="JSON report path.")] = Path(
        ".apex-ray/reports/review.json"
    ),
    html_output: Annotated[Path | None, typer.Option("--html", help="Optional HTML report path.")] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Path to config file.")] = None,
    llm: Annotated[bool, typer.Option("--llm", help="Run LLM review over generated context packs.")] = False,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Disable configured LLM review.")] = False,
    llm_provider: Annotated[str | None, typer.Option("--llm-provider", help="Override LLM provider.")] = None,
    llm_model: Annotated[str | None, typer.Option("--llm-model", help="Override LLM model.")] = None,
    llm_jobs: Annotated[
        int | None, typer.Option("--llm-jobs", min=1, help="Concurrent LLM pack/verifier jobs.")
    ] = None,
    llm_coverage_mode: Annotated[
        str | None,
        typer.Option("--llm-coverage-mode", help="Override LLM coverage mode: fast, balanced, or exhaustive."),
    ] = None,
    llm_max_deep_packs: Annotated[
        int | None,
        typer.Option("--llm-max-deep-packs", min=1, help="Maximum deep-reviewed context packs."),
    ] = None,
    llm_max_input_tokens: Annotated[
        int | None,
        typer.Option("--llm-max-input-tokens", min=1, help="Approximate total LLM review input-token budget."),
    ] = None,
    verify: Annotated[bool, typer.Option("--verify", help="Run verifier pass over LLM findings.")] = False,
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Disable verifier pass.")] = False,
    cache: Annotated[bool, typer.Option("--cache/--no-cache", help="Use the LLM response cache.")] = True,
    refresh_cache: Annotated[bool, typer.Option("--refresh-cache", help="Refresh cached LLM responses.")] = False,
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir", help="LLM cache directory.")] = None,
    analyzer_cache: Annotated[
        bool,
        typer.Option("--analyzer-cache/--no-analyzer-cache", help="Use analyzer repo index caches."),
    ] = True,
    refresh_analyzer_cache: Annotated[
        bool,
        typer.Option("--refresh-analyzer-cache", help="Refresh analyzer repo index caches."),
    ] = False,
    analyzer_cache_dir: Annotated[
        Path | None,
        typer.Option("--analyzer-cache-dir", help="Analyzer index cache directory."),
    ] = None,
    telemetry: Annotated[
        bool,
        typer.Option("--telemetry", help="Append this local review to review telemetry JSONL."),
    ] = False,
    no_telemetry: Annotated[
        bool,
        typer.Option("--no-telemetry", help="Disable configured local review telemetry for this run."),
    ] = False,
    telemetry_path: Annotated[
        Path | None,
        typer.Option("--telemetry-path", help="Review telemetry JSONL path."),
    ] = None,
) -> None:
    """Inspect a diff and write markdown/JSON reports."""
    root = git.repo_root(Path.cwd()) or Path.cwd()
    try:
        review_config, config_path = load_config(root, config)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc

    explicit_modes = sum(
        bool(value) for value in (staged, worktree, diff_file is not None, base is not None, continue_from is not None)
    )
    if explicit_modes > 1:
        raise typer.BadParameter(
            "Use only one review target: --staged, --worktree, --diff, --base, or --continue-from."
        )
    if llm and no_llm:
        raise typer.BadParameter("Use only one of --llm or --no-llm.")
    if verify and no_verify:
        raise typer.BadParameter("Use only one of --verify or --no-verify.")
    if refresh_cache and not cache:
        raise typer.BadParameter("Use --refresh-cache only when cache is enabled.")
    if refresh_analyzer_cache and not analyzer_cache:
        raise typer.BadParameter("Use --refresh-analyzer-cache only when analyzer cache is enabled.")
    if telemetry and no_telemetry:
        raise typer.BadParameter("Use only one of --telemetry or --no-telemetry.")
    if telemetry_path is not None and no_telemetry:
        raise typer.BadParameter("Use --telemetry-path only when telemetry is enabled.")
    if continue_review_depth not in {"deep", "shallow"}:
        raise typer.BadParameter("--continue-review-depth must be 'deep' or 'shallow'.")
    prior_report = None
    if continue_from is not None:
        try:
            prior_report = load_review_report(continue_from)
        except OSError as exc:
            raise typer.BadParameter(f"Unable to read report {continue_from}: {exc}") from exc
        except ReviewReportLoadError as exc:
            raise typer.BadParameter(str(exc)) from exc
        root = Path(prior_report.project.root)
        review_config = prior_report.config

    output = resolve_output_path(root, output)
    json_output = resolve_output_path(root, json_output)
    html_output = resolve_output_path(root, html_output) if html_output is not None else None
    ensure_distinct_outputs(output, json_output, html_output)

    parsed_provider = None
    if llm_provider:
        try:
            parsed_provider = LLMProviderName(llm_provider)
        except ValueError as exc:
            raise typer.BadParameter(f"Unsupported LLM provider: {llm_provider}") from exc
    parsed_coverage_mode = None
    if llm_coverage_mode is not None:
        try:
            parsed_coverage_mode = LLMCoverageMode(llm_coverage_mode)
        except ValueError as exc:
            raise typer.BadParameter(f"Unsupported LLM coverage mode: {llm_coverage_mode}") from exc
    effective_config = apply_review_overrides(
        review_config,
        ReviewOverrides(
            llm_enabled=True if llm else False if no_llm else None,
            provider=parsed_provider,
            model=llm_model,
            verify=True if verify else False if no_verify else None,
            cache_allowed=cache,
            refresh_cache=refresh_cache,
            cache_dir=cache_dir,
            llm_jobs=llm_jobs,
            coverage_mode=parsed_coverage_mode,
            max_deep_packs=llm_max_deep_packs,
            max_input_tokens=llm_max_input_tokens,
            analyzer_cache_allowed=analyzer_cache,
            refresh_analyzer_cache=refresh_analyzer_cache,
            analyzer_cache_dir=analyzer_cache_dir,
        ),
    )
    try:
        effective_config = resolve_runtime_config_paths(root, effective_config)
    except LocalDataPathError as exc:
        raise typer.BadParameter(str(exc)) from exc
    telemetry_enabled = (
        effective_config.telemetry.enabled or telemetry or telemetry_path is not None
    ) and not no_telemetry
    try:
        effective_telemetry_path = resolve_config_path(
            root,
            effective_config.local_data,
            telemetry_path or effective_config.telemetry.path,
        )
    except LocalDataPathError as exc:
        raise typer.BadParameter(str(exc)) from exc

    started_monotonic = time.monotonic()
    try:
        if prior_report is not None:
            priorities = set(residual_priority or []) or None
            invalid_priorities = sorted(
                priority for priority in priorities or set() if priority not in {"p0", "p1", "p2"}
            )
            if invalid_priorities:
                raise typer.BadParameter(f"Unsupported residual priority: {', '.join(invalid_priorities)}")
            report, selected_packs = continue_review_from_report(
                prior_report,
                repo_root=root,
                config=effective_config,
                residual_priorities=priorities,
                slices=set(only_slice or []) or None,
                pack_ids=set(only_pack or []) or None,
                only_unreviewed=only_unreviewed,
                review_depth=continue_review_depth,  # type: ignore[arg-type]
            )
            if not selected_packs:
                typer.echo("No continuation context packs matched the requested filters.")
        else:
            target_base = base or effective_config.base
            target_mode, diff_text = _load_diff(root, target_base, staged, worktree, diff_file)
            report = run_review_pipeline(
                root,
                diff_text,
                target_mode,
                effective_config,
                base=target_base if target_mode == TargetMode.BASE else None,
                config_path=config_path,
            )
            if auto_followup and report.llm_coverage.partial_severity == "critical":
                report, selected_packs = continue_review_from_report(
                    report,
                    repo_root=root,
                    config=effective_config,
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

    markdown_text = render_markdown(report)
    json_text = report.model_dump_json(indent=2)
    html_text = render_html(report) if html_output else None
    ensure_apex_ignore_for_outputs(root, output, json_output, html_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown_text, encoding="utf-8")
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json_text, encoding="utf-8")

    if html_output:
        html_output.parent.mkdir(parents=True, exist_ok=True)
        html_output.write_text(html_text or "", encoding="utf-8")

    artifacts = [
        ReportArtifact(output, markdown_text),
        ReportArtifact(json_output, json_text),
    ]
    if html_output and html_text is not None:
        artifacts.append(ReportArtifact(html_output, html_text))
    archive_path = archive_report_artifacts(
        root,
        effective_config.reports,
        artifacts,
        created_at=report.generated_at,
    )

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

    typer.echo(f"Wrote {output}")
    typer.echo(f"Wrote {json_output}")
    if html_output:
        typer.echo(f"Wrote {html_output}")
    if archive_path:
        typer.echo(f"Archived report: {archive_path}")
    if telemetry_enabled:
        typer.echo(f"Appended telemetry: {effective_telemetry_path}")


def _set_continue_commands(report: ReviewReport, json_output: Path) -> None:
    for todo in report.llm_coverage.coverage_todos:
        todo.suggested_command = continue_command_for_pack(todo.context_pack_id, str(json_output))


def _load_diff(
    root: Path,
    base: str,
    staged: bool,
    worktree: bool,
    diff_file: Path | None,
) -> tuple[TargetMode, str]:
    if diff_file is not None:
        if not diff_file.exists():
            raise typer.BadParameter(f"Diff file does not exist: {diff_file}")
        return TargetMode.PATCH, diff_file.read_text(encoding="utf-8")

    if not git.is_git_repo(root):
        raise typer.BadParameter("Current directory is not a git repository. Use --diff to review a patch file.")

    try:
        if staged:
            return TargetMode.STAGED, git.diff_staged(root)
        if worktree:
            return TargetMode.WORKTREE, git.diff_worktree(root)
        return TargetMode.BASE, git.diff_base(root, base)
    except git.GitError as exc:
        raise typer.BadParameter(str(exc)) from exc
