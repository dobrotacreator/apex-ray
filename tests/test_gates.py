import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apex_ray.cli import app
from apex_ray.cli.gate import (
    _combine_incremental_decision,
    _coverage_followup_blocking_pack_ids,
    _coverage_followup_force_pack_ids,
    _coverage_followup_policy,
    _coverage_followup_reviewer_ids,
    _resolve_incremental_carried_findings,
    _retry_coverage_report,
    _retry_resolution_report,
    resolve_carried_findings,
)
from apex_ray.diff import parse_unified_diff
from apex_ray.findings import findings_are_duplicates
from apex_ray.gate_retry import (
    CarriedFinding,
    CoverageDebt,
    build_pre_push_state,
    config_fingerprint,
    load_pre_push_state,
    relevant_files_for_finding,
    stale_carried_finding_reason,
)
from apex_ray.gates import PrePushGateDecision, evaluate_pre_push_gate, render_pre_push_gate_stdout
from apex_ray.llm.providers import FakeLLMProvider
from apex_ray.models import (
    AnalyzerReference,
    ChangedFile,
    ContextPack,
    DiffStats,
    DiffSummary,
    FileKind,
    FileStatus,
    Finding,
    FindingConfidence,
    FindingSeverity,
    FindingVerification,
    LLMAPIConfig,
    LLMContextSelection,
    LLMCoverageTodo,
    LLMPackReviewStatus,
    LLMProfile,
    LLMProviderName,
    LLMResidualRiskSummary,
    LLMReviewerCoverageSummary,
    LLMRun,
    ProjectProfile,
    ReviewConfig,
    ReviewerConfig,
    ReviewInputSnapshot,
    RiskSeverity,
    RiskSignal,
    RuleMatch,
    RuleMode,
    TargetMode,
)
from apex_ray.progress import NoopProgress
from apex_ray.report import build_report


