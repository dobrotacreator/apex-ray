import hashlib
import json
import os
import shutil
import tempfile
import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol

import typer

from apex_ray import __version__, git
from apex_ray.analyzers import go_analyzer_runtime_dir, typescript_analyzer_script
from apex_ray.cli.benchmark import register_benchmark_commands
from apex_ray.cli.common import (
    atomic_write_text,
    ensure_apex_ignore_for_outputs,
    ensure_distinct_outputs,
    resolve_output_path,
    warn_outdated_agent_artifacts,
)
from apex_ray.cli.eval import eval_app
from apex_ray.cli.findings import findings_app
from apex_ray.cli.gate import gate_app
from apex_ray.cli.memory import memory_app
from apex_ray.config import (
    ConfigError,
    agent_artifact_statuses,
    find_local_config,
    init_project,
    load_config,
    managed_hook_statuses,
)
from apex_ray.config import (
    refresh_agent_artifacts as refresh_project_agent_artifacts,
)
from apex_ray.config import (
    refresh_managed_artifacts as refresh_project_managed_artifacts,
)
from apex_ray.discovery import DiscoveryError, discover_project, discover_repo_root
from apex_ray.invocation import ApexRayLauncher, ReviewOverrides, apply_review_overrides, render_apex_ray_command
from apex_ray.llm import LLMProviderError
from apex_ray.llm.cache import cache_for_config
from apex_ray.local_data import LOCAL_DATA_TOKEN, LocalDataPathError, resolve_config_path, resolve_runtime_config_paths
from apex_ray.models import (
    DEFAULT_AUTO_FOLLOWUP_P0_MAX_PACK_REVIEWS,
    DartAnalyzerConfig,
    LLMCoverageMode,
    LLMProviderName,
    ReviewConfig,
    ReviewCoverageCompletion,
    ReviewReport,
    TargetMode,
)
from apex_ray.pipeline import (
    CoverageScopeError,
    continue_review_from_report,
    continue_review_until_complete,
    resolve_completion_reviewer_scope,
    run_review_pipeline,
)
from apex_ray.pipeline.snapshot import ReviewInputSnapshotError, validate_review_input_snapshot
from apex_ray.report import (
    ReportArtifact,
    ReviewReportLoadError,
    archive_report_artifacts,
    load_review_report,
    render_html,
    render_markdown,
    render_sarif,
)
from apex_ray.report.coverage import render_coverage_summary_lines, set_continue_commands
from apex_ray.repository_runtime import (
    RepositoryRuntimeError,
    RepositoryRuntimeMode,
    assert_repository_runtime,
    assert_source_uv_project,
    inspect_repository_runtime,
)
from apex_ray.reviewers import ReviewerConfigError, effective_reviewers, llm_config_for_reviewer
from apex_ray.telemetry import (
    TelemetryError,
    append_review_telemetry,
    load_review_telemetry,
    render_review_telemetry_summary,
)
from apex_ray.version_lock import VersionLockError, render_uvx_command

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


class InitRuntimeMode(StrEnum):
    LOCKED = "locked"
    SOURCE = "source"


class _DartToolchainResolution(Protocol):
    @property
    def command(self) -> list[str]: ...

    @property
    def source(self) -> str: ...

    @property
    def version(self) -> str | None: ...

    @property
    def error(self) -> str | None: ...

    @property
    def remediation(self) -> str | None: ...


