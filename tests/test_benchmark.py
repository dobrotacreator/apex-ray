import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apex_ray.benchmark import (
    BenchmarkCaseResult,
    BenchmarkError,
    BenchmarkReport,
    ExpectedContext,
    ExpectedContextResult,
    ExpectedFinding,
    ExpectedFindingResult,
    _llm_run_cache_hits,
    _llm_run_cache_misses,
    _match_expected_findings,
    benchmark_comparison_gate_failures,
    compare_benchmark_reports,
    load_benchmark_case,
    load_benchmark_report,
    render_benchmark_comparison,
    render_benchmark_report,
    run_benchmark_cases,
)
from apex_ray.cli import app
from apex_ray.llm.cache import REVIEW_PROMPT_VERSION, VERIFIER_PROMPT_VERSION
from apex_ray.models import Finding, FindingConfidence, FindingSeverity, LLMRun

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "tests" / "benchmarks"
CONTEXT_BENCHMARK_CASES = [
    *sorted(BENCHMARKS.glob("*_context.yml")),
    BENCHMARKS / "routes_context_static.yml",
]
runner = CliRunner()


def test_load_benchmark_case() -> None:
    case = load_benchmark_case(BENCHMARKS / "cart_bug_fake.yml")

    assert case.name == "cart quantity regression fake"
    assert case.provider == "fake"
    assert case.expected[0].title_contains == "quantity"
    assert case.expected_context[0].related_test == "tests/cart.test.ts"


def test_codex_benchmark_case_paths_exist() -> None:
    for case_path in sorted((BENCHMARKS / "codex").glob("*.yml")):
        case = load_benchmark_case(case_path)

        assert (case_path.parent / case.repo).resolve().exists(), case_path.name
        assert (case_path.parent / case.diff).resolve().exists(), case_path.name


def test_load_benchmark_case_missing_file_raises_benchmark_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yml"

    try:
        load_benchmark_case(missing)
    except BenchmarkError as exc:
        assert str(missing) in str(exc)
    else:
        raise AssertionError("Expected BenchmarkError")


def test_load_benchmark_case_rejects_unknown_expected_keys(tmp_path: Path) -> None:
    case_path = tmp_path / "case.yml"
    case_path.write_text(
        "name: typo\n"
        "repo: repo\n"
        "diff: change.diff\n"
        "expected:\n"
        "  - title_contains: quantity\n"
        "    failure_mode_contians: typo\n",
        encoding="utf-8",
    )

    try:
        load_benchmark_case(case_path)
    except BenchmarkError as exc:
        assert "failure_mode_contians" in str(exc)
    else:
        raise AssertionError("Expected BenchmarkError")


def test_run_benchmark_cases_with_fake_provider() -> None:
    report = run_benchmark_cases([BENCHMARKS / "cart_bug_fake.yml"])

    assert report.total == 1
    assert report.passed == 1
    assert report.failed == 0
    assert report.cases[0].findings_count == 1
    assert report.cases[0].llm_runs_count == 2
    assert report.cases[0].llm_input_chars > 0
    assert report.cases[0].llm_estimated_input_tokens > 0
    assert report.cases[0].llm_routes
    assert report.cases[0].llm_prompt_versions == [REVIEW_PROMPT_VERSION, VERIFIER_PROMPT_VERSION]
    assert report.cases[0].verifications_count == 1
    assert report.cases[0].verifier_approved_count == 1
    assert report.cases[0].verifier_rejected_count == 0
    assert report.cases[0].expected_context_results[0].matched is True
    assert report.expected_findings_total == 1
    assert report.missed_findings_total == 0
    assert report.expected_context_total == 1
    assert report.missed_context_total == 0
    assert report.extra_findings_total == 0


@pytest.mark.parametrize("case_path", CONTEXT_BENCHMARK_CASES, ids=lambda path: path.stem)
def test_context_benchmark_case_passes(case_path: Path, built_ts_analyzer: None) -> None:
    report = run_benchmark_cases([case_path])

    assert report.total == 1
    assert report.failed == 0
    assert report.missed_context_total == 0


def test_benchmark_cache_telemetry_uses_batch_counters() -> None:
    run = LLMRun(
        kind="verify",
        provider="fake",
        context_pack_id="pack-1",
        status="ok",
        duration_ms=1,
        cache_hits=2,
        cache_misses=1,
    )

    assert _llm_run_cache_hits(run) == 2
    assert _llm_run_cache_misses(run) == 1


