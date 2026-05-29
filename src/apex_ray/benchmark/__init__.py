import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from apex_ray.benchmark.capture import capture_benchmark_case
from apex_ray.benchmark.errors import BenchmarkError
from apex_ray.benchmark.matching import match_expected_context as _match_expected_context
from apex_ray.benchmark.matching import match_expected_findings as _match_expected_findings
from apex_ray.benchmark.models import (
    BenchmarkCase,
    BenchmarkCaseComparison,
    BenchmarkCaseResult,
    BenchmarkComparisonReport,
    BenchmarkComparisonSummary,
    BenchmarkReport,
    CaptureResult,
    ExpectedContext,
    ExpectedContextResult,
    ExpectedFinding,
    ExpectedFindingResult,
)
from apex_ray.benchmark.models import StrictBenchmarkModel as StrictBenchmarkModel
from apex_ray.benchmark.reporting import (
    benchmark_comparison_gate_failures,
    compare_benchmark_reports,
    render_benchmark_comparison,
    render_benchmark_report,
)
from apex_ray.benchmark.reporting import (
    format_llm_route_summary as _format_llm_route_summary,
)
from apex_ray.invocation import ReviewOverrides, apply_review_overrides
from apex_ray.llm import FakeLLMProvider, LLMProvider
from apex_ray.models import (
    LLMConfig,
    LLMProviderName,
    LLMRun,
    ReviewConfig,
    TargetMode,
)
from apex_ray.pipeline import run_review_pipeline

__all__ = [
    "BenchmarkCase",
    "BenchmarkCaseComparison",
    "BenchmarkCaseResult",
    "BenchmarkComparisonReport",
    "BenchmarkComparisonSummary",
    "BenchmarkError",
    "BenchmarkReport",
    "CaptureResult",
    "ExpectedContext",
    "ExpectedContextResult",
    "ExpectedFinding",
    "ExpectedFindingResult",
    "_llm_run_cache_hits",
    "_llm_run_cache_misses",
    "_match_expected_context",
    "_match_expected_findings",
    "benchmark_comparison_gate_failures",
    "capture_benchmark_case",
    "compare_benchmark_reports",
    "load_benchmark_case",
    "load_benchmark_report",
    "render_benchmark_comparison",
    "render_benchmark_report",
    "run_benchmark_cases",
]


def run_benchmark_cases(
    case_paths: list[Path],
    llm_enabled: bool = False,
    provider_override: LLMProviderName | None = None,
    verify_override: bool | None = None,
    cache_enabled: bool | None = None,
    refresh_cache: bool = False,
    cache_dir: Path | None = None,
    jobs: int = 1,
    llm_jobs: int | None = None,
    analyzer_cache_enabled: bool | None = None,
    refresh_analyzer_cache: bool = False,
    analyzer_cache_dir: Path | None = None,
) -> BenchmarkReport:
    if jobs < 1:
        raise BenchmarkError("Benchmark jobs must be at least 1.")

    def run_path(path: Path) -> BenchmarkCaseResult:
        return run_benchmark_case(
            load_benchmark_case(path),
            case_path=path,
            llm_enabled=llm_enabled,
            provider_override=provider_override,
            verify_override=verify_override,
            cache_enabled=cache_enabled,
            refresh_cache=refresh_cache,
            cache_dir=cache_dir,
            llm_jobs=llm_jobs,
            analyzer_cache_enabled=analyzer_cache_enabled,
            refresh_analyzer_cache=refresh_analyzer_cache,
            analyzer_cache_dir=analyzer_cache_dir,
        )

    if jobs == 1 or len(case_paths) <= 1:
        results = [run_path(path) for path in case_paths]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = [executor.submit(run_path, path) for path in case_paths]
            results = [future.result() for future in futures]

    passed = sum(1 for result in results if result.passed)
    return BenchmarkReport(
        cases=results,
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
    )


