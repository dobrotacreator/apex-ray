import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, model_validator

from apex_ray import git
from apex_ray.diff import parse_unified_diff
from apex_ray.invocation import ReviewOverrides, apply_review_overrides
from apex_ray.llm import FakeLLMProvider, LLMProvider
from apex_ray.models import (
    AnalyzerReference,
    ContextPack,
    FileStatus,
    Finding,
    FindingConfidence,
    FindingSeverity,
    LLMConfig,
    LLMProfile,
    LLMProviderName,
    LLMRoutingConfig,
    LLMRun,
    ReviewConfig,
    TargetMode,
)
from apex_ray.pipeline import run_review_pipeline


class BenchmarkError(RuntimeError):
    pass


class StrictBenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedFinding(StrictBenchmarkModel):
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    line_min: int | None = Field(default=None, ge=1)
    line_max: int | None = Field(default=None, ge=1)
    title_contains: str | None = None
    severity: FindingSeverity | None = None
    confidence: FindingConfidence | None = None
    failure_mode_contains: str | None = None
    evidence_contains: str | None = None
    suggested_fix_contains: str | None = None
    suggested_test_contains: str | None = None

    @model_validator(mode="after")
    def validate_line_range(self) -> ExpectedFinding:
        if self.line_min is not None and self.line_max is not None and self.line_min > self.line_max:
            raise ValueError("line_min must be less than or equal to line_max")
        return self


class ExpectedContext(StrictBenchmarkModel):
    pack_file: str | None = None
    pack_id_contains: str | None = None
    related_test: str | None = None
    related_test_index: int | None = Field(default=None, ge=0)
    related_test_snippet_contains: str | None = None
    related_test_snippet_start_min: int | None = Field(default=None, ge=1)
    reference_file: str | None = None
    reference_kind: str | None = None
    reference_text_contains: str | None = None
    reference_snippet_contains: str | None = None
    callee_file: str | None = None
    callee_kind: str | None = None
    callee_text_contains: str | None = None
    callee_snippet_contains: str | None = None
    contract_file: str | None = None
    contract_kind: str | None = None
    contract_text_contains: str | None = None
    contract_snippet_contains: str | None = None
    metadata_file: str | None = None
    metadata_kind: str | None = None
    metadata_text_contains: str | None = None
    metadata_snippet_contains: str | None = None


class BenchmarkCase(StrictBenchmarkModel):
    name: str
    repo: str
    diff: str
    rules: list[str] = Field(default_factory=list)
    llm: bool | None = None
    provider: LLMProviderName | None = None
    model: str | None = None
    profiles: dict[str, LLMProfile] = Field(default_factory=dict)
    routing: LLMRoutingConfig | None = None
    verify: bool | None = None
    fake_findings: list[Finding] = Field(default_factory=list)
    expected: list[ExpectedFinding] = Field(default_factory=list)
    expected_context: list[ExpectedContext] = Field(default_factory=list)


class ExpectedFindingResult(BaseModel):
    expected: ExpectedFinding
    matched: bool
    matched_title: str | None = None


class ExpectedContextResult(BaseModel):
    expected: ExpectedContext
    matched: bool
    matched_pack_id: str | None = None


class BenchmarkCaseResult(BaseModel):
    name: str
    passed: bool
    repo: str
    diff: str
    findings_count: int
    context_packs_count: int
    llm_runs_count: int
    llm_cache_hits: int = 0
    llm_cache_misses: int = 0
    llm_duration_ms: int
    llm_input_chars: int = 0
    llm_estimated_input_tokens: int = 0
    llm_prompt_versions: list[str] = Field(default_factory=list)
    llm_models: list[str] = Field(default_factory=list)
    llm_profiles: list[str] = Field(default_factory=list)
    llm_routes: list[str] = Field(default_factory=list)
    verifications_count: int = 0
    verifier_approved_count: int = 0
    verifier_rejected_count: int = 0
    expected_results: list[ExpectedFindingResult]
    expected_context_results: list[ExpectedContextResult] = Field(default_factory=list)
    extra_findings: list[Finding]
    warnings: list[str] = Field(default_factory=list)


class BenchmarkReport(BaseModel):
    cases: list[BenchmarkCaseResult]
    total: int
    passed: int
    failed: int

    @computed_field
    @property
    def expected_findings_total(self) -> int:
        return sum(len(case.expected_results) for case in self.cases)

    @computed_field
    @property
    def missed_findings_total(self) -> int:
        return sum(1 for case in self.cases for result in case.expected_results if not result.matched)

    @computed_field
    @property
    def expected_context_total(self) -> int:
        return _sum_context_expected(self.cases)

    @computed_field
    @property
    def missed_context_total(self) -> int:
        return _sum_context_misses(self.cases)

    @computed_field
    @property
    def extra_findings_total(self) -> int:
        return sum(len(case.extra_findings) for case in self.cases)