def test_run_benchmark_cases_parallel_preserves_input_order() -> None:
    paths = [BENCHMARKS / "cart_bug_fake.yml", BENCHMARKS / "cart_bug_fake.yml"]

    report = run_benchmark_cases(paths, jobs=2)

    assert report.total == 2
    assert [case.name for case in report.cases] == ["cart quantity regression fake", "cart quantity regression fake"]


def test_expected_finding_matching_checks_quality_fields() -> None:
    finding = _finding("Cart totals ignore item quantity")

    results, extra = _match_expected_findings(
        [
            ExpectedFinding(
                file="src/cart.ts",
                line=7,
                title_contains="quantity",
                severity=FindingSeverity.MEDIUM,
                confidence=FindingConfidence.HIGH,
                failure_mode_contains="unexpected behavior",
                evidence_contains="benchmark fixture",
                suggested_fix_contains="fix the issue",
                suggested_test_contains="regression test",
            )
        ],
        [finding],
    )

    assert results[0].matched is True
    assert extra == []


def test_expected_finding_matching_counts_duplicate_titles_as_extra() -> None:
    first = _finding("Missing permission check")
    second = first.model_copy(update={"line": 11, "failure_mode": "Second endpoint is public."})

    results, extra = _match_expected_findings(
        [ExpectedFinding(file="src/cart.ts", title_contains="permission")],
        [first, second],
    )

    assert results[0].matched is True
    assert extra == [second]


def test_expected_finding_matching_finds_non_greedy_assignment() -> None:
    specific = _finding("Cart totals ignore item quantity")
    broad = specific.model_copy(
        update={
            "line": 12,
            "title": "Different cart problem",
            "failure_mode": "A different failure mode.",
        }
    )

    results, extra = _match_expected_findings(
        [
            ExpectedFinding(file="src/cart.ts"),
            ExpectedFinding(file="src/cart.ts", line=7, title_contains="quantity"),
        ],
        [specific, broad],
    )

    assert [result.matched for result in results] == [True, True]
    assert results[1].matched_title == "Cart totals ignore item quantity"
    assert extra == []


def test_benchmark_report_json_includes_quality_totals() -> None:
    report = BenchmarkReport(
        cases=[
            _benchmark_case_result("matched", passed=True, matched=True, context_matched=True),
            _benchmark_case_result("missed", passed=False, matched=False, context_matched=False),
            _benchmark_case_result("extra", passed=False, matched=True),
        ],
        total=3,
        passed=1,
        failed=2,
    )

    data = report.model_dump(mode="json")

    assert data["expected_findings_total"] == 3
    assert data["missed_findings_total"] == 1
    assert data["expected_context_total"] == 2
    assert data["missed_context_total"] == 1
    assert data["extra_findings_total"] == 1


def test_load_benchmark_report_recomputes_quality_totals_for_legacy_json(tmp_path: Path) -> None:
    report = BenchmarkReport(
        cases=[_benchmark_case_result("case", passed=False, matched=False, context_matched=False)],
        total=1,
        passed=0,
        failed=1,
    )
    legacy_data = report.model_dump(mode="json")
    for key in [
        "expected_findings_total",
        "missed_findings_total",
        "expected_context_total",
        "missed_context_total",
        "extra_findings_total",
    ]:
        legacy_data.pop(key)
    report_path = tmp_path / "benchmark.json"
    report_path.write_text(json.dumps(legacy_data), encoding="utf-8")

    loaded = load_benchmark_report(report_path)

    assert loaded.expected_findings_total == 1
    assert loaded.missed_findings_total == 1
    assert loaded.expected_context_total == 1
    assert loaded.missed_context_total == 1
    assert loaded.extra_findings_total == 0


def test_render_benchmark_report() -> None:
    report = run_benchmark_cases([BENCHMARKS / "cart_bug_fake.yml"])
    rendered = render_benchmark_report(report)

    assert "# Apex Ray Benchmark" in rendered
    assert "PASS: cart quantity regression fake" in rendered
    assert "Expected context" in rendered
    assert "Missed context: `0`" in rendered