class InitAgentFilesMode(StrEnum):
    BOTH = "both"
    CODEX = "codex"
    CLAUDE = "claude"
    NONE = "none"


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show version and exit.")] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand in {None, "doctor", "init", "review"}:
        return
    root = git.repo_root(Path.cwd()) or Path.cwd()
    try:
        assert_repository_runtime(root, runtime_version=__version__)
    except RepositoryRuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def init(
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", "--force", help="Overwrite Apex Ray-managed setup files when safe."),
    ] = False,
    hooks: Annotated[
        InitHookMode | None,
        typer.Option(
            "--hooks",
            help="Hook setup mode. New projects default to lefthook; managed refreshes auto-detect the existing mode.",
        ),
    ] = None,
    agent_files: Annotated[
        InitAgentFilesMode,
        typer.Option("--agent-files", help="Agent instruction files: both, codex, claude, or none."),
    ] = InitAgentFilesMode.BOTH,
    agent_skill: Annotated[
        bool,
        typer.Option("--agent-skill/--no-agent-skill", help="Add Apex Ray project skill files for selected agents."),
    ] = True,
    runtime: Annotated[
        InitRuntimeMode | None,
        typer.Option(
            "--runtime",
            help="Repository runtime: locked published package or current Apex Ray development checkout.",
        ),
    ] = None,
    refresh_agent_artifacts: Annotated[
        bool,
        typer.Option(
            "--refresh-agent-artifacts",
            help="Refresh only Apex Ray-managed AGENTS/CLAUDE blocks and generated skills.",
        ),
    ] = False,
    refresh_managed_artifacts: Annotated[
        bool,
        typer.Option(
            "--refresh-managed-artifacts",
            help="Refresh repository runtime metadata, the exact hook launcher, and managed agent artifacts.",
        ),
    ] = False,
    update_version_lock: Annotated[
        bool,
        typer.Option(
            "--update-version-lock",
            help="With --refresh-managed-artifacts, pin the running Apex Ray package version.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show agent artifacts that would be refreshed without writing files."),
    ] = False,
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
    requested_hook_mode = hooks.value if hooks is not None else None
    requested_runtime_mode = runtime.value if runtime is not None else None
    init_hook_mode = requested_hook_mode or InitHookMode.LEFTHOOK.value
    if refresh_agent_artifacts and refresh_managed_artifacts:
        raise typer.BadParameter("Use only one of --refresh-agent-artifacts or --refresh-managed-artifacts.")
    if dry_run and not (refresh_agent_artifacts or refresh_managed_artifacts):
        raise typer.BadParameter("Use --dry-run with an artifact refresh option.")
    if update_version_lock and not refresh_managed_artifacts:
        raise typer.BadParameter("Use --update-version-lock only with --refresh-managed-artifacts.")
    if runtime is not None and refresh_agent_artifacts:
        raise typer.BadParameter("Use --runtime only with initial setup or --refresh-managed-artifacts.")
    existing_runtime = inspect_repository_runtime(root, runtime_version=__version__)
    if existing_runtime.mode == RepositoryRuntimeMode.SOURCE:
        try:
            assert_repository_runtime(root, runtime_version=__version__)
        except RepositoryRuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc
    try:
        if refresh_agent_artifacts:
            paths = refresh_project_agent_artifacts(
                root,
                agent_files=agent_files.value,
                agent_skill=agent_skill,
                dry_run=dry_run,
                runtime_version=__version__,
            )
        elif refresh_managed_artifacts:
            paths = refresh_project_managed_artifacts(
                root,
                hooks=requested_hook_mode,
                agent_files=agent_files.value,
                agent_skill=agent_skill,
                runtime_version=__version__,
                runtime_mode=requested_runtime_mode,
                update_version_lock=update_version_lock,
                overwrite=overwrite,
                dry_run=dry_run,
            )
        else:
            paths = init_project(
                root,
                overwrite=overwrite,
                update_gitignore=update_gitignore,
                hooks=init_hook_mode,
                agent_files=agent_files.value,
                agent_skill=agent_skill,
                runtime_version=__version__,
                runtime_mode=requested_runtime_mode,
            )
    except (ConfigError, RepositoryRuntimeError, VersionLockError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if refresh_agent_artifacts or refresh_managed_artifacts:
        action = "would refresh" if dry_run else "refreshed"
        scope = "managed artifacts" if refresh_managed_artifacts else "agent artifacts"
        typer.echo(f"Apex Ray {scope} {action}: {root}")
        if not paths:
            typer.echo("- already current")
        for path in paths:
            typer.echo(f"- {path}")
        return
    typer.echo(f"Apex Ray ready: {root}")
    for path in paths:
        typer.echo(f"- {path}")
    runtime_status = inspect_repository_runtime(root, runtime_version=__version__)
    for message in _init_next_steps(
        init_hook_mode,
        source_runtime=runtime_status.mode == RepositoryRuntimeMode.SOURCE,
    ):
        typer.echo(message)


def _init_next_steps(hooks: str, *, source_runtime: bool = False) -> list[str]:
    messages = [
        "Next: inspect and commit Apex Ray setup files before reviewing application changes.",
    ]
    if hooks == "lefthook":
        if shutil.which("lefthook") is None:
            messages.append("Hook note: install Lefthook, then run `lefthook install` to activate pre-push review.")
        else:
            messages.append("Hook note: run `lefthook install` to activate pre-push review.")
    if hooks in {"lefthook", "git"}:
        if source_runtime:
            messages.append(
                "Hook note: generated hooks use `uv run --locked` to exercise this repository's current checkout."
            )
        else:
            messages.append(
                "Hook note: generated hooks use `uvx` to run the repository-pinned Apex Ray version; "
                "ensure `uv` is available on PATH for Git hooks."
            )
    return messages


@app.command()
def doctor(
    config: Annotated[Path | None, typer.Option("--config", help="Path to config file.")] = None,
) -> None:
    """Check local Apex Ray prerequisites and project discovery."""
    try:
        root = discover_repo_root(Path.cwd())
    except DiscoveryError as exc:
        raise typer.BadParameter(str(exc)) from exc
    runtime_status = inspect_repository_runtime(root, runtime_version=__version__)
    version_lock_status = runtime_status.version_lock
    typer.echo("Apex Ray doctor")
    typer.echo(f"- Version: {__version__}")
    typer.echo(f"- Runtime mode: {runtime_status.mode.value}")
    if runtime_status.mode == RepositoryRuntimeMode.SOURCE:
        typer.echo("- Version lock: not applicable (source checkout)")
    else:
        typer.echo(f"- Version lock: {version_lock_status.state.value}")
        if version_lock_status.locked_version is not None:
            typer.echo(f"- Required version: {version_lock_status.locked_version}")
    typer.echo(f"- Running version: {version_lock_status.runtime_version}")
    if runtime_status.reason:
        typer.echo(f"- Runtime detail: {runtime_status.reason}")
    elif version_lock_status.reason:
        typer.echo(f"- Version lock detail: {version_lock_status.reason}")
    if runtime_status.mode == RepositoryRuntimeMode.LEGACY:
        typer.echo(
            f"- Version lock remediation: {render_uvx_command(__version__, 'init', '--refresh-managed-artifacts')}"
        )
    uvx_available = shutil.which("uvx") is not None
    uv_available = shutil.which("uv") is not None
    source_pyproject = root / "pyproject.toml"
    source_lock = root / "uv.lock"
    source_pyproject_current = source_pyproject.is_file() and not source_pyproject.is_symlink()
    source_lock_current = source_lock.is_file() and not source_lock.is_symlink()
    source_lock_synchronized = False
    source_runtime_detail = ""
    if runtime_status.mode == RepositoryRuntimeMode.SOURCE:
        try:
            assert_source_uv_project(root)
        except RepositoryRuntimeError as exc:
            source_runtime_detail = str(exc)
        else:
            source_lock_synchronized = True
    if runtime_status.mode == RepositoryRuntimeMode.SOURCE:
        typer.echo(f"- uv available: {str(uv_available).lower()}")
        typer.echo(f"- Source pyproject.toml: {str(source_pyproject_current).lower()}")
        typer.echo(f"- Source uv.lock: {str(source_lock_current).lower()}")
        typer.echo(f"- Source uv.lock synchronized: {str(source_lock_synchronized).lower()}")
        typer.echo(f"- Source checkout: {'current' if runtime_status.source_checkout_current else 'mismatch'}")
        if source_runtime_detail:
            typer.echo(f"- Source runtime detail: {source_runtime_detail}")
    else:
        typer.echo(f"- uvx available: {str(uvx_available).lower()}")
    try:
        review_config, config_path = load_config(root, config)
    except ConfigError as exc:
        typer.echo(f"- Config: invalid ({exc})")
        raise typer.BadParameter(str(exc)) from exc

    try:
        project = discover_project(
            root,
            ignored_patterns=review_config.ignore,
            timeout_seconds=review_config.analyzer.timeout_seconds,
        )
    except DiscoveryError as exc:
        raise typer.BadParameter(str(exc)) from exc
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
    typer.echo(f"- Dart analyzer enabled: {str(review_config.analyzer.dart.enabled).lower()}")
    if not review_config.analyzer.dart.enabled:
        typer.echo("- Dart SDK command: skipped (analyzer disabled)")
        typer.echo("- Dart SDK source: disabled")
        typer.echo("- Dart SDK version: skipped")
        typer.echo("- Dart analyzer available: false")
    else:
        try:
            dart_toolchain = _resolve_dart_toolchain_for_doctor(root, review_config.analyzer.dart)
        except Exception as exc:
            typer.echo("- Dart SDK command: not found")
            typer.echo("- Dart SDK source: unavailable")
            typer.echo("- Dart SDK version: unavailable")
            typer.echo("- Dart analyzer available: false")
            typer.echo(f"- Dart SDK error: unable to inspect Dart SDK ({exc})")
            typer.echo(
                "- Dart remediation: Install Flutter or Dart, configure FVM, or set review.analyzer.dart.command."
            )
        else:
            command = json.dumps(dart_toolchain.command, ensure_ascii=False) if dart_toolchain.command else "not found"
            version = dart_toolchain.version or "unavailable"
            if version.startswith("Dart SDK version:"):
                version = version.removeprefix("Dart SDK version:").strip() or "unavailable"
            typer.echo(f"- Dart SDK command: {command}")
            typer.echo(f"- Dart SDK source: {dart_toolchain.source}")
            typer.echo(f"- Dart SDK version: {version}")
            dart_available = bool(dart_toolchain.command) and dart_toolchain.error is None
            typer.echo(f"- Dart analyzer available: {str(dart_available).lower()}")
            if dart_toolchain.error:
                typer.echo(f"- Dart SDK error: {dart_toolchain.error}")
            if dart_toolchain.remediation:
                typer.echo(f"- Dart remediation: {dart_toolchain.remediation}")
    typer.echo(f"- Node available: {str(shutil.which('node') is not None).lower()}")
    typer.echo(f"- TypeScript analyzer: {analyzer_script}")
    typer.echo(f"- TypeScript analyzer built: {str(analyzer_script.exists()).lower()}")
    launcher = runtime_status.launcher
    hook_statuses = (
        managed_hook_statuses(
            root,
            runtime_version=version_lock_status.runtime_version,
            launcher=launcher,
        )
        if launcher is not None
        else []
    )
    stale_hooks = [status for status in hook_statuses if status.needs_refresh]
    duplicate_hook_modes = len({status.kind for status in hook_statuses}) > 1
    if launcher is None:
        typer.echo("- Managed hooks: not checked (repository runtime is invalid)")
    elif not hook_statuses:
        typer.echo("- Managed hooks: not found")
    elif duplicate_hook_modes:
        typer.echo("- Managed hooks: inconsistent (multiple Apex Ray hook modes)")
        for status in hook_statuses:
            typer.echo(f"  - {status.path}: {status.kind} hook is also configured")
        typer.echo(f"  Run: remove one hook mode, then run {render_apex_ray_command(['doctor'], launcher=launcher)}")
    elif stale_hooks:
        typer.echo(f"- Managed hooks: inconsistent ({len(stale_hooks)})")
        for status in stale_hooks:
            detail = status.reason or f"found {status.actual_command!r}"
            typer.echo(f"  - {status.path}: {detail}")
        typer.echo(f"  Run: {render_apex_ray_command(['init', '--refresh-managed-artifacts'], launcher=launcher)}")
    else:
        typer.echo("- Managed hooks: current")
    if runtime_status.blocking:
        detail = (
            "rerun doctor with the repository-pinned version"
            if runtime_status.mode == RepositoryRuntimeMode.LOCKED
            else "repository runtime is invalid or not the current source checkout"
        )
        typer.echo(f"- Agent artifacts: not checked ({detail})")
    else:
        try:
            artifact_statuses = agent_artifact_statuses(
                root,
                runtime_version=version_lock_status.runtime_version,
                launcher=launcher,
            )
        except (ConfigError, RepositoryRuntimeError) as exc:
            typer.echo(f"- Agent artifacts: unable to inspect ({exc})")
        else:
            managed_statuses = [status for status in artifact_statuses if status.status != "unmanaged"]
            outdated_statuses = [status for status in managed_statuses if status.status != "current"]
            if not managed_statuses:
                typer.echo("- Agent artifacts: not found")
            elif outdated_statuses:
                typer.echo(f"- Agent artifacts: outdated ({len(outdated_statuses)})")
                for status in outdated_statuses[:5]:
                    typer.echo(f"  - {status.path}: {status.reason}")
                typer.echo(
                    f"  Run: {render_apex_ray_command(['init', '--refresh-agent-artifacts'], launcher=launcher)}"
                )
            else:
                typer.echo("- Agent artifacts: current")
    source_prerequisites_invalid = runtime_status.mode == RepositoryRuntimeMode.SOURCE and (
        not uv_available or not source_pyproject_current or not source_lock_current or not source_lock_synchronized
    )
    launcher_unavailable = bool(hook_statuses) and (
        (runtime_status.mode == RepositoryRuntimeMode.SOURCE and not uv_available)
        or (runtime_status.mode != RepositoryRuntimeMode.SOURCE and not uvx_available)
    )
    if (
        runtime_status.blocking
        or source_prerequisites_invalid
        or stale_hooks
        or duplicate_hook_modes
        or launcher_unavailable
    ):
        raise typer.Exit(code=1)


def _python_analyzer_available() -> bool:
    try:
        from apex_ray.analyzers.python import run_python_analyzer
    except Exception:
        return False
    return callable(run_python_analyzer)


def _resolve_dart_toolchain_for_doctor(
    root: Path,
    config: DartAnalyzerConfig,
) -> _DartToolchainResolution:
    from apex_ray.analyzers.dart.toolchain import resolve_dart_toolchain

    return resolve_dart_toolchain(root, config, probe_version=True, timeout_seconds=2.0)


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
    auto_followup_max_pack_reviews: Annotated[
        int,
        typer.Option(
            "--auto-followup-max-pack-reviews",
            min=1,
            help="Maximum reviewer-pack assignments in the automatic P0 follow-up.",
        ),
    ] = DEFAULT_AUTO_FOLLOWUP_P0_MAX_PACK_REVIEWS,
    until_complete: Annotated[
        bool,
        typer.Option(
            "--until-complete",
            help="Continue one explicit/baseline reviewer scope in bounded batches until coverage is complete.",
        ),
    ] = False,
    strict_coverage: Annotated[
        bool,
        typer.Option(
            "--strict-coverage",
            help="Require complete coverage after bounded continuation; reports are still written on failure.",
        ),
    ] = False,
    followup_max_pack_reviews: Annotated[
        int,
        typer.Option(
            "--followup-max-pack-reviews",
            min=1,
            help="Maximum reviewer-pack assignments in each coverage-completion batch.",
        ),
    ] = DEFAULT_AUTO_FOLLOWUP_P0_MAX_PACK_REVIEWS,
    max_followup_passes: Annotated[
        int,
        typer.Option(
            "--max-followup-passes",
            min=1,
            help="Maximum bounded coverage-completion batches.",
        ),
    ] = 8,
    output: Annotated[Path, typer.Option("--output", help="Markdown report path.")] = Path(
        ".apex-ray/reports/review.md"
    ),
    json_output: Annotated[Path, typer.Option("--json", help="JSON report path.")] = Path(
        ".apex-ray/reports/review.json"
    ),
    html_output: Annotated[Path | None, typer.Option("--html", help="Optional HTML report path.")] = None,
    sarif_output: Annotated[
        Path | None,
        typer.Option("--sarif", help="Optional SARIF 2.1.0 report path."),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Path to config file.")] = None,
    reviewer: Annotated[
        list[str] | None,
        typer.Option("--reviewer", help="Run only this configured reviewer. May be repeated."),
    ] = None,
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
    llm_max_packs: Annotated[
        int | None,
        typer.Option(
            "--llm-max-packs",
            min=1,
            help="Maximum context packs for every configured reviewer pass.",
        ),
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
    launcher: ApexRayLauncher | None = None
    try:
        root = discover_repo_root(Path.cwd())
    except DiscoveryError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if continue_from is None:
        try:
            runtime_status = assert_repository_runtime(root, runtime_version=__version__)
            launcher = runtime_status.launcher
            review_config, config_path = load_config(root, config)
        except (ConfigError, RepositoryRuntimeError) as exc:
            raise typer.BadParameter(str(exc)) from exc
    else:
        review_config = ReviewConfig()
        config_path = None

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
    completion_requested = until_complete or strict_coverage
    if completion_requested and auto_followup:
        raise typer.BadParameter("Use --until-complete/--strict-coverage or --auto-followup, not both.")
    if completion_requested and (only_pack or residual_priority or only_slice):
        raise typer.BadParameter(
            "Coverage completion cannot be combined with --only-pack, --residual-priority, or --only-slice."
        )
    if completion_requested and no_llm:
        raise typer.BadParameter("Coverage completion requires LLM review; do not use --no-llm.")
    prior_report = None
    if continue_from is not None:
        try:
            prior_report = load_review_report(continue_from)
        except OSError as exc:
            raise typer.BadParameter(f"Unable to read report {continue_from}: {exc}") from exc
        except ReviewReportLoadError as exc:
            raise typer.BadParameter(str(exc)) from exc
        root = Path(prior_report.project.root)
        try:
            runtime_status = assert_repository_runtime(root, runtime_version=__version__)
            launcher = runtime_status.launcher
        except RepositoryRuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if prior_report.input_snapshot is None:
            if completion_requested:
                raise typer.BadParameter(
                    "Coverage completion requires a report with a review-input snapshot; run a fresh review."
                )
            typer.echo(
                "Warning: legacy report has no review-input snapshot; continuing its archived context packs "
                "without validating the live Git target.",
                err=True,
            )
        else:
            try:
                snapshot_status = _validate_report_input_snapshot(prior_report, root)
            except ReviewInputSnapshotError as exc:
                raise typer.BadParameter(f"Cannot continue stale review report: {exc}") from exc
            if snapshot_status == "detached":
                typer.echo(
                    "Continuing an immutable --diff report snapshot; live Git state is not part of this target.",
                    err=True,
                )
        if config is None:
            review_config = prior_report.config
        else:
            try:
                review_config, config_path = load_config(root, config)
            except ConfigError as exc:
                raise typer.BadParameter(str(exc)) from exc
    warn_outdated_agent_artifacts(root)

    output = resolve_output_path(root, output)
    json_output = resolve_output_path(root, json_output)
    html_output = resolve_output_path(root, html_output) if html_output is not None else None
    sarif_output = resolve_output_path(root, sarif_output) if sarif_output is not None else None
    ensure_distinct_outputs(output, json_output, html_output, sarif_output)
    saved_target_mode = TargetMode(prior_report.diff.target_mode) if prior_report is not None else None
    worktree_target = worktree or saved_target_mode == TargetMode.WORKTREE
    try:
        if worktree_target and git.is_git_repo(root):
            _validate_worktree_output_paths(root, output, json_output, html_output, sarif_output)
        elif not worktree_target:
            ensure_apex_ignore_for_outputs(root, output, json_output, html_output, sarif_output)
        if prior_report is not None and prior_report.input_snapshot is not None:
            _validate_report_input_snapshot(prior_report, root)
    except (ConfigError, ReviewInputSnapshotError, git.GitError) as exc:
        raise typer.BadParameter(str(exc)) from exc

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
            max_packs=llm_max_packs,
            max_deep_packs=llm_max_deep_packs,
            max_input_tokens=llm_max_input_tokens,
            analyzer_cache_allowed=analyzer_cache,
            refresh_analyzer_cache=refresh_analyzer_cache,
            analyzer_cache_dir=analyzer_cache_dir,
        ),
    )
    try:
        if reviewer:
            effective_reviewers(effective_config.reviewers, reviewer)
    except ReviewerConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if completion_requested and not effective_config.llm.enabled:
        raise typer.BadParameter("Coverage completion requires --llm or review.llm.enabled: true.")
    try:
        completion_reviewer_ids = (
            resolve_completion_reviewer_scope(effective_config, reviewer) if completion_requested else reviewer
        )
    except CoverageScopeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    report_config = effective_config.model_copy(deep=True)
    try:
        effective_config = resolve_runtime_config_paths(root, effective_config)
        if prior_report is not None:
            prior_report = prior_report.model_copy(deep=True)
            prior_report.config = resolve_runtime_config_paths(root, prior_report.config)
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
    if worktree_target and git.is_git_repo(root):
        worktree_directory_paths: list[Path] = []
        worktree_file_paths: list[Path] = []
        if effective_config.llm.enabled:
            llm_cache = cache_for_config(root, effective_config.llm)
            if llm_cache is not None:
                worktree_directory_paths.append(llm_cache.root)
        if prior_report is None:
            worktree_directory_paths.extend(_analyzer_cache_write_roots(root, effective_config))
        if effective_config.reports.archive:
            worktree_directory_paths.append(Path(effective_config.reports.archive_dir))
        if telemetry_enabled:
            worktree_file_paths.append(effective_telemetry_path)
        try:
            _validate_worktree_output_paths(root, *worktree_directory_paths, directory=True)
            _validate_worktree_output_paths(root, *worktree_file_paths)
        except git.GitError as exc:
            raise typer.BadParameter(str(exc)) from exc

    started_monotonic = time.monotonic()
    try:
        if prior_report is not None and completion_requested:
            report = prior_report
        elif prior_report is not None:
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
                max_pack_reviews=_continuation_pack_review_cap(effective_config, reviewer),
                respect_config_budgets=True,
                review_depth=continue_review_depth,  # type: ignore[arg-type]
                reviewer_ids=reviewer,
            )
            if not selected_packs:
                typer.echo(
                    "No continuation context packs were eligible within the requested filters and configured budgets."
                )
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
                reviewer_ids=completion_reviewer_ids if completion_requested else reviewer,
            )
            if auto_followup and report.llm_coverage.partial_severity == "critical":
                report, selected_packs = continue_review_from_report(
                    report,
                    repo_root=root,
                    config=effective_config,
                    residual_priorities={"p0"},
                    only_unreviewed=True,
                    max_pack_reviews=auto_followup_max_pack_reviews,
                    review_depth="deep",
                    reviewer_ids=reviewer,
                )
                if selected_packs:
                    typer.echo(f"Auto-followup reviewed {len(selected_packs)} residual P0 context pack(s).")
        completion_result = None
        if completion_requested:

            def persist_completion_batch(batch_report: ReviewReport, _batch: int) -> None:
                _write_intermediate_review_report(
                    batch_report,
                    report_config=report_config,
                    output=output,
                    json_output=json_output,
                    launcher=launcher,
                )

            completion_result = continue_review_until_complete(
                report,
                repo_root=root,
                config=effective_config,
                reviewer_ids=completion_reviewer_ids,
                batch_size=followup_max_pack_reviews,
                max_batches=max_followup_passes,
                on_batch=persist_completion_batch,
            )
            report = completion_result.report
            report.coverage_completion = ReviewCoverageCompletion(
                status="complete" if completion_result.complete else "incomplete",
                reviewer_ids=completion_reviewer_ids or [],
                batches=completion_result.batches,
                stop_reason=completion_result.stop_reason,
            )
        if TargetMode(report.diff.target_mode) != TargetMode.WORKTREE:
            ensure_apex_ignore_for_outputs(root, output, json_output, html_output, sarif_output)
        if report.input_snapshot is not None:
            _validate_report_input_snapshot(report, root)
    except (DiscoveryError, LLMProviderError, ReviewInputSnapshotError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    duration_ms = round((time.monotonic() - started_monotonic) * 1000)
    report.config = report_config

    set_continue_commands(report, str(json_output), launcher=launcher)

    markdown_text = render_markdown(report)
    json_text = report.model_dump_json(indent=2)
    html_text = render_html(report) if html_output else None
    sarif_text = render_sarif(report) if sarif_output else None
    atomic_write_text(output, markdown_text)
    atomic_write_text(json_output, json_text)

    if html_output:
        atomic_write_text(html_output, html_text or "")
    if sarif_output:
        atomic_write_text(sarif_output, sarif_text or "")

    artifacts = [
        ReportArtifact(output, markdown_text),
        ReportArtifact(json_output, json_text),
    ]
    if html_output and html_text is not None:
        artifacts.append(ReportArtifact(html_output, html_text))
    if sarif_output and sarif_text is not None:
        artifacts.append(ReportArtifact(sarif_output, sarif_text))
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

    _render_review_stdout_summary(
        report,
        output=output,
        json_output=json_output,
        html_output=html_output,
        sarif_output=sarif_output,
        completion_reviewer_ids=completion_reviewer_ids,
        launcher=launcher,
    )
    if completion_result is not None:
        if completion_result.complete:
            typer.echo(f"Coverage completion: COMPLETE after {completion_result.batches} follow-up batch(es).")
        else:
            typer.echo(f"Coverage completion: INCOMPLETE ({completion_result.stop_reason}).")
    typer.echo(f"Wrote {output}")
    typer.echo(f"Wrote {json_output}")
    if html_output:
        typer.echo(f"Wrote {html_output}")
    if sarif_output:
        typer.echo(f"Wrote {sarif_output}")
    if archive_path:
        typer.echo(f"Archived report: {archive_path}")
    if telemetry_enabled:
        typer.echo(f"Appended telemetry: {effective_telemetry_path}")
    if strict_coverage and completion_result is not None and not completion_result.complete:
        raise typer.Exit(code=1)


def _render_review_stdout_summary(
    report: ReviewReport,
    *,
    output: Path,
    json_output: Path,
    html_output: Path | None,
    sarif_output: Path | None,
    completion_reviewer_ids: list[str] | None,
    launcher: ApexRayLauncher | None,
) -> None:
    completion_status = report.llm_coverage.completion_status
    headline = {
        "disabled": "LLM DISABLED",
        "complete": "COMPLETE",
        "partial": "PARTIAL COVERAGE",
        "incomplete": "INCOMPLETE REVIEW",
    }[completion_status]
    typer.echo(f"APEX RAY REVIEW: {headline}")
    for line in render_coverage_summary_lines(report.llm_coverage):
        typer.echo(line)
    typer.echo(f"Findings: {len(report.findings)} in reviewed scope")
    if completion_status in {"partial", "incomplete"}:
        scopes = _completion_command_scopes(report, completion_reviewer_ids)
        for scope in scopes:
            args = [
                "review",
                "--continue-from",
                str(json_output),
                "--until-complete",
                "--llm",
                "--output",
                str(output),
                "--json",
                str(json_output),
            ]
            if html_output is not None:
                args.extend(["--html", str(html_output)])
            if sarif_output is not None:
                args.extend(["--sarif", str(sarif_output)])
            for reviewer_id in scope:
                args.extend(["--reviewer", reviewer_id])
            command = render_apex_ray_command(args, launcher=launcher)
            if not scope:
                label = "Continue"
            elif len(scope) == 1:
                label = f"Continue reviewer {scope[0]}"
            else:
                label = f"Continue reviewers {', '.join(scope)}"
            typer.echo(f"{label}: {command}")
    typer.echo()


def _completion_command_scopes(
    report: ReviewReport,
    requested_reviewer_ids: list[str] | None,
) -> list[list[str]]:
    if requested_reviewer_ids is not None:
        return [list(dict.fromkeys(requested_reviewer_ids))]

    reviewers = effective_reviewers(report.config.reviewers)
    reviewer_order = {reviewer.id: index for index, reviewer in enumerate(reviewers)}
    summaries = {
        summary.reviewer_id: summary
        for summary in report.llm_coverage.reviewers
        if summary.reviewer_id in reviewer_order
    }
    debt_reviewer_ids = {
        todo.reviewer_id for todo in report.llm_coverage.coverage_todos if todo.reviewer_id in reviewer_order
    }
    debt_reviewer_ids.update(
        summary.reviewer_id
        for summary in summaries.values()
        if summary.reviewed_context_packs < summary.matching_context_packs or summary.status in {"warn", "fail"}
    )
    for pack_id in report.llm_coverage.shallow_only_high_risk_context_pack_ids:
        matching = [summary for summary in summaries.values() if pack_id in summary.matching_context_pack_ids]
        if matching:
            selected = min(
                matching,
                key=lambda summary: (
                    not summary.required,
                    reviewer_order[summary.reviewer_id],
                ),
            )
            debt_reviewer_ids.add(selected.reviewer_id)
    if debt_reviewer_ids:
        return [[reviewer_id] for reviewer_id in reviewer_order if reviewer_id in debt_reviewer_ids]

    if not report.config.reviewers:
        return [[]]
    required = [reviewer.id for reviewer in reviewers if reviewer.required]
    if len(required) == 1:
        return [required]
    if len(reviewers) == 1:
        return [[reviewers[0].id]]
    return [[reviewer.id] for reviewer in reviewers]


def _write_intermediate_review_report(
    report: ReviewReport,
    *,
    report_config: ReviewConfig,
    output: Path,
    json_output: Path,
    launcher: ApexRayLauncher | None,
) -> None:
    persisted = report.model_copy(deep=True)
    persisted.config = report_config
    set_continue_commands(persisted, str(json_output), launcher=launcher)
    atomic_write_text(output, render_markdown(persisted))
    atomic_write_text(json_output, persisted.model_dump_json(indent=2))


def _validate_report_input_snapshot(report: ReviewReport, root: Path) -> str:
    if report.input_snapshot is None:  # pragma: no cover - callers guard legacy reports
        raise ReviewInputSnapshotError("saved report has no review-input snapshot; run a fresh review")
    return validate_review_input_snapshot(
        report.input_snapshot,
        root,
        expected_target_mode=TargetMode(report.diff.target_mode),
        expected_base_ref=report.diff.base,
    )


def _validate_worktree_output_paths(
    root: Path,
    *paths: Path | None,
    directory: bool = False,
) -> None:
    for path in paths:
        if path is not None and not git.worktree_output_path_is_stable(root, path, directory=directory):
            display_path = _display_worktree_output_path(root, path)
            raise typer.BadParameter(
                f"Worktree review writable path {display_path} must be outside the repository or Git-ignored and "
                "untracked; "
                "run apex-ray init and commit .apex-ray/.gitignore, use another already ignored path, "
                "or use an external path."
            )


def _display_worktree_output_path(root: Path, path: Path) -> Path:
    """Prefer a concise repository-relative label for an unsafe path."""

    if not path.is_absolute():
        return path
    try:
        return path.resolve(strict=False).relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):  # fmt: skip
        return path


