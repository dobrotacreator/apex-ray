import shutil
import time
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from apex_ray import __version__, git
from apex_ray.analyzers import typescript_analyzer_script
from apex_ray.benchmark import (
    BenchmarkError,
    benchmark_comparison_gate_failures,
    capture_benchmark_case,
    compare_benchmark_reports,
    load_benchmark_report,
    render_benchmark_comparison,
    render_benchmark_report,
    run_benchmark_cases,
)
from apex_ray.config import ConfigError, find_local_config, init_project, load_config
from apex_ray.discovery import discover_project
from apex_ray.invocation import ReviewOverrides, apply_review_overrides
from apex_ray.llm import LLMProviderError
from apex_ray.memory import memory_suggestions_from_report
from apex_ray.models import LLMCoverageMode, LLMProviderName, ReviewReport, TargetMode
from apex_ray.pipeline import continue_review_from_report, run_review_pipeline
from apex_ray.pr_eval import (
    DEFAULT_FIRST_PASS_WINDOW_MINUTES,
    DEFAULT_LABELS_DIR,
    DEFAULT_TELEMETRY_PATH,
    PrEvalError,
    capture_pr_eval_cases,
    load_pr_eval_run_report,
    load_pr_eval_telemetry,
    memory_suggestions_from_pr_eval_report,
    render_pr_eval_telemetry_summary,
    run_pr_eval_cases,
    write_pr_eval_label_templates,
)
from apex_ray.report import render_html, render_markdown
from apex_ray.telemetry import (
    DEFAULT_REVIEW_TELEMETRY_PATH,
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
memory_app = typer.Typer(help="Inspect and maintain repo-committed Apex Ray memory.")
app.add_typer(memory_app, name="memory")
eval_app = typer.Typer(help="Capture and replay historical PR review evals.")
app.add_typer(eval_app, name="eval")


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
        str,
        typer.Option("--hooks", help="Hook setup mode: lefthook, git, or none."),
    ] = "lefthook",
    agent_files: Annotated[
        str,
        typer.Option("--agent-files", help="Agent instruction files: both, codex, claude, or none."),
    ] = "both",
    update_gitignore: Annotated[
        bool,
        typer.Option("--update-gitignore/--no-update-gitignore", help="Add Apex Ray outputs to root .gitignore."),
    ] = True,
) -> None:
    """Create project Apex Ray config, ignores, hooks, and agent instructions."""
    root = git.repo_root(Path.cwd()) or Path.cwd()
    try:
        paths = init_project(
            root,
            overwrite=overwrite,
            update_gitignore=update_gitignore,
            hooks=hooks,
            agent_files=agent_files,
        )
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Apex Ray ready: {root}")
    for path in paths:
        typer.echo(f"- {path}")


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
    typer.echo(f"- Node available: {str(shutil.which('node') is not None).lower()}")
    typer.echo(f"- TypeScript analyzer: {analyzer_script}")
    typer.echo(f"- TypeScript analyzer built: {str(analyzer_script.exists()).lower()}")


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
) -> None:
    """Draft curated memory cards from a review JSON report."""
    try:
        report = ReviewReport.model_validate_json(from_report.read_text(encoding="utf-8"))
    except OSError as exc:
        raise typer.BadParameter(f"Unable to read report {from_report}: {exc}") from exc
    except ValidationError as exc:
        raise typer.BadParameter(f"Invalid Apex Ray report {from_report}: {exc}") from exc

    suggestions = memory_suggestions_from_report(report)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(suggestions, encoding="utf-8")
        typer.echo(f"Wrote {output}")
    else:
        typer.echo(suggestions)