def test_compare_benchmark_reports_detects_regression_and_improvement() -> None:
    old = BenchmarkReport(
        cases=[
            _benchmark_case_result("regressed", passed=True, matched=True),
            _benchmark_case_result("improved", passed=False, matched=False),
            _benchmark_case_result("unchanged", passed=True, matched=True, duration_ms=1000, cache_hits=0),
        ],
        total=3,
        passed=2,
        failed=1,
    )
    new = BenchmarkReport(
        cases=[
            _benchmark_case_result("regressed", passed=False, matched=False),
            _benchmark_case_result("improved", passed=True, matched=True),
            _benchmark_case_result("unchanged", passed=True, matched=True, duration_ms=8000, cache_hits=1),
            _benchmark_case_result("added", passed=True, matched=True),
        ],
        total=4,
        passed=3,
        failed=1,
    )

    comparison = compare_benchmark_reports(old, new)

    assert comparison.summary.regressions == 1
    assert comparison.summary.improvements == 1
    assert comparison.summary.added == 1
    by_name = {case.name: case for case in comparison.cases}
    assert by_name["regressed"].status == "regression"
    assert "new missed expected finding" in by_name["regressed"].messages[0]
    assert by_name["improved"].status == "improvement"
    assert by_name["unchanged"].status == "unchanged_pass"
    assert "LLM duration increased by 7000ms" in by_name["unchanged"].messages
    assert comparison.summary.context_miss_delta == 0


def test_benchmark_comparison_gate_fails_on_added_failing_case() -> None:
    old = BenchmarkReport(
        cases=[_benchmark_case_result("stable", passed=True, matched=True)],
        total=1,
        passed=1,
        failed=0,
    )
    new = BenchmarkReport(
        cases=[
            _benchmark_case_result("stable", passed=True, matched=True),
            _benchmark_case_result("new failing", passed=False, matched=False),
        ],
        total=2,
        passed=1,
        failed=1,
    )

    comparison = compare_benchmark_reports(old, new)

    assert comparison.summary.regressions == 0
    assert benchmark_comparison_gate_failures(comparison) == [
        "failed cases increased from 0 to 1",
    ]


def test_compare_benchmark_reports_tracks_context_misses() -> None:
    old = BenchmarkReport(
        cases=[_benchmark_case_result("case", passed=True, matched=True, context_matched=True)],
        total=1,
        passed=1,
        failed=0,
    )
    new = BenchmarkReport(
        cases=[_benchmark_case_result("case", passed=False, matched=True, context_matched=False)],
        total=1,
        passed=0,
        failed=1,
    )

    comparison = compare_benchmark_reports(old, new)

    assert comparison.summary.old_context_misses == 0
    assert comparison.summary.new_context_misses == 1
    assert comparison.summary.context_miss_delta == 1
    assert comparison.cases[0].status == "regression"
    assert any("new missed expected context" in message for message in comparison.cases[0].messages)


def test_compare_benchmark_reports_tracks_prompt_version_changes() -> None:
    old = BenchmarkReport(
        cases=[
            _benchmark_case_result(
                "case",
                passed=True,
                matched=True,
                prompt_versions=["review-v1", "verify-v1"],
            )
        ],
        total=1,
        passed=1,
        failed=0,
    )
    new = BenchmarkReport(
        cases=[
            _benchmark_case_result(
                "case",
                passed=True,
                matched=True,
                prompt_versions=["review-v2", "verify-v2"],
            )
        ],
        total=1,
        passed=1,
        failed=0,
    )

    comparison = compare_benchmark_reports(old, new)

    assert comparison.cases[0].old_llm_prompt_versions == ["review-v1", "verify-v1"]
    assert comparison.cases[0].new_llm_prompt_versions == ["review-v2", "verify-v2"]
    assert "LLM prompt versions changed" in comparison.cases[0].messages[0]


def test_compare_benchmark_reports_tracks_verifier_count_changes() -> None:
    old = BenchmarkReport(
        cases=[
            _benchmark_case_result(
                "case",
                passed=True,
                matched=True,
                verifier_approved_count=1,
                verifier_rejected_count=0,
            )
        ],
        total=1,
        passed=1,
        failed=0,
    )
    new = BenchmarkReport(
        cases=[
            _benchmark_case_result(
                "case",
                passed=True,
                matched=True,
                verifier_approved_count=0,
                verifier_rejected_count=1,
            )
        ],
        total=1,
        passed=1,
        failed=0,
    )

    comparison = compare_benchmark_reports(old, new)

    case = comparison.cases[0]
    assert case.old_verifier_approved_count == 1
    assert case.new_verifier_approved_count == 0
    assert case.old_verifier_rejected_count == 0
    assert case.new_verifier_rejected_count == 1
    assert "verifier approvals changed from 1 to 0" in case.messages
    assert "verifier rejections changed from 0 to 1" in case.messages


