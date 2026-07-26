from pathlib import Path

from apex_ray.cli.gate import _combine_incremental_decision, _resolve_incremental_carried_findings
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