@eval_app.command("capture-prs")
def eval_capture_prs(
    repo: Annotated[Path, typer.Option("--repo", help="Source git repository with GitHub remote.")],
    output: Annotated[Path, typer.Option("--output", help="Output PR eval cases directory.")],
    pr: Annotated[list[int] | None, typer.Option("--pr", help="Specific PR number to capture.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Number of latest merged PRs to capture.")] = 10,
    first_window_minutes: Annotated[
        int,
        typer.Option(
            "--first-window-minutes", min=1, help="Minutes after first Greptile comment treated as first pass."
        ),
    ] = DEFAULT_FIRST_PASS_WINDOW_MINUTES,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Allow writing into a non-empty output directory.")
    ] = False,
) -> None:
    """Capture PR diffs and first-pass Greptile findings for replay."""
    try:
        result = capture_pr_eval_cases(
            source_repo=repo,
            output_dir=output,
            pr_numbers=pr,
            limit=limit,
            first_pass_window_minutes=first_window_minutes,
            overwrite=overwrite,
        )
    except PrEvalError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Wrote {result.output_dir}")
    typer.echo(f"Captured {len(result.cases)} PR eval case(s)")
    for case in result.cases:
        first_pass = sum(1 for finding in case.greptile_findings if finding.first_pass)
        typer.echo(f"- PR #{case.number}: {first_pass} first-pass Greptile finding(s)")
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}")


@eval_app.command("run-prs")
def eval_run_prs(
    repo: Annotated[Path, typer.Option("--repo", help="Source git repository used to create temporary worktrees.")],
    cases: Annotated[Path, typer.Option("--cases", help="PR eval cases directory from capture-prs.")],
    output: Annotated[Path, typer.Option("--output", help="Output PR eval run directory.")],
    pr: Annotated[list[int] | None, typer.Option("--pr", help="Specific PR number to run.")] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1, help="Optional number of cases to run.")] = None,
    llm: Annotated[bool, typer.Option("--llm", help="Run LLM review for replayed PRs.")] = False,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Disable LLM review for replayed PRs.")] = False,
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
    analyzer_timeout: Annotated[
        int | None,
        typer.Option("--analyzer-timeout", min=1, help="Override analyzer timeout seconds for replay worktrees."),
    ] = None,
    allow_extra_findings: Annotated[
        bool,
        typer.Option("--allow-extra-findings", help="Use a recall-only pass gate; still report extra Apex findings."),
    ] = False,
    labels_dir: Annotated[
        Path | None,
        typer.Option("--labels-dir", help="Optional PR eval labels directory for triage feedback."),
    ] = None,
    telemetry: Annotated[bool, typer.Option("--telemetry", help="Append aggregate PR eval telemetry JSONL.")] = False,
    telemetry_path: Annotated[
        Path | None,
        typer.Option(
            "--telemetry-path", help="Telemetry JSONL path. Defaults to .apex-ray/eval/telemetry/pr-eval-runs.jsonl."
        ),
    ] = None,
    resume: Annotated[
        bool, typer.Option("--resume", help="Skip completed PR eval cases with terminal status artifacts.")
    ] = False,
    case_jobs: Annotated[int, typer.Option("--case-jobs", min=1, help="Concurrent PR eval cases.")] = 1,
    case_timeout: Annotated[
        int | None,
        typer.Option("--case-timeout", min=1, help="Maximum seconds per PR eval case before it is marked timed_out."),
    ] = None,
) -> None:
    """Replay captured PR eval cases and compare Apex Ray findings with Greptile findings."""
    if llm and no_llm:
        raise typer.BadParameter("Use only one of --llm or --no-llm.")
    if verify and no_verify:
        raise typer.BadParameter("Use only one of --verify or --no-verify.")
    if refresh_cache and not cache:
        raise typer.BadParameter("Use --refresh-cache only when cache is enabled.")
    provider_override = None
    if llm_provider:
        try:
            provider_override = LLMProviderName(llm_provider)
        except ValueError as exc:
            raise typer.BadParameter(f"Unsupported LLM provider: {llm_provider}") from exc
    verify_override = True if verify else False if no_verify else None
    telemetry_enabled = telemetry or telemetry_path is not None

    try:
        report = run_pr_eval_cases(
            source_repo=repo,
            cases_dir=cases,
            output_dir=output,
            pr_numbers=pr,
            llm_enabled=llm and not no_llm,
            provider_override=provider_override,
            model_override=llm_model,
            verify_override=verify_override,
            cache_enabled=cache,
            refresh_cache=refresh_cache,
            cache_dir=cache_dir,
            llm_jobs=llm_jobs,
            llm_coverage_mode=llm_coverage_mode,
            llm_max_deep_packs=llm_max_deep_packs,
            llm_max_input_tokens=llm_max_input_tokens,
            analyzer_timeout_seconds=analyzer_timeout,
            allow_extra_findings=allow_extra_findings,
            labels_dir=labels_dir,
            telemetry_path=(telemetry_path or Path(DEFAULT_TELEMETRY_PATH)) if telemetry_enabled else None,
            limit=limit,
            resume=resume,
            case_jobs=case_jobs,
            case_timeout_seconds=case_timeout,
        )
    except PrEvalError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except LLMProviderError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Wrote {output / 'pr-eval-report.md'}")
    typer.echo(f"Wrote {output / 'pr-eval-report.json'}")
    typer.echo(
        "Matched Greptile findings: "
        f"{report.matched_greptile_findings_total}/{report.greptile_findings_total}; "
        f"extra Apex findings: {report.extra_apex_findings_total}"
    )
    if report.failed:
        raise typer.Exit(code=1)