def test_render_benchmark_comparison() -> None:
    comparison = compare_benchmark_reports(
        BenchmarkReport(cases=[_benchmark_case_result("case", passed=True, matched=True)], total=1, passed=1, failed=0),
        BenchmarkReport(
            cases=[_benchmark_case_result("case", passed=False, matched=False)], total=1, passed=0, failed=1
        ),
    )

    rendered = render_benchmark_comparison(comparison)

    assert "# Apex Ray Benchmark Compare" in rendered
    assert "Gate: `fail`" in rendered
    assert "Gate reasons:" in rendered
    assert "Regressions: `1`" in rendered
    assert "Context misses:" in rendered
    assert "## Regressions" in rendered
    assert "### case" in rendered


def test_compare_benchmark_cli_writes_reports(tmp_path: Path) -> None:
    old_report = BenchmarkReport(
        cases=[_benchmark_case_result("case", passed=True, matched=True)],
        total=1,
        passed=1,
        failed=0,
    )
    new_report = BenchmarkReport(
        cases=[_benchmark_case_result("case", passed=False, matched=False)],
        total=1,
        passed=0,
        failed=1,
    )
    old_path = tmp_path / "old.json"
    new_path = tmp_path / "new.json"
    output = tmp_path / "compare.md"
    json_output = tmp_path / "compare.json"
    old_path.write_text(old_report.model_dump_json(indent=2), encoding="utf-8")
    new_path.write_text(new_report.model_dump_json(indent=2), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "compare-benchmark",
            str(old_path),
            str(new_path),
            "--output",
            str(output),
            "--json",
            str(json_output),
            "--no-fail-on-regression",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Wrote" in result.stdout
    assert "Regressions: `1`" in output.read_text(encoding="utf-8")
    assert json.loads(json_output.read_text(encoding="utf-8"))["summary"]["regressions"] == 1


def test_compare_benchmark_cli_fails_on_regression_by_default(tmp_path: Path) -> None:
    old_report = BenchmarkReport(
        cases=[_benchmark_case_result("case", passed=True, matched=True)],
        total=1,
        passed=1,
        failed=0,
    )
    new_report = BenchmarkReport(
        cases=[_benchmark_case_result("case", passed=False, matched=False)],
        total=1,
        passed=0,
        failed=1,
    )
    old_path = tmp_path / "old.json"
    new_path = tmp_path / "new.json"
    output = tmp_path / "compare.md"
    json_output = tmp_path / "compare.json"
    old_path.write_text(old_report.model_dump_json(indent=2), encoding="utf-8")
    new_path.write_text(new_report.model_dump_json(indent=2), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "compare-benchmark",
            str(old_path),
            str(new_path),
            "--output",
            str(output),
            "--json",
            str(json_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Gate: `fail`" in output.read_text(encoding="utf-8")


def test_benchmark_cli_writes_reports(tmp_path: Path, monkeypatch, built_ts_analyzer: None) -> None:
    monkeypatch.chdir(ROOT)
    output = tmp_path / "benchmark.md"
    json_output = tmp_path / "benchmark.json"

    result = runner.invoke(
        app,
        [
            "benchmark",
            str(BENCHMARKS / "cart_bug_fake.yml"),
            "--output",
            str(output),
            "--json",
            str(json_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Wrote" in result.stdout
    assert "PASS: cart quantity regression fake" in output.read_text(encoding="utf-8")
    data = json.loads(json_output.read_text(encoding="utf-8"))
    assert data["passed"] == 1
    assert data["expected_findings_total"] == 1
    assert data["expected_context_total"] == 1
    assert data["missed_context_total"] == 0
    assert data["cases"][0]["llm_prompt_versions"] == [REVIEW_PROMPT_VERSION, VERIFIER_PROMPT_VERSION]
    assert data["cases"][0]["verifications_count"] == 1
    assert data["cases"][0]["verifier_approved_count"] == 1
    assert data["cases"][0]["verifier_rejected_count"] == 0


def test_capture_benchmark_cli_writes_self_contained_case(tmp_path: Path, built_ts_analyzer: None) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    (repo / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (repo / "tsconfig.json").write_text('{"include":["src/**/*.ts"]}\n', encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "tsconfig.json").write_text('{"include":["*.ts"]}\n', encoding="utf-8")
    source = repo / "src" / "cart.ts"
    source.write_text(
        "export function calculateTotal(price: number, quantity: number) {\n  return price * quantity;\n}\n",
        encoding="utf-8",
    )
    (repo / "src" / "checkout.ts").write_text(
        'import { calculateTotal } from "./cart";\nexport const checkoutTotal = calculateTotal(10, 2);\n',
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "cart.test.ts").write_text(
        'import { calculateTotal } from "../src/cart";\n'
        "it('multiplies quantity', () => expect(calculateTotal(10, 2)).toBe(20));\n",
        encoding="utf-8",
    )
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", "initial"], repo)
    source.write_text(
        "export function calculateTotal(price: number, quantity: number) {\n  return price;\n}\n",
        encoding="utf-8",
    )

    output = tmp_path / "captured"
    result = runner.invoke(
        app,
        [
            "capture-benchmark",
            "--repo",
            str(repo),
            "--worktree",
            "--name",
            "captured cart regression",
            "--output",
            str(output),
            "--expected-title-contains",
            "quantity",
            "--expected-file",
            "src/cart.ts",
            "--no-llm",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert (output / "case.yml").exists()
    assert (output / "change.diff").exists()
    assert (output / "repo" / "src" / "cart.ts").read_text(encoding="utf-8").count("return price;") == 1
    assert (output / "repo" / "src" / "checkout.ts").exists()
    assert (output / "repo" / "src" / "tsconfig.json").exists()
    assert (output / "repo" / "tests" / "cart.test.ts").exists()
    assert not (repo / ".apex-ray" / "cache").exists()
    case = load_benchmark_case(output / "case.yml")
    assert case.name == "captured cart regression"
    assert case.repo == "repo"
    assert case.diff == "change.diff"
    assert case.llm is False
    assert case.expected[0].title_contains == "quantity"
    assert case.expected_context[0].pack_file == "src/cart.ts"
    assert case.expected_context[0].related_test == "tests/cart.test.ts"
    assert case.expected_context[0].reference_file == "src/checkout.ts"
    assert case.expected_context[0].reference_kind == "call"
    assert case.expected_context[0].reference_text_contains == "calculateTotal"


def _run(command: list[str], cwd: Path) -> None:
    import subprocess

    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _benchmark_case_result(
    name: str,
    passed: bool,
    matched: bool,
    duration_ms: int = 100,
    cache_hits: int = 0,
    context_matched: bool | None = None,
    prompt_versions: list[str] | None = None,
    verifier_approved_count: int = 0,
    verifier_rejected_count: int = 0,
) -> BenchmarkCaseResult:
    expected = ExpectedFinding(file="src/cart.ts", title_contains="quantity")
    extra_findings = [] if passed or not matched else [_finding("Unexpected issue")]
    expected_context_results = []
    if context_matched is not None:
        expected_context_results = [
            ExpectedContextResult(
                expected=ExpectedContext(pack_file="src/cart.ts", reference_file="src/checkout.ts"),
                matched=context_matched,
                matched_pack_id="src/cart.ts#calculateTotal:1" if context_matched else None,
            )
        ]
    return BenchmarkCaseResult(
        name=name,
        passed=passed,
        repo="/repo",
        diff="/repo/change.diff",
        findings_count=1 if matched or extra_findings else 0,
        context_packs_count=1,
        llm_runs_count=1,
        llm_cache_hits=cache_hits,
        llm_cache_misses=1 - cache_hits,
        llm_duration_ms=duration_ms,
        llm_prompt_versions=prompt_versions or [],
        verifications_count=verifier_approved_count + verifier_rejected_count,
        verifier_approved_count=verifier_approved_count,
        verifier_rejected_count=verifier_rejected_count,
        expected_results=[
            ExpectedFindingResult(
                expected=expected,
                matched=matched,
                matched_title="Cart totals ignore item quantity" if matched else None,
            )
        ],
        expected_context_results=expected_context_results,
        extra_findings=extra_findings,
    )


def _finding(title: str) -> Finding:
    return Finding(
        title=title,
        severity=FindingSeverity.MEDIUM,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Unexpected behavior.",
        evidence="Benchmark fixture.",
        suggested_fix="Fix the issue.",
        suggested_test="Add a regression test.",
    )
