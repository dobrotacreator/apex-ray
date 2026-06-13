from pathlib import Path

from apex_ray.cli.gate import _resolve_incremental_carried_findings
from apex_ray.gate_retry import CarriedFinding, relevant_files_for_finding
from apex_ray.gates import evaluate_pre_push_gate, render_pre_push_gate_stdout
from apex_ray.models import (
    ChangedFile,
    ContextPack,
    DiffStats,
    DiffSummary,
    FileKind,
    Finding,
    FindingConfidence,
    FindingSeverity,
    LLMRun,
    ProjectProfile,
    ReviewConfig,
    RiskSeverity,
    RiskSignal,
    RuleMatch,
    RuleMode,
    TargetMode,
)
from apex_ray.progress import NoopProgress
from apex_ray.report import build_report


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