def _analyzer_cache_write_roots(root: Path, config: ReviewConfig) -> list[Path]:
    """Return every cache root the bundled TS/Dart analyzers may write."""

    if not config.analyzer.index_cache_enabled:
        return []
    configured = config.analyzer.index_cache_dir
    if configured:
        raw = Path(configured)
        return _unique_paths(
            [
                _absolute_from(raw, root),
                _absolute_from(raw.expanduser(), root),
            ]
        )

    repo_hash = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    explicit_home = os.environ.get("APEX_RAY_CACHE_HOME")
    xdg_home = os.environ.get("XDG_CACHE_HOME")
    if explicit_home and explicit_home.strip():
        raw = Path(explicit_home)
        typescript_home = _absolute_from(raw, root)
        dart_home = _absolute_from(raw.expanduser(), Path.cwd())
    elif xdg_home and xdg_home.strip():
        raw = Path(xdg_home)
        typescript_home = _absolute_from(raw, root) / "apex-ray"
        dart_home = _absolute_from(raw.expanduser(), Path.cwd()) / "apex-ray"
    else:
        typescript_home = _typescript_default_cache_home(root)
        try:
            raw = Path.home() / ".cache" / "apex-ray"
            dart_home = _absolute_from(raw, Path.cwd())
        except RuntimeError:  # pragma: no cover - platform-specific missing home
            dart_home = Path(tempfile.gettempdir()) / "apex-ray-cache"
    return _unique_paths(
        [
            typescript_home / "repos" / repo_hash / "typescript",
            dart_home / "repos" / repo_hash / "dart",
        ]
    )


