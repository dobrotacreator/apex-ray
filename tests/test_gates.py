from pathlib import Path

import pytest
from typer.testing import CliRunner

from apex_ray.cli import app
from apex_ray.cli.gate import (
    _combine_incremental_decision,
    _resolve_incremental_carried_findings,
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
    LLMRun,
    ProjectProfile,
    ReviewConfig,
    ReviewerConfig,
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
