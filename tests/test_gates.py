from pathlib import Path

import pytest

from apex_ray.cli.gate import _combine_incremental_decision, _resolve_incremental_carried_findings
from apex_ray.findings import findings_are_duplicates
from apex_ray.gate_retry import CarriedFinding, CoverageDebt, config_fingerprint, relevant_files_for_finding
from apex_ray.gates import PrePushGateDecision, evaluate_pre_push_gate, render_pre_push_gate_stdout
from apex_ray.models import (
    ChangedFile,
    ContextPack,
    DiffStats,
    DiffSummary,
    FileKind,
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
    assert active[0].resolution_reason == "Relevant files changed, but LLM resolution is disabled."


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
