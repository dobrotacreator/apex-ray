from pathlib import Path
from typing import Annotated

import typer

from apex_ray import git
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
from apex_ray.cli_common import ensure_distinct_outputs
from apex_ray.llm import LLMProviderError
from apex_ray.models import LLMProviderName, TargetMode


def register_benchmark_commands(app: typer.Typer) -> None:
    app.command()(benchmark)
    app.command("compare-benchmark")(compare_benchmark)
    app.command("capture-benchmark")(capture_benchmark)


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
    ensure_distinct_outputs(output, json_output)
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
    ensure_distinct_outputs(output, json_output)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_benchmark_comparison(comparison), encoding="utf-8")
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(comparison.model_dump_json(indent=2), encoding="utf-8")

    typer.echo(f"Wrote {output}")
    typer.echo(f"Wrote {json_output}")
    if fail_on_regression and benchmark_comparison_gate_failures(comparison):
        raise typer.Exit(code=1)


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
    llm: Annotated[bool, typer.Option("--llm", help="Create case with llm: true. Default is no LLM cost.")] = False,
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
    if llm and no_llm:
        raise typer.BadParameter("Use only one of --llm or --no-llm.")
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
            llm=llm and not no_llm,
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