@eval_app.command("suggest-memory")
def eval_suggest_memory(
    from_run: Annotated[
        Path,
        typer.Option("--from-run", help="PR eval run directory or pr-eval-report.json file."),
    ],
    output: Annotated[Path | None, typer.Option("--output", help="Optional markdown output path.")] = None,
) -> None:
    """Draft repo memory cards from missed first-pass Greptile findings."""
    try:
        report = load_pr_eval_run_report(from_run)
    except PrEvalError as exc:
        raise typer.BadParameter(str(exc)) from exc

    suggestions = memory_suggestions_from_pr_eval_report(report)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(suggestions, encoding="utf-8")
        typer.echo(f"Wrote {output}")
    else:
        typer.echo(suggestions)


@eval_app.command("init-labels")
def eval_init_labels(
    from_run: Annotated[
        Path,
        typer.Option("--from-run", help="PR eval run directory or pr-eval-report.json file."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="Output labels directory."),
    ] = Path(DEFAULT_LABELS_DIR),
    overwrite: Annotated[bool, typer.Option("--overwrite", help="Overwrite existing label files.")] = False,
) -> None:
    """Create repo-committable triage label templates from a PR eval run."""
    try:
        report = load_pr_eval_run_report(from_run)
        paths = write_pr_eval_label_templates(report, output, overwrite=overwrite)
    except PrEvalError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Wrote {len(paths)} label file(s) to {output}")
    if not paths and not overwrite:
        typer.echo("No files changed; pass --overwrite to regenerate existing labels.")


@eval_app.command("telemetry-summary")
def eval_telemetry_summary(
    telemetry_path: Annotated[
        Path,
        typer.Option("--telemetry-path", help="Telemetry JSONL path."),
    ] = Path(DEFAULT_TELEMETRY_PATH),
) -> None:
    """Summarize long-lived PR replay telemetry."""
    try:
        entries = load_pr_eval_telemetry(telemetry_path)
    except PrEvalError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(render_pr_eval_telemetry_summary(entries))


@app.command("telemetry-summary")
def review_telemetry_summary(
    telemetry_path: Annotated[
        Path,
        typer.Option("--telemetry-path", help="Review telemetry JSONL path."),
    ] = Path(DEFAULT_REVIEW_TELEMETRY_PATH),
) -> None:
    """Summarize long-lived local review telemetry."""
    root = git.repo_root(Path.cwd()) or Path.cwd()
    path = telemetry_path if telemetry_path.is_absolute() else root / telemetry_path
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
    output: Annotated[Path, typer.Option("--output", help="Markdown report path.")] = Path("review.md"),
    json_output: Annotated[Path, typer.Option("--json", help="JSON report path.")] = Path("review.json"),
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
        typer.Option("--analyzer-cache/--no-analyzer-cache", help="Use the TS/JS analyzer repo index cache."),
    ] = True,
    refresh_analyzer_cache: Annotated[
        bool,
        typer.Option("--refresh-analyzer-cache", help="Refresh the TS/JS analyzer repo index cache."),
    ] = False,
    analyzer_cache_dir: Annotated[
        Path | None,
        typer.Option("--analyzer-cache-dir", help="TS/JS analyzer index cache directory."),
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
    _ensure_distinct_outputs(output, json_output, html_output)

    prior_report = None
    if continue_from is not None:
        try:
            prior_report = ReviewReport.model_validate_json(continue_from.read_text(encoding="utf-8"))
        except OSError as exc:
            raise typer.BadParameter(f"Unable to read report {continue_from}: {exc}") from exc
        except ValidationError as exc:
            raise typer.BadParameter(f"Invalid Apex Ray report {continue_from}: {exc}") from exc
        root = Path(prior_report.project.root)
        review_config = prior_report.config

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
    telemetry_enabled = (
        effective_config.telemetry.enabled or telemetry or telemetry_path is not None
    ) and not no_telemetry
    effective_telemetry_path = telemetry_path or Path(effective_config.telemetry.path)
    if not effective_telemetry_path.is_absolute():
        effective_telemetry_path = root / effective_telemetry_path

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

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(report), encoding="utf-8")

    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    if html_output:
        html_output.parent.mkdir(parents=True, exist_ok=True)
        html_output.write_text(render_html(report), encoding="utf-8")

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
    if telemetry_enabled:
        typer.echo(f"Appended telemetry: {effective_telemetry_path}")