class BenchmarkCaseComparison(BaseModel):
    name: str
    status: str
    old_passed: bool | None = None
    new_passed: bool | None = None
    old_findings_count: int | None = None
    new_findings_count: int | None = None
    old_missed_expected_count: int | None = None
    new_missed_expected_count: int | None = None
    old_extra_findings_count: int | None = None
    new_extra_findings_count: int | None = None
    old_llm_duration_ms: int | None = None
    new_llm_duration_ms: int | None = None
    llm_duration_delta_ms: int | None = None
    old_llm_cache_hits: int | None = None
    new_llm_cache_hits: int | None = None
    llm_cache_hit_delta: int | None = None
    old_llm_cache_misses: int | None = None
    new_llm_cache_misses: int | None = None
    llm_cache_miss_delta: int | None = None
    old_llm_prompt_versions: list[str] = Field(default_factory=list)
    new_llm_prompt_versions: list[str] = Field(default_factory=list)
    old_verifications_count: int | None = None
    new_verifications_count: int | None = None
    old_verifier_approved_count: int | None = None
    new_verifier_approved_count: int | None = None
    old_verifier_rejected_count: int | None = None
    new_verifier_rejected_count: int | None = None
    messages: list[str] = Field(default_factory=list)


class BenchmarkComparisonSummary(BaseModel):
    old_total: int
    new_total: int
    old_passed: int
    new_passed: int
    old_failed: int
    new_failed: int
    regressions: int
    improvements: int
    added: int
    removed: int
    unchanged: int
    llm_duration_delta_ms: int
    llm_cache_hit_delta: int
    llm_cache_miss_delta: int
    old_context_misses: int = 0
    new_context_misses: int = 0
    context_miss_delta: int = 0


class BenchmarkComparisonReport(BaseModel):
    summary: BenchmarkComparisonSummary
    cases: list[BenchmarkCaseComparison]


class CaptureResult(BaseModel):
    output_dir: str
    case_path: str
    diff_path: str
    repo_dir: str
    copied_files: list[str]
    warnings: list[str] = Field(default_factory=list)


CONFIG_FILES = (
    "package.json",
    "tsconfig.json",
    "jsconfig.json",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
)
LOCAL_CONFIG_FILES = ("package.json", "tsconfig.json", "jsconfig.json")


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


def compare_benchmark_reports(old: BenchmarkReport, new: BenchmarkReport) -> BenchmarkComparisonReport:
    old_by_name = {case.name: case for case in old.cases}
    new_by_name = {case.name: case for case in new.cases}
    names = sorted(set(old_by_name) | set(new_by_name))
    cases = [_compare_benchmark_case(name, old_by_name.get(name), new_by_name.get(name)) for name in names]
    return BenchmarkComparisonReport(
        summary=BenchmarkComparisonSummary(
            old_total=old.total,
            new_total=new.total,
            old_passed=old.passed,
            new_passed=new.passed,
            old_failed=old.failed,
            new_failed=new.failed,
            regressions=sum(1 for case in cases if case.status == "regression"),
            improvements=sum(1 for case in cases if case.status == "improvement"),
            added=sum(1 for case in cases if case.status == "added"),
            removed=sum(1 for case in cases if case.status == "removed"),
            unchanged=sum(1 for case in cases if case.status.startswith("unchanged")),
            llm_duration_delta_ms=_sum_duration(new.cases) - _sum_duration(old.cases),
            llm_cache_hit_delta=_sum_cache_hits(new.cases) - _sum_cache_hits(old.cases),
            llm_cache_miss_delta=_sum_cache_misses(new.cases) - _sum_cache_misses(old.cases),
            old_context_misses=_sum_context_misses(old.cases),
            new_context_misses=_sum_context_misses(new.cases),
            context_miss_delta=_sum_context_misses(new.cases) - _sum_context_misses(old.cases),
        ),
        cases=cases,
    )


def render_benchmark_comparison(report: BenchmarkComparisonReport) -> str:
    summary = report.summary
    gate_failures = benchmark_comparison_gate_failures(report)
    lines = [
        "# Apex Ray Benchmark Compare",
        "",
        f"- Old: `{summary.old_passed}/{summary.old_total}` passed, `{summary.old_failed}` failed",
        f"- New: `{summary.new_passed}/{summary.new_total}` passed, `{summary.new_failed}` failed",
        f"- Gate: `{'fail' if gate_failures else 'pass'}`",
        f"- Regressions: `{summary.regressions}`",
        f"- Improvements: `{summary.improvements}`",
        f"- Added cases: `{summary.added}`",
        f"- Removed cases: `{summary.removed}`",
        f"- LLM duration delta: `{_format_delta(summary.llm_duration_delta_ms)}ms`",
        f"- LLM cache hit delta: `{_format_delta(summary.llm_cache_hit_delta)}`",
        f"- LLM cache miss delta: `{_format_delta(summary.llm_cache_miss_delta)}`",
        f"- Context misses: `{summary.old_context_misses}` -> `{summary.new_context_misses}` "
        f"({_format_delta(summary.context_miss_delta)})",
        "",
    ]
    if gate_failures:
        lines.append("- Gate reasons:")
        for reason in gate_failures:
            lines.append(f"  - {reason}")
        lines.append("")

    _append_comparison_section(lines, "Regressions", report.cases, {"regression"})
    _append_comparison_section(lines, "Improvements", report.cases, {"improvement"})
    _append_comparison_section(lines, "Added Cases", report.cases, {"added"})
    _append_comparison_section(lines, "Removed Cases", report.cases, {"removed"})
    _append_comparison_section(
        lines,
        "Unchanged Cases",
        report.cases,
        {"unchanged_pass", "unchanged_fail"},
        include_empty=False,
    )
    return "\n".join(lines)