def load_benchmark_case(path: Path) -> BenchmarkCase:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise BenchmarkError(f"Unable to read benchmark case {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise BenchmarkError(f"Invalid benchmark YAML in {path}: {exc}") from exc
    try:
        return BenchmarkCase.model_validate(raw)
    except ValidationError as exc:
        raise BenchmarkError(f"Invalid benchmark case in {path}: {exc}") from exc


def load_benchmark_report(path: Path) -> BenchmarkReport:
    if not path.exists():
        raise BenchmarkError(f"Benchmark report does not exist: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"Invalid benchmark JSON in {path}: {exc}") from exc
    try:
        return BenchmarkReport.model_validate(raw)
    except ValidationError as exc:
        raise BenchmarkError(f"Invalid benchmark report in {path}: {exc}") from exc


def run_benchmark_case(
    case: BenchmarkCase,
    case_path: Path,
    llm_enabled: bool,
    provider_override: LLMProviderName | None,
    verify_override: bool | None,
    cache_enabled: bool | None,
    refresh_cache: bool,
    cache_dir: Path | None,
    llm_jobs: int | None = None,
    analyzer_cache_enabled: bool | None = None,
    refresh_analyzer_cache: bool = False,
    analyzer_cache_dir: Path | None = None,
) -> BenchmarkCaseResult:
    repo_root = _resolve_case_path(case_path, case.repo)
    diff_path = _resolve_case_path(case_path, case.diff)
    if not repo_root.exists():
        raise BenchmarkError(f"Benchmark repo does not exist for {case.name}: {repo_root}")
    if not diff_path.exists():
        raise BenchmarkError(f"Benchmark diff does not exist for {case.name}: {diff_path}")

    config = ReviewConfig(rules=case.rules)
    if case.profiles:
        config.llm.profiles = case.profiles
    if case.routing:
        config.llm.routing = case.routing
    config = apply_review_overrides(
        config,
        ReviewOverrides(
            llm_enabled=case.llm if case.llm is not None else llm_enabled,
            provider=provider_override or case.provider,
            model=case.model,
            clear_routing_on_model=not (case.profiles or case.routing),
            verify=verify_override if verify_override is not None else case.verify,
            cache_allowed=cache_enabled,
            refresh_cache=refresh_cache,
            cache_dir=cache_dir,
            llm_jobs=llm_jobs,
            analyzer_cache_allowed=analyzer_cache_enabled,
            refresh_analyzer_cache=refresh_analyzer_cache,
            analyzer_cache_dir=analyzer_cache_dir,
        ),
    )

    provider = _provider_for_case(case, config.llm)
    report = run_review_pipeline(
        repo_root,
        diff_path.read_text(encoding="utf-8"),
        TargetMode.PATCH,
        config,
        provider=provider,
    )

    expected_results, extra_findings = _match_expected_findings(case.expected, report.findings)
    expected_context_results = [
        _match_expected_context(expected, report.context_packs) for expected in case.expected_context
    ]
    passed = (
        all(result.matched for result in expected_results)
        and all(result.matched for result in expected_context_results)
        and not extra_findings
    )

    return BenchmarkCaseResult(
        name=case.name,
        passed=passed,
        repo=str(repo_root),
        diff=str(diff_path),
        findings_count=len(report.findings),
        context_packs_count=len(report.context_packs),
        llm_runs_count=len(report.llm_runs),
        llm_cache_hits=sum(_llm_run_cache_hits(run) for run in report.llm_runs),
        llm_cache_misses=sum(_llm_run_cache_misses(run) for run in report.llm_runs),
        llm_duration_ms=sum(run.duration_ms for run in report.llm_runs),
        llm_input_chars=report.llm_coverage.input_chars,
        llm_estimated_input_tokens=report.llm_coverage.estimated_input_tokens,
        llm_prompt_versions=sorted({run.prompt_version for run in report.llm_runs if run.prompt_version}),
        llm_models=sorted({run.model for run in report.llm_runs if run.model}),
        llm_profiles=sorted({run.profile for run in report.llm_runs if run.profile}),
        llm_routes=[_format_llm_route_summary(route) for route in report.llm_coverage.routes],
        verifications_count=len(report.verifications),
        verifier_approved_count=sum(1 for verification in report.verifications if verification.approved),
        verifier_rejected_count=sum(1 for verification in report.verifications if not verification.approved),
        expected_results=expected_results,
        expected_context_results=expected_context_results,
        extra_findings=extra_findings,
        warnings=report.diff.warnings,
    )


def _llm_run_cache_hits(run: LLMRun) -> int:
    if run.cache_hits or run.cache_misses:
        return run.cache_hits
    return 1 if run.cache_hit else 0


def _llm_run_cache_misses(run: LLMRun) -> int:
    if run.cache_hits or run.cache_misses:
        return run.cache_misses
    return 1 if run.cache_key and not run.cache_hit else 0


def _provider_for_case(case: BenchmarkCase, config: LLMConfig) -> LLMProvider | None:
    if config.provider == LLMProviderName.FAKE:
        return FakeLLMProvider(case.fake_findings)
    return None


def _resolve_case_path(case_path: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (case_path.parent / path).resolve()