@app.command()
def benchmark(
    cases: Annotated[list[Path], typer.Argument(help="Benchmark YAML case files.")],
    output: Annotated[Path, typer.Option("--output", help="Markdown benchmark report path.")] = Path("benchmark.md"),
    json_output: Annotated[Path, typer.Option("--json", help="JSON benchmark report path.")] = Path("benchmark.json"),
    llm: Annotated[bool, typer.Option("--llm", help="Enable LLM review for cases that do not override it.")] = False,
    llm_provider: Annotated[str | None, typer.Option("--llm-provider", help="Override LLM provider.")] = None,
    llm_jobs: Annotated[
        int | None, typer.Option("--llm-jobs", min=1, help="Concurrent LLM pack/verifier jobs.")
    ] = None,
    verify: Annotated[bool, typer.Option("--verify", help="Enable verifier pass for all cases.")] = False,
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Disable verifier pass for all cases.")] = False,
    cache: Annotated[bool, typer.Option("--cache/--no-cache", help="Use the LLM response cache.")] = True,
    refresh_cache: Annotated[bool, typer.Option("--refresh-cache", help="Refresh cached LLM responses.")] = False,
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir", help="LLM cache directory.")] = None,
    jobs: Annotated[int, typer.Option("--jobs", min=1, help="Number of benchmark cases to run concurrently.")] = 1,
    analyzer_cache: Annotated[
        bool,
        typer.Option("--analyzer-cache/--no-analyzer-cache", help="Use the TS/JS analyzer repo index cache."),
    ] = True,
    refresh_analyzer_cache: Annotated[
        bool,
        typer.Option("--refresh-analyzer-cache", help="Refresh the TS/JS analyzer repo index cache."),
    ] = False,
    analyzer_cache_dir: Annotated[
        Path | None,
        typer.Option("--analyzer-cache-dir", help="TS/JS analyzer index cache directory."),
    ] = None,
) -> None:
    """Run benchmark cases and write markdown/JSON reports."""
    if verify and no_verify:
        raise typer.BadParameter("Use only one of --verify or --no-verify.")
    if refresh_cache and not cache:
        raise typer.BadParameter("Use --refresh-cache only when cache is enabled.")
    if refresh_analyzer_cache and not analyzer_cache:
        raise typer.BadParameter("Use --refresh-analyzer-cache only when analyzer cache is enabled.")
    _ensure_distinct_outputs(output, json_output)
    provider_override = None
    if llm_provider:
        try:
            provider_override = LLMProviderName(llm_provider)
        except ValueError as exc:
            raise typer.BadParameter(f"Unsupported LLM provider: {llm_provider}") from exc
    verify_override = True if verify else False if no_verify else None

    try:
        report = run_benchmark_cases(
            cases,
            llm_enabled=llm,
            provider_override=provider_override,
            verify_override=verify_override,
            cache_enabled=cache,
            refresh_cache=refresh_cache,
            cache_dir=cache_dir,
            jobs=jobs,
            llm_jobs=llm_jobs,
            analyzer_cache_enabled=analyzer_cache,
            refresh_analyzer_cache=refresh_analyzer_cache,
            analyzer_cache_dir=analyzer_cache_dir,
        )
    except BenchmarkError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except LLMProviderError as exc:
        raise typer.BadParameter(str(exc)) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_benchmark_report(report), encoding="utf-8")
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    typer.echo(f"Wrote {output}")
    typer.echo(f"Wrote {json_output}")
    if report.failed:
        raise typer.Exit(code=1)