def benchmark_comparison_gate_failures(report: BenchmarkComparisonReport) -> list[str]:
    summary = report.summary
    failures: list[str] = []
    if summary.regressions:
        failures.append(f"{summary.regressions} regression case(s)")
    if summary.new_failed > summary.old_failed:
        failures.append(f"failed cases increased from {summary.old_failed} to {summary.new_failed}")
    if summary.context_miss_delta > 0:
        failures.append(f"context misses increased by {summary.context_miss_delta}")
    if summary.removed:
        failures.append(f"{summary.removed} benchmark case(s) removed")
    return failures


def render_benchmark_report(report: BenchmarkReport) -> str:
    lines = [
        "# Apex Ray Benchmark",
        "",
        f"- Total: `{report.total}`",
        f"- Passed: `{report.passed}`",
        f"- Failed: `{report.failed}`",
        f"- Expected context: `{_sum_context_expected(report.cases)}`",
        f"- Missed context: `{_sum_context_misses(report.cases)}`",
        "",
        "## Cases",
        "",
    ]
    for case in report.cases:
        status = "PASS" if case.passed else "FAIL"
        lines.append(f"### {status}: {case.name}")
        lines.append("")
        lines.append(f"- Context packs: `{case.context_packs_count}`")
        lines.append(f"- Findings: `{case.findings_count}`")
        lines.append(f"- LLM runs: `{case.llm_runs_count}`")
        lines.append(f"- LLM cache hits: `{case.llm_cache_hits}`")
        lines.append(f"- LLM cache misses: `{case.llm_cache_misses}`")
        lines.append(f"- LLM duration: `{case.llm_duration_ms}ms`")
        if case.llm_input_chars:
            lines.append(f"- LLM input: `{case.llm_input_chars}` chars (`~{case.llm_estimated_input_tokens}` tokens)")
        if case.llm_prompt_versions:
            lines.append(f"- LLM prompt versions: `{_format_prompt_versions(case.llm_prompt_versions)}`")
        if case.llm_models:
            lines.append(f"- LLM models: `{', '.join(case.llm_models)}`")
        if case.llm_profiles:
            lines.append(f"- LLM profiles: `{', '.join(case.llm_profiles)}`")
        if case.llm_routes:
            lines.append(f"- LLM routes: `{'; '.join(case.llm_routes)}`")
        if case.verifications_count:
            lines.append(
                f"- Verifier: `{case.verifier_approved_count}` approved, `{case.verifier_rejected_count}` rejected"
            )
        if case.expected_results:
            lines.append("- Expected:")
            for result in case.expected_results:
                marker = "matched" if result.matched else "missed"
                title = f" -> {result.matched_title}" if result.matched_title else ""
                lines.append(f"  - {marker}: {result.expected.model_dump(mode='json', exclude_none=True)}{title}")
        if case.expected_context_results:
            lines.append("- Expected context:")
            for result in case.expected_context_results:
                marker = "matched" if result.matched else "missed"
                pack = f" -> {result.matched_pack_id}" if result.matched_pack_id else ""
                lines.append(f"  - {marker}: {result.expected.model_dump(mode='json', exclude_none=True)}{pack}")
        if case.extra_findings:
            lines.append("- Extra findings:")
            for finding in case.extra_findings:
                lines.append(
                    f"  - {finding.severity}/{finding.confidence}: {finding.title} ({finding.file}:{finding.line})"
                )
        if case.warnings:
            lines.append("- Warnings:")
            for warning in case.warnings:
                lines.append(f"  - {warning}")
        lines.append("")
    return "\n".join(lines)