def test_incremental_retry_fingerprint_includes_selected_reviewer_scope() -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", paths=["src/auth/**"]),
            ReviewerConfig(id="finance", paths=["src/payments/**"]),
        ]
    )

    security = config_fingerprint(config, config.gates.pre_push, reviewer_ids=["security"])
    finance = config_fingerprint(config, config.gates.pre_push, reviewer_ids=["finance"])

    assert security != finance


def test_incremental_retry_fingerprint_normalizes_reviewer_order() -> None:
    first = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", paths=["src/auth/**"]),
            ReviewerConfig(id="finance", paths=["src/payments/**"]),
        ]
    )
    second = ReviewConfig(reviewers=list(reversed(first.reviewers)))

    first_hash = config_fingerprint(
        first,
        first.gates.pre_push,
        reviewer_ids=["security", "finance", "security"],
    )
    second_hash = config_fingerprint(
        second,
        second.gates.pre_push,
        reviewer_ids=["finance", "security"],
    )

    assert first_hash == second_hash


def test_incremental_retry_fingerprint_includes_effective_reviewer_config() -> None:
    original = ReviewConfig(reviewers=[ReviewerConfig(id="security", paths=["src/auth/**"], risk_tags=["security"])])
    changed = original.model_copy(deep=True)
    changed.reviewers[0].paths = ["src/identity/**"]

    original_hash = config_fingerprint(
        original,
        original.gates.pre_push,
        reviewer_ids=["security"],
    )
    changed_hash = config_fingerprint(
        changed,
        changed.gates.pre_push,
        reviewer_ids=["security"],
    )

    assert original_hash != changed_hash


def test_incremental_retry_fingerprint_includes_api_endpoint_environment_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.OPENAI_COMPATIBLE
    config.llm.api = LLMAPIConfig(base_url_env="CUSTOM_REVIEW_URL")
    monkeypatch.setenv("CUSTOM_REVIEW_URL", "https://first.example/v1")
    first_hash = config_fingerprint(config, config.gates.pre_push)

    monkeypatch.setenv("CUSTOM_REVIEW_URL", "https://second.example/v1")
    second_hash = config_fingerprint(config, config.gates.pre_push)

    assert first_hash != second_hash


def test_incremental_retry_fingerprint_includes_api_header_environment_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.OPENAI_COMPATIBLE
    config.llm.api = LLMAPIConfig(
        base_url="https://review.example/v1",
        headers_from_env={"X-Tenant": "CUSTOM_REVIEW_TENANT"},
    )
    monkeypatch.setenv("CUSTOM_REVIEW_TENANT", "tenant-a")
    first_hash = config_fingerprint(config, config.gates.pre_push)

    monkeypatch.setenv("CUSTOM_REVIEW_TENANT", "tenant-b")
    second_hash = config_fingerprint(config, config.gates.pre_push)

    assert first_hash != second_hash


def test_incremental_retry_fingerprint_includes_api_profile_environment_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.profiles = {
        "api-review": LLMProfile(
            provider=LLMProviderName.OPENAI_COMPATIBLE,
            api=LLMAPIConfig(base_url_env="PROFILE_REVIEW_URL"),
        )
    }
    monkeypatch.setenv("PROFILE_REVIEW_URL", "https://first.example/v1")
    first_hash = config_fingerprint(config, config.gates.pre_push)

    monkeypatch.setenv("PROFILE_REVIEW_URL", "https://second.example/v1")
    second_hash = config_fingerprint(config, config.gates.pre_push)

    assert first_hash != second_hash


def test_incremental_retry_fingerprint_does_not_include_api_key_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.OPENAI_COMPATIBLE
    config.llm.api = LLMAPIConfig(
        base_url="https://review.example/v1",
        api_key_env="CUSTOM_REVIEW_API_KEY",
    )
    monkeypatch.setenv("CUSTOM_REVIEW_API_KEY", "first-secret")
    first_hash = config_fingerprint(config, config.gates.pre_push)

    monkeypatch.setenv("CUSTOM_REVIEW_API_KEY", "second-secret")
    second_hash = config_fingerprint(config, config.gates.pre_push)

    assert first_hash == second_hash


def test_incremental_decision_deduplicates_current_and_carried_finding() -> None:
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        failure_mode="The lookup can cross tenant boundaries.",
        evidence="The same concrete issue remains in the current report.",
        suggested_fix="Restore tenant scoping.",
        suggested_test="Add a cross-tenant lookup regression test.",
    )
    current = PrePushGateDecision(
        blocked=True,
        reasons=["Blocking findings: 1 >= high"],
        blocking_findings=[finding],
    )

    combined = _combine_incremental_decision(
        current,
        [CarriedFinding(finding=finding)],
        CoverageDebt(),
    )

    assert combined.blocking_findings == [finding]
    assert combined.reasons == current.reasons


def test_unverified_gate_keeps_legacy_finding_with_positive_in_scope_run(
    tmp_path: Path,
) -> None:
    canonical = ContextPack(id="src/a.ts#authorize:1", file="src/a.ts")
    possible_origin = ContextPack(id="src/b.ts#dispatch:2", file="src/b.ts")
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="finance",
                paths=["src/b.ts"],
                verify=False,
            )
        ]
    )
    config.gates.pre_push.require_verified_findings = False
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=canonical.file,
        failure_mode="A transfer can bypass the tenant boundary.",
        evidence="No authorization predicate precedes the transfer.",
        suggested_fix="Add a tenant authorization check.",
        suggested_test="Reject cross-tenant transfers.",
        context_pack_id=canonical.id,
        reviewer_ids=["finance"],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[canonical, possible_origin],
        findings=[finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id=possible_origin.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            )
        ],
    )

    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    assert decision.blocked is True
    assert decision.blocking_findings == [finding]


def test_unverified_gate_keeps_legacy_finding_when_in_scope_review_failed(
    tmp_path: Path,
) -> None:
    canonical = ContextPack(id="src/a.ts#authorize:1", file="src/a.ts")
    current_scope = ContextPack(id="src/b.ts#dispatch:2", file="src/b.ts")
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="finance",
                paths=["src/b.ts"],
                verify=False,
            )
        ]
    )
    config.gates.pre_push.require_verified_findings = False
    config.gates.pre_push.fail_on_quality_gate = False
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=canonical.file,
        failure_mode="A transfer can bypass the tenant boundary.",
        evidence="No authorization predicate precedes the transfer.",
        suggested_fix="Add a tenant authorization check.",
        suggested_test="Reject cross-tenant transfers.",
        context_pack_id=canonical.id,
        reviewer_ids=["finance"],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[canonical, current_scope],
        findings=[finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id=current_scope.id,
                status="failed_provider",
                duration_ms=1,
            )
        ],
    )

    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    assert decision.blocked is True
    assert decision.blocking_findings == [finding]


@pytest.mark.parametrize(
    ("reviewer_ids", "reviewers"),
    [
        pytest.param(
            ["finance"],
            [ReviewerConfig(id="security", paths=["src/auth/**"], verify=False)],
            id="removed-reviewer",
        ),
        pytest.param(
            ["finance"],
            [
                ReviewerConfig(id="security", paths=["src/auth/**"], verify=False),
                ReviewerConfig(id="finance", enabled=False, verify=False),
            ],
            id="disabled-reviewer",
        ),
        pytest.param(
            [],
            [ReviewerConfig(id="security", paths=["src/auth/**"], verify=False)],
            id="legacy-empty-provenance",
        ),
    ],
)
def test_unverified_gate_keeps_finding_when_provenance_cannot_be_mapped(
    tmp_path: Path,
    reviewer_ids: list[str],
    reviewers: list[ReviewerConfig],
) -> None:
    pack = ContextPack(id="src/legacy.ts#authorize:1", file="src/legacy.ts")
    config = ReviewConfig(reviewers=reviewers)
    config.gates.pre_push.require_verified_findings = False
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        failure_mode="A transfer can bypass the tenant boundary.",
        evidence="No authorization predicate precedes the transfer.",
        suggested_fix="Add a tenant authorization check.",
        suggested_test="Reject cross-tenant transfers.",
        context_pack_id=pack.id,
        reviewer_ids=reviewer_ids,
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        reviewer_scope_ids=["security"],
    )

    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    assert decision.blocked is True
    assert decision.blocking_findings == [finding]


def test_unverified_gate_retires_legacy_multi_reviewer_debt_after_clean_scopes(
    tmp_path: Path,
) -> None:
    canonical = ContextPack(id="src/old.ts#authorize:1", file="src/old.ts")
    security_pack = ContextPack(id="src/auth.ts#authorize:2", file="src/auth.ts")
    finance_pack = ContextPack(
        id="src/payments.ts#settle:3",
        file="src/payments.ts",
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", paths=["src/auth.ts"], verify=False),
            ReviewerConfig(id="finance", paths=["src/payments.ts"], verify=False),
        ]
    )
    config.gates.pre_push.require_verified_findings = False
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=canonical.file,
        failure_mode="A transfer can bypass the tenant boundary.",
        evidence="No authorization predicate precedes the transfer.",
        suggested_fix="Add a tenant authorization check.",
        suggested_test="Reject cross-tenant transfers.",
        context_pack_id=canonical.id,
        reviewer_ids=["finance", "security"],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[canonical, security_pack, finance_pack],
        findings=[finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=security_pack.id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id=finance_pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
    )

    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    assert decision.blocked is False
    assert decision.blocking_findings == []


def test_pre_push_gate_stdout_explains_provider_failures_without_findings(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[
            ContextPack(
                id="src/auth.ts#login:1",
                file="src/auth.ts",
                file_kind=FileKind.SOURCE,
                risk_signals=[
                    RiskSignal(kind="auth", severity=RiskSeverity.HIGH, reason="Auth changed.", file="src/auth.ts")
                ],
            )
        ],
        llm_runs=[
            LLMRun(
                provider="claude_code_cli",
                model="opus",
                context_pack_id="src/auth.ts#login:1",
                status="failed_provider",
                duration_ms=12,
                error="LLM finding response contained invalid JSON.",
            )
        ],
    )

    decision = evaluate_pre_push_gate(report, config.gates.pre_push)
    stdout = render_pre_push_gate_stdout(
        report,
        decision,
        markdown_path=tmp_path / "pre-push.md",
        json_path=tmp_path / "pre-push.json",
        base="main",
        config=config.gates.pre_push,
    )

    assert decision.blocked is True
    assert report.findings == []
    assert "LLM review run failures: failed_provider: 1" in stdout


def test_pre_push_gate_stdout_never_labels_partial_review_as_plain_pass(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.gates.pre_push.fail_on_partial_severity = "critical"
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    report.llm_coverage.total_context_packs = 10
    report.llm_coverage.reviewed_context_packs = 4
    report.llm_coverage.unreviewed_context_packs = 6
    report.llm_coverage.coverage_ratio = 0.4
    report.llm_coverage.high_risk_context_packs = 2
    report.llm_coverage.reviewed_high_risk_context_packs = 2
    report.llm_coverage.high_risk_coverage_ratio = 1.0
    report.llm_coverage.partial_severity = "minor"
    report.llm_coverage.residual_risk_context_packs = [
        LLMResidualRiskSummary(
            context_pack_id=f"src/file-{index}.ts#pack:1",
            file=f"src/file-{index}.ts",
            priority="p1" if index < 2 else "p2",
            reason="not selected by LLM pack cap",
        )
        for index in range(6)
    ]
    report.llm_coverage.reviewers = [
        LLMReviewerCoverageSummary(
            reviewer_id="correctness",
            required=True,
            matching_context_packs=10,
            reviewed_context_packs=4,
        )
    ]
    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    stdout = render_pre_push_gate_stdout(
        report,
        decision,
        markdown_path=tmp_path / "pre-push.md",
        json_path=tmp_path / "pre-push.json",
        base="main",
        config=config.gates.pre_push,
    )

    assert decision.blocked is False
    assert stdout.splitlines()[0] == "APEX RAY GATE: PASSED WITH PARTIAL COVERAGE"
    assert "Push decision: ALLOWED" in stdout
    assert "Review coverage: PARTIAL - 4/10 unique context packs (40.0%)" in stdout
    assert "Reviewer assignments: 4/10" in stdout
    assert "High-risk coverage: COMPLETE - 2/2" in stdout
    assert "Remaining: P0 0, P1 2, P2 4" in stdout
    assert "No blocking findings in reviewed scope." in stdout


def test_pre_push_gate_stdout_shows_continuations_for_quality_only_coverage_block(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.gates.pre_push.fail_on_partial_severity = "none"
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts", file_kind=FileKind.SOURCE)
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
    )
    report.llm_coverage.quality_gate_status = "fail"
    report.llm_coverage.quality_gate_reasons = ["Required reviewer coverage remains."]
    report.llm_coverage.partial_severity = "critical"
    report.llm_coverage.coverage_todos = [
        LLMCoverageTodo(
            context_pack_id=pack.id,
            file=pack.file,
            priority="p0",
            suggested_command="apex-ray review --continue-from report.json --only-pack pack",
        )
    ]
    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    stdout = render_pre_push_gate_stdout(
        report,
        decision,
        markdown_path=tmp_path / "pre-push.md",
        json_path=tmp_path / "pre-push.json",
        base="main",
        config=config.gates.pre_push,
    )

    assert decision.quality_gate_failed is True
    assert decision.partial_blocked is False
    assert "Coverage continuation commands:" in stdout
    assert "--only-pack pack" in stdout


@pytest.mark.parametrize(
    ("threshold", "expected_priorities"),
    [
        ("critical", {"p0"}),
        ("major", {"p0", "p1"}),
        ("minor", {"p0", "p1", "p2"}),
        ("none", set()),
        (None, set()),
    ],
)
def test_generalized_followup_priorities_follow_blocking_policy(
    threshold: str | None,
    expected_priorities: set[str],
) -> None:
    config = ReviewConfig()
    config.gates.pre_push.auto_followup = True
    config.gates.pre_push.fail_on_partial_severity = threshold

    enabled, priorities, max_pack_reviews = _coverage_followup_policy(
        config.gates.pre_push,
        report=None,
    )

    assert enabled is bool(expected_priorities)
    assert priorities == expected_priorities
    assert max_pack_reviews == 16


def test_legacy_p0_followup_keeps_its_original_scope() -> None:
    config = ReviewConfig()
    config.gates.pre_push.auto_followup = None
    config.gates.pre_push.auto_followup_p0 = True
    config.gates.pre_push.fail_on_partial_severity = "minor"

    enabled, priorities, max_pack_reviews = _coverage_followup_policy(
        config.gates.pre_push,
        report=None,
    )

    assert enabled is True
    assert priorities == {"p0"}
    assert max_pack_reviews == config.gates.pre_push.auto_followup_p0_max_pack_reviews


def test_generalized_followup_retries_failed_p2_pack_that_blocks_partial_policy(
    tmp_path: Path,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.gates.pre_push.auto_followup = True
    config.gates.pre_push.fail_on_partial_severity = "major"
    pack = ContextPack(id="docs/runbook.md#file:1", file="docs/runbook.md", file_kind=FileKind.DOCS)
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
    )
    report.llm_coverage.partial_severity = "major"
    report.llm_coverage.pack_statuses = [
        LLMPackReviewStatus(
            context_pack_id=pack.id,
            file=pack.file,
            file_kind=pack.file_kind,
            status="failed_provider",
            priority="p2",
        )
    ]
    report.llm_coverage.coverage_todos = [
        LLMCoverageTodo(
            context_pack_id=pack.id,
            file=pack.file,
            file_kind=pack.file_kind,
            priority="p2",
            reason="provider failed",
        )
    ]

    enabled, priorities, _max_pack_reviews = _coverage_followup_policy(config.gates.pre_push, report)

    assert enabled is True
    assert priorities == {"p0", "p1", "p2"}
    assert _coverage_followup_blocking_pack_ids(config.gates.pre_push, report) == {pack.id}
    assert _coverage_followup_force_pack_ids(config.gates.pre_push, report) == set()


def test_generalized_followup_targets_source_pack_that_failed_quality_threshold(
    tmp_path: Path,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.min_source_line_coverage = 0.9
    config.gates.pre_push.auto_followup = True
    config.gates.pre_push.fail_on_partial_severity = "none"
    pack = ContextPack(id="src/orders.ts#submit:1", file="src/orders.ts", file_kind=FileKind.SOURCE)
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
    )
    report.llm_coverage.quality_gate_status = "fail"
    report.llm_coverage.quality_gate_reasons = ["Source changed-line coverage below threshold"]
    report.llm_coverage.source_changed_line_coverage_ratio = 0.5
    report.llm_coverage.partial_severity = "major"
    report.llm_coverage.coverage_todos = [
        LLMCoverageTodo(
            context_pack_id=pack.id,
            file=pack.file,
            file_kind=pack.file_kind,
            priority="p1",
            slice="source",
        )
    ]

    enabled, priorities, _max_pack_reviews = _coverage_followup_policy(config.gates.pre_push, report)

    assert enabled is True
    assert priorities == {"p1"}
    assert _coverage_followup_blocking_pack_ids(config.gates.pre_push, report) == {pack.id}
    assert _coverage_followup_force_pack_ids(config.gates.pre_push, report) == set()


def test_generalized_followup_targets_required_reviewer_assignment_debt(
    tmp_path: Path,
) -> None:
    config = ReviewConfig(reviewers=[ReviewerConfig(id="ux", required=True)])
    config.llm.enabled = True
    config.gates.pre_push.auto_followup = True
    config.gates.pre_push.fail_on_partial_severity = "none"
    pack = ContextPack(id="docs/flow.md#file:1", file="docs/flow.md", file_kind=FileKind.DOCS)
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
    )
    report.llm_coverage.quality_gate_status = "fail"
    report.llm_coverage.partial_severity = "critical"
    report.llm_coverage.reviewers = [LLMReviewerCoverageSummary(reviewer_id="ux", required=True, status="fail")]
    report.llm_coverage.coverage_todos = [
        LLMCoverageTodo(
            context_pack_id=pack.id,
            file=pack.file,
            file_kind=pack.file_kind,
            reviewer_id="ux",
            priority="p2",
            reason="required reviewer assignment remains",
        )
    ]

    enabled, priorities, _max_pack_reviews = _coverage_followup_policy(config.gates.pre_push, report)

    assert enabled is True
    assert priorities == {"p2"}
    blocking_pack_ids = _coverage_followup_blocking_pack_ids(config.gates.pre_push, report)
    assert blocking_pack_ids == {pack.id}
    assert _coverage_followup_force_pack_ids(config.gates.pre_push, report) == set()
    assert _coverage_followup_reviewer_ids(report, blocking_pack_ids) == ["ux"]


def test_generalized_followup_excludes_optional_assignment_debt_on_required_blocking_packs(
    tmp_path: Path,
) -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="optional"),
            ReviewerConfig(id="required", required=True),
        ]
    )
    config.llm.enabled = True
    config.gates.pre_push.auto_followup = True
    config.gates.pre_push.fail_on_partial_severity = "none"
    pack = ContextPack(id="src/orders.ts#submit:1", file="src/orders.ts", file_kind=FileKind.SOURCE)
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
    )

    blocking_pack_ids = _coverage_followup_blocking_pack_ids(config.gates.pre_push, report)

    assert blocking_pack_ids == {pack.id}
    assert {(todo.reviewer_id, todo.context_pack_id) for todo in report.llm_coverage.coverage_todos} == {
        ("optional", pack.id),
        ("required", pack.id),
    }
    assert _coverage_followup_reviewer_ids(report, blocking_pack_ids) == ["required"]


def test_generalized_followup_targets_optional_active_failure_that_blocks_partial_policy(
    tmp_path: Path,
) -> None:
    config = ReviewConfig(reviewers=[ReviewerConfig(id="first"), ReviewerConfig(id="second")])
    config.llm.enabled = True
    config.gates.pre_push.auto_followup = True
    config.gates.pre_push.fail_on_partial_severity = "major"
    pack = ContextPack(id="docs/runbook.md#file:1", file="docs/runbook.md", file_kind=FileKind.DOCS)
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="first",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="second",
                context_pack_id=pack.id,
                status="failed_provider",
                duration_ms=1,
            ),
        ],
        reviewer_selections={"first": selection, "second": selection},
    )

    assert report.llm_coverage.partial_severity == "major"
    assert evaluate_pre_push_gate(report, config.gates.pre_push).partial_blocked is True
    assert _coverage_followup_blocking_pack_ids(config.gates.pre_push, report) == {pack.id}
    assert _coverage_followup_policy(config.gates.pre_push, report)[1] == {"p0", "p1", "p2"}


def test_generalized_followup_targets_unresolved_general_verification_debt(
    tmp_path: Path,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.verify = True
    config.gates.pre_push.auto_followup = True
    pack = ContextPack(id="docs/runbook.md#file:1", file="docs/runbook.md", file_kind=FileKind.DOCS)
    finding = Finding(
        title="Runbook command is unsafe",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=4,
        failure_mode="The documented command can overwrite production state.",
        evidence="The command omits the required environment guard.",
        suggested_fix="Add the guard before the destructive command.",
        suggested_test="Validate the guarded example.",
        context_pack_id=pack.id,
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="general",
                approved=False,
                confidence=FindingConfidence.LOW,
                reason="Verifier result was unavailable.",
                superseded=True,
                superseded_reason="Verification did not produce a usable decision.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="general",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="general",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"general": selection},
    )

    assert report.llm_coverage.quality_gate_status == "fail"
    assert report.llm_coverage.coverage_todos[0].priority == "p2"
    assert _coverage_followup_blocking_pack_ids(config.gates.pre_push, report) == {pack.id}
    assert _coverage_followup_policy(config.gates.pre_push, report)[1] == {"p0", "p2"}


def test_generalized_followup_assigns_overlapping_depth_only_debt_once(
    tmp_path: Path,
) -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="first", review_depth="shallow"),
            ReviewerConfig(id="second", review_depth="shallow"),
        ]
    )
    config.llm.enabled = True
    config.gates.pre_push.auto_followup = True
    config.gates.pre_push.fail_on_partial_severity = "major"
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="auth",
                severity=RiskSeverity.HIGH,
                reason="Authorization changed.",
                file="src/auth.ts",
            )
        ],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        shallow_selected_context_pack_ids=[pack.id],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                kind="review_shallow",
                provider="fake",
                reviewer_id=reviewer_id,
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            )
            for reviewer_id in ("first", "second")
        ],
        reviewer_selections={"first": selection, "second": selection},
    )
    blocking_pack_ids = _coverage_followup_blocking_pack_ids(config.gates.pre_push, report)
    force_review_pack_ids = _coverage_followup_force_pack_ids(config.gates.pre_push, report)

    assert blocking_pack_ids == {pack.id}
    assert force_review_pack_ids == {pack.id}
    assert _coverage_followup_reviewer_ids(
        report,
        blocking_pack_ids,
        force_review_pack_ids=force_review_pack_ids,
    ) == ["first"]


def test_generalized_followup_preserves_required_assignment_amid_depth_debt(
    tmp_path: Path,
) -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="required", required=True),
            ReviewerConfig(id="optional"),
        ]
    )
    required_pack_id = "src/auth.ts#authorize:1"
    depth_only_pack_id = "src/payments.ts#charge:1"
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    report.llm_coverage.reviewers = [
        LLMReviewerCoverageSummary(
            reviewer_id="required",
            required=True,
            status="fail",
            matching_context_pack_ids=[required_pack_id],
        ),
        LLMReviewerCoverageSummary(
            reviewer_id="optional",
            status="warn",
            matching_context_pack_ids=[required_pack_id, depth_only_pack_id],
        ),
    ]
    report.llm_coverage.coverage_todos = [
        LLMCoverageTodo(
            context_pack_id=required_pack_id,
            file="src/auth.ts",
            reviewer_id="required",
            priority="p1",
            reason="required reviewer assignment remains",
        )
    ]
    blocking_pack_ids = {required_pack_id, depth_only_pack_id}

    reviewer_ids = _coverage_followup_reviewer_ids(
        report,
        blocking_pack_ids,
        force_review_pack_ids=blocking_pack_ids,
    )

    assert reviewer_ids is None
    assert _coverage_followup_reviewer_ids(
        report,
        blocking_pack_ids,
        force_review_pack_ids=blocking_pack_ids,
        requested_reviewer_ids=["optional"],
    ) == ["optional"]
    assert _coverage_followup_reviewer_ids(
        report,
        blocking_pack_ids,
        force_review_pack_ids=blocking_pack_ids,
        requested_reviewer_ids=["optional", "required"],
    ) == ["required"]


def test_coverage_resume_requires_matching_output_and_saved_input_identity(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.gates.pre_push.incremental_retry.enabled = True
    pack = ContextPack(id="src/service.ts#run:1", file="src/service.ts", file_kind=FileKind.SOURCE)
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.BASE, base="main"),
        context_packs=[pack],
        input_snapshot=ReviewInputSnapshot(
            target_mode=TargetMode.BASE,
            base_ref="main",
            head_sha="1" * 40,
            merge_base_sha="a" * 40,
            diff_sha256="b" * 64,
        ),
    )
    config_hash = config_fingerprint(config, config.gates.pre_push)
    json_output = tmp_path / ".apex-ray" / "reports" / "pre-push.json"
    state = build_pre_push_state(
        repo_root=tmp_path,
        base_ref="main",
        merge_base_sha="a" * 40,
        head_sha="1" * 40,
        config_hash=config_hash,
        report=report,
        report_path=tmp_path / ".apex-ray" / "reports" / "pre-push.md",
        json_path=json_output,
        active_findings=[],
        coverage_debt=CoverageDebt(partial_blocked=True),
    )

    assert (
        _retry_coverage_report(
            state,
            report,
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is report
    )
    legacy_state = state.model_copy(update={"input_snapshot_fingerprint": ""})
    assert (
        _retry_coverage_report(
            legacy_state,
            report,
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is None
    )
    assert (
        _retry_coverage_report(
            state,
            report,
            json_output=tmp_path / "other.json",
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is None
    )

    mismatched = report.model_copy(deep=True)
    assert mismatched.input_snapshot is not None
    mismatched.input_snapshot.head_sha = "2" * 40
    assert (
        _retry_coverage_report(
            state,
            mismatched,
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is None
    )


def test_coverage_resume_rejects_patch_snapshot_with_different_range_start(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.gates.pre_push.incremental_retry.enabled = True
    pack = ContextPack(id="src/service.ts#run:1", file="src/service.ts", file_kind=FileKind.SOURCE)
    range_start = "1" * 40
    head_sha = "3" * 40
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, base=f"{range_start}..HEAD"),
        context_packs=[pack],
        input_snapshot=ReviewInputSnapshot(
            target_mode=TargetMode.PATCH,
            base_ref=f"{range_start}..HEAD",
            head_sha=head_sha,
            range_start_sha=range_start,
            diff_sha256="b" * 64,
        ),
    )
    config_hash = config_fingerprint(config, config.gates.pre_push)
    json_output = tmp_path / ".apex-ray" / "reports" / "pre-push.json"
    state = build_pre_push_state(
        repo_root=tmp_path,
        base_ref="main",
        merge_base_sha="a" * 40,
        head_sha=head_sha,
        config_hash=config_hash,
        report=report,
        report_path=tmp_path / ".apex-ray" / "reports" / "pre-push.md",
        json_path=json_output,
        active_findings=[],
        coverage_debt=CoverageDebt(partial_blocked=True),
    )

    assert (
        _retry_coverage_report(
            state,
            report,
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is report
    )

    substituted = report.model_copy(deep=True)
    substituted_range_start = "2" * 40
    substituted.diff.base = f"{substituted_range_start}..HEAD"
    assert substituted.input_snapshot is not None
    substituted.input_snapshot.base_ref = substituted.diff.base
    substituted.input_snapshot.range_start_sha = substituted_range_start
    assert (
        _retry_coverage_report(
            state,
            substituted,
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is None
    )


def test_coverage_resume_accepts_saved_snapshot_while_new_head_is_pending(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.gates.pre_push.incremental_retry.enabled = True
    pack = ContextPack(id="src/service.ts#run:1", file="src/service.ts", file_kind=FileKind.SOURCE)
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.BASE, base="main"),
        context_packs=[pack],
        input_snapshot=ReviewInputSnapshot(
            target_mode=TargetMode.BASE,
            base_ref="main",
            head_sha="1" * 40,
            merge_base_sha="a" * 40,
            diff_sha256="b" * 64,
        ),
    )
    config_hash = config_fingerprint(config, config.gates.pre_push)
    json_output = tmp_path / ".apex-ray" / "reports" / "pre-push.json"
    state = build_pre_push_state(
        repo_root=tmp_path,
        base_ref="main",
        merge_base_sha="a" * 40,
        head_sha="1" * 40,
        config_hash=config_hash,
        report=report,
        report_path=tmp_path / ".apex-ray" / "reports" / "pre-push.md",
        json_path=json_output,
        active_findings=[],
        coverage_debt=CoverageDebt(partial_blocked=True),
    )

    # The live HEAD may already be newer. Coverage resume is deliberately bound
    # to state.head_sha and the caller separately blocks the pending delta.
    assert (
        _retry_coverage_report(
            state,
            report,
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is report
    )


def test_verified_semantic_duplicate_remains_eligible_for_gate() -> None:
    approved = Finding(
        title="Authorization guard is bypassed before settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.MEDIUM,
        file="src/settlement.ts",
        line=42,
        failure_mode=(
            "An untrusted caller can bypass the account authorization guard and submit "
            "a settlement without the required ownership check."
        ),
        evidence=(
            "The changed early return executes before the account ownership authorization guard on the settlement path."
        ),
        suggested_fix="Move the early return after the ownership authorization guard.",
        suggested_test="Add a denied-account settlement regression test.",
    )
    canonical = approved.model_copy(
        update={
            "title": "Settlement authorization can be bypassed",
            "confidence": FindingConfidence.HIGH,
            "reviewer_ids": ["finance", "security"],
        }
    )
    config = ReviewConfig()
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        findings=[canonical],
        verifications=[
            FindingVerification(
                finding=approved,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Evidence confirms the authorization bypass.",
            )
        ],
    )

    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    assert decision.blocked is True
    assert decision.blocking_findings == [canonical]


def test_latest_legacy_verification_decision_controls_gate_eligibility() -> None:
    finding = Finding(
        title="Authorization guard is bypassed",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/settlement.ts",
        failure_mode="A settlement can bypass its account authorization guard.",
        evidence="The changed branch executes before the ownership check.",
        suggested_fix="Move the branch after the ownership check.",
        suggested_test="Reject a settlement for an unauthorized account.",
    )
    config = ReviewConfig()
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The original verifier approved the finding.",
            ),
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=False,
                confidence=FindingConfidence.HIGH,
                reason="A later verifier rejected the finding.",
            ),
        ],
    )

    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    assert decision.blocked is False
    assert decision.blocking_findings == []


@pytest.mark.parametrize(
    ("latest_approved", "expected_blocked"),
    [
        (False, False),
        (True, True),
    ],
)
def test_latest_legacy_verification_decision_matches_across_severity_changes(
    latest_approved: bool,
    expected_blocked: bool,
) -> None:
    finding = Finding(
        title="Authorization guard is bypassed",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/settlement.ts",
        failure_mode="A settlement can bypass its account authorization guard.",
        evidence="The changed branch executes before the ownership check.",
        suggested_fix="Move the branch after the ownership check.",
        suggested_test="Reject a settlement for an unauthorized account.",
        context_pack_id="src/settlement.ts#settle:1",
    )
    escalated = finding.model_copy(update={"severity": FindingSeverity.CRITICAL})
    config = ReviewConfig()
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=not latest_approved,
                confidence=FindingConfidence.HIGH,
                reason="The original decision.",
            ),
            FindingVerification(
                finding=escalated,
                reviewer_id="security",
                approved=latest_approved,
                confidence=FindingConfidence.HIGH,
                reason="The later decision after severity escalation.",
            ),
        ],
    )

    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    assert decision.blocked is expected_blocked
    assert decision.blocking_findings == ([finding] if expected_blocked else [])


def test_distinct_fuzzy_legacy_verification_decisions_remain_independent() -> None:
    authorization = Finding(
        title="Authorization guard is bypassed before settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/settlement.ts",
        line=42,
        failure_mode=(
            "An untrusted caller can bypass the authorization guard and submit a settlement "
            "without the required account ownership check."
        ),
        evidence=(
            "The changed early return executes before the account ownership authorization guard on the settlement path."
        ),
        suggested_fix="Move the early return after the ownership guard.",
        suggested_test="Reject a denied-account settlement.",
        context_pack_id="src/settlement.ts#settle:42",
    )
    audit = authorization.model_copy(
        update={
            "title": "Settlement ownership bypass is missing an audit event",
            "failure_mode": (
                "An untrusted caller can bypass the account ownership guard and submit a "
                "settlement without the required authorization audit event."
            ),
            "evidence": (
                "The changed early return executes before the account ownership authorization "
                "audit event on the settlement path."
            ),
        }
    )
    assert findings_are_duplicates(authorization, audit) is True
    config = ReviewConfig()
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        findings=[authorization],
        verifications=[
            FindingVerification(
                finding=authorization,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The authorization bypass is confirmed.",
            ),
            FindingVerification(
                finding=audit,
                reviewer_id="security",
                approved=False,
                confidence=FindingConfidence.HIGH,
                reason="The audit issue is not reproducible.",
            ),
        ],
    )

    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    assert decision.blocked is True
    assert decision.blocking_findings == [authorization]


def test_cross_severity_verification_does_not_merge_distinct_same_line_risks() -> None:
    authorization = Finding(
        title="Missing authorization allows account deletion",
        severity=FindingSeverity.CRITICAL,
        confidence=FindingConfidence.HIGH,
        file="src/accounts.ts",
        line=42,
        failure_mode=(
            "The deleteAccount handler uses the account deletion token before authorization "
            "and permits another tenant account deletion."
        ),
        evidence="The changed handler invokes deleteAccount before checking caller authorization.",
        suggested_fix="Enforce caller authorization before using the account deletion token.",
        suggested_test="Reject another tenant account deletion before the operation runs.",
        context_pack_id="src/accounts.ts#deleteAccount:42",
    )
    audit_leak = authorization.model_copy(
        update={
            "title": "Audit log leaks the account deletion token",
            "severity": FindingSeverity.MEDIUM,
            "failure_mode": (
                "The deleteAccount handler logs the account deletion token before authorization "
                "and exposes another tenant account deletion token."
            ),
            "evidence": "The changed handler invokes logger.info before checking caller authorization.",
        }
    )
    config = ReviewConfig()
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        findings=[authorization],
        verifications=[
            FindingVerification(
                finding=authorization,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The authorization bypass is confirmed.",
            ),
            FindingVerification(
                finding=audit_leak,
                reviewer_id="security",
                approved=False,
                confidence=FindingConfidence.HIGH,
                reason="The audit token leak is not reproducible.",
            ),
        ],
    )

    decision = evaluate_pre_push_gate(report, config.gates.pre_push)

    assert decision.blocked is True
    assert decision.blocking_findings == [authorization]


def test_verified_semantic_duplicate_does_not_cross_provenance_scope() -> None:
    approved = Finding(
        title="Authorization guard is bypassed before settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.MEDIUM,
        file="src/accounts/settlement.ts",
        line=42,
        failure_mode=(
            "An untrusted caller can bypass the account authorization guard and submit "
            "a settlement without the required ownership check."
        ),
        evidence=(
            "The changed early return executes before the account ownership authorization guard on the settlement path."
        ),
        suggested_fix="Move the early return after the ownership authorization guard.",
        suggested_test="Add a denied-account settlement regression test.",
        context_pack_id="src/accounts/settlement.ts#settle:42",
    )
    config = ReviewConfig()
    unrelated_findings = [
        approved.model_copy(
            update={
                "title": "Settlement authorization can be bypassed",
                "file": "src/cards/settlement.ts",
                "context_pack_id": "src/cards/settlement.ts#settle:42",
                "confidence": FindingConfidence.HIGH,
            }
        ),
        approved.model_copy(
            update={
                "title": "Settlement authorization can be bypassed",
                "line": 242,
                "confidence": FindingConfidence.HIGH,
            }
        ),
        approved.model_copy(
            update={
                "file": "src/cards/settlement.ts",
                "context_pack_id": "src/cards/settlement.ts#settle:42",
                "reviewer_ids": ["security"],
                "confidence": FindingConfidence.HIGH,
            }
        ),
    ]

    for unrelated in unrelated_findings:
        report = build_report(
            ProjectProfile(root="/repo", is_git_repo=True),
            config,
            DiffSummary(target_mode=TargetMode.PATCH),
            findings=[unrelated],
            verifications=[
                FindingVerification(
                    finding=approved,
                    reviewer_id="security",
                    approved=True,
                    confidence=FindingConfidence.HIGH,
                    reason="Evidence confirms the authorization bypass in the account settlement pack.",
                )
            ],
        )

        decision = evaluate_pre_push_gate(report, config.gates.pre_push)

        assert decision.blocked is False
        assert decision.blocking_findings == []


def test_relevant_files_include_matched_rule_resolution_surfaces(tmp_path: Path) -> None:
    pack = ContextPack(
        id="apps/api/src/database/database.types.ts#LpOutboundMovementAttemptTable:1",
        file="apps/api/src/database/database.types.ts",
        rule_matches=[
            RuleMatch(
                id="schema-migration-contracts",
                title="Keep schemas and migrations aligned",
                severity=FindingSeverity.HIGH,
                mode=RuleMode.STRICT,
                resolution_surfaces=["apps/api/src/database/**", "apps/migrator/migrations/**"],
            )
        ],
    )
    finding = Finding(
        title="Added persisted column type without a matching migration",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="apps/api/src/database/database.types.ts",
        line=206,
        failure_mode="Schema drift",
        evidence="The diff adds `submission_claim_token` without a migration.",
        suggested_fix="Add a migration.",
        suggested_test="Run migration checks.",
        context_pack_id=pack.id,
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
    )

    assert relevant_files_for_finding(report, finding) == [
        "apps/api/src/database/**",
        "apps/api/src/database/database.types.ts",
        "apps/migrator/migrations/**",
    ]


def test_incremental_retry_uses_resolution_surface_globs(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = False
    finding = Finding(
        title="Added persisted column type without a matching migration",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="apps/api/src/database/database.types.ts",
        line=206,
        failure_mode="Schema drift",
        evidence="The diff adds `submission_claim_token` without a migration.",
        suggested_fix="Add a migration.",
        suggested_test="Run migration checks.",
        context_pack_id="apps/api/src/database/database.types.ts#LpOutboundMovementAttemptTable:1",
    )
    carried = CarriedFinding(
        finding=finding,
        relevant_files=["apps/api/src/database/**", "apps/migrator/migrations/**"],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(
            target_mode=TargetMode.PATCH,
            files=[
                ChangedFile(
                    old_path=None,
                    new_path="apps/migrator/migrations/api/1781291922674_lp-outbound-submission-claim-token.ts",
                )
            ],
            stats=DiffStats(files_changed=1),
        ),
    )

    active, resolved_count = _resolve_incremental_carried_findings(
        [carried],
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )

    assert resolved_count == 0
    assert len(active) == 1
    assert active[0].status == "uncertain"
    assert (
        active[0].resolution_reason == "Current delta may contain resolution evidence, but LLM resolution is disabled."
    )


def test_incremental_retry_keeps_carried_finding_when_head_content_is_not_utf8(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "apex@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Apex Test"], cwd=tmp_path, check=True)
    source = tmp_path / "src" / "orders.ts"
    source.parent.mkdir()
    source.write_bytes(b"\xff\xfeinvalid source")
    subprocess.run(["git", "add", "src/orders.ts"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "invalid utf8 fixture"], cwd=tmp_path, check=True)
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        failure_mode="The lookup can return another tenant's order.",
        evidence="The query no longer includes `tenantId`.",
        suggested_fix="Restore the tenant predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
    )

    reason = stale_carried_finding_reason(CarriedFinding(finding=finding), tmp_path)

    assert reason is None


def test_incremental_retry_resolves_carried_finding_for_unanchored_new_file(
    tmp_path: Path,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = False
    finding = Finding(
        title="Provider is not registered",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/providers/registry.ts",
        line=24,
        failure_mode="Requests cannot resolve the provider.",
        evidence="The provider registration is absent.",
        suggested_fix="Register the provider.",
        suggested_test="Resolve the provider through the application container.",
        context_pack_id="src/providers/registry.ts#providers:1",
    )
    carried = CarriedFinding(
        finding=finding,
        relevant_files=["src/providers/registry.ts"],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(
            target_mode=TargetMode.PATCH,
            files=[
                ChangedFile(
                    old_path=None,
                    new_path="src/providers/register-payment-provider.ts",
                    status=FileStatus.ADDED,
                )
            ],
            stats=DiffStats(files_changed=1),
        ),
    )

    active, resolved_count = _resolve_incremental_carried_findings(
        [carried],
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )

    assert resolved_count == 0
    assert len(active) == 1
    assert active[0].status == "uncertain"
    assert (
        active[0].resolution_reason == "Current delta may contain resolution evidence, but LLM resolution is disabled."
    )


def test_incremental_retry_skips_resolution_for_unrelated_modified_file(
    tmp_path: Path,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = False
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        failure_mode="The lookup can return another tenant's order.",
        evidence="The lookup omits its tenant predicate.",
        suggested_fix="Restore the tenant predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
    )
    carried = CarriedFinding(finding=finding, relevant_files=[finding.file])
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(
            target_mode=TargetMode.PATCH,
            files=[
                ChangedFile(
                    old_path="src/unrelated.ts",
                    new_path="src/unrelated.ts",
                    status=FileStatus.MODIFIED,
                )
            ],
            stats=DiffStats(files_changed=1),
        ),
    )

    active, resolved_count = _resolve_incremental_carried_findings(
        [carried],
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )

    assert resolved_count == 0
    assert len(active) == 1
    assert active[0].status == "still_present"
    assert active[0].resolution_reason == "No relevant resolution surface changed since the previous gate attempt."


def test_incremental_retry_resolves_graph_connected_modified_file(
    tmp_path: Path,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = False
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        failure_mode="The lookup can return another tenant's order.",
        evidence="The lookup omits its tenant predicate.",
        suggested_fix="Restore the tenant predicate through the query adapter.",
        suggested_test="Add a cross-tenant lookup regression test.",
    )
    carried = CarriedFinding(finding=finding, relevant_files=[finding.file])
    adapter_path = "src/query-adapter.ts"
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(
            target_mode=TargetMode.PATCH,
            files=[
                ChangedFile(
                    old_path=adapter_path,
                    new_path=adapter_path,
                    status=FileStatus.MODIFIED,
                )
            ],
            stats=DiffStats(files_changed=1),
        ),
        context_packs=[
            ContextPack(
                id=f"{adapter_path}#query:1",
                file=adapter_path,
                references=[
                    AnalyzerReference(
                        file=finding.file,
                        line=1,
                        text="getOrder",
                        kind="reference",
                    )
                ],
            )
        ],
    )

    active, resolved_count = _resolve_incremental_carried_findings(
        [carried],
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )

    assert resolved_count == 0
    assert len(active) == 1
    assert active[0].status == "uncertain"
    assert (
        active[0].resolution_reason == "Current delta may contain resolution evidence, but LLM resolution is disabled."
    )


def test_incremental_retry_keeps_carried_finding_when_delta_is_empty(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = False
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        failure_mode="The lookup can return another tenant's order.",
        evidence="The lookup omits its tenant predicate.",
        suggested_fix="Restore the tenant predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
    )
    carried = CarriedFinding(finding=finding, relevant_files=[finding.file])
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
    )

    active, resolved_count = _resolve_incremental_carried_findings(
        [carried],
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )

    assert resolved_count == 0
    assert len(active) == 1
    assert active[0].status == "still_present"
    assert active[0].resolution_reason == "No relevant resolution surface changed since the previous gate attempt."


def test_incremental_retry_fails_closed_without_provider_when_diff_warnings_are_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        failure_mode="The lookup can return another tenant's order.",
        evidence="The lookup omits its tenant predicate.",
        suggested_fix="Restore the tenant predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
    )
    carried = CarriedFinding(finding=finding, relevant_files=[finding.file])
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(
            target_mode=TargetMode.PATCH,
            warnings=[f"warning-{index}" for index in range(9)],
        ),
    )
    provider_calls = 0

    class UnexpectedResolver:
        def resolve_finding(self, *args, **kwargs):
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("incomplete warning evidence must bypass the provider")

    monkeypatch.setattr(
        "apex_ray.cli.gate.provider_from_config",
        lambda _config: UnexpectedResolver(),
    )

    active = resolve_carried_findings(
        [carried],
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )

    assert provider_calls == 0
    assert len(active) == 1
    assert active[0].status == "uncertain"
    assert active[0].resolution_reason == (
        "Current diff warnings were omitted or truncated; resolution evidence is incomplete."
    )


def test_incremental_retry_bounds_resolution_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.gates.pre_push.incremental_retry.max_resolution_calls_per_retry = 1
    findings = [
        Finding(
            title=f"Blocking finding {index}",
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.HIGH,
            file=f"src/file-{index}.ts",
            failure_mode="The old implementation violates the invariant.",
            evidence="The prior review verified the failure mode.",
            suggested_fix="Repair the implementation.",
            suggested_test="Add a regression test.",
        )
        for index in range(2)
    ]
    carried_findings = [CarriedFinding(finding=finding, relevant_files=[finding.file]) for finding in findings]
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    provider = FakeLLMProvider(resolution_statuses=["resolved", "resolved"])
    monkeypatch.setattr(
        "apex_ray.cli.gate.provider_from_config",
        lambda _config: provider,
    )

    active = resolve_carried_findings(
        carried_findings,
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )

    assert provider.resolved_finding_titles == ["Blocking finding 0"]
    assert len(active) == 1
    assert active[0].finding.title == "Blocking finding 1"
    assert active[0].status == "uncertain"
    assert active[0].resolution_reason == (
        "Resolution call budget exhausted for this retry; the finding remains blocking."
    )


def test_incremental_retry_rotates_deferred_resolution_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.gates.pre_push.incremental_retry.max_resolution_calls_per_retry = 1
    carried_findings = [
        CarriedFinding(
            finding=Finding(
                title=f"Blocking finding {index}",
                severity=FindingSeverity.HIGH,
                confidence=FindingConfidence.HIGH,
                file=f"src/file-{index}.ts",
                failure_mode="The old implementation violates the invariant.",
                evidence="The prior review verified the failure mode.",
                suggested_fix="Repair the implementation.",
                suggested_test="Add a regression test.",
            )
        )
        for index in range(2)
    ]
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    provider = FakeLLMProvider(resolution_statuses=["uncertain", "uncertain"])
    monkeypatch.setattr(
        "apex_ray.cli.gate.provider_from_config",
        lambda _config: provider,
    )

    first_active = resolve_carried_findings(
        carried_findings,
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )
    second_active = resolve_carried_findings(
        first_active,
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )

    assert provider.resolved_finding_titles == [
        "Blocking finding 0",
        "Blocking finding 1",
    ]
    assert len(second_active) == 2


def test_incremental_retry_prioritizes_severity_before_deferred_fairness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.gates.pre_push.incremental_retry.max_resolution_calls_per_retry = 1
    low_deferred = CarriedFinding(
        finding=Finding(
            title="Deferred low finding",
            severity=FindingSeverity.LOW,
            confidence=FindingConfidence.HIGH,
            file="src/low.ts",
            failure_mode="A low-severity invariant may remain broken.",
            evidence="The prior review verified the failure mode.",
            suggested_fix="Repair the low-severity issue.",
            suggested_test="Add a regression test.",
        ),
        resolution_reason="Resolution call budget exhausted for this retry; the finding remains blocking.",
    )
    fresh_critical = CarriedFinding(
        finding=Finding(
            title="Fresh critical finding",
            severity=FindingSeverity.CRITICAL,
            confidence=FindingConfidence.HIGH,
            file="src/critical.ts",
            failure_mode="A critical invariant may remain broken.",
            evidence="The prior review verified the failure mode.",
            suggested_fix="Repair the critical issue.",
            suggested_test="Add a regression test.",
        )
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    provider = FakeLLMProvider(resolution_statuses=["resolved"])
    monkeypatch.setattr(
        "apex_ray.cli.gate.provider_from_config",
        lambda _config: provider,
    )

    active = resolve_carried_findings(
        [low_deferred, fresh_critical],
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )

    assert provider.resolved_finding_titles == ["Fresh critical finding"]
    assert [carried.finding.title for carried in active] == ["Deferred low finding"]


def test_pre_push_same_head_reuses_report_to_rotate_deferred_resolutions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".apex-ray" / "config.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
review:
  llm:
    enabled: true
    provider: fake
  gates:
    pre_push:
      require_verified_findings: true
      min_finding_severity: high
      incremental_retry:
        enabled: true
        max_resolution_calls_per_retry: 1
""".lstrip(),
        encoding="utf-8",
    )
    findings = [
        Finding(
            title=f"Blocking finding {index}",
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.HIGH,
            file=f"src/file-{index}.ts",
            failure_mode="The old implementation violates the invariant.",
            evidence="The prior review verified the failure mode.",
            suggested_fix="Repair the implementation.",
            suggested_test="Add a regression test.",
        )
        for index in range(2)
    ]
    full_diff = (
        "diff --git a/src/file-0.ts b/src/file-0.ts\n"
        "--- a/src/file-0.ts\n"
        "+++ b/src/file-0.ts\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+broken\n"
    )
    resolution_diff = (
        "diff --git a/src/fix.ts b/src/fix.ts\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/fix.ts\n"
        "@@ -0,0 +1 @@\n"
        "+register_fix()\n"
    )
    heads = iter(["head-1", "head-2", "head-2", "head-2"])
    range_diffs = iter([resolution_diff, ""])
    pipeline_calls = 0

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            parse_unified_diff(
                diff_text,
                target_mode=target_mode,
                base=kwargs.get("base"),
            ),
            findings=findings if pipeline_calls == 1 else [],
            verifications=(
                [
                    FindingVerification(
                        finding=finding,
                        approved=True,
                        confidence=FindingConfidence.HIGH,
                        reason="The failure mode is reproducible.",
                    )
                    for finding in findings
                ]
                if pipeline_calls == 1
                else []
            ),
        )

    provider = FakeLLMProvider(resolution_statuses=["uncertain", "uncertain"])
    monkeypatch.setattr("apex_ray.cli.gate.discover_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.warn_outdated_agent_artifacts", lambda _root: None)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.diff_base", lambda _root, _base: full_diff)
    monkeypatch.setattr("apex_ray.cli.gate.git.diff_range", lambda _root, _old, _new: next(range_diffs))
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr(
        "apex_ray.cli.gate.continue_review_from_report",
        lambda report, **_kwargs: (report, []),
    )
    monkeypatch.setattr("apex_ray.cli.gate.provider_from_config", lambda _config: provider)

    runner = CliRunner()
    first = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    third = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert [first.exit_code, second.exit_code, third.exit_code] == [1, 1, 1]
    assert pipeline_calls == 2
    assert provider.resolved_finding_titles == [
        "Blocking finding 0",
        "Blocking finding 1",
    ]
    assert "Mode: resolution-resume" in third.stdout
    state_after_rotation = load_pre_push_state(tmp_path / ".apex-ray" / "reports" / "pre-push-state.json")
    assert state_after_rotation is not None
    assert all(
        carried.resolution_reason != "Resolution call budget exhausted for this retry; the finding remains blocking."
        for carried in state_after_rotation.active_findings
    )

    fourth = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert fourth.exit_code == 1
    assert pipeline_calls == 3
    assert provider.resolved_finding_titles == [
        "Blocking finding 0",
        "Blocking finding 1",
    ]
    assert "Mode: incremental" in fourth.stdout


def test_resolution_resume_rejects_unavailable_or_mismatched_report(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.gates.pre_push.incremental_retry.enabled = True
    finding = Finding(
        title="Deferred blocker",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/registry.ts",
        failure_mode="The provider remains unregistered.",
        evidence="The previous review verified the missing registration.",
        suggested_fix="Register the provider.",
        suggested_test="Resolve it through the container.",
    )
    deferred = CarriedFinding(
        finding=finding,
        relevant_files=[finding.file],
        resolution_reason="Resolution call budget exhausted for this retry; the finding remains blocking.",
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(
            target_mode=TargetMode.PATCH,
            files=[
                ChangedFile(
                    old_path=None,
                    new_path="src/register-provider.ts",
                    status=FileStatus.ADDED,
                )
            ],
            stats=DiffStats(files_changed=1),
        ),
    )
    json_output = tmp_path / ".apex-ray" / "reports" / "pre-push.json"
    config_hash = config_fingerprint(config, config.gates.pre_push)
    state = build_pre_push_state(
        repo_root=tmp_path,
        base_ref="main",
        merge_base_sha="base-1",
        head_sha="head-2",
        config_hash=config_hash,
        report=report,
        report_path=tmp_path / ".apex-ray" / "reports" / "pre-push.md",
        json_path=json_output,
        active_findings=[deferred],
        coverage_debt=CoverageDebt(),
    )

    assert (
        _retry_resolution_report(
            state,
            report,
            current_head="head-2",
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is report
    )
    round_tripped_report = type(report).model_validate_json(report.model_dump_json())
    assert (
        _retry_resolution_report(
            state,
            round_tripped_report,
            current_head="head-2",
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is round_tripped_report
    )
    assert (
        _retry_resolution_report(
            state,
            None,
            current_head="head-2",
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is None
    )
    state_without_report_fingerprint = state.model_copy(update={"report_fingerprint": ""})
    assert (
        _retry_resolution_report(
            state_without_report_fingerprint,
            report,
            current_head="head-2",
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is None
    )
    mismatched_report = report.model_copy(deep=True)
    mismatched_report.project.root = str(tmp_path / "different-repo")
    assert (
        _retry_resolution_report(
            state,
            mismatched_report,
            current_head="head-2",
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is None
    )
    modified_findings_report = report.model_copy(deep=True)
    modified_findings_report.findings = [finding]
    assert (
        _retry_resolution_report(
            state,
            modified_findings_report,
            current_head="head-2",
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is None
    )
    modified_verifications_report = report.model_copy(deep=True)
    modified_verifications_report.verifications = [
        FindingVerification(
            finding=finding,
            approved=False,
            confidence=FindingConfidence.HIGH,
            reason="Locally modified verification state.",
        )
    ]
    assert (
        _retry_resolution_report(
            state,
            modified_verifications_report,
            current_head="head-2",
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is None
    )
    modified_diff_report = report.model_copy(deep=True)
    modified_diff_report.diff.files.append(
        ChangedFile(
            old_path=None,
            new_path="src/unreviewed.ts",
            status=FileStatus.ADDED,
        )
    )
    assert (
        _retry_resolution_report(
            state,
            modified_diff_report,
            current_head="head-2",
            json_output=json_output,
            config_hash=config_hash,
            gate_config=config.gates.pre_push,
            reviewer_ids=None,
        )
        is None
    )


def test_incremental_retry_keeps_reviewed_clean_carried_finding_without_resolution(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = False
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        line=84,
        failure_mode="The changed query can return another tenant's order.",
        evidence="The diff removes tenantId from the lookup predicate.",
        suggested_fix="Restore the tenantId predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
        context_pack_id="src/orders.ts#getOrder:1",
    )
    pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
        diff_snippet=["@@ -84,1 +84,1 @@", "-  query({ id, tenantId })", "+  query({ id })"],
    )
    carried = CarriedFinding(finding=finding, context_pack=pack)
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=0,
            )
        ],
    )

    active, resolved_count = _resolve_incremental_carried_findings(
        [carried],
        report,
        repo_root=tmp_path,
        config=config,
        progress=NoopProgress(),
    )

    assert resolved_count == 0
    assert len(active) == 1
    assert active[0].status == "uncertain"
