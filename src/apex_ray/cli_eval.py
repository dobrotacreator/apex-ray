from pathlib import Path
from typing import Annotated

import typer

from apex_ray.llm import LLMProviderError
from apex_ray.models import LLMProviderName
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

eval_app = typer.Typer(help="Capture and replay historical PR review evals.")


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