def _compare_benchmark_case(
    name: str,
    old: BenchmarkCaseResult | None,
    new: BenchmarkCaseResult | None,
) -> BenchmarkCaseComparison:
    if old is None and new is None:
        raise BenchmarkError(f"Cannot compare missing case: {name}")
    if old is None:
        assert new is not None
        return BenchmarkCaseComparison(
            name=name,
            status="added",
            new_passed=new.passed,
            new_findings_count=new.findings_count,
            new_missed_expected_count=_missed_expected_count(new),
            new_extra_findings_count=len(new.extra_findings),
            new_llm_duration_ms=new.llm_duration_ms,
            new_llm_cache_hits=new.llm_cache_hits,
            new_llm_cache_misses=new.llm_cache_misses,
            new_llm_prompt_versions=new.llm_prompt_versions,
            new_verifications_count=new.verifications_count,
            new_verifier_approved_count=new.verifier_approved_count,
            new_verifier_rejected_count=new.verifier_rejected_count,
            messages=_case_messages(None, new),
        )
    if new is None:
        return BenchmarkCaseComparison(
            name=name,
            status="removed",
            old_passed=old.passed,
            old_findings_count=old.findings_count,
            old_missed_expected_count=_missed_expected_count(old),
            old_extra_findings_count=len(old.extra_findings),
            old_llm_duration_ms=old.llm_duration_ms,
            old_llm_cache_hits=old.llm_cache_hits,
            old_llm_cache_misses=old.llm_cache_misses,
            old_llm_prompt_versions=old.llm_prompt_versions,
            old_verifications_count=old.verifications_count,
            old_verifier_approved_count=old.verifier_approved_count,
            old_verifier_rejected_count=old.verifier_rejected_count,
            messages=["case removed from new report"],
        )

    status = _comparison_status(old, new)
    return BenchmarkCaseComparison(
        name=name,
        status=status,
        old_passed=old.passed,
        new_passed=new.passed,
        old_findings_count=old.findings_count,
        new_findings_count=new.findings_count,
        old_missed_expected_count=_missed_expected_count(old),
        new_missed_expected_count=_missed_expected_count(new),
        old_extra_findings_count=len(old.extra_findings),
        new_extra_findings_count=len(new.extra_findings),
        old_llm_duration_ms=old.llm_duration_ms,
        new_llm_duration_ms=new.llm_duration_ms,
        llm_duration_delta_ms=new.llm_duration_ms - old.llm_duration_ms,
        old_llm_cache_hits=old.llm_cache_hits,
        new_llm_cache_hits=new.llm_cache_hits,
        llm_cache_hit_delta=new.llm_cache_hits - old.llm_cache_hits,
        old_llm_cache_misses=old.llm_cache_misses,
        new_llm_cache_misses=new.llm_cache_misses,
        llm_cache_miss_delta=new.llm_cache_misses - old.llm_cache_misses,
        old_llm_prompt_versions=old.llm_prompt_versions,
        new_llm_prompt_versions=new.llm_prompt_versions,
        old_verifications_count=old.verifications_count,
        new_verifications_count=new.verifications_count,
        old_verifier_approved_count=old.verifier_approved_count,
        new_verifier_approved_count=new.verifier_approved_count,
        old_verifier_rejected_count=old.verifier_rejected_count,
        new_verifier_rejected_count=new.verifier_rejected_count,
        messages=_case_messages(old, new),
    )


def _comparison_status(old: BenchmarkCaseResult, new: BenchmarkCaseResult) -> str:
    if old.passed and not new.passed:
        return "regression"
    if not old.passed and new.passed:
        return "improvement"

    old_problem_count = _missed_expected_count(old) + len(old.extra_findings)
    new_problem_count = _missed_expected_count(new) + len(new.extra_findings)
    if new_problem_count > old_problem_count:
        return "regression"
    if new_problem_count < old_problem_count:
        return "improvement"
    return "unchanged_pass" if new.passed else "unchanged_fail"


def _case_messages(old: BenchmarkCaseResult | None, new: BenchmarkCaseResult) -> list[str]:
    messages: list[str] = []
    if old is None:
        messages.append("case added to new report")
        if not new.passed:
            messages.append("added case is failing")
        return messages

    old_missed = _missed_expected_descriptions(old)
    new_missed = _missed_expected_descriptions(new)
    for missed in sorted(new_missed - old_missed):
        messages.append(f"new missed expected finding: {missed}")
    for recovered in sorted(old_missed - new_missed):
        messages.append(f"recovered expected finding: {recovered}")

    old_missed_context = _missed_expected_context_descriptions(old)
    new_missed_context = _missed_expected_context_descriptions(new)
    for missed in sorted(new_missed_context - old_missed_context):
        messages.append(f"new missed expected context: {missed}")
    for recovered in sorted(old_missed_context - new_missed_context):
        messages.append(f"recovered expected context: {recovered}")

    old_extra = _extra_finding_descriptions(old)
    new_extra = _extra_finding_descriptions(new)
    for extra in sorted(new_extra - old_extra):
        messages.append(f"new extra finding: {extra}")
    for removed in sorted(old_extra - new_extra):
        messages.append(f"removed extra finding: {removed}")

    for changed in _changed_matched_titles(old, new):
        messages.append(changed)

    duration_delta = new.llm_duration_ms - old.llm_duration_ms
    if duration_delta > 5000:
        messages.append(f"LLM duration increased by {duration_delta}ms")
    elif duration_delta < -5000:
        messages.append(f"LLM duration decreased by {-duration_delta}ms")
    if new.context_packs_count != old.context_packs_count:
        messages.append(f"context pack count changed from {old.context_packs_count} to {new.context_packs_count}")
    if new.llm_cache_hits != old.llm_cache_hits:
        messages.append(f"LLM cache hits changed from {old.llm_cache_hits} to {new.llm_cache_hits}")
    if new.llm_cache_misses != old.llm_cache_misses:
        messages.append(f"LLM cache misses changed from {old.llm_cache_misses} to {new.llm_cache_misses}")
    if new.llm_prompt_versions != old.llm_prompt_versions:
        messages.append(
            f"LLM prompt versions changed from {_format_prompt_versions(old.llm_prompt_versions)} "
            f"to {_format_prompt_versions(new.llm_prompt_versions)}"
        )
    if new.verifier_approved_count != old.verifier_approved_count:
        messages.append(
            f"verifier approvals changed from {old.verifier_approved_count} to {new.verifier_approved_count}"
        )
    if new.verifier_rejected_count != old.verifier_rejected_count:
        messages.append(
            f"verifier rejections changed from {old.verifier_rejected_count} to {new.verifier_rejected_count}"
        )
    return messages