@app.command("compare-benchmark")
def compare_benchmark(
    old_report: Annotated[Path, typer.Argument(help="Previous benchmark JSON report.")],
    new_report: Annotated[Path, typer.Argument(help="New benchmark JSON report.")],
    output: Annotated[Path, typer.Option("--output", help="Markdown comparison report path.")] = Path(
        "benchmark-compare.md"
    ),
    json_output: Annotated[Path, typer.Option("--json", help="JSON comparison report path.")] = Path(
        "benchmark-compare.json"
    ),
    fail_on_regression: Annotated[
        bool,
        typer.Option(
            "--fail-on-regression/--no-fail-on-regression",
            help="Exit with code 1 when the benchmark comparison gate fails.",
        ),
    ] = True,
) -> None:
    """Compare two benchmark JSON reports."""
    try:
        comparison = compare_benchmark_reports(
            load_benchmark_report(old_report),
            load_benchmark_report(new_report),
        )
    except BenchmarkError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _ensure_distinct_outputs(output, json_output)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_benchmark_comparison(comparison), encoding="utf-8")
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(comparison.model_dump_json(indent=2), encoding="utf-8")

    typer.echo(f"Wrote {output}")
    typer.echo(f"Wrote {json_output}")
    if fail_on_regression and benchmark_comparison_gate_failures(comparison):
        raise typer.Exit(code=1)


@app.command("capture-benchmark")
def capture_benchmark(
    repo: Annotated[Path, typer.Option("--repo", help="Source git repository to capture from.")],
    name: Annotated[str, typer.Option("--name", help="Benchmark case name.")],
    output: Annotated[Path, typer.Option("--output", help="Output benchmark case directory.")],
    base: Annotated[str | None, typer.Option("--base", help="Capture git diff <base>...HEAD.")] = None,
    staged: Annotated[bool, typer.Option("--staged", help="Capture staged changes.")] = False,
    worktree: Annotated[bool, typer.Option("--worktree", help="Capture unstaged worktree changes.")] = False,
    expected_title_contains: Annotated[
        str | None,
        typer.Option("--expected-title-contains", help="Optional expected finding title substring."),
    ] = None,
    expected_file: Annotated[
        str | None, typer.Option("--expected-file", help="Optional expected finding file path.")
    ] = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Create case with llm: false.")] = False,
    llm_provider: Annotated[str, typer.Option("--llm-provider", help="LLM provider for captured case.")] = "codex_cli",
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Create case with verify: false.")] = False,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Allow writing into a non-empty output directory.")
    ] = False,
) -> None:
    """Capture a real repository diff as a self-contained benchmark case."""
    explicit_modes = sum(bool(value) for value in (staged, worktree, base is not None))
    if explicit_modes != 1:
        raise typer.BadParameter("Use exactly one capture target: --worktree, --staged, or --base.")
    try:
        provider = LLMProviderName(llm_provider)
    except ValueError as exc:
        raise typer.BadParameter(f"Unsupported LLM provider: {llm_provider}") from exc

    target_mode = TargetMode.WORKTREE if worktree else TargetMode.STAGED if staged else TargetMode.BASE
    try:
        result = capture_benchmark_case(
            source_repo=repo,
            output_dir=output,
            name=name,
            target_mode=target_mode,
            base=base,
            expected_title_contains=expected_title_contains,
            expected_file=expected_file,
            llm=not no_llm,
            provider=provider,
            verify=not no_verify,
            overwrite=overwrite,
        )
    except BenchmarkError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except git.GitError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Wrote {result.case_path}")
    typer.echo(f"Wrote {result.diff_path}")
    typer.echo(f"Copied {len(result.copied_files)} files into {result.repo_dir}")
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}")


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


def _ensure_distinct_outputs(output: Path, json_output: Path, html_output: Path | None = None) -> None:
    outputs = [("Markdown", output), ("JSON", json_output)]
    if html_output is not None:
        outputs.append(("HTML", html_output))
    seen: dict[Path, str] = {}
    for label, path in outputs:
        resolved = path.resolve()
        existing = seen.get(resolved)
        if existing:
            raise typer.BadParameter(f"{existing} and {label} output paths must be different.")
        seen[resolved] = label
