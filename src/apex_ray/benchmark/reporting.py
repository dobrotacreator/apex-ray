from apex_ray.benchmark.models import (
    BenchmarkCaseComparison,
    BenchmarkCaseResult,
    BenchmarkComparisonReport,
    BenchmarkComparisonSummary,
    BenchmarkReport,
)


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
            old_context_misses=sum_context_misses(old.cases),
            new_context_misses=sum_context_misses(new.cases),
            context_miss_delta=sum_context_misses(new.cases) - sum_context_misses(old.cases),
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
        f"- Expected context: `{sum_context_expected(report.cases)}`",
        f"- Missed context: `{sum_context_misses(report.cases)}`",
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
            lines.append(f"- LLM prompt versions: `{format_prompt_versions(case.llm_prompt_versions)}`")
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


def format_llm_route_summary(route: object) -> str:
    kind = getattr(route, "kind", "run")
    provider = getattr(route, "provider", "unknown")
    status = getattr(route, "status", "unknown")
    profile = getattr(route, "profile", None)
    model = getattr(route, "model", None)
    runs = getattr(route, "runs", 0)
    tokens = getattr(route, "estimated_input_tokens", 0)
    identity = profile or model or "default"
    return f"{kind}/{provider}/{identity}/{status}: {runs} runs, ~{tokens} tokens"


def sum_context_expected(cases: list[BenchmarkCaseResult]) -> int:
    return sum(len(case.expected_context_results) for case in cases)


def sum_context_misses(cases: list[BenchmarkCaseResult]) -> int:
    return sum(1 for case in cases for result in case.expected_context_results if not result.matched)


def _compare_benchmark_case(
    name: str,
    old: BenchmarkCaseResult | None,
    new: BenchmarkCaseResult | None,
) -> BenchmarkCaseComparison:
    if old is None and new is None:
        raise ValueError(f"Cannot compare missing case: {name}")
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
            f"LLM prompt versions changed from {format_prompt_versions(old.llm_prompt_versions)} "
            f"to {format_prompt_versions(new.llm_prompt_versions)}"
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
                f"- LLM prompt versions: `{format_prompt_versions(case.old_llm_prompt_versions)}` -> "
                f"`{format_prompt_versions(case.new_llm_prompt_versions)}`"
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


def _format_delta(value: int) -> str:
    if value > 0:
        return f"+{value}"
    return str(value)


def format_prompt_versions(prompt_versions: list[str]) -> str:
    return ", ".join(prompt_versions) if prompt_versions else "none"