def _missed_expected_count(case: BenchmarkCaseResult) -> int:
    return sum(1 for result in case.expected_results if not result.matched) + sum(
        1 for result in case.expected_context_results if not result.matched
    )


def _missed_expected_descriptions(case: BenchmarkCaseResult) -> set[str]:
    return {
        str(result.expected.model_dump(mode="json", exclude_none=True))
        for result in case.expected_results
        if not result.matched
    }


def _missed_expected_context_descriptions(case: BenchmarkCaseResult) -> set[str]:
    return {
        str(result.expected.model_dump(mode="json", exclude_none=True))
        for result in case.expected_context_results
        if not result.matched
    }


def _extra_finding_descriptions(case: BenchmarkCaseResult) -> set[str]:
    return {
        f"{finding.severity}/{finding.confidence}: {finding.title} ({finding.file}:{finding.line})"
        for finding in case.extra_findings
    }


def _changed_matched_titles(old: BenchmarkCaseResult, new: BenchmarkCaseResult) -> list[str]:
    messages: list[str] = []
    for index, old_result in enumerate(old.expected_results):
        if index >= len(new.expected_results):
            continue
        new_result = new.expected_results[index]
        if (
            old_result.matched
            and new_result.matched
            and old_result.matched_title
            and new_result.matched_title
            and old_result.matched_title != new_result.matched_title
        ):
            messages.append(f"matched title changed from {old_result.matched_title!r} to {new_result.matched_title!r}")
    return messages


def _append_comparison_section(
    lines: list[str],
    title: str,
    cases: list[BenchmarkCaseComparison],
    statuses: set[str],
    include_empty: bool = True,
) -> None:
    matching = [case for case in cases if case.status in statuses]
    if not matching and not include_empty:
        return
    lines.extend([f"## {title}", ""])
    if not matching:
        lines.append("None.")
        lines.append("")
        return
    for case in matching:
        lines.append(f"### {case.name}")
        lines.append("")
        lines.append(f"- Status: `{case.status}`")
        if case.old_passed is not None or case.new_passed is not None:
            lines.append(f"- Passed: `{case.old_passed}` -> `{case.new_passed}`")
        if case.old_findings_count is not None or case.new_findings_count is not None:
            lines.append(f"- Findings: `{case.old_findings_count}` -> `{case.new_findings_count}`")
        if case.old_missed_expected_count is not None or case.new_missed_expected_count is not None:
            lines.append(f"- Missed expected: `{case.old_missed_expected_count}` -> `{case.new_missed_expected_count}`")
        if case.old_extra_findings_count is not None or case.new_extra_findings_count is not None:
            lines.append(f"- Extra findings: `{case.old_extra_findings_count}` -> `{case.new_extra_findings_count}`")
        if case.old_verifications_count is not None or case.new_verifications_count is not None:
            lines.append(
                f"- Verifier: `{case.old_verifier_approved_count}` approved / "
                f"`{case.old_verifier_rejected_count}` rejected -> "
                f"`{case.new_verifier_approved_count}` approved / "
                f"`{case.new_verifier_rejected_count}` rejected"
            )
        if case.llm_duration_delta_ms is not None:
            lines.append(
                f"- LLM duration: `{case.old_llm_duration_ms}ms` -> `{case.new_llm_duration_ms}ms` "
                f"({_format_delta(case.llm_duration_delta_ms)}ms)"
            )
        if case.llm_cache_hit_delta is not None:
            lines.append(
                f"- LLM cache hits: `{case.old_llm_cache_hits}` -> `{case.new_llm_cache_hits}` "
                f"({_format_delta(case.llm_cache_hit_delta)})"
            )
        if case.llm_cache_miss_delta is not None:
            lines.append(
                f"- LLM cache misses: `{case.old_llm_cache_misses}` -> `{case.new_llm_cache_misses}` "
                f"({_format_delta(case.llm_cache_miss_delta)})"
            )
        if case.old_llm_prompt_versions or case.new_llm_prompt_versions:
            lines.append(
                f"- LLM prompt versions: `{_format_prompt_versions(case.old_llm_prompt_versions)}` -> "
                f"`{_format_prompt_versions(case.new_llm_prompt_versions)}`"
            )
        if case.messages:
            lines.append("- Notes:")
            for message in case.messages:
                lines.append(f"  - {message}")
        lines.append("")


def _sum_duration(cases: list[BenchmarkCaseResult]) -> int:
    return sum(case.llm_duration_ms for case in cases)


def _sum_cache_hits(cases: list[BenchmarkCaseResult]) -> int:
    return sum(case.llm_cache_hits for case in cases)


def _sum_cache_misses(cases: list[BenchmarkCaseResult]) -> int:
    return sum(case.llm_cache_misses for case in cases)


def _sum_context_expected(cases: list[BenchmarkCaseResult]) -> int:
    return sum(len(case.expected_context_results) for case in cases)


def _sum_context_misses(cases: list[BenchmarkCaseResult]) -> int:
    return sum(1 for case in cases for result in case.expected_context_results if not result.matched)


def _format_delta(value: int) -> str:
    if value > 0:
        return f"+{value}"
    return str(value)