def _continuation_pack_review_cap(config: ReviewConfig, reviewer_ids: list[str] | None) -> int | None:
    reviewers = effective_reviewers(config.reviewers, reviewer_ids)
    if not reviewers:
        return None
    return sum(llm_config_for_reviewer(config.llm, reviewer).max_packs for reviewer in reviewers)


def _typescript_default_cache_home(root: Path) -> Path:
    raw_home: Path | None = None
    if os.name != "nt" and "HOME" in os.environ:
        home_value = os.environ["HOME"]
        if home_value.strip():
            raw_home = Path(home_value)
    else:
        try:
            raw_home = Path.home()
        except RuntimeError:  # pragma: no cover - platform-specific missing home
            pass
    if raw_home is not None:
        return _absolute_from(raw_home, root) / ".cache" / "apex-ray"
    return _absolute_from(_node_temp_dir(), root) / "apex-ray-cache"


def _node_temp_dir() -> Path:
    environment_names = ("TEMP", "TMP") if os.name == "nt" else ("TMPDIR", "TMP", "TEMP")
    for name in environment_names:
        value = os.environ.get(name)
        if value:
            return Path(value)
    if os.name == "nt":  # pragma: no cover - platform-specific fallback
        system_root = os.environ.get("SystemRoot") or os.environ.get("windir")
        if system_root:
            return Path(system_root) / "temp"
    return Path(tempfile.gettempdir())


def _absolute_from(path: Path, base: Path) -> Path:
    return path if path.is_absolute() else base / path


def _unique_paths(paths: list[Path]) -> list[Path]:
    return list(dict.fromkeys(Path(os.path.abspath(path)) for path in paths))


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