def _format_prompt_versions(prompt_versions: list[str]) -> str:
    return ", ".join(prompt_versions) if prompt_versions else "none"


def _format_llm_route_summary(route: object) -> str:
    kind = getattr(route, "kind", "run")
    provider = getattr(route, "provider", "unknown")
    status = getattr(route, "status", "unknown")
    profile = getattr(route, "profile", None)
    model = getattr(route, "model", None)
    runs = getattr(route, "runs", 0)
    tokens = getattr(route, "estimated_input_tokens", 0)
    identity = profile or model or "default"
    return f"{kind}/{provider}/{identity}/{status}: {runs} runs, ~{tokens} tokens"


def capture_benchmark_case(
    source_repo: Path,
    output_dir: Path,
    name: str,
    target_mode: TargetMode,
    base: str | None = None,
    expected_title_contains: str | None = None,
    expected_file: str | None = None,
    llm: bool = True,
    provider: LLMProviderName = LLMProviderName.CODEX_CLI,
    verify: bool = True,
    overwrite: bool = False,
) -> CaptureResult:
    repo_root = git.repo_root(source_repo) or source_repo.resolve()
    if not git.is_git_repo(repo_root):
        raise BenchmarkError(f"Source repo is not a git repository: {source_repo}")
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise BenchmarkError(f"Output directory is not empty: {output_dir}")

    diff_text = _diff_for_capture(repo_root, target_mode, base)
    diff_summary = parse_unified_diff(diff_text, target_mode)
    if not diff_summary.files:
        raise BenchmarkError("No changed files found to capture.")
    context_paths, expected_context, context_warnings = _capture_context(repo_root, diff_text, target_mode, base)

    repo_dir = output_dir / "repo"
    diff_path = output_dir / "change.diff"
    case_path = output_dir / "case.yml"
    repo_dir.mkdir(parents=True, exist_ok=True)
    copied_files: list[str] = []
    warnings: list[str] = [*context_warnings]

    for config_file in CONFIG_FILES:
        copied_files.extend(_copy_if_exists(repo_root, repo_dir, config_file))

    for changed_file in diff_summary.files:
        if changed_file.status == FileStatus.DELETED or changed_file.new_path is None:
            warnings.append(f"Skipped deleted file: {changed_file.old_path}")
            continue
        copied_files.extend(_copy_file_with_local_configs(repo_root, repo_dir, changed_file.new_path))

    for context_path in sorted(context_paths):
        copied_files.extend(_copy_file_with_local_configs(repo_root, repo_dir, context_path))

    diff_path.write_text(diff_text, encoding="utf-8")
    case = _captured_case_dict(
        name=name,
        llm=llm,
        provider=provider,
        verify=verify,
        expected_title_contains=expected_title_contains,
        expected_file=expected_file,
        expected_context=expected_context,
    )
    case_path.write_text(yaml.safe_dump(case, sort_keys=False), encoding="utf-8")

    return CaptureResult(
        output_dir=str(output_dir),
        case_path=str(case_path),
        diff_path=str(diff_path),
        repo_dir=str(repo_dir),
        copied_files=sorted(set(copied_files)),
        warnings=warnings,
    )


def _capture_context(
    repo_root: Path,
    diff_text: str,
    target_mode: TargetMode,
    base: str | None,
) -> tuple[set[str], list[ExpectedContext], list[str]]:
    config = ReviewConfig()
    config.llm.enabled = False
    config.analyzer.index_cache_enabled = False
    report = run_review_pipeline(
        repo_root,
        diff_text,
        target_mode,
        config,
        base=base if target_mode == TargetMode.BASE else None,
    )

    paths: set[str] = set()
    expected_context: list[ExpectedContext] = []
    for pack in report.context_packs:
        paths.add(pack.file)
        paths.update(pack.related_tests)
        paths.update(reference.file for reference in pack.references)
        paths.update(callee.file for callee in pack.callees)
        paths.update(contract.file for contract in pack.contracts)
        paths.update(reference.file for reference in pack.metadata)
        paths.update(snippet.file for snippet in pack.changed_snippets)
        paths.update(snippet.file for snippet in pack.reference_snippets)
        paths.update(snippet.file for snippet in pack.callee_snippets)
        paths.update(snippet.file for snippet in pack.contract_snippets)
        paths.update(snippet.file for snippet in pack.metadata_snippets)
        paths.update(snippet.file for snippet in pack.related_test_snippets)
        captured_expectation = _captured_expected_context(pack)
        if captured_expectation:
            expected_context.append(captured_expectation)

    warnings = [*report.diff.warnings]
    for result in report.analyzer_results:
        warnings.extend(result.warnings)
    return paths, expected_context, warnings


def _captured_expected_context(pack: ContextPack) -> ExpectedContext | None:
    reference = next((reference for reference in pack.references if reference.kind != "import"), None)
    contract = next(iter(pack.contracts), None)
    metadata = next(iter(pack.metadata), None)
    related_test = pack.related_tests[0] if pack.related_tests else None
    if reference is None and contract is None and metadata is None and related_test is None:
        return None
    return ExpectedContext(
        pack_file=pack.file,
        related_test=related_test,
        reference_file=reference.file if reference else None,
        reference_kind=reference.kind if reference else None,
        reference_text_contains=_reference_text_marker(pack, reference) if reference else None,
        contract_file=contract.file if contract else None,
        contract_kind=contract.kind if contract else None,
        contract_text_contains=contract.text if contract else None,
        metadata_file=metadata.file if metadata else None,
        metadata_kind=metadata.kind if metadata else None,
        metadata_text_contains=metadata.text if metadata else None,
    )


def _reference_text_marker(pack: ContextPack, reference: AnalyzerReference) -> str | None:
    for symbol in [pack.symbol, *pack.symbols]:
        if symbol and symbol.name in reference.text:
            return symbol.name
    return reference.text


def _provider_for_case(case: BenchmarkCase, config: LLMConfig) -> LLMProvider | None:
    if config.provider == LLMProviderName.FAKE:
        return FakeLLMProvider(case.fake_findings)
    return None


def _match_expected_findings(
    expected_findings: list[ExpectedFinding],
    findings: list[Finding],
) -> tuple[list[ExpectedFindingResult], list[Finding]]:
    candidate_indices = [
        [index for index, finding in enumerate(findings) if _finding_matches_expected(expected, finding)]
        for expected in expected_findings
    ]
    matched_finding_to_expected: dict[int, int] = {}

    def assign(expected_index: int, seen_findings: set[int]) -> bool:
        for finding_index in candidate_indices[expected_index]:
            if finding_index in seen_findings:
                continue
            seen_findings.add(finding_index)
            current_expected = matched_finding_to_expected.get(finding_index)
            if current_expected is None or assign(current_expected, seen_findings):
                matched_finding_to_expected[finding_index] = expected_index
                return True
        return False

    expected_order = sorted(
        range(len(expected_findings)),
        key=lambda index: (len(candidate_indices[index]), index),
    )
    for expected_index in expected_order:
        if candidate_indices[expected_index]:
            assign(expected_index, set())

    matched_expected_to_finding = {
        expected_index: finding_index for finding_index, expected_index in matched_finding_to_expected.items()
    }
    results = [
        ExpectedFindingResult(
            expected=expected,
            matched=index in matched_expected_to_finding,
            matched_title=(
                findings[matched_expected_to_finding[index]].title if index in matched_expected_to_finding else None
            ),
        )
        for index, expected in enumerate(expected_findings)
    ]
    unused = [finding for index, finding in enumerate(findings) if index not in matched_finding_to_expected]
    return results, unused


def _finding_matches_expected(expected: ExpectedFinding, finding: Finding) -> bool:
    if expected.file and finding.file != expected.file:
        return False
    if expected.line is not None and finding.line != expected.line:
        return False
    if expected.line_min is not None and (finding.line is None or finding.line < expected.line_min):
        return False
    if expected.line_max is not None and (finding.line is None or finding.line > expected.line_max):
        return False
    if expected.title_contains and expected.title_contains.lower() not in finding.title.lower():
        return False
    if expected.severity and finding.severity != expected.severity:
        return False
    if expected.confidence and finding.confidence != expected.confidence:
        return False
    if expected.failure_mode_contains and expected.failure_mode_contains.lower() not in finding.failure_mode.lower():
        return False
    if expected.evidence_contains and expected.evidence_contains.lower() not in finding.evidence.lower():
        return False
    if expected.suggested_fix_contains and expected.suggested_fix_contains.lower() not in finding.suggested_fix.lower():
        return False
    if (
        expected.suggested_test_contains
        and expected.suggested_test_contains.lower() not in finding.suggested_test.lower()
    ):
        return False
    return True


def _match_expected_context(expected: ExpectedContext, packs: list[ContextPack]) -> ExpectedContextResult:
    for pack in packs:
        if expected.pack_file and pack.file != expected.pack_file:
            continue
        if expected.pack_id_contains and expected.pack_id_contains not in pack.id:
            continue
        if expected.related_test and expected.related_test not in pack.related_tests:
            continue
        if expected.related_test_index is not None:
            if expected.related_test is None:
                continue
            if expected.related_test_index >= len(pack.related_tests):
                continue
            if pack.related_tests[expected.related_test_index] != expected.related_test:
                continue
        if _expects_related_test_snippet(expected) and not any(
            _related_test_snippet_matches(expected, snippet) for snippet in pack.related_test_snippets
        ):
            continue
        if _expects_reference(expected) and not any(
            _reference_matches(expected, reference) for reference in pack.references
        ):
            continue
        if expected.reference_snippet_contains and not any(
            expected.reference_snippet_contains in snippet.code for snippet in pack.reference_snippets
        ):
            continue
        if _expects_callee(expected) and not any(_callee_matches(expected, callee) for callee in pack.callees):
            continue
        if expected.callee_snippet_contains and not any(
            expected.callee_snippet_contains in snippet.code for snippet in pack.callee_snippets
        ):
            continue
        if _expects_contract(expected) and not any(
            _contract_matches(expected, contract) for contract in pack.contracts
        ):
            continue
        if expected.contract_snippet_contains and not any(
            expected.contract_snippet_contains in snippet.code for snippet in pack.contract_snippets
        ):
            continue
        if _expects_metadata(expected) and not any(
            _metadata_matches(expected, reference) for reference in pack.metadata
        ):
            continue
        if expected.metadata_snippet_contains and not any(
            expected.metadata_snippet_contains in snippet.code for snippet in pack.metadata_snippets
        ):
            continue
        return ExpectedContextResult(expected=expected, matched=True, matched_pack_id=pack.id)
    return ExpectedContextResult(expected=expected, matched=False)


def _expects_related_test_snippet(expected: ExpectedContext) -> bool:
    return bool(expected.related_test_snippet_contains or expected.related_test_snippet_start_min)


def _related_test_snippet_matches(expected: ExpectedContext, snippet: Any) -> bool:
    if expected.related_test and snippet.file != expected.related_test:
        return False
    if expected.related_test_snippet_contains and expected.related_test_snippet_contains not in snippet.code:
        return False
    if expected.related_test_snippet_start_min and snippet.start_line < expected.related_test_snippet_start_min:
        return False
    return True


def _expects_reference(expected: ExpectedContext) -> bool:
    return bool(expected.reference_file or expected.reference_kind or expected.reference_text_contains)


def _reference_matches(expected: ExpectedContext, reference: AnalyzerReference) -> bool:
    if expected.reference_file and reference.file != expected.reference_file:
        return False
    if expected.reference_kind and reference.kind != expected.reference_kind:
        return False
    if expected.reference_text_contains and expected.reference_text_contains not in reference.text:
        return False
    return True


def _expects_callee(expected: ExpectedContext) -> bool:
    return bool(expected.callee_file or expected.callee_kind or expected.callee_text_contains)


def _callee_matches(expected: ExpectedContext, callee: AnalyzerReference) -> bool:
    if expected.callee_file and callee.file != expected.callee_file:
        return False
    if expected.callee_kind and callee.kind != expected.callee_kind:
        return False
    if expected.callee_text_contains and expected.callee_text_contains not in callee.text:
        return False
    return True


def _expects_contract(expected: ExpectedContext) -> bool:
    return bool(expected.contract_file or expected.contract_kind or expected.contract_text_contains)


def _contract_matches(expected: ExpectedContext, contract: AnalyzerReference) -> bool:
    if expected.contract_file and contract.file != expected.contract_file:
        return False
    if expected.contract_kind and contract.kind != expected.contract_kind:
        return False
    if expected.contract_text_contains and expected.contract_text_contains not in contract.text:
        return False
    return True


def _expects_metadata(expected: ExpectedContext) -> bool:
    return bool(expected.metadata_file or expected.metadata_kind or expected.metadata_text_contains)


def _metadata_matches(expected: ExpectedContext, reference: AnalyzerReference) -> bool:
    if expected.metadata_file and reference.file != expected.metadata_file:
        return False
    if expected.metadata_kind and reference.kind != expected.metadata_kind:
        return False
    if expected.metadata_text_contains and expected.metadata_text_contains not in reference.text:
        return False
    return True


def _resolve_case_path(case_path: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (case_path.parent / path).resolve()


def _diff_for_capture(repo_root: Path, target_mode: TargetMode, base: str | None) -> str:
    if target_mode == TargetMode.WORKTREE:
        return git.diff_worktree(repo_root)
    if target_mode == TargetMode.STAGED:
        return git.diff_staged(repo_root)
    if target_mode == TargetMode.BASE:
        return git.diff_base(repo_root, base or "main")
    raise BenchmarkError("Patch mode is not supported for capture; use worktree, staged, or base.")


def _copy_if_exists(repo_root: Path, repo_dir: Path, rel_path: str) -> list[str]:
    source = (repo_root / rel_path).resolve()
    try:
        source.relative_to(repo_root.resolve())
    except ValueError:
        return []
    if not source.exists() or not source.is_file():
        return []
    target = repo_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return [rel_path]


def _copy_file_with_local_configs(repo_root: Path, repo_dir: Path, rel_path: str) -> list[str]:
    copied = _copy_if_exists(repo_root, repo_dir, rel_path)
    if not copied:
        return []
    copied.extend(_copy_local_config_ancestors(repo_root, repo_dir, rel_path))
    return copied


def _copy_local_config_ancestors(repo_root: Path, repo_dir: Path, rel_path: str) -> list[str]:
    copied: list[str] = []
    current = Path(rel_path).parent
    while True:
        for config_file in LOCAL_CONFIG_FILES:
            candidate = str(current / config_file) if str(current) != "." else config_file
            copied.extend(_copy_if_exists(repo_root, repo_dir, candidate))
        if str(current) == ".":
            break
        current = current.parent
    return copied


def _captured_case_dict(
    name: str,
    llm: bool,
    provider: LLMProviderName,
    verify: bool,
    expected_title_contains: str | None,
    expected_file: str | None,
    expected_context: list[ExpectedContext],
) -> dict[str, object]:
    expected = []
    if expected_title_contains or expected_file:
        expected.append(
            {
                key: value
                for key, value in {
                    "file": expected_file,
                    "title_contains": expected_title_contains,
                }.items()
                if value
            }
        )
    return {
        "name": name,
        "repo": "repo",
        "diff": "change.diff",
        "llm": llm,
        "provider": provider.value,
        "verify": verify,
        "expected": expected,
        "expected_context": [item.model_dump(mode="json", exclude_none=True) for item in expected_context],
    }
