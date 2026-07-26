from pathlib import Path

import pytest

from apex_ray.classify import classify_diff
from apex_ray.diff import parse_unified_diff
from apex_ray.findings import (
    active_verifications,
    matching_active_verifications,
    unresolved_verification_candidate_pack_ids,
    verified_report_findings,
)
from apex_ray.gates import evaluate_pre_push_gate
from apex_ray.llm import FakeLLMProvider, LLMProviderError
from apex_ray.models import (
    ChangedFile,
    ContextPack,
    ContextPackStats,
    DiffStats,
    DiffSummary,
    FileKind,
    Finding,
    FindingConfidence,
    FindingSeverity,
    FindingVerification,
    LLMContextSelection,
    LLMProfile,
    LLMProviderName,
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
from apex_ray.pipeline import (
    apply_language_filter,
    consolidate_findings,
    continue_review_from_report,
    plan_llm_context_selection,
    run_review_pipeline,
    select_llm_context_packs,
)
from apex_ray.pipeline.runner import _apply_active_verifications_to_findings
from apex_ray.report import build_report
from apex_ray.reviewers import ReviewerConfigError


def test_select_llm_context_packs_keeps_test_packs_when_source_packs_exist() -> None:
    source_pack = ContextPack(id="src/cart.ts#calculateTotal:1", file="src/cart.ts")
    test_pack = ContextPack(id="src/cart.test.ts#test:1", file="src/cart.test.ts")
    selected = select_llm_context_packs(
        [source_pack, test_pack],
        [
            ChangedFile(old_path="src/cart.ts", new_path="src/cart.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path=None, new_path="src/cart.test.ts", file_kind=FileKind.TEST),
        ],
    )

    assert selected == [source_pack, test_pack]


def test_select_llm_context_packs_keeps_test_only_reviews() -> None:
    test_pack = ContextPack(id="src/cart.test.ts#test:1", file="src/cart.test.ts")
    selected = select_llm_context_packs(
        [test_pack],
        [ChangedFile(old_path="src/cart.test.ts", new_path="src/cart.test.ts", file_kind=FileKind.TEST)],
    )

    assert selected == [test_pack]


def test_select_llm_context_packs_keeps_unknown_packs_with_source_reviews() -> None:
    source_pack = ContextPack(id="src/cart.ts#calculateTotal:1", file="src/cart.ts")
    unknown_pack = ContextPack(id="scripts/check.ts#file", file="scripts/check.ts")
    selected = select_llm_context_packs(
        [source_pack, unknown_pack],
        [ChangedFile(old_path="src/cart.ts", new_path="src/cart.ts", file_kind=FileKind.SOURCE)],
    )

    assert selected == [source_pack, unknown_pack]


def test_select_llm_context_packs_caps_and_prioritizes_source_risk() -> None:
    test_pack = ContextPack(id="src/cart.test.ts#test:1", file="src/cart.test.ts")
    source_pack = ContextPack(
        id="src/cart.ts#calculateTotal:1",
        file="src/cart.ts",
        risk_signals=[
            RiskSignal(
                kind="public_api",
                severity=RiskSeverity.MEDIUM,
                reason="Boundary changed.",
                file="src/cart.ts",
            )
        ],
    )
    docs_pack = ContextPack(id="docs/cart.md#diff", file="docs/cart.md")

    selected = select_llm_context_packs(
        [test_pack, source_pack, docs_pack],
        [
            ChangedFile(old_path="src/cart.test.ts", new_path="src/cart.test.ts", file_kind=FileKind.TEST),
            ChangedFile(old_path="src/cart.ts", new_path="src/cart.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="docs/cart.md", new_path="docs/cart.md", file_kind=FileKind.DOCS),
        ],
        max_packs=1,
    )

    assert selected == [source_pack]


def test_select_llm_context_packs_uses_project_risk_score_and_critical_severity() -> None:
    critical_pack = ContextPack(
        id="src/settlement.ts#settle:1",
        file="src/settlement.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="policy:settlement",
                severity=RiskSeverity.CRITICAL,
                score=97,
                reason="Financial boundary.",
                file="src/settlement.ts",
                source="project",
            )
        ],
    )
    high_pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="auth",
                severity=RiskSeverity.HIGH,
                score=75,
                reason="Authentication boundary.",
                file="src/auth.ts",
            )
        ],
    )

    selected = select_llm_context_packs(
        [critical_pack, high_pack],
        [
            ChangedFile(old_path="src/settlement.ts", new_path="src/settlement.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/auth.ts", new_path="src/auth.ts", file_kind=FileKind.SOURCE),
        ],
        max_packs=1,
    )

    assert selected == [critical_pack]


def test_explicit_critical_project_risk_outranks_low_risk_source_across_file_kinds() -> None:
    source_pack = ContextPack(
        id="src/cart.ts#calculateTotal:1",
        file="src/cart.ts",
        file_kind=FileKind.SOURCE,
    )
    critical_test_pack = ContextPack(
        id="src/settlement.test.ts#settles:1",
        file="src/settlement.test.ts",
        file_kind=FileKind.TEST,
        risk_signals=[
            RiskSignal(
                kind="policy:settlement-regression",
                severity=RiskSeverity.CRITICAL,
                score=100,
                reason="Explicit financial regression boundary.",
                file="src/settlement.test.ts",
                source="project",
            )
        ],
    )

    selected = select_llm_context_packs(
        [source_pack, critical_test_pack],
        [
            ChangedFile(old_path=source_pack.file, new_path=source_pack.file, file_kind=FileKind.SOURCE),
            ChangedFile(
                old_path=critical_test_pack.file,
                new_path=critical_test_pack.file,
                file_kind=FileKind.TEST,
            ),
        ],
        max_packs=1,
    )

    assert selected == [critical_test_pack]


def test_builtin_high_risk_outranks_low_risk_source_across_file_kinds() -> None:
    source_pack = ContextPack(
        id="src/cart.ts#calculateTotal:1",
        file="src/cart.ts",
        file_kind=FileKind.SOURCE,
    )
    risky_test_pack = ContextPack(
        id="src/authorization.test.ts#denies:1",
        file="src/authorization.test.ts",
        file_kind=FileKind.TEST,
        risk_signals=[
            RiskSignal(
                kind="auth",
                severity=RiskSeverity.HIGH,
                reason="Authorization behavior changed.",
                file="src/authorization.test.ts",
            )
        ],
    )

    selected = select_llm_context_packs(
        [source_pack, risky_test_pack],
        [
            ChangedFile(old_path=source_pack.file, new_path=source_pack.file, file_kind=FileKind.SOURCE),
            ChangedFile(
                old_path=risky_test_pack.file,
                new_path=risky_test_pack.file,
                file_kind=FileKind.TEST,
            ),
        ],
        max_packs=1,
    )

    assert selected == [risky_test_pack]


def test_select_llm_context_packs_does_not_let_duplicate_same_band_test_risks_crowd_out_source() -> None:
    noisy_test_pack = ContextPack(
        id="src/cart.test.ts#test:1",
        file="src/cart.test.ts",
        file_kind=FileKind.TEST,
        risk_signals=[
            RiskSignal(
                kind="persistence",
                severity=RiskSeverity.MEDIUM,
                reason="Noisy test fixture.",
                file="src/cart.test.ts",
            )
            for _ in range(20)
        ],
    )
    source_pack = ContextPack(
        id="src/cart.ts#calculateTotal:1",
        file="src/cart.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="persistence",
                severity=RiskSeverity.MEDIUM,
                reason="Source persistence boundary.",
                file="src/cart.ts",
            )
        ],
    )

    selected = select_llm_context_packs(
        [noisy_test_pack, source_pack],
        [
            ChangedFile(old_path="src/cart.test.ts", new_path="src/cart.test.ts", file_kind=FileKind.TEST),
            ChangedFile(old_path="src/cart.ts", new_path="src/cart.ts", file_kind=FileKind.SOURCE),
        ],
        max_packs=1,
    )

    assert selected == [source_pack]


def test_risk_severity_band_outranks_numeric_score() -> None:
    medium_pack = ContextPack(
        id="src/settlement.ts#settle:1",
        file="src/settlement.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="policy:medium",
                severity=RiskSeverity.MEDIUM,
                score=0,
                reason="Explicit medium risk.",
                file="src/settlement.ts",
                source="project",
            )
        ],
    )
    low_pack = ContextPack(
        id="src/large.ts#run:1",
        file="src/large.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="policy:low",
                severity=RiskSeverity.LOW,
                score=100,
                reason="Explicit low risk with a high within-band score.",
                file="src/large.ts",
                source="project",
            )
        ],
        stats=ContextPackStats(estimated_chars=100_000),
    )

    selected = select_llm_context_packs(
        [low_pack, medium_pack],
        [
            ChangedFile(old_path=low_pack.file, new_path=low_pack.file, file_kind=FileKind.SOURCE),
            ChangedFile(
                old_path=medium_pack.file,
                new_path=medium_pack.file,
                file_kind=FileKind.SOURCE,
            ),
        ],
        max_packs=1,
    )

    assert selected == [medium_pack]


def test_select_llm_context_packs_prioritizes_non_test_residual_risk_over_file_kind() -> None:
    source_pack = ContextPack(
        id="src/cart.ts#file",
        file="src/cart.ts",
        file_kind=FileKind.SOURCE,
    )
    schema_pack = ContextPack(
        id="src/cart.schema.ts#file",
        file="src/cart.schema.ts",
        file_kind=FileKind.SCHEMA,
        rule_matches=[
            RuleMatch(
                id="schema-boundary",
                title="Schema boundary",
                severity=FindingSeverity.HIGH,
                mode=RuleMode.STRICT,
            )
        ],
    )

    selected = select_llm_context_packs(
        [source_pack, schema_pack],
        [
            ChangedFile(old_path="src/cart.ts", new_path="src/cart.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/cart.schema.ts", new_path="src/cart.schema.ts", file_kind=FileKind.SCHEMA),
        ],
        max_packs=1,
    )

    assert selected == [schema_pack]


def test_select_llm_context_packs_spreads_cap_across_files() -> None:
    first_a = ContextPack(
        id="src/a.ts#first",
        file="src/a.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[RiskSignal(kind="persistence", severity=RiskSeverity.HIGH, reason="High risk.", file="src/a.ts")],
    )
    second_a = first_a.model_copy(update={"id": "src/a.ts#second"})
    third_a = first_a.model_copy(update={"id": "src/a.ts#third"})
    first_b = first_a.model_copy(update={"id": "src/b.ts#first", "file": "src/b.ts"})

    selected = select_llm_context_packs(
        [first_a, second_a, third_a, first_b],
        [
            ChangedFile(old_path="src/a.ts", new_path="src/a.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/b.ts", new_path="src/b.ts", file_kind=FileKind.SOURCE),
        ],
        max_packs=2,
    )

    assert selected == [first_a, first_b]


def test_select_llm_context_packs_exhausts_p0_before_p1_breadth() -> None:
    first_critical = ContextPack(
        id="src/a.ts#first",
        file="src/a.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="policy:money-movement",
                severity=RiskSeverity.CRITICAL,
                score=100,
                reason="Critical financial boundary.",
                file="src/a.ts",
                source="project",
            )
        ],
    )
    second_critical = first_critical.model_copy(update={"id": "src/a.ts#second"})
    normal_source = ContextPack(
        id="src/b.ts#first",
        file="src/b.ts",
        file_kind=FileKind.SOURCE,
    )

    selected = select_llm_context_packs(
        [first_critical, second_critical, normal_source],
        [
            ChangedFile(old_path="src/a.ts", new_path="src/a.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/b.ts", new_path="src/b.ts", file_kind=FileKind.SOURCE),
        ],
        max_packs=2,
    )

    assert selected == [first_critical, second_critical]


def test_select_llm_context_packs_keeps_risky_sibling_before_low_priority_files() -> None:
    first_a = ContextPack(
        id="src/a.ts#first",
        file="src/a.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[RiskSignal(kind="persistence", severity=RiskSeverity.HIGH, reason="High risk.", file="src/a.ts")],
    )
    second_a = first_a.model_copy(update={"id": "src/a.ts#second"})
    low_b = ContextPack(id="src/b.ts#first", file="src/b.ts", file_kind=FileKind.SOURCE)
    low_c = ContextPack(id="src/c.ts#first", file="src/c.ts", file_kind=FileKind.SOURCE)

    selected = select_llm_context_packs(
        [first_a, second_a, low_b, low_c],
        [
            ChangedFile(old_path="src/a.ts", new_path="src/a.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/b.ts", new_path="src/b.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/c.ts", new_path="src/c.ts", file_kind=FileKind.SOURCE),
        ],
        max_packs=3,
    )

    assert selected == [first_a, second_a, low_b]


def test_select_llm_context_packs_covers_risky_files_before_more_siblings() -> None:
    risky_a = ContextPack(
        id="src/a.ts#first",
        file="src/a.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[RiskSignal(kind="persistence", severity=RiskSeverity.HIGH, reason="High risk.", file="src/a.ts")],
    )
    second_a = risky_a.model_copy(update={"id": "src/a.ts#second"})
    third_a = risky_a.model_copy(update={"id": "src/a.ts#third"})
    risky_b = risky_a.model_copy(update={"id": "src/b.ts#first", "file": "src/b.ts"})
    risky_c = risky_a.model_copy(update={"id": "src/c.ts#first", "file": "src/c.ts"})
    risky_d = risky_a.model_copy(update={"id": "src/d.ts#first", "file": "src/d.ts"})

    selected = select_llm_context_packs(
        [risky_a, second_a, third_a, risky_b, risky_c, risky_d],
        [
            ChangedFile(old_path="src/a.ts", new_path="src/a.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/b.ts", new_path="src/b.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/c.ts", new_path="src/c.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/d.ts", new_path="src/d.ts", file_kind=FileKind.SOURCE),
        ],
        max_packs=4,
    )

    assert selected == [risky_a, risky_b, risky_c, risky_d]


def test_plan_llm_context_selection_skips_over_budget_packs_before_deep_cap() -> None:
    over_budget = ContextPack(
        id="src/a.ts#large",
        file="src/a.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[RiskSignal(kind="persistence", severity=RiskSeverity.HIGH, reason="High risk.", file="src/a.ts")],
        stats=ContextPackStats(estimated_chars=200),
    )
    reviewable = ContextPack(
        id="src/b.ts#file",
        file="src/b.ts",
        file_kind=FileKind.SOURCE,
        stats=ContextPackStats(estimated_chars=80),
    )

    selection = plan_llm_context_selection(
        [over_budget, reviewable],
        [
            ChangedFile(old_path="src/a.ts", new_path="src/a.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/b.ts", new_path="src/b.ts", file_kind=FileKind.SOURCE),
        ],
        max_packs=1,
        max_pack_chars=100,
    )

    assert selection.selected_context_pack_ids == ["src/b.ts#file"]
    assert selection.deep_selected_context_pack_ids == ["src/b.ts#file"]
    assert selection.over_budget_context_pack_ids == ["src/a.ts#large"]
    assert selection.skipped_context_pack_reasons == {"src/a.ts#large": "over context budget"}
    assert selection.stages[0].stage == "deep"
    assert selection.stages[0].selected_context_pack_ids == ["src/b.ts#file"]


def test_plan_llm_context_selection_balanced_reviews_remaining_packs_shallow() -> None:
    risky = ContextPack(
        id="src/a.ts#file",
        file="src/a.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[RiskSignal(kind="auth", severity=RiskSeverity.HIGH, reason="Auth changed.", file="src/a.ts")],
    )
    low = ContextPack(id="src/b.ts#file", file="src/b.ts", file_kind=FileKind.SOURCE)
    test_pack = ContextPack(id="src/a.test.ts#file", file="src/a.test.ts", file_kind=FileKind.TEST)

    selection = plan_llm_context_selection(
        [low, risky, test_pack],
        [
            ChangedFile(old_path="src/a.ts", new_path="src/a.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/b.ts", new_path="src/b.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/a.test.ts", new_path="src/a.test.ts", file_kind=FileKind.TEST),
        ],
        max_packs=3,
        max_deep_packs=1,
        max_input_tokens=None,
    )

    assert selection.selected_context_pack_ids == ["src/b.ts#file", "src/a.ts#file", "src/a.test.ts#file"]
    assert selection.deep_selected_context_pack_ids == ["src/a.ts#file"]
    assert selection.shallow_selected_context_pack_ids == ["src/b.ts#file", "src/a.test.ts#file"]
    assert selection.unselected_context_pack_ids == []
    assert [stage.stage for stage in selection.stages] == ["deep", "shallow"]


def test_plan_llm_context_selection_enforces_max_packs_across_deep_and_shallow() -> None:
    risky = ContextPack(
        id="src/a.ts#file",
        file="src/a.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[RiskSignal(kind="auth", severity=RiskSeverity.HIGH, reason="Auth changed.", file="src/a.ts")],
    )
    low = ContextPack(id="src/b.ts#file", file="src/b.ts", file_kind=FileKind.SOURCE)
    test_pack = ContextPack(id="src/a.test.ts#file", file="src/a.test.ts", file_kind=FileKind.TEST)

    selection = plan_llm_context_selection(
        [low, risky, test_pack],
        [
            ChangedFile(old_path="src/a.ts", new_path="src/a.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/b.ts", new_path="src/b.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/a.test.ts", new_path="src/a.test.ts", file_kind=FileKind.TEST),
        ],
        max_packs=2,
        max_deep_packs=1,
        max_input_tokens=None,
    )

    assert len(selection.selected_context_pack_ids) == 2
    assert selection.deep_selected_context_pack_ids == ["src/a.ts#file"]
    assert len(selection.shallow_selected_context_pack_ids) == 1
    assert len(selection.unselected_context_pack_ids) == 1
    assert set(selection.selected_context_pack_ids).isdisjoint(selection.unselected_context_pack_ids)


def test_plan_llm_context_selection_exhaustive_mode_keeps_pack_caps_without_token_cap() -> None:
    packs = [
        ContextPack(
            id=f"src/{index}.ts#file",
            file=f"src/{index}.ts",
            file_kind=FileKind.SOURCE,
        )
        for index in range(3)
    ]
    changed_files = [
        ChangedFile(
            old_path=pack.file,
            new_path=pack.file,
            file_kind=FileKind.SOURCE,
        )
        for pack in packs
    ]

    selection = plan_llm_context_selection(
        packs,
        changed_files,
        max_packs=1,
        max_deep_packs=1,
        max_input_tokens=None,
        coverage_mode="exhaustive",
    )

    assert len(selection.selected_context_pack_ids) == 1
    assert len(selection.deep_selected_context_pack_ids) == 1
    assert selection.shallow_selected_context_pack_ids == []
    assert len(selection.unselected_context_pack_ids) == 2
    assert set(selection.skipped_context_pack_reasons.values()) == {"not selected by LLM pack cap"}


def test_plan_llm_context_selection_reports_token_budget_skips() -> None:
    first = ContextPack(id="src/a.ts#file", file="src/a.ts", file_kind=FileKind.SOURCE)
    second = ContextPack(id="src/b.ts#file", file="src/b.ts", file_kind=FileKind.SOURCE)

    selection = plan_llm_context_selection(
        [first, second],
        [
            ChangedFile(old_path="src/a.ts", new_path="src/a.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/b.ts", new_path="src/b.ts", file_kind=FileKind.SOURCE),
        ],
        max_packs=2,
        max_input_tokens=1,
    )

    assert selection.selected_context_pack_ids == []
    assert selection.over_token_budget_context_pack_ids == ["src/a.ts#file", "src/b.ts#file"]
    assert selection.skipped_context_pack_reasons == {
        "src/a.ts#file": "not selected by LLM token budget",
        "src/b.ts#file": "not selected by LLM token budget",
    }


def test_plan_llm_context_selection_shallow_reviews_deep_over_budget_pack() -> None:
    large = ContextPack(
        id="src/a.ts#large",
        file="src/a.ts",
        file_kind=FileKind.SOURCE,
        diff_snippet=["+const value = 1;"],
        stats=ContextPackStats(estimated_chars=400),
    )

    selection = plan_llm_context_selection(
        [large],
        [ChangedFile(old_path="src/a.ts", new_path="src/a.ts", file_kind=FileKind.SOURCE)],
        max_packs=1,
        max_pack_chars=350,
    )

    assert selection.deep_selected_context_pack_ids == []
    assert selection.shallow_selected_context_pack_ids == ["src/a.ts#large"]
    assert selection.selected_context_pack_ids == ["src/a.ts#large"]
    assert selection.over_budget_context_pack_ids == []
    assert selection.skipped_context_pack_reasons == {}


def test_continue_review_from_report_reviews_residual_pack(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    reviewed = ContextPack(id="src/auth.ts#login:1", file="src/auth.ts", file_kind=FileKind.SOURCE)
    residual = ContextPack(
        id="src/payments.ts#capture:1",
        file="src/payments.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(kind="persistence", severity=RiskSeverity.HIGH, reason="State changed.", file="src/payments.ts")
        ],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=2)),
        context_packs=[reviewed, residual],
        llm_runs=[
            LLMRun(
                provider="fake",
                context_pack_id=reviewed.id,
                status="ok",
                duration_ms=1,
            )
        ],
    )
    finding = Finding(
        title="Capture skips ledger lock",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/payments.ts",
        line=10,
        failure_mode="Concurrent capture can double-spend.",
        evidence="The context pack changed capture.",
        suggested_fix="Lock the ledger row before updating.",
        suggested_test="Add a concurrent capture test.",
    )

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        residual_priorities={"p0"},
        provider=FakeLLMProvider([finding]),
    )

    assert [pack.id for pack in selected] == [residual.id]
    assert continued.llm_coverage.partial_severity == "none"
    assert continued.llm_coverage.reviewed_context_pack_ids == [reviewed.id, residual.id]
    assert continued.findings[0].context_pack_id == residual.id
    assert any(stage.stage == "continue_deep" for stage in continued.llm_selection.stages)


def test_capped_continuation_preserves_archived_residual_priority(tmp_path: Path) -> None:
    archived_p0 = ContextPack(
        id="src/archived.ts#run:1",
        file="src/archived.ts",
        file_kind=FileKind.SOURCE,
    )
    recomputed_p0 = ContextPack(
        id="src/current.ts#run:1",
        file="src/current.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="security",
                severity=RiskSeverity.HIGH,
                reason="Current classification is high risk.",
                file="src/current.ts",
            )
        ],
    )
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = False
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(
            target_mode=TargetMode.PATCH,
            files=[
                ChangedFile(
                    old_path=archived_p0.file,
                    new_path=archived_p0.file,
                    file_kind=FileKind.SOURCE,
                ),
                ChangedFile(
                    old_path=recomputed_p0.file,
                    new_path=recomputed_p0.file,
                    file_kind=FileKind.SOURCE,
                ),
            ],
        ),
        context_packs=[archived_p0, recomputed_p0],
    )
    statuses = {status.context_pack_id: status for status in initial.llm_coverage.pack_statuses}
    statuses[archived_p0.id].priority = "p0"
    statuses[recomputed_p0.id].priority = "p1"

    _continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        max_pack_reviews=1,
        provider=FakeLLMProvider([]),
    )

    assert [pack.id for pack in selected] == [archived_p0.id]


def test_continue_review_updates_modern_default_general_reviewer_selection(
    tmp_path: Path,
) -> None:
    reviewed = ContextPack(id="src/a.ts#run:1", file="src/a.ts")
    residual = ContextPack(id="src/b.ts#run:1", file="src/b.ts")
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = False
    config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[reviewed.id, residual.id],
        selected_context_pack_ids=[reviewed.id],
        deep_selected_context_pack_ids=[reviewed.id],
        unselected_context_pack_ids=[residual.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[reviewed, residual],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="general",
                context_pack_id=reviewed.id,
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"general": selection},
    )

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        provider=FakeLLMProvider([]),
    )

    assert [pack.id for pack in selected] == [residual.id]
    assert continued.reviewer_selections["general"].selected_context_pack_ids == [reviewed.id, residual.id]
    assert continued.llm_coverage.reviewers[0].selected_context_pack_ids == [
        reviewed.id,
        residual.id,
    ]
    assert continued.llm_coverage.reviewers[0].reviewed_context_pack_ids == [
        reviewed.id,
        residual.id,
    ]


def test_continue_review_uses_all_configured_reviewers_when_not_explicitly_scoped(
    tmp_path: Path,
) -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", focus="Authorization boundaries."),
            ReviewerConfig(id="correctness", focus="Behavioral regressions."),
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = False
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
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[pack],
    )
    finding = Finding(
        title="Authorization guard can be bypassed",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=1,
        failure_mode="An untrusted caller can bypass authorization.",
        evidence="The changed branch returns before the guard.",
        suggested_fix="Keep the guard before the return.",
        suggested_test="Add a denied-role regression test.",
    )

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        provider=FakeLLMProvider([finding]),
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert {run.reviewer_id for run in continued.llm_runs if run.kind in {"review", "review_shallow"}} == {
        "correctness",
        "security",
    }
    assert continued.findings[0].reviewer_ids == ["correctness", "security"]
    assert set(continued.reviewer_selections) == {"correctness", "security"}


def test_continue_review_retries_required_reviewer_debt_even_when_pack_was_reviewed(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/ledger.ts#settle:1",
        file="src/ledger.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="financial",
                severity=RiskSeverity.CRITICAL,
                reason="Settlement arithmetic changed.",
                file="src/ledger.ts",
            )
        ],
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", focus="Authorization."),
            ReviewerConfig(id="finance", focus="Financial correctness.", required=True),
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id=pack.id,
                status="failed_auth",
                duration_ms=1,
                error="invalid credentials",
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection, "finance": selection},
    )

    assert initial.llm_coverage.reviewed_context_pack_ids == [pack.id]
    assert initial.llm_coverage.quality_gate_status == "fail"
    assert [(todo.context_pack_id, todo.reviewer_id) for todo in initial.llm_coverage.coverage_todos] == [
        (pack.id, "finance")
    ]
    assert "--reviewer finance" in initial.llm_coverage.coverage_todos[0].suggested_command

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        residual_priorities={"p0"},
        reviewer_ids=["finance"],
        provider=FakeLLMProvider([]),
    )

    reviewer_coverage = {reviewer.reviewer_id: reviewer for reviewer in continued.llm_coverage.reviewers}
    assert [candidate.id for candidate in selected] == [pack.id]
    assert [run.reviewer_id for run in continued.llm_runs if run.context_pack_id == pack.id and run.status == "ok"] == [
        "security",
        "finance",
    ]
    assert set(reviewer_coverage) == {"security", "finance"}
    assert reviewer_coverage["finance"].status == "pass"
    assert continued.llm_coverage.quality_gate_status != "fail"


def test_continue_review_caps_reviewer_pack_assignments_fairly(
    tmp_path: Path,
) -> None:
    packs = [
        ContextPack(
            id=f"src/payment-{index}.ts#settle:1",
            file=f"src/payment-{index}.ts",
            file_kind=FileKind.SOURCE,
            risk_signals=[
                RiskSignal(
                    kind="financial",
                    severity=RiskSeverity.CRITICAL,
                    reason="Settlement behavior changed.",
                    file=f"src/payment-{index}.ts",
                )
            ],
        )
        for index in range(3)
    ]
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", focus="Authorization.", verify=False),
            ReviewerConfig(id="finance", focus="Financial correctness.", required=True, verify=False),
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.cache_enabled = False
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(
            target_mode=TargetMode.PATCH,
            stats=DiffStats(files_changed=3),
            files=[
                ChangedFile(
                    old_path=pack.file,
                    new_path=pack.file,
                    file_kind=FileKind.SOURCE,
                )
                for pack in packs
            ],
        ),
        context_packs=packs,
    )

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        residual_priorities={"p0"},
        max_pack_reviews=3,
        provider=FakeLLMProvider([]),
    )

    primary_runs = [run for run in continued.llm_runs if run.kind in {"review", "review_shallow"}]
    assert [(run.reviewer_id, run.context_pack_id) for run in primary_runs] == [
        ("security", packs[0].id),
        ("finance", packs[0].id),
        ("finance", packs[1].id),
    ]
    assert [pack.id for pack in selected] == [packs[0].id, packs[1].id]
    assert continued.llm_coverage.partial_severity == "critical"
    assert {(todo.reviewer_id, todo.context_pack_id) for todo in continued.llm_coverage.coverage_todos}.issuperset(
        {
            ("security", packs[1].id),
            ("security", packs[2].id),
            ("finance", packs[2].id),
        }
    )


def test_continue_review_cap_allocation_is_independent_of_reviewer_order(
    tmp_path: Path,
) -> None:
    finance_pack = ContextPack(
        id="src/payments/ledger.ts#post:1",
        file="src/payments/ledger.ts",
        risk_signals=[
            RiskSignal(
                kind="financial",
                severity=RiskSeverity.CRITICAL,
                reason="Ledger mutation changed.",
                file="src/payments/ledger.ts",
            )
        ],
    )
    security_pack = ContextPack(
        id="src/auth/session.ts#authorize:1",
        file="src/auth/session.ts",
        risk_signals=[
            RiskSignal(
                kind="security",
                severity=RiskSeverity.CRITICAL,
                reason="Authorization behavior changed.",
                file="src/auth/session.ts",
            )
        ],
    )
    packs = [security_pack, finance_pack]
    changed_files = [ChangedFile(old_path=pack.file, new_path=pack.file, file_kind=FileKind.SOURCE) for pack in packs]

    def capped_assignment(config: ReviewConfig, reviewer_ids: list[str]) -> list[tuple[str | None, str]]:
        config.llm.enabled = True
        config.llm.provider = LLMProviderName.FAKE
        config.llm.cache_enabled = False
        initial = build_report(
            ProjectProfile(root=str(tmp_path), is_git_repo=True),
            config,
            DiffSummary(
                target_mode=TargetMode.PATCH,
                stats=DiffStats(files_changed=2),
                files=changed_files,
            ),
            context_packs=packs,
        )
        continued, _selected = continue_review_from_report(
            initial,
            repo_root=tmp_path,
            residual_priorities={"p0"},
            max_pack_reviews=1,
            reviewer_ids=reviewer_ids,
            provider=FakeLLMProvider([]),
        )
        return [
            (run.reviewer_id, run.context_pack_id)
            for run in continued.llm_runs
            if run.kind in {"review", "review_shallow"}
        ]

    security = ReviewerConfig(
        id="security",
        paths=["src/auth/**"],
        required=True,
        verify=False,
    )
    finance = ReviewerConfig(
        id="finance",
        paths=["src/payments/**"],
        required=True,
        verify=False,
    )

    forward = capped_assignment(
        ReviewConfig(reviewers=[security, finance]),
        ["security", "finance"],
    )
    reversed_order = capped_assignment(
        ReviewConfig(reviewers=[finance, security]),
        ["finance", "security"],
    )

    assert forward == reversed_order == [("finance", finance_pack.id)]


def test_continue_review_clean_retry_supersedes_failed_verifier_run(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization.",
                required=True,
                verify=True,
            )
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="provider_error",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )

    continued, first_selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )
    _unchanged, second_selected = continue_review_from_report(
        continued,
        repo_root=tmp_path,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert [candidate.id for candidate in first_selected] == [pack.id]
    assert continued.llm_coverage.reviewers[0].status == "pass"
    assert continued.llm_coverage.quality_gate_status == "pass"
    assert second_selected == []


def test_continue_review_reverifies_stored_debt_after_a_clean_forced_review(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
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
        reviewer_ids=["security"],
    )
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", required=True, verify=True)])
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=False,
                confidence=FindingConfidence.LOW,
                reason="The verifier was unavailable.",
                superseded=True,
                superseded_reason=("Verification run did not complete successfully (failed_provider)."),
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="failed_provider",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    provider = FakeLLMProvider([], verification_approvals=[True])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        pack_ids={pack.id},
        only_unreviewed=False,
        provider=provider,
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert provider.reviewed_pack_ids == [pack.id]
    assert provider.verified_batch_pack_ids == [pack.id]
    assert continued.findings == [finding]
    assert continued.llm_coverage.reviewers[0].status == "pass"


def test_clean_forced_review_does_not_verify_explicit_pending_candidate(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
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
        reviewer_ids=["security"],
        reviewer_context_pack_ids={"security": [pack.id]},
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                required=True,
                verify=True,
            )
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=False,
                confidence=FindingConfidence.LOW,
                reason="The verifier was unavailable.",
                superseded=True,
                superseded_reason=("Verification run did not complete successfully (failed_provider)."),
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="failed_provider",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    provider = FakeLLMProvider([], verification_approvals=[True])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        pack_ids={pack.id},
        only_unreviewed=False,
        provider=provider,
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert provider.reviewed_pack_ids == [pack.id]
    assert provider.verified_batch_pack_ids == []
    assert continued.findings == []
    assert continued.llm_coverage.reviewers[0].status == "pass"


def test_continue_review_retries_failed_verifier_route_when_a_sibling_route_succeeded(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    low = Finding(
        title="Low-risk authorization diagnostic",
        severity=FindingSeverity.LOW,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=10,
        failure_mode="An authorization diagnostic can be misleading.",
        evidence="The changed diagnostic omits the tenant id.",
        suggested_fix="Include the tenant id.",
        suggested_test="Assert the diagnostic identifies the tenant.",
        context_pack_id=pack.id,
        reviewer_ids=["security"],
    )
    high = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=100,
        failure_mode="A transfer can bypass the tenant boundary.",
        evidence="No authorization predicate precedes the transfer.",
        suggested_fix="Add a tenant authorization check.",
        suggested_test="Reject cross-tenant transfers.",
        context_pack_id=pack.id,
        reviewer_ids=["security"],
    )
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", required=True, verify=True)])
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[low],
        verifications=[
            FindingVerification(
                finding=low,
                reviewer_id="security",
                approved=False,
                confidence=FindingConfidence.LOW,
                reason="An earlier cheap verifier attempt failed.",
                superseded=True,
                superseded_reason=("Verification run did not complete successfully (failed_provider)."),
            ),
            FindingVerification(
                finding=low,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The low-risk finding was verified.",
            ),
            FindingVerification(
                finding=high,
                reviewer_id="security",
                approved=False,
                confidence=FindingConfidence.LOW,
                reason="The strong verifier was unavailable.",
                superseded=True,
                superseded_reason=("Verification run did not complete successfully (failed_provider)."),
            ),
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=2,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                profile="cheap",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                profile="strong",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="failed_provider",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    provider = FakeLLMProvider([], verification_approvals=[True, True])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert selected == []
    assert provider.verified_finding_titles == [high.title]
    assert continued.llm_coverage.reviewers[0].status == "pass"
    assert continued.llm_coverage.quality_gate_status == "pass"


def test_continue_review_keeps_new_failed_subject_after_retrying_old_debt(
    tmp_path: Path,
) -> None:
    class NewSubjectVerifierFailure(FakeLLMProvider):
        def verify_findings(
            self,
            findings: list[Finding],
            pack: ContextPack,
            repo_root: Path,
        ) -> list[FindingVerification]:
            self.verified_batches.append([finding.title for finding in findings])
            self.verified_batch_pack_ids.append(pack.id)
            self.verified_finding_titles.extend(finding.title for finding in findings)
            if findings[0].title == "Expired sessions remain active":
                raise LLMProviderError("temporary verifier outage")
            return [
                FindingVerification(
                    finding=finding,
                    approved=True,
                    confidence=FindingConfidence.HIGH,
                    reason="The old debt is confirmed.",
                )
                for finding in findings
            ]

    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    old_debt = Finding(
        title="Old verification debt",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=10,
        failure_mode="An old authorization path may bypass its tenant guard.",
        evidence="The old branch executes before the tenant check.",
        suggested_fix="Move the old branch after the tenant check.",
        suggested_test="Reject the old unauthorized path.",
        context_pack_id=pack.id,
        reviewer_ids=["security"],
    )
    new_subject = Finding(
        title="Expired sessions remain active",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=100,
        failure_mode="A request can continue after its authenticated session expires.",
        evidence="The changed branch skips the session expiry comparison.",
        suggested_fix="Reject expired sessions before dispatch.",
        suggested_test="Assert an expired session cannot continue.",
        context_pack_id=pack.id,
        reviewer_ids=["security"],
    )
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", required=True, verify=True)])
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        verifications=[
            FindingVerification(
                finding=old_debt,
                reviewer_id="security",
                approved=False,
                confidence=FindingConfidence.LOW,
                reason="The original verifier was unavailable.",
                superseded=True,
                superseded_reason=("Verification run did not complete successfully (failed_provider)."),
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="failed_provider",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    provider = NewSubjectVerifierFailure([new_subject])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        pack_ids={pack.id},
        only_unreviewed=False,
        provider=provider,
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert provider.verified_batches == [
        [new_subject.title],
        [old_debt.title],
    ]
    assert continued.findings == [old_debt]
    assert continued.llm_coverage.reviewers[0].status == "fail"
    assert continued.llm_coverage.quality_gate_status == "fail"
    assert [(todo.context_pack_id, todo.reviewer_id) for todo in continued.llm_coverage.coverage_todos] == [
        (pack.id, "security")
    ]

    retry_provider = FakeLLMProvider([], verification_approvals=[True])
    retried, retry_selected = continue_review_from_report(
        continued,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        provider=retry_provider,
    )

    assert retry_selected == []
    assert retry_provider.verified_finding_titles == [new_subject.title]
    assert {finding.title for finding in retried.findings} == {
        old_debt.title,
        new_subject.title,
    }
    assert retried.llm_coverage.reviewers[0].status == "pass"
    assert retried.llm_coverage.quality_gate_status == "pass"


def test_continue_review_disabled_verifier_ignores_prior_verifier_debt(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                required=True,
                verify=True,
            )
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="provider_error",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    disabled_config = original_config.model_copy(deep=True)
    disabled_config.reviewers[0].verify = False

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=disabled_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert selected == []
    assert continued.llm_coverage.verify_enabled is False
    assert continued.llm_coverage.reviewers[0].status == "pass"
    assert continued.llm_coverage.quality_gate_status == "pass"
    assert continued.llm_coverage.partial_severity == "none"


def test_continue_review_expanded_reviewer_scope_selects_new_matching_pack(
    tmp_path: Path,
) -> None:
    first = ContextPack(id="src/a.ts#file:1", file="src/a.ts")
    newly_matching = ContextPack(id="src/b.ts#file:1", file="src/b.ts")
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                paths=["src/a.ts"],
                verify=False,
            )
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.cache_enabled = False
    reviewer_selection = LLMContextSelection(
        total_context_pack_ids=[first.id],
        selected_context_pack_ids=[first.id],
        deep_selected_context_pack_ids=[first.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[first, newly_matching],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=first.id,
                status="ok",
                duration_ms=1,
            )
        ],
        reviewer_selections={"security": reviewer_selection},
    )
    expanded_config = original_config.model_copy(deep=True)
    expanded_config.reviewers[0].paths = ["src/**"]

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=expanded_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    reviewer = continued.llm_coverage.reviewers[0]
    assert [candidate.id for candidate in selected] == [newly_matching.id]
    assert reviewer.matching_context_pack_ids == [first.id, newly_matching.id]
    assert reviewer.reviewed_context_pack_ids == [first.id, newly_matching.id]
    assert reviewer.status == "pass"


def test_continue_review_narrowed_reviewer_scope_drops_out_of_scope_debt(
    tmp_path: Path,
) -> None:
    kept = ContextPack(id="src/a.ts#file:1", file="src/a.ts")
    removed = ContextPack(id="src/b.ts#file:1", file="src/b.ts")
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                paths=["src/**"],
                required=True,
                verify=False,
            )
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    selection = LLMContextSelection(
        total_context_pack_ids=[kept.id, removed.id],
        selected_context_pack_ids=[kept.id, removed.id],
        deep_selected_context_pack_ids=[kept.id, removed.id],
    )
    removed_finding = Finding(
        title="Out-of-scope authorization finding",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=removed.file,
        failure_mode="The removed scope contains an authorization bypass.",
        evidence="The old reviewer reported a missing guard.",
        suggested_fix="Run the guard before dispatch.",
        suggested_test="Reject an unauthorized request.",
        context_pack_id=removed.id,
        reviewer_ids=["security"],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[kept, removed],
        findings=[removed_finding],
        verifications=[
            FindingVerification(
                finding=removed_finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The old reviewer confirmed the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=kept.id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=removed.id,
                status="failed_auth",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    narrowed_config = original_config.model_copy(deep=True)
    narrowed_config.reviewers[0].paths = ["src/a.ts"]

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=narrowed_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    reviewer = continued.llm_coverage.reviewers[0]
    assert selected == []
    assert continued.llm_selection is not None
    assert continued.llm_selection.selected_context_pack_ids == [kept.id]
    assert reviewer.matching_context_pack_ids == [kept.id]
    assert reviewer.reviewed_context_pack_ids == [kept.id]
    assert reviewer.status == "pass"
    assert continued.findings == [removed_finding]
    assert continued.verifications[0].superseded is True
    assert continued.verifications[0].superseded_reason == ("Replaced by the current reviewer configuration scope.")
    assert evaluate_pre_push_gate(continued, narrowed_config.gates.pre_push).blocked is False
    unverified_gate = narrowed_config.gates.pre_push.model_copy(
        update={"require_verified_findings": False},
    )
    assert evaluate_pre_push_gate(continued, unverified_gate).blocked is False
    assert continued.llm_coverage.quality_gate_status != "fail"
    assert not any("review run(s) failed" in reason for reason in continued.llm_coverage.partial_reasons)

    verify_narrowed_config = narrowed_config.model_copy(deep=True)
    verify_narrowed_config.reviewers[0].verify = True
    verify_provider = FakeLLMProvider([], verification_approvals=[True])
    verify_narrowed, verify_selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=verify_narrowed_config,
        reviewer_ids=["security"],
        provider=verify_provider,
    )

    assert verify_selected == []
    assert verify_provider.reviewed_pack_ids == []
    assert verify_provider.verified_batch_pack_ids == []
    assert verify_narrowed.findings == [removed_finding]
    assert (
        evaluate_pre_push_gate(
            verify_narrowed,
            verify_narrowed_config.gates.pre_push,
        ).blocked
        is False
    )

    refocused_config = narrowed_config.model_copy(deep=True)
    refocused_config.reviewers[0].focus = "Re-check the narrowed authorization surface."
    refocused, refocused_selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=refocused_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )
    assert [pack.id for pack in refocused_selected] == [kept.id]

    refocused_verify_config = refocused_config.model_copy(deep=True)
    refocused_verify_config.reviewers[0].verify = True
    refocused_verify_provider = FakeLLMProvider([], verification_approvals=[True])
    refocused_verify, refocused_verify_selected = continue_review_from_report(
        refocused,
        repo_root=tmp_path,
        config=refocused_verify_config,
        reviewer_ids=["security"],
        provider=refocused_verify_provider,
    )

    assert refocused_verify_selected == []
    assert refocused_verify_provider.reviewed_pack_ids == []
    assert refocused_verify_provider.verified_batch_pack_ids == []
    assert (
        evaluate_pre_push_gate(
            refocused_verify,
            refocused_verify_config.gates.pre_push,
        ).blocked
        is False
    )


def test_continue_review_current_config_drops_removed_reviewer_state(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", verify=False),
            ReviewerConfig(id="finance", required=True, verify=False),
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    finance_finding = Finding(
        title="Removed finance reviewer finding",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        failure_mode="A settlement can be duplicated.",
        evidence="The old finance reviewer found a missing idempotency check.",
        suggested_fix="Add the idempotency check.",
        suggested_test="Retry the settlement request.",
        context_pack_id=pack.id,
        reviewer_ids=["finance"],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finance_finding],
        verifications=[
            FindingVerification(
                finding=finance_finding,
                reviewer_id="finance",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The finance reviewer confirmed the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id=pack.id,
                status="failed_auth",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={
            "security": selection,
            "finance": selection,
        },
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers = [current_config.reviewers[0]]

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert selected == []
    assert set(continued.reviewer_selections) == {"security"}
    assert [reviewer.reviewer_id for reviewer in continued.llm_coverage.reviewers] == ["security"]
    assert continued.findings == []
    assert continued.verifications[0].superseded is True
    assert continued.verifications[0].superseded_reason == ("Replaced by the current reviewer configuration scope.")
    assert evaluate_pre_push_gate(continued, current_config.gates.pre_push).blocked is False
    assert not any("review run(s) failed" in reason for reason in continued.llm_coverage.partial_reasons)


def test_continue_review_preserves_cross_pack_approval_after_duplicate_reviewer_is_removed(
    tmp_path: Path,
) -> None:
    test_pack = ContextPack(
        id="tests/auth.test.ts#authorize:1",
        file="tests/auth.test.ts",
        file_kind=FileKind.TEST,
    )
    source_pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                paths=["tests/**"],
                required=True,
                verify=True,
            ),
            ReviewerConfig(
                id="finance",
                paths=["src/**"],
                verify=False,
            ),
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.gates.pre_push.require_verified_findings = True
    original_config.gates.pre_push.min_finding_severity = FindingSeverity.HIGH
    security_finding = Finding(
        title="Authorization guard can be bypassed before settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=test_pack.file,
        line=42,
        failure_mode=(
            "An untrusted caller can bypass the authorization guard and submit a settlement "
            "without the required account ownership check."
        ),
        evidence=(
            "The changed early return executes before the account ownership authorization guard on the settlement path."
        ),
        suggested_fix="Move the early return after the ownership guard.",
        suggested_test="Add a denied-account settlement regression test.",
        context_pack_id=test_pack.id,
        reviewer_ids=["security"],
        reviewer_context_pack_ids={"security": [test_pack.id]},
    )
    finance_finding = security_finding.model_copy(
        update={
            "file": source_pack.file,
            "context_pack_id": source_pack.id,
            "reviewer_ids": ["finance"],
            "reviewer_context_pack_ids": {"finance": [source_pack.id]},
        }
    )
    consolidated_finding = consolidate_findings([security_finding, finance_finding])[0]
    security_selection = LLMContextSelection(
        total_context_pack_ids=[test_pack.id],
        selected_context_pack_ids=[test_pack.id],
        deep_selected_context_pack_ids=[test_pack.id],
    )
    finance_selection = LLMContextSelection(
        total_context_pack_ids=[source_pack.id],
        selected_context_pack_ids=[source_pack.id],
        deep_selected_context_pack_ids=[source_pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[test_pack, source_pack],
        findings=[consolidated_finding],
        verifications=[
            FindingVerification(
                finding=security_finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The security reviewer confirmed the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=test_pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=test_pack.id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id=source_pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
        ],
        reviewer_selections={
            "security": security_selection,
            "finance": finance_selection,
        },
    )
    initial_gate = evaluate_pre_push_gate(initial, original_config.gates.pre_push)
    assert initial_gate.blocked is True
    assert initial_gate.blocking_findings == [consolidated_finding]

    noop, noop_selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=original_config,
        provider=FakeLLMProvider([]),
    )

    assert noop_selected == []
    assert noop.findings == [consolidated_finding]

    current_config = original_config.model_copy(deep=True)
    current_config.reviewers = [current_config.reviewers[0]]

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert selected == []
    assert continued.findings == [security_finding]
    assert continued.verifications[0].superseded is False
    assert evaluate_pre_push_gate(continued, current_config.gates.pre_push).blocked is True


@pytest.mark.parametrize("explicit_scope", [False, True])
def test_continue_review_verifies_cross_pack_canonical_finding_after_reviewer_is_removed(
    tmp_path: Path,
    explicit_scope: bool,
) -> None:
    class FailingVerificationProvider(FakeLLMProvider):
        def verify_findings(
            self,
            findings: list[Finding],
            pack: ContextPack,
            repo_root: Path,
        ) -> list[FindingVerification]:
            self.verified_batch_pack_ids.append(pack.id)
            raise LLMProviderError("temporary verifier outage")

    test_pack = ContextPack(
        id="tests/auth.test.ts#authorize:1",
        file="tests/auth.test.ts",
        file_kind=FileKind.TEST,
    )
    source_pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                paths=["tests/**", "src/**"],
                required=True,
                verify=False,
            ),
            ReviewerConfig(id="finance", paths=["src/**"], verify=False),
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.cache_enabled = False
    original_config.gates.pre_push.require_verified_findings = True
    original_config.gates.pre_push.min_finding_severity = FindingSeverity.HIGH
    security_finding = Finding(
        title="Authorization guard can be bypassed before settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=test_pack.file,
        line=42,
        failure_mode=(
            "An untrusted caller can bypass the authorization guard and submit a settlement "
            "without the required account ownership check."
        ),
        evidence="The changed early return executes before the account ownership authorization guard.",
        suggested_fix="Move the early return after the ownership guard.",
        suggested_test="Add a denied-account settlement regression test.",
        context_pack_id=test_pack.id,
        reviewer_ids=["security"],
        reviewer_context_pack_ids={"security": [test_pack.id]},
    )
    finance_finding = security_finding.model_copy(
        update={
            "file": source_pack.file,
            "context_pack_id": source_pack.id,
            "reviewer_ids": ["finance"],
            "reviewer_context_pack_ids": {"finance": [source_pack.id]},
        }
    )
    consolidated_finding = consolidate_findings([security_finding, finance_finding])[0]
    security_selection = LLMContextSelection(
        total_context_pack_ids=[test_pack.id, source_pack.id],
        selected_context_pack_ids=[test_pack.id, source_pack.id],
        deep_selected_context_pack_ids=[test_pack.id, source_pack.id],
    )
    finance_selection = LLMContextSelection(
        total_context_pack_ids=[source_pack.id],
        selected_context_pack_ids=[source_pack.id],
        deep_selected_context_pack_ids=[source_pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[test_pack, source_pack],
        findings=[consolidated_finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=test_pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=source_pack.id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id=source_pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
        ],
        reviewer_selections={
            "security": security_selection,
            "finance": finance_selection,
        },
        reviewer_scope_ids=["security", "finance"] if explicit_scope else None,
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers = [current_config.reviewers[0]]
    current_config.reviewers[0].paths = ["tests/**"]
    current_config.reviewers[0].verify = True
    failing_provider = FailingVerificationProvider([])

    failed, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=failing_provider,
    )

    assert selected == []
    assert failing_provider.reviewed_pack_ids == []
    assert failing_provider.verified_batch_pack_ids == [source_pack.id]
    security_only_finding = consolidated_finding.model_copy(
        update={
            "reviewer_ids": ["security"],
            "reviewer_context_pack_ids": {"security": [test_pack.id]},
        }
    )
    assert failed.findings == [security_only_finding]
    assert failed.llm_coverage.reviewers[0].status == "fail"
    assert any(
        todo.reviewer_id == "security" and todo.context_pack_id == source_pack.id
        for todo in failed.llm_coverage.coverage_todos
    )
    deferred = build_report(
        failed.project,
        current_config,
        failed.diff,
        context_packs=failed.context_packs,
        findings=failed.findings,
        verifications=failed.verifications,
        llm_runs=[
            *failed.llm_runs,
            LLMRun(
                kind="verify_reset",
                provider="apex-ray",
                reviewer_id="security",
                context_pack_id=source_pack.id,
                status="verification_retry",
                duration_ms=0,
            ),
        ],
        llm_selection=failed.llm_selection,
        reviewer_selections=failed.reviewer_selections,
        reviewer_scope_ids=failed.reviewer_scope_ids,
    )
    assert deferred.llm_coverage.reviewers[0].status == "fail"
    assert deferred.llm_coverage.quality_gate_status == "fail"

    retry_provider = FakeLLMProvider([], verification_approvals=[True])
    continued, retry_selected = continue_review_from_report(
        failed,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=retry_provider,
    )

    assert retry_selected == []
    assert retry_provider.reviewed_pack_ids == []
    assert retry_provider.verified_batch_pack_ids == [source_pack.id]
    assert continued.findings == [security_only_finding]
    assert evaluate_pre_push_gate(continued, current_config.gates.pre_push).blocked is True

    noop_provider = FakeLLMProvider([])
    repeated, repeated_selected = continue_review_from_report(
        continued,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=noop_provider,
    )

    assert repeated_selected == []
    assert noop_provider.reviewed_pack_ids == []
    assert noop_provider.verified_batch_pack_ids == []
    assert repeated.findings == continued.findings
    assert repeated.verifications == continued.verifications


def test_continue_review_drops_cross_pack_provenance_after_origin_review_finds_nothing(
    tmp_path: Path,
) -> None:
    test_pack = ContextPack(
        id="tests/auth.test.ts#authorize:1",
        file="tests/auth.test.ts",
        file_kind=FileKind.TEST,
    )
    source_pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization boundaries.",
                paths=["tests/**"],
                required=True,
                verify=False,
            ),
            ReviewerConfig(id="finance", paths=["src/**"], verify=False),
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.cache_enabled = False
    original_config.gates.pre_push.min_finding_severity = FindingSeverity.HIGH
    security_finding = Finding(
        title="Authorization guard can be bypassed before settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=test_pack.file,
        line=42,
        failure_mode=(
            "An untrusted caller can bypass the authorization guard and submit a settlement "
            "without the required account ownership check."
        ),
        evidence="The changed early return executes before the account ownership authorization guard.",
        suggested_fix="Move the early return after the ownership guard.",
        suggested_test="Add a denied-account settlement regression test.",
        context_pack_id=test_pack.id,
        reviewer_ids=["security"],
        reviewer_context_pack_ids={"security": [test_pack.id]},
    )
    finance_finding = security_finding.model_copy(
        update={
            "file": source_pack.file,
            "context_pack_id": source_pack.id,
            "reviewer_ids": ["finance"],
            "reviewer_context_pack_ids": {"finance": [source_pack.id]},
        }
    )
    consolidated_finding = consolidate_findings([security_finding, finance_finding])[0]
    security_selection = LLMContextSelection(
        total_context_pack_ids=[test_pack.id],
        selected_context_pack_ids=[test_pack.id],
        deep_selected_context_pack_ids=[test_pack.id],
    )
    finance_selection = LLMContextSelection(
        total_context_pack_ids=[source_pack.id],
        selected_context_pack_ids=[source_pack.id],
        deep_selected_context_pack_ids=[source_pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[test_pack, source_pack],
        findings=[consolidated_finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=test_pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id=source_pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
        ],
        reviewer_selections={
            "security": security_selection,
            "finance": finance_selection,
        },
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers = [current_config.reviewers[0]]
    current_config.reviewers[0].focus = "Re-evaluate current authorization behavior."
    provider = FakeLLMProvider([])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert [pack.id for pack in selected] == [test_pack.id]
    assert provider.reviewed_pack_ids == [test_pack.id]
    assert continued.findings == []
    assert evaluate_pre_push_gate(continued, current_config.gates.pre_push).blocked is False

    rediscovery_config = original_config.model_copy(deep=True)
    rediscovery_config.reviewers[0].focus = "Re-evaluate current authorization behavior."
    rediscovery_config.reviewers[0].verify = True
    rediscovery_provider = FakeLLMProvider(
        [security_finding],
        verification_approvals=[True],
    )
    rediscovered, rediscovered_selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=rediscovery_config,
        reviewer_ids=["security"],
        provider=rediscovery_provider,
    )

    assert [pack.id for pack in rediscovered_selected] == [test_pack.id]
    assert rediscovery_provider.reviewed_pack_ids == [test_pack.id]
    assert rediscovery_provider.verified_batch_pack_ids == [test_pack.id]
    assert len(rediscovered.findings) == 1
    assert rediscovered.findings[0].reviewer_ids == ["finance", "security"]
    assert rediscovered.findings[0].reviewer_context_pack_ids == {
        "finance": [source_pack.id],
        "security": [test_pack.id],
    }

    repeated_provider = FakeLLMProvider([])
    repeated, repeated_selected = continue_review_from_report(
        rediscovered,
        repo_root=tmp_path,
        config=rediscovery_config,
        reviewer_ids=["security"],
        provider=repeated_provider,
    )

    assert repeated_selected == []
    assert repeated_provider.reviewed_pack_ids == []
    assert repeated_provider.verified_batch_pack_ids == []
    assert repeated.findings == rediscovered.findings
    assert repeated.verifications == rediscovered.verifications

    class FailingReviewProvider(FakeLLMProvider):
        def review_context_pack(
            self,
            pack: ContextPack,
            repo_root: Path,
        ) -> list[Finding]:
            self.reviewed_pack_ids.append(pack.id)
            raise LLMProviderError("temporary reviewer outage")

    retry_config = current_config.model_copy(deep=True)
    retry_config.reviewers[0].verify = True
    failed_provider = FailingReviewProvider([])
    failed, failed_selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=retry_config,
        reviewer_ids=["security"],
        provider=failed_provider,
    )

    assert [pack.id for pack in failed_selected] == [test_pack.id]
    assert failed_provider.reviewed_pack_ids == [test_pack.id]
    assert failed.llm_coverage.reviewers[0].status == "fail"

    recovered_provider = FakeLLMProvider([], verification_approvals=[True])
    recovered, recovered_selected = continue_review_from_report(
        failed,
        repo_root=tmp_path,
        config=retry_config,
        reviewer_ids=["security"],
        provider=recovered_provider,
    )

    assert [pack.id for pack in recovered_selected] == [test_pack.id]
    assert recovered_provider.reviewed_pack_ids == [test_pack.id]
    assert recovered_provider.verified_batch_pack_ids == []
    assert recovered.findings == []
    assert recovered.llm_coverage.reviewers[0].status == "pass"
    assert evaluate_pre_push_gate(recovered, retry_config.gates.pre_push).blocked is False

    recovered_noop_provider = FakeLLMProvider([])
    recovered_noop, recovered_noop_selected = continue_review_from_report(
        recovered,
        repo_root=tmp_path,
        config=retry_config,
        reviewer_ids=["security"],
        provider=recovered_noop_provider,
    )

    assert recovered_noop_selected == []
    assert recovered_noop_provider.reviewed_pack_ids == []
    assert recovered_noop_provider.verified_batch_pack_ids == []
    assert recovered_noop.findings == recovered.findings
    assert recovered_noop.verifications == recovered.verifications

    narrowed_scope_config = original_config.model_copy(deep=True)
    narrowed_scope_config.reviewers[0].paths = ["src/**"]
    narrowed_scope, narrowed_scope_selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=narrowed_scope_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert [pack.id for pack in narrowed_scope_selected] == [source_pack.id]
    assert narrowed_scope.findings[0].reviewer_ids == ["finance"]
    assert narrowed_scope.findings[0].reviewer_context_pack_ids == {
        "finance": [source_pack.id],
    }

    expanded_scope_config = narrowed_scope_config.model_copy(deep=True)
    expanded_scope_config.reviewers[0].paths = ["tests/**", "src/**"]
    expanded_scope_provider = FakeLLMProvider([security_finding])
    expanded_scope, expanded_scope_selected = continue_review_from_report(
        narrowed_scope,
        repo_root=tmp_path,
        config=expanded_scope_config,
        reviewer_ids=["security"],
        provider=expanded_scope_provider,
    )

    assert [pack.id for pack in expanded_scope_selected] == [test_pack.id]
    assert expanded_scope_provider.reviewed_pack_ids == [test_pack.id]
    assert len(expanded_scope.findings) == 1
    assert expanded_scope.findings[0].reviewer_ids == ["finance", "security"]
    assert expanded_scope.findings[0].reviewer_context_pack_ids == {
        "finance": [source_pack.id],
        "security": [test_pack.id],
    }

    expanded_scope_noop_provider = FakeLLMProvider([])
    expanded_scope_noop, expanded_scope_noop_selected = continue_review_from_report(
        expanded_scope,
        repo_root=tmp_path,
        config=expanded_scope_config,
        reviewer_ids=["security"],
        provider=expanded_scope_noop_provider,
    )

    assert expanded_scope_noop_selected == []
    assert expanded_scope_noop_provider.reviewed_pack_ids == []
    assert expanded_scope_noop.findings == expanded_scope.findings


@pytest.mark.parametrize("retirement_mode", ["disabled", "removed"])
def test_continue_review_reenabled_reviewer_does_not_reuse_retired_success(
    tmp_path: Path,
    retirement_mode: str,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", required=True, verify=False),
            ReviewerConfig(id="finance", verify=False),
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.cache_enabled = False
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        failure_mode="A transfer can bypass the tenant ownership boundary.",
        evidence="No ownership predicate precedes the transfer dispatch.",
        suggested_fix="Add a tenant ownership check.",
        suggested_test="Reject cross-tenant transfers.",
        context_pack_id=pack.id,
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        reviewer_selections={"security": selection, "finance": selection},
    )
    disabled_config = original_config.model_copy(deep=True)
    if retirement_mode == "disabled":
        disabled_config.reviewers[0].enabled = False
    else:
        disabled_config.reviewers = [disabled_config.reviewers[1]]
    retired, retired_selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=disabled_config,
        provider=FakeLLMProvider([]),
    )
    assert retired_selected == []
    assert retired.findings == []

    provider = FakeLLMProvider([finding])
    reenabled, selected = continue_review_from_report(
        retired,
        repo_root=tmp_path,
        config=original_config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert provider.reviewed_pack_ids == [pack.id]
    assert reenabled.findings == [
        finding.model_copy(
            update={
                "reviewer_context_pack_ids": {"security": [pack.id]},
            }
        )
    ]


def test_active_verification_application_prefers_exact_sibling_decisions() -> None:
    approved = Finding(
        title="Authorization guard bypass permits an unauthorized settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        line=42,
        failure_mode="The settlement path bypasses the account ownership authorization guard.",
        evidence="The changed early return executes before the account ownership authorization guard.",
        suggested_fix="Move the early return after the account ownership authorization guard.",
        suggested_test="Reject a settlement for an account the caller does not own.",
        context_pack_id="src/auth.ts#authorize:1",
        reviewer_ids=["finance", "security"],
    )
    rejected = approved.model_copy(
        update={"title": "Authorization guard bypass permits settlement without ownership validation"}
    )
    verifications = [
        FindingVerification(
            finding=approved,
            reviewer_id="security",
            approved=True,
            confidence=FindingConfidence.HIGH,
            reason="Confirmed.",
            review_snapshot_id="snapshot-1",
        ),
        FindingVerification(
            finding=rejected,
            reviewer_id="security",
            approved=False,
            confidence=FindingConfidence.HIGH,
            reason="Rejected.",
            review_snapshot_id="snapshot-1",
        ),
    ]

    assert _apply_active_verifications_to_findings([approved], verifications) == [approved]
    assert _apply_active_verifications_to_findings([rejected], verifications) == [approved]


def test_explicit_general_finding_does_not_use_specialist_verification() -> None:
    general = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        line=42,
        failure_mode="A request can bypass authorization.",
        evidence="The changed branch returns before the guard.",
        suggested_fix="Run the guard first.",
        suggested_test="Reject an unauthorized request.",
        context_pack_id="src/auth.ts#authorize:1",
        reviewer_context_pack_ids={
            "general": ["src/auth.ts#authorize:1"],
        },
    )
    security = general.model_copy(
        update={
            "reviewer_ids": ["security"],
            "reviewer_context_pack_ids": {
                "security": ["src/auth.ts#authorize:1"],
            },
        }
    )
    verification = FindingVerification(
        finding=security,
        reviewer_id="security",
        approved=True,
        confidence=FindingConfidence.HIGH,
        reason="The security reviewer confirmed its own candidate.",
    )

    assert matching_active_verifications(general, [verification]) == []
    assert verified_report_findings([general], [verification]) == []


def test_cross_pack_fuzzy_siblings_with_one_origin_keep_distinct_decisions() -> None:
    approved = Finding(
        title="Authorization guard bypass permits an unauthorized settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/settlement.ts",
        line=42,
        failure_mode="The settlement path bypasses the account ownership authorization guard.",
        evidence="The changed early return executes before the account ownership authorization guard.",
        suggested_fix="Move the early return after the account ownership authorization guard.",
        suggested_test="Reject a settlement for an account the caller does not own.",
        context_pack_id="src/settlement.ts#authorize:1",
        reviewer_ids=["security"],
        reviewer_context_pack_ids={
            "security": ["tests/settlement.test.ts#authorize:1"],
        },
    )
    rejected = approved.model_copy(
        update={
            "title": "Authorization guard bypass permits settlement without ownership validation",
            "file": "src/audit.ts",
            "failure_mode": ("The audit settlement path can bypass the account ownership authorization guard."),
            "context_pack_id": "src/audit.ts#authorize:1",
        }
    )
    verifications = [
        FindingVerification(
            finding=approved,
            reviewer_id="security",
            approved=True,
            confidence=FindingConfidence.HIGH,
            reason="Confirmed.",
        ),
        FindingVerification(
            finding=rejected,
            reviewer_id="security",
            approved=False,
            confidence=FindingConfidence.HIGH,
            reason="Rejected as a distinct sibling.",
        ),
    ]

    assert active_verifications(verifications) == verifications
    assert verified_report_findings([approved], verifications) == [approved]


def test_exact_cross_pack_rediscovery_for_one_reviewer_consolidates_origins() -> None:
    first = Finding(
        title="Authorization guard bypass permits an unauthorized settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/settlement.ts",
        line=42,
        failure_mode="The settlement path bypasses the account ownership authorization guard.",
        evidence="The changed early return executes before the account ownership authorization guard.",
        suggested_fix="Move the early return after the account ownership authorization guard.",
        suggested_test="Reject a settlement for an account the caller does not own.",
        context_pack_id="src/settlement.ts#authorize:1",
        reviewer_ids=["security"],
        reviewer_context_pack_ids={
            "security": ["src/settlement.ts#authorize:1"],
        },
    )
    rediscovered = first.model_copy(
        update={
            "context_pack_id": "src/settlement.ts#dispatch:2",
            "reviewer_context_pack_ids": {
                "security": ["src/settlement.ts#dispatch:2"],
            },
        }
    )

    consolidated = _apply_active_verifications_to_findings(
        [first, rediscovered],
        [],
    )

    assert len(consolidated) == 1
    assert consolidated[0].reviewer_context_pack_ids == {
        "security": [
            "src/settlement.ts#authorize:1",
            "src/settlement.ts#dispatch:2",
        ],
    }


def test_exact_concise_cross_pack_rediscovery_consolidates_origins() -> None:
    first = Finding(
        title="Tenant bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        line=42,
        failure_mode="Guard skipped.",
        evidence="Early return.",
        suggested_fix="Run guard.",
        suggested_test="Reject tenant.",
        context_pack_id="src/auth.ts#authorize:1",
        reviewer_ids=["security"],
        reviewer_context_pack_ids={
            "security": ["src/auth.ts#authorize:1"],
        },
    )
    rediscovered = first.model_copy(
        update={
            "context_pack_id": "src/auth.ts#dispatch:2",
            "reviewer_context_pack_ids": {
                "security": ["src/auth.ts#dispatch:2"],
            },
        }
    )

    consolidated = _apply_active_verifications_to_findings(
        [first, rediscovered],
        [],
    )

    assert len(consolidated) == 1
    assert consolidated[0].reviewer_context_pack_ids == {
        "security": [
            "src/auth.ts#authorize:1",
            "src/auth.ts#dispatch:2",
        ],
    }


def test_verification_candidate_debt_unions_cross_pack_and_exact_decisions() -> None:
    canonical = Finding(
        title="Authorization guard bypass permits an unauthorized settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        line=42,
        failure_mode="The settlement path bypasses the account ownership authorization guard.",
        evidence="The changed early return executes before the account ownership authorization guard.",
        suggested_fix="Move the early return after the account ownership authorization guard.",
        suggested_test="Reject a settlement for an account the caller does not own.",
        context_pack_id="src/auth.ts#authorize:1",
        reviewer_ids=["finance", "security"],
    )
    raw_cross_pack = canonical.model_copy(
        update={
            "file": "tests/auth.test.ts",
            "context_pack_id": "tests/auth.test.ts#authorize:1",
        }
    )
    rejected_sibling = canonical.model_copy(
        update={
            "title": "Authorization guard bypass permits settlement without ownership validation",
        }
    )
    verifications = [
        FindingVerification(
            finding=raw_cross_pack,
            reviewer_id="security",
            approved=True,
            confidence=FindingConfidence.HIGH,
            reason="Confirmed.",
            review_snapshot_id="snapshot-a",
        ),
        FindingVerification(
            finding=rejected_sibling,
            reviewer_id="security",
            approved=False,
            confidence=FindingConfidence.HIGH,
            reason="Rejected.",
            review_snapshot_id="snapshot-a",
        ),
    ]

    assert unresolved_verification_candidate_pack_ids([canonical], verifications) == {
        ("finance", canonical.context_pack_id)
    }
    assert [
        verification.approved
        for verification in matching_active_verifications(
            canonical,
            verifications,
            reviewer_id="security",
        )
    ] == [True]
    assert verified_report_findings([canonical], verifications) == [canonical]


def test_continue_review_empty_reviewer_config_replaces_prior_specialists_with_general(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", verify=False),
            ReviewerConfig(id="finance", verify=False),
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id=reviewer_id,
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            )
            for reviewer_id in ("security", "finance")
        ],
        llm_selection=selection,
        reviewer_selections={
            "security": selection,
            "finance": selection,
        },
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers = []

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        provider=FakeLLMProvider([]),
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert set(continued.reviewer_selections) == {"general"}
    assert [reviewer.reviewer_id for reviewer in continued.llm_coverage.reviewers] == ["general"]
    assert continued.llm_coverage.reviewers[0].status == "pass"
    assert any(run.reviewer_id == "general" and run.status == "ok" for run in continued.llm_runs)


def test_continue_review_all_disabled_reviewers_excludes_prior_active_state(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    original_config = ReviewConfig(reviewers=[ReviewerConfig(id="security", verify=False)])
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers[0].enabled = False

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        provider=FakeLLMProvider([]),
    )

    assert selected == []
    assert continued.reviewer_selections == {}
    assert continued.llm_coverage.reviewers == []
    assert continued.llm_coverage.reviewed_context_pack_ids == []
    assert continued.llm_coverage.unreviewed_context_pack_ids == [pack.id]
    assert continued.llm_selection is not None
    assert continued.llm_selection.selected_context_pack_ids == []
    assert continued.llm_selection.skipped_context_pack_reasons == {pack.id: "not matched by any enabled reviewer"}


def test_continue_review_all_disabled_reviewers_retires_prior_general_finding(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    original_config = ReviewConfig()
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
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
        reviewer_context_pack_ids={"general": [pack.id]},
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="general",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"general": selection},
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers = [ReviewerConfig(id="security", enabled=False)]
    current_config.gates.pre_push.require_verified_findings = False

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        provider=FakeLLMProvider([]),
    )

    assert selected == []
    assert continued.findings == []
    assert continued.llm_coverage.reviewers == []
    assert evaluate_pre_push_gate(continued, current_config.gates.pre_push).blocked is False


def test_continue_review_preserves_general_alongside_and_after_specialist(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="general", verify=False),
            ReviewerConfig(id="security", verify=False),
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
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
        reviewer_ids=["general", "security"],
        reviewer_context_pack_ids={
            "general": [pack.id],
            "security": [pack.id],
        },
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id=reviewer_id,
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            )
            for reviewer_id in ("general", "security")
        ],
        reviewer_selections={
            "general": selection,
            "security": selection,
        },
    )

    unchanged, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=config,
        provider=FakeLLMProvider([]),
    )

    assert selected == []
    assert unchanged.findings == [finding]

    general_only_config = config.model_copy(deep=True)
    general_only_config.reviewers = [general_only_config.reviewers[0]]
    general_only, general_only_selected = continue_review_from_report(
        unchanged,
        repo_root=tmp_path,
        config=general_only_config,
        provider=FakeLLMProvider([]),
    )

    assert general_only_selected == []
    assert len(general_only.findings) == 1
    assert general_only.findings[0].reviewer_ids == []
    assert general_only.findings[0].reviewer_context_pack_ids == {
        "general": [pack.id],
    }
    assert (
        evaluate_pre_push_gate(
            general_only,
            general_only_config.gates.pre_push,
        ).blocked
        is True
    )


def test_continue_review_includes_reviewer_packs_skipped_by_prior_budget(
    tmp_path: Path,
) -> None:
    reviewed = ContextPack(id="src/a.ts#run:1", file="src/a.ts")
    capped = ContextPack(
        id="src/b.ts#settle:1",
        file="src/b.ts",
        risk_signals=[
            RiskSignal(
                kind="financial",
                severity=RiskSeverity.CRITICAL,
                reason="Settlement changed.",
                file="src/b.ts",
            )
        ],
    )
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", focus="Security boundaries.")])
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = False
    selection = LLMContextSelection(
        total_context_pack_ids=[reviewed.id, capped.id],
        selected_context_pack_ids=[reviewed.id],
        deep_selected_context_pack_ids=[reviewed.id],
        unselected_context_pack_ids=[capped.id],
        skipped_context_pack_reasons={capped.id: "not selected by LLM pack cap"},
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=2)),
        context_packs=[reviewed, capped],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=reviewed.id,
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert [pack.id for pack in selected] == [capped.id]
    assert [
        run.context_pack_id for run in continued.llm_runs if run.reviewer_id == "security" and run.status == "ok"
    ] == [reviewed.id, capped.id]


def test_continue_review_shares_route_circuit_across_reviewers(tmp_path: Path) -> None:
    class TerminalProvider:
        calls = 0

        def review_context_pack(self, _pack: ContextPack, _repo_root: Path) -> list[Finding]:
            self.calls += 1
            raise LLMProviderError("Invalid API key.", category="auth")

    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", focus="Authorization."),
            ReviewerConfig(id="correctness", focus="Behavior."),
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = False
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[pack],
    )
    provider = TerminalProvider()

    continued, _selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        provider=provider,  # type: ignore[arg-type]
    )

    assert provider.calls == 1
    assert [run.status for run in continued.llm_runs] == [
        "failed_auth",
        "skipped_circuit_open",
    ]


def test_continue_review_from_report_does_not_enable_llm_implicitly(tmp_path: Path) -> None:
    config = ReviewConfig()
    reviewed = ContextPack(id="src/auth.ts#login:1", file="src/auth.ts", file_kind=FileKind.SOURCE)
    residual = ContextPack(
        id="src/payments.ts#capture:1",
        file="src/payments.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(kind="persistence", severity=RiskSeverity.HIGH, reason="State changed.", file="src/payments.ts")
        ],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=2)),
        context_packs=[reviewed, residual],
        llm_runs=[
            LLMRun(
                provider="fake",
                context_pack_id=reviewed.id,
                status="ok",
                duration_ms=1,
            )
        ],
    )
    finding = Finding(
        title="Capture skips ledger lock",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/payments.ts",
        line=10,
        failure_mode="Concurrent capture can double-spend.",
        evidence="The context pack changed capture.",
        suggested_fix="Lock the ledger row before updating.",
        suggested_test="Add a concurrent capture test.",
    )

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        residual_priorities={"p0"},
        provider=FakeLLMProvider([finding]),
    )

    assert [pack.id for pack in selected] == [residual.id]
    assert continued.config.llm.enabled is False
    assert continued.llm_runs == initial.llm_runs
    assert continued.findings == []
    assert "LLM review is disabled" in "\n".join(continued.diff.warnings)


def test_run_review_pipeline_skips_over_budget_llm_packs(tmp_path: Path) -> None:
    diff_text = """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1,3 +1,3 @@
 {
-  "name": "old"
+  "name": "new"
 }
"""
    config = ReviewConfig()
    config.context.max_pack_chars = 1
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE

    report = run_review_pipeline(tmp_path, diff_text, TargetMode.PATCH, config)

    assert report.context_packs
    assert report.llm_runs == []
    assert any("over-budget" in warning for warning in report.diff.warnings)


def test_run_review_pipeline_bounds_project_discovery_with_analyzer_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_timeout: float | None = None

    def fake_discover_project_with_files(
        cwd: Path,
        ignored_patterns: list[str] | None = None,
        config_path: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[ProjectProfile, list[Path]]:
        del ignored_patterns, config_path
        nonlocal seen_timeout
        seen_timeout = timeout_seconds
        return ProjectProfile(root=str(cwd), is_git_repo=False), []

    monkeypatch.setattr(
        "apex_ray.pipeline.runner.discover_project_with_files",
        fake_discover_project_with_files,
    )
    config = ReviewConfig()
    config.analyzer.timeout_seconds = 17

    run_review_pipeline(tmp_path, "", TargetMode.PATCH, config)

    assert seen_timeout == 17


def test_run_review_pipeline_runs_scoped_reviewers_and_merges_provenance(tmp_path: Path) -> None:
    diff_text = """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1 +1 @@
-{"name":"old"}
+{"name":"new","scripts":{"postinstall":"node setup.js"}}
"""
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", focus="Supply-chain and command execution risks."),
            ReviewerConfig(id="finance", focus="Financial and settlement correctness."),
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = False
    candidate = Finding(
        title="Untrusted install command can execute during dependency installation",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="package.json",
        failure_mode=(
            "Dependency installation can execute an untrusted setup command before operators "
            "can validate the package contents."
        ),
        evidence=(
            "The changed package manifest adds a postinstall command that invokes a local setup "
            "script during every installation."
        ),
        suggested_fix="Remove the lifecycle command or pin and validate the invoked artifact.",
        suggested_test="Install with lifecycle scripts disabled and assert the build remains valid.",
    )
    provider = FakeLLMProvider([candidate])

    report = run_review_pipeline(tmp_path, diff_text, TargetMode.PATCH, config, provider=provider)

    assert len(provider.reviewed_pack_ids) == 2
    assert len(report.findings) == 1
    assert report.findings[0].reviewer_ids == ["finance", "security"]
    assert report.findings[0].reviewer_context_pack_ids == {
        "finance": [report.findings[0].context_pack_id],
        "security": [report.findings[0].context_pack_id],
    }
    assert {run.reviewer_id for run in report.llm_runs} == {"finance", "security"}
    assert set(report.reviewer_selections) == {"finance", "security"}
    assert report.llm_selection is not None
    assert (
        report.llm_selection.selected_context_pack_ids
        == report.reviewer_selections["security"].selected_context_pack_ids
    )
    assert set(report.stage_durations_ms) == {
        "diff",
        "discovery",
        "analyzers",
        "context",
        "llm",
        "report",
        "total",
    }
    assert report.stage_durations_ms["total"] >= report.stage_durations_ms["llm"]


def test_new_finding_provenance_allows_scope_narrowing_to_retire_gate_debt(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def kept():\n    return True\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "b.py").write_text(
        "def removed():\n    return False\n",
        encoding="utf-8",
    )
    diff_text = """diff --git a/src/a.py b/src/a.py
new file mode 100644
--- /dev/null
+++ b/src/a.py
@@ -0,0 +1,2 @@
+def kept():
+    return True
diff --git a/src/b.py b/src/b.py
new file mode 100644
--- /dev/null
+++ b/src/b.py
@@ -0,0 +1,2 @@
+def removed():
+    return False
"""
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                paths=["src/**"],
                required=True,
                verify=False,
            )
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.cache_enabled = False
    config.gates.pre_push.require_verified_findings = False
    candidate = Finding(
        title="Removed authorization guard",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/b.py",
        line=2,
        failure_mode="Requests can reach the removed path without authorization.",
        evidence="The changed return bypasses the authorization decision.",
        suggested_fix="Restore the authorization guard before returning.",
        suggested_test="Reject an unauthorized request.",
    )

    initial = run_review_pipeline(
        tmp_path,
        diff_text,
        TargetMode.PATCH,
        config,
        provider=FakeLLMProvider([candidate]),
    )

    assert len(initial.findings) == 1
    finding = initial.findings[0]
    assert finding.reviewer_context_pack_ids == {
        "security": [finding.context_pack_id],
    }
    assert evaluate_pre_push_gate(initial, config.gates.pre_push).blocked is True

    narrowed_config = config.model_copy(deep=True)
    narrowed_config.reviewers[0].paths = ["src/a.py"]
    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=narrowed_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert selected == []
    assert continued.findings == []
    assert evaluate_pre_push_gate(continued, narrowed_config.gates.pre_push).blocked is False


def test_explicit_reviewer_scope_excludes_out_of_scope_packs_from_global_gate(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "def authorize(token):\n    return bool(token)\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "ui.py").write_text(
        "def button_label():\n    return 'Continue'\n",
        encoding="utf-8",
    )
    diff_text = """diff --git a/src/auth.py b/src/auth.py
--- /dev/null
+++ b/src/auth.py
@@ -0,0 +1,2 @@
+def authorize(token):
+    return bool(token)
diff --git a/src/ui.py b/src/ui.py
--- /dev/null
+++ b/src/ui.py
@@ -0,0 +1,2 @@
+def button_label():
+    return "Continue"
"""
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization boundaries.",
                paths=["src/ui.py"],
                verify=False,
                required=True,
            ),
            ReviewerConfig(
                id="ux",
                focus="User-facing behavior.",
                paths=["src/ui.py"],
                verify=False,
            ),
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = False
    config.llm.cache_enabled = False

    scoped = run_review_pipeline(
        tmp_path,
        diff_text,
        TargetMode.PATCH,
        config,
        provider=FakeLLMProvider([]),
        reviewer_ids=["ux"],
    )
    full = run_review_pipeline(
        tmp_path,
        diff_text,
        TargetMode.PATCH,
        config,
        provider=FakeLLMProvider([]),
    )

    assert len(scoped.context_packs) == 2
    assert scoped.llm_selection is not None
    assert scoped.llm_selection.total_context_pack_ids == scoped.reviewer_selections["ux"].total_context_pack_ids
    assert scoped.llm_coverage.total_context_packs == 1
    assert scoped.llm_coverage.quality_gate_status == "pass"
    assert scoped.reviewer_scope_ids == ["ux"]
    assert full.llm_coverage.total_context_packs == 2
    assert full.llm_coverage.quality_gate_status == "fail"
    assert full.reviewer_scope_ids is None
    assert len(full.llm_coverage.residual_risk_p0_context_pack_ids) == 1

    continued, selected = continue_review_from_report(
        scoped,
        repo_root=tmp_path,
        config=config,
        reviewer_id="ux",
        provider=FakeLLMProvider([]),
    )

    assert selected == []
    assert continued.llm_selection is not None
    assert continued.llm_selection.total_context_pack_ids == scoped.llm_selection.total_context_pack_ids
    assert continued.llm_coverage.total_context_packs == 1
    assert continued.llm_coverage.quality_gate_status == "pass"
    assert continued.reviewer_scope_ids == ["ux"]


@pytest.mark.parametrize(
    ("reviewer_ids", "message"),
    [
        pytest.param(["missing"], "Unknown or disabled reviewer: missing", id="unknown"),
        pytest.param(["disabled"], "Unknown or disabled reviewer: disabled", id="disabled"),
        pytest.param([], "At least one reviewer must be selected", id="empty"),
    ],
)
def test_disabled_llm_still_validates_explicit_reviewer_scope(
    tmp_path: Path,
    reviewer_ids: list[str],
    message: str,
) -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="ux"),
            ReviewerConfig(id="disabled", enabled=False),
        ]
    )

    with pytest.raises(ReviewerConfigError, match=message):
        run_review_pipeline(
            tmp_path,
            "",
            TargetMode.PATCH,
            config,
            reviewer_ids=reviewer_ids,
        )


def test_disabled_llm_persists_resolved_explicit_reviewer_scope(tmp_path: Path) -> None:
    config = ReviewConfig(reviewers=[ReviewerConfig(id="ux")])

    report = run_review_pipeline(
        tmp_path,
        "",
        TargetMode.PATCH,
        config,
        reviewer_ids=["ux", "ux"],
    )

    assert report.reviewer_scope_ids == ["ux"]


def test_continue_review_unions_persisted_and_new_explicit_reviewer_scopes(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", required=True, verify=False),
            ReviewerConfig(id="ux", verify=False),
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = False
    config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="ux",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"ux": selection},
        reviewer_scope_ids=["ux"],
    )

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert continued.reviewer_scope_ids == ["ux", "security"]
    assert {reviewer.reviewer_id: reviewer.status for reviewer in continued.llm_coverage.reviewers} == {
        "security": "pass",
        "ux": "pass",
    }
    assert [run.reviewer_id for run in continued.llm_runs if run.context_pack_id == pack.id and run.status == "ok"] == [
        "ux",
        "security",
    ]


def test_continue_review_preserves_persisted_scope_without_selection_data(
    tmp_path: Path,
) -> None:
    ux_pack = ContextPack(
        id="src/ui.ts#render:1",
        file="src/ui.ts",
        file_kind=FileKind.SOURCE,
    )
    security_pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="ux",
                paths=["src/ui.ts"],
                required=True,
                verify=False,
            ),
            ReviewerConfig(
                id="security",
                paths=["src/auth.ts"],
                required=True,
                verify=False,
            ),
        ]
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ux_pack, security_pack],
        reviewer_scope_ids=["ux"],
    )
    enabled_config = config.model_copy(deep=True)
    enabled_config.llm.enabled = True
    enabled_config.llm.provider = LLMProviderName.FAKE
    enabled_config.llm.verify = False
    enabled_config.llm.cache_enabled = False

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=enabled_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert [pack.id for pack in selected] == [security_pack.id]
    assert continued.reviewer_scope_ids == ["ux", "security"]
    assert continued.llm_selection is not None
    assert continued.llm_selection.total_context_pack_ids == [
        ux_pack.id,
        security_pack.id,
    ]
    assert continued.reviewer_selections["ux"].total_context_pack_ids == [ux_pack.id]
    assert continued.reviewer_selections["ux"].selected_context_pack_ids == []
    coverage = {reviewer.reviewer_id: reviewer for reviewer in continued.llm_coverage.reviewers}
    assert coverage["security"].status == "pass"
    assert coverage["ux"].status == "fail"
    assert coverage["ux"].matching_context_pack_ids == [ux_pack.id]
    assert coverage["ux"].reviewed_context_pack_ids == []
    assert continued.llm_coverage.quality_gate_status == "fail"


@pytest.mark.parametrize(
    ("persisted_scope", "expected_scope"),
    [
        (["security"], ["security", "other"]),
        (None, None),
    ],
)
def test_continue_review_rebases_persisted_scope_to_changed_reviewer_config(
    tmp_path: Path,
    persisted_scope: list[str] | None,
    expected_scope: list[str] | None,
) -> None:
    old_security_pack = ContextPack(
        id="src/old.ts#authorize:1",
        file="src/old.ts",
        file_kind=FileKind.SOURCE,
    )
    new_security_pack = ContextPack(
        id="src/new.ts#authorize:1",
        file="src/new.ts",
        file_kind=FileKind.SOURCE,
    )
    other_pack = ContextPack(
        id="src/other.ts#render:1",
        file="src/other.ts",
        file_kind=FileKind.SOURCE,
    )
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                paths=["src/old.ts"],
                required=True,
                verify=False,
            ),
            ReviewerConfig(id="other", paths=["src/other.ts"], verify=False),
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.verify = False
    original_config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[old_security_pack.id],
        selected_context_pack_ids=[old_security_pack.id],
        deep_selected_context_pack_ids=[old_security_pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[old_security_pack, new_security_pack, other_pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=old_security_pack.id,
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
        reviewer_scope_ids=persisted_scope,
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers[0].paths = ["src/new.ts"]

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["other"],
        provider=FakeLLMProvider([]),
    )

    assert [pack.id for pack in selected] == [other_pack.id]
    assert continued.reviewer_scope_ids == expected_scope
    security_selection = continued.reviewer_selections["security"]
    assert security_selection.total_context_pack_ids == [new_security_pack.id]
    assert security_selection.selected_context_pack_ids == []
    coverage = {reviewer.reviewer_id: reviewer for reviewer in continued.llm_coverage.reviewers}
    assert coverage["security"].matching_context_pack_ids == [new_security_pack.id]
    assert coverage["security"].reviewed_context_pack_ids == []
    assert coverage["security"].status == "fail"
    assert continued.llm_coverage.quality_gate_status == "fail"


def test_continue_review_rechecks_pack_when_reviewer_behavior_changes(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization boundaries.",
                required=True,
                verify=False,
            )
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.verify = False
    original_config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
        reviewer_scope_ids=["security"],
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers[0].focus = "Financial authorization and settlement risk."
    provider = FakeLLMProvider([])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert provider.reviewed_pack_ids == [pack.id]
    assert [(run.kind, run.status) for run in continued.llm_runs] == [
        ("review", "ok"),
        ("review_reset", "reviewer_config_changed"),
        ("review", "ok"),
    ]
    reviewer = continued.llm_coverage.reviewers[0]
    assert reviewer.reviewed_context_pack_ids == [pack.id]
    assert reviewer.status == "pass"
    assert continued.llm_coverage.review_runs == 2
    assert continued.llm_coverage.run_status_counts == {"ok": 2}
    assert {route.provider for route in continued.llm_coverage.routes} == {"fake"}

    repeated_provider = FakeLLMProvider([])
    repeated, repeated_selected = continue_review_from_report(
        continued,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=repeated_provider,
    )

    assert repeated_selected == []
    assert repeated_provider.reviewed_pack_ids == []
    assert repeated.llm_runs == continued.llm_runs
    assert repeated.llm_coverage.reviewers[0].status == "pass"


def test_continue_review_replaces_verified_snapshot_when_reviewer_focus_changes(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization boundaries.",
                required=True,
                verify=True,
                verify_profile="verify-a",
            )
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.verify = True
    original_config.llm.cache_enabled = False
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
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The original verifier approved the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
        reviewer_scope_ids=["security"],
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers[0].focus = "Financial authorization and settlement risk."
    current_config.reviewers[0].verify_profile = "verify-b"
    provider = FakeLLMProvider([])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert provider.reviewed_pack_ids == [pack.id]
    assert provider.verified_batch_pack_ids == []
    assert continued.findings == []
    assert len(continued.verifications) == 1
    assert continued.verifications[0].superseded is True
    assert continued.llm_coverage.reviewers[0].status == "pass"
    gate_config = current_config.gates.pre_push.model_copy(update={"fail_on_quality_gate": False})
    assert evaluate_pre_push_gate(continued, gate_config).blocked is False


def test_continue_review_replaces_prior_pack_snapshot_when_finding_severity_changes(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                required=True,
                verify=True,
            )
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = True
    config.llm.cache_enabled = False
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
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The original verifier approved the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    escalated = finding.model_copy(update={"severity": FindingSeverity.CRITICAL})
    provider = FakeLLMProvider([escalated], verification_approvals=[False])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        only_unreviewed=False,
        provider=provider,
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert continued.findings == []
    assert [
        (decision.finding.severity, decision.approved, decision.superseded) for decision in continued.verifications
    ] == [
        (FindingSeverity.HIGH, True, True),
        (FindingSeverity.CRITICAL, False, False),
    ]
    assert continued.llm_coverage.reviewers[0].status == "pass"


def test_continue_review_invalidates_changed_unrequested_reviewer_coverage(
    tmp_path: Path,
) -> None:
    security_pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    other_pack = ContextPack(
        id="src/ui.ts#render:1",
        file="src/ui.ts",
        file_kind=FileKind.SOURCE,
    )
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization boundaries.",
                paths=["src/auth.ts"],
                required=True,
                verify=False,
            ),
            ReviewerConfig(
                id="other",
                paths=["src/ui.ts"],
                verify=False,
            ),
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.verify = False
    original_config.llm.cache_enabled = False
    security_selection = LLMContextSelection(
        total_context_pack_ids=[security_pack.id],
        selected_context_pack_ids=[security_pack.id],
        deep_selected_context_pack_ids=[security_pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[security_pack, other_pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=security_pack.id,
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=security_selection,
        reviewer_selections={"security": security_selection},
        reviewer_scope_ids=None,
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers[0].instructions = ["Treat authorization bypasses that can move funds as release blockers."]

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["other"],
        provider=FakeLLMProvider([]),
    )

    assert [candidate.id for candidate in selected] == [other_pack.id]
    security = {reviewer.reviewer_id: reviewer for reviewer in continued.llm_coverage.reviewers}["security"]
    assert security.matching_context_pack_ids == [security_pack.id]
    assert security.selected_context_pack_ids == [security_pack.id]
    assert security.reviewed_context_pack_ids == []
    assert security.status == "fail"
    assert continued.llm_coverage.quality_gate_status == "fail"
    assert any(
        run.kind == "review_reset" and run.reviewer_id == "security" and run.context_pack_id == security_pack.id
        for run in continued.llm_runs
    )


def test_continue_review_full_scope_includes_new_unrequested_required_reviewer(
    tmp_path: Path,
) -> None:
    other_pack = ContextPack(
        id="src/ui.ts#render:1",
        file="src/ui.ts",
        file_kind=FileKind.SOURCE,
    )
    security_pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="other",
                paths=["src/ui.ts"],
                verify=False,
            )
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.verify = False
    original_config.llm.cache_enabled = False
    other_selection = LLMContextSelection(
        total_context_pack_ids=[other_pack.id],
        selected_context_pack_ids=[other_pack.id],
        deep_selected_context_pack_ids=[other_pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[other_pack, security_pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="other",
                context_pack_id=other_pack.id,
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=other_selection,
        reviewer_selections={"other": other_selection},
        reviewer_scope_ids=None,
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers.append(
        ReviewerConfig(
            id="security",
            paths=["src/auth.ts"],
            required=True,
            verify=False,
        )
    )

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["other"],
        provider=FakeLLMProvider([]),
    )

    assert selected == []
    assert continued.reviewer_scope_ids is None
    assert continued.llm_selection is not None
    assert continued.llm_selection.total_context_pack_ids == [
        other_pack.id,
        security_pack.id,
    ]
    assert continued.reviewer_selections["security"].total_context_pack_ids == [security_pack.id]
    assert continued.reviewer_selections["security"].selected_context_pack_ids == []
    security = {reviewer.reviewer_id: reviewer for reviewer in continued.llm_coverage.reviewers}["security"]
    assert security.status == "fail"
    assert security.matching_context_pack_ids == [security_pack.id]
    assert continued.llm_coverage.quality_gate_status == "fail"


def test_continue_review_restores_selection_from_effective_run_when_matcher_returns(
    tmp_path: Path,
) -> None:
    first = ContextPack(
        id="src/a.ts#file:1",
        file="src/a.ts",
        file_kind=FileKind.SOURCE,
    )
    second = ContextPack(
        id="src/b.ts#file:1",
        file="src/b.ts",
        file_kind=FileKind.SOURCE,
    )
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                paths=["src/a.ts"],
                required=True,
                verify=False,
            )
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.verify = False
    original_config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[first.id],
        selected_context_pack_ids=[first.id],
        deep_selected_context_pack_ids=[first.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[first, second],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=first.id,
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    second_config = original_config.model_copy(deep=True)
    second_config.reviewers[0].paths = ["src/b.ts"]
    moved, moved_selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=second_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )
    assert [pack.id for pack in moved_selected] == [second.id]

    returned, returned_selected = continue_review_from_report(
        moved,
        repo_root=tmp_path,
        config=original_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert returned_selected == []
    restored = returned.reviewer_selections["security"]
    assert restored.total_context_pack_ids == [first.id]
    assert restored.selected_context_pack_ids == [first.id]
    assert restored.deep_selected_context_pack_ids == [first.id]
    assert returned.llm_coverage.reviewers[0].status == "pass"
    assert returned.llm_coverage.quality_gate_status != "fail"


@pytest.mark.parametrize("policy_source", ["reviewer", "global"])
def test_continue_review_verifies_carried_findings_when_verification_is_enabled(
    tmp_path: Path,
    policy_source: str,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    reviewer = ReviewerConfig(
        id="security",
        required=True,
        verify=False if policy_source == "reviewer" else None,
    )
    original_config = ReviewConfig(reviewers=[reviewer])
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.verify = False
    original_config.llm.cache_enabled = False
    finding = Finding(
        title="Authorization bypass permits an unscoped transfer",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        failure_mode="The transfer path accepts an account outside the authorized tenant.",
        evidence="The authorization predicate is absent before the transfer call.",
        suggested_fix="Require tenant-scoped authorization before dispatch.",
        suggested_test="Assert a cross-tenant transfer is rejected.",
        context_pack_id=pack.id,
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    current_config = original_config.model_copy(deep=True)
    if policy_source == "reviewer":
        current_config.reviewers[0].verify = True
    else:
        current_config.llm.verify = True
    provider = FakeLLMProvider([], verification_approvals=[True])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert selected == []
    assert provider.reviewed_pack_ids == []
    assert provider.verified_batch_pack_ids == [pack.id]
    assert len(continued.verifications) == 1
    assert continued.verifications[0].approved is True
    assert [(run.kind, run.status) for run in continued.llm_runs] == [
        ("review", "ok"),
        ("verify_reset", "reviewer_verification_changed"),
        ("verify", "ok"),
    ]
    assert continued.llm_coverage.reviewers[0].status == "pass"
    assert continued.llm_coverage.quality_gate_status != "fail"


def test_continue_review_unrequested_verification_policy_change_fails_closed(
    tmp_path: Path,
) -> None:
    security_pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    other_pack = ContextPack(
        id="src/ui.ts#render:1",
        file="src/ui.ts",
        file_kind=FileKind.SOURCE,
    )
    original_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                paths=["src/auth.ts"],
                required=True,
                verify=False,
            ),
            ReviewerConfig(
                id="other",
                paths=["src/ui.ts"],
                verify=False,
            ),
        ]
    )
    original_config.llm.enabled = True
    original_config.llm.provider = LLMProviderName.FAKE
    original_config.llm.verify = False
    original_config.llm.cache_enabled = False
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=security_pack.file,
        failure_mode="A transfer can bypass the tenant boundary.",
        evidence="No authorization predicate precedes the transfer.",
        suggested_fix="Add a tenant authorization check.",
        suggested_test="Reject cross-tenant transfers.",
        context_pack_id=security_pack.id,
        reviewer_ids=["security"],
    )
    security_selection = LLMContextSelection(
        total_context_pack_ids=[security_pack.id],
        selected_context_pack_ids=[security_pack.id],
        deep_selected_context_pack_ids=[security_pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[security_pack, other_pack],
        findings=[finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=security_pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            )
        ],
        reviewer_selections={"security": security_selection},
        reviewer_scope_ids=None,
    )
    current_config = original_config.model_copy(deep=True)
    current_config.reviewers[0].verify = True
    provider = FakeLLMProvider([])

    continued, _selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["other"],
        provider=provider,
    )

    assert provider.verified_batch_pack_ids == []
    security = {reviewer.reviewer_id: reviewer for reviewer in continued.llm_coverage.reviewers}["security"]
    assert security.status == "fail"
    assert any("unresolved verification subjects" in reason for reason in security.reasons)
    assert continued.llm_coverage.quality_gate_status == "fail"
    assert [(todo.context_pack_id, todo.reviewer_id) for todo in continued.llm_coverage.coverage_todos] == [
        (security_pack.id, "security")
    ]
    assert "unresolved verification subjects" in continued.llm_coverage.coverage_todos[0].reason
    assert "--reviewer security" in continued.llm_coverage.coverage_todos[0].suggested_command

    retry_provider = FakeLLMProvider([], verification_approvals=[True])
    retried, retry_selected = continue_review_from_report(
        continued,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=retry_provider,
    )

    assert retry_selected == []
    assert retry_provider.reviewed_pack_ids == []
    assert retry_provider.verified_batch_pack_ids == [security_pack.id]
    assert retried.llm_coverage.reviewers[0].status == "pass"
    assert retried.llm_coverage.quality_gate_status != "fail"


def test_continue_review_reverifies_carried_findings_when_verify_profile_changes(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                required=True,
                verify=True,
                verify_profile="verify-a",
            )
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = True
    config.llm.cache_enabled = False
    config.llm.profiles = {
        "verify-a": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-a"),
        "verify-b": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-b"),
    }
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
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The original verifier approved the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    current_config = config.model_copy(deep=True)
    current_config.reviewers[0].verify_profile = "verify-b"
    provider = FakeLLMProvider([], verification_approvals=[False])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert selected == []
    assert provider.reviewed_pack_ids == []
    assert provider.verified_batch_pack_ids == [pack.id]
    assert [(run.kind, run.profile) for run in continued.llm_runs] == [
        ("review", None),
        ("verify", None),
        ("verify_reset", None),
        ("verify", "verify-b"),
    ]
    assert continued.llm_coverage.reviewers[0].status == "pass"
    assert [(verification.approved, verification.superseded) for verification in continued.verifications] == [
        (True, True),
        (False, False),
    ]
    assert continued.findings == []
    gate_config = current_config.gates.pre_push.model_copy(update={"fail_on_quality_gate": False})
    assert evaluate_pre_push_gate(continued, gate_config).blocked is False

    repeated_provider = FakeLLMProvider([])
    repeated, repeated_selected = continue_review_from_report(
        continued,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=repeated_provider,
    )

    assert repeated_selected == []
    assert repeated_provider.reviewed_pack_ids == []
    assert repeated_provider.verified_batch_pack_ids == []
    assert repeated.findings == []
    assert repeated.verifications == continued.verifications


@pytest.mark.parametrize("mutation", ["root_model", "unused_profile"])
def test_continue_review_skips_reverification_when_effective_verifier_route_is_unchanged(
    tmp_path: Path,
    mutation: str,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                required=True,
                verify=True,
                profile="review",
                verify_profile="verify",
            )
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.model = "root-a"
    config.llm.verify = True
    config.llm.cache_enabled = False
    config.llm.profiles = {
        "review": LLMProfile(provider=LLMProviderName.FAKE, model="reviewer-fixed"),
        "verify": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-fixed"),
        "unused": LLMProfile(provider=LLMProviderName.FAKE, model="unused-a"),
    }
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
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The verifier approved the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    current_config = config.model_copy(deep=True)
    if mutation == "root_model":
        current_config.llm.model = "root-b"
    else:
        current_config.llm.profiles["unused"].model = "unused-b"
    provider = FakeLLMProvider([])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert selected == []
    assert provider.reviewed_pack_ids == []
    assert provider.verified_batch_pack_ids == []
    assert continued.verifications == initial.verifications
    assert [(run.kind, run.status) for run in continued.llm_runs] == [
        ("review", "ok"),
        ("verify", "ok"),
    ]
    assert continued.llm_coverage.reviewers[0].status == "pass"


def test_continue_review_reruns_when_effective_root_review_route_changes(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", required=True, verify=False)])
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.model = "root-a"
    config.llm.verify = False
    config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                model="root-a",
                reviewer_id="security",
                context_pack_id=pack.id,
                route_reason="default",
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    current_config = config.model_copy(deep=True)
    current_config.llm.model = "root-b"
    provider = FakeLLMProvider([])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert provider.reviewed_pack_ids == [pack.id]
    assert any(run.kind == "review_reset" for run in continued.llm_runs)


def test_continue_review_reruns_when_successful_fallback_profile_changes(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", required=True, verify=False)])
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.model = "primary"
    config.llm.verify = False
    config.llm.cache_enabled = False
    config.llm.profiles = {
        "fallback": LLMProfile(provider=LLMProviderName.FAKE, model="fallback-a"),
    }
    config.llm.routing.escalated_review_profile = "fallback"
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                model="primary",
                reviewer_id="security",
                context_pack_id=pack.id,
                route_reason="default",
                status="failed_quota",
                duration_ms=1,
            ),
            LLMRun(
                provider="fake",
                model="fallback-a",
                profile="fallback",
                reviewer_id="security",
                context_pack_id=pack.id,
                route_reason="fallback:fallback:after_failed_quota",
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    current_config = config.model_copy(deep=True)
    current_config.llm.profiles["fallback"].model = "fallback-b"
    provider = FakeLLMProvider([])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert provider.reviewed_pack_ids == [pack.id]
    assert any(run.kind == "review_reset" for run in continued.llm_runs)


def test_continue_review_can_recover_a_previously_rejected_carried_finding(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                required=True,
                verify=True,
                verify_profile="verify-a",
            )
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = True
    config.llm.cache_enabled = False
    config.llm.profiles = {
        "verify-a": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-a"),
        "verify-b": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-b"),
    }
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
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=False,
                confidence=FindingConfidence.HIGH,
                reason="The original verifier rejected the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    current_config = config.model_copy(deep=True)
    current_config.reviewers[0].verify_profile = "verify-b"
    provider = FakeLLMProvider([], verification_approvals=[True])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert selected == []
    assert provider.verified_finding_titles == [finding.title]
    assert [(decision.approved, decision.superseded) for decision in continued.verifications] == [
        (False, True),
        (True, False),
    ]
    assert len(continued.findings) == 1
    assert continued.findings[0].reviewer_ids == ["security"]
    assert continued.llm_coverage.reviewers[0].status == "pass"


def test_continue_review_rejection_removes_only_the_refreshing_reviewer_provenance(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/transfer.ts#submit:1",
        file="src/transfer.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="finance", required=True, verify=True, verify_profile="verify-a"),
            ReviewerConfig(id="security", required=True, verify=True, verify_profile="verify-a"),
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = True
    config.llm.cache_enabled = False
    config.llm.profiles = {
        "verify-a": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-a"),
        "verify-b": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-b"),
    }
    finding = Finding(
        title="Transfer can exceed its approved limit",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        failure_mode="The submitted amount is not checked against the approved limit.",
        evidence="The transfer is dispatched without a limit predicate.",
        suggested_fix="Enforce the approved limit before dispatch.",
        suggested_test="Reject a transfer above its approved limit.",
        context_pack_id=pack.id,
        reviewer_ids=["finance", "security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding.model_copy(update={"reviewer_ids": [reviewer_id]}),
                reviewer_id=reviewer_id,
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason=f"The {reviewer_id} verifier approved the finding.",
            )
            for reviewer_id in ("finance", "security")
        ],
        llm_runs=[
            *[
                LLMRun(
                    provider="fake",
                    reviewer_id=reviewer_id,
                    context_pack_id=pack.id,
                    status="ok",
                    duration_ms=1,
                    findings_count=1,
                )
                for reviewer_id in ("finance", "security")
            ],
            *[
                LLMRun(
                    kind="verify",
                    provider="fake",
                    reviewer_id=reviewer_id,
                    context_pack_id=pack.id,
                    status="ok",
                    duration_ms=1,
                )
                for reviewer_id in ("finance", "security")
            ],
        ],
        llm_selection=selection,
        reviewer_selections={"finance": selection, "security": selection},
    )
    current_config = config.model_copy(deep=True)
    current_config.reviewers[1].verify_profile = "verify-b"

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([], verification_approvals=[False]),
    )

    assert selected == []
    assert len(continued.findings) == 1
    assert continued.findings[0].reviewer_ids == ["finance"]
    assert [(decision.reviewer_id, decision.approved, decision.superseded) for decision in continued.verifications] == [
        ("finance", True, False),
        ("security", True, True),
        ("security", False, False),
    ]


def test_continue_review_verifier_failure_preserves_existing_finding_and_debt(
    tmp_path: Path,
) -> None:
    class FailingVerificationProvider(FakeLLMProvider):
        def verify_findings(
            self,
            findings: list[Finding],
            pack: ContextPack,
            repo_root: Path,
        ) -> list[FindingVerification]:
            raise LLMProviderError("temporary verifier outage")

    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                required=True,
                verify=True,
                verify_profile="verify-a",
            )
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = True
    config.llm.cache_enabled = False
    config.llm.profiles = {
        "verify-a": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-a"),
        "verify-b": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-b"),
    }
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
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The original verifier approved the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    current_config = config.model_copy(deep=True)
    current_config.reviewers[0].verify_profile = "verify-b"

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=FailingVerificationProvider([]),
    )

    assert selected == []
    assert continued.findings == [finding]
    assert all(decision.superseded for decision in continued.verifications)
    assert continued.llm_coverage.reviewers[0].status == "fail"
    assert any(run.kind == "verify" and run.status != "ok" for run in continued.llm_runs)
    assert any(
        todo.context_pack_id == pack.id and todo.reviewer_id == "security"
        for todo in continued.llm_coverage.coverage_todos
    )

    retry_provider = FakeLLMProvider([], verification_approvals=[True])
    retried, retry_selected = continue_review_from_report(
        continued,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=retry_provider,
    )

    assert retry_selected == []
    assert retry_provider.reviewed_pack_ids == []
    assert retry_provider.verified_batch_pack_ids == [pack.id]
    assert retried.findings == [finding]
    assert retried.llm_coverage.reviewers[0].status == "pass"


def test_continue_review_retries_failed_verification_with_an_older_active_approval(
    tmp_path: Path,
) -> None:
    class FailingVerificationProvider(FakeLLMProvider):
        def verify_findings(
            self,
            findings: list[Finding],
            pack: ContextPack,
            repo_root: Path,
        ) -> list[FindingVerification]:
            raise LLMProviderError("temporary verifier outage")

    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", required=True, verify=True)])
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = True
    config.llm.cache_enabled = False
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
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The original verifier approved the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )

    failed, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        pack_ids={pack.id},
        only_unreviewed=False,
        provider=FailingVerificationProvider([finding]),
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert failed.findings == [finding]
    assert failed.llm_coverage.reviewers[0].status == "fail"

    retry_provider = FakeLLMProvider([], verification_approvals=[True])
    retried, retry_selected = continue_review_from_report(
        failed,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        provider=retry_provider,
    )

    assert retry_selected == []
    assert retry_provider.reviewed_pack_ids == []
    assert retry_provider.verified_batch_pack_ids == [pack.id]
    assert retried.findings == [
        finding.model_copy(
            update={
                "reviewer_context_pack_ids": {"security": [pack.id]},
            }
        )
    ]
    assert retried.llm_coverage.reviewers[0].status == "pass"


def test_continue_review_replaces_stale_approval_after_pending_snapshot_completes(
    tmp_path: Path,
) -> None:
    class FailingVerificationProvider(FakeLLMProvider):
        def verify_findings(
            self,
            findings: list[Finding],
            pack: ContextPack,
            repo_root: Path,
        ) -> list[FindingVerification]:
            self.verified_batch_pack_ids.append(pack.id)
            self.verified_batches.append([finding.title for finding in findings])
            self.verified_finding_titles.extend(finding.title for finding in findings)
            raise LLMProviderError("temporary verifier outage")

    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", required=True, verify=True)])
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = True
    config.llm.cache_enabled = False
    stale = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=10,
        failure_mode="A transfer can bypass the tenant boundary.",
        evidence="No authorization predicate precedes the transfer.",
        suggested_fix="Add a tenant authorization check.",
        suggested_test="Reject cross-tenant transfers.",
        context_pack_id=pack.id,
        reviewer_ids=["security"],
    )
    replacement = Finding(
        title="Audit event omits request metadata",
        severity=FindingSeverity.LOW,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=100,
        failure_mode="The new audit event cannot be correlated to its request.",
        evidence="The changed branch omits the request identifier.",
        suggested_fix="Include the request identifier in the audit payload.",
        suggested_test="Assert the audit payload includes its request identifier.",
        context_pack_id=pack.id,
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[stale],
        verifications=[
            FindingVerification(
                finding=stale,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The original verifier approved the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )

    failed, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        pack_ids={pack.id},
        only_unreviewed=False,
        provider=FailingVerificationProvider([replacement]),
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert failed.findings == [stale]
    pending = [verification for verification in failed.verifications if verification.finding.title == replacement.title]
    assert len(pending) == 1
    assert pending[0].review_snapshot_id is not None
    assert pending[0].superseded is True
    assert failed.llm_coverage.reviewers[0].status == "fail"

    retry_provider = FakeLLMProvider([], verification_approvals=[True])
    retried, retry_selected = continue_review_from_report(
        failed,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        provider=retry_provider,
    )

    assert retry_selected == []
    assert retry_provider.verified_finding_titles == [replacement.title]
    assert retried.findings == [
        replacement.model_copy(
            update={
                "reviewer_context_pack_ids": {"security": [pack.id]},
            }
        )
    ]
    assert retried.verifications[0].superseded is True
    assert retried.verifications[0].superseded_reason == ("Replaced by a newer successful reviewer-pack snapshot.")
    assert retried.llm_coverage.reviewers[0].status == "pass"
    assert evaluate_pre_push_gate(retried, config.gates.pre_push).blocked is False

    repeated_provider = FakeLLMProvider([])
    repeated, repeated_selected = continue_review_from_report(
        retried,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        provider=repeated_provider,
    )

    assert repeated_selected == []
    assert repeated_provider.reviewed_pack_ids == []
    assert repeated_provider.verified_batch_pack_ids == []
    assert repeated.findings == retried.findings


def test_continue_review_retries_exact_pending_candidate_across_mixed_verifier_routes(
    tmp_path: Path,
) -> None:
    class PartiallyFailingVerificationProvider(FakeLLMProvider):
        def __init__(self, findings: list[Finding]) -> None:
            super().__init__(findings, verification_approvals=[True])
            self.attempted_batches: list[list[str]] = []

        def verify_findings(
            self,
            findings: list[Finding],
            pack: ContextPack,
            repo_root: Path,
        ) -> list[FindingVerification]:
            self.attempted_batches.append([finding.title for finding in findings])
            if len(self.attempted_batches) == 2:
                raise LLMProviderError("default verifier route is temporarily unavailable")
            return super().verify_findings(findings, pack, repo_root)

    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", required=True, verify=True)])
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = True
    config.llm.cache_enabled = False
    config.llm.profiles = {
        "high-verify": LLMProfile(provider=LLMProviderName.FAKE, model="high-verifier"),
    }
    config.llm.routing.escalated_verify_profile = "high-verify"
    config.llm.routing.escalate_verify_when.finding_severity = [FindingSeverity.HIGH]

    def make_finding(
        title: str,
        severity: FindingSeverity,
        failure_kind: str,
    ) -> Finding:
        return Finding(
            title=title,
            severity=severity,
            confidence=FindingConfidence.HIGH,
            file=pack.file,
            line=20,
            failure_mode=(
                f"{failure_kind} allows unauthorized requests to cross tenant boundaries "
                "and mutate protected account state."
            ),
            evidence=(
                f"The changed handler accepts {failure_kind} input without validating "
                "tenant ownership before the database operation."
            ),
            suggested_fix=(
                f"Validate tenant ownership and reject invalid {failure_kind} requests before mutating account data."
            ),
            suggested_test=(f"Prove cross-tenant {failure_kind} requests are rejected without state changes."),
            context_pack_id=pack.id,
            reviewer_ids=["security"],
        )

    stale = make_finding(
        "Legacy tenant authorization bypass",
        FindingSeverity.MEDIUM,
        "legacy transfer",
    )
    approved_on_first_route = make_finding(
        "Cache poisoning enables account takeover",
        FindingSeverity.HIGH,
        "cache poisoning",
    )
    pending_on_failed_route = make_finding(
        "Audit log omission hides invalid transfer",
        FindingSeverity.MEDIUM,
        "audit omission",
    )
    assert consolidate_findings([stale, pending_on_failed_route]) == [stale]

    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[stale],
        verifications=[
            FindingVerification(
                finding=stale,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The original verifier approved the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                route_reason="default",
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                route_reason="default",
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
        reviewer_scope_ids=["security"],
    )
    first_provider = PartiallyFailingVerificationProvider([approved_on_first_route, pending_on_failed_route])

    failed, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        pack_ids={pack.id},
        only_unreviewed=False,
        provider=first_provider,
    )

    assert [candidate.id for candidate in selected] == [pack.id]
    assert first_provider.attempted_batches == [
        [approved_on_first_route.title],
        [pending_on_failed_route.title],
    ]
    assert [finding.title for finding in failed.findings] == [
        stale.title,
        approved_on_first_route.title,
    ]
    assert failed.llm_coverage.reviewers[0].status == "fail"

    retry_provider = FakeLLMProvider([], verification_approvals=[True])
    retried, retry_selected = continue_review_from_report(
        failed,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        provider=retry_provider,
    )

    assert retry_selected == []
    assert retry_provider.reviewed_pack_ids == []
    assert retry_provider.verified_batches == [[pending_on_failed_route.title]]
    expected_origin = {"security": [pack.id]}
    assert retried.findings == [
        approved_on_first_route.model_copy(
            update={"reviewer_context_pack_ids": expected_origin},
        ),
        pending_on_failed_route.model_copy(
            update={"reviewer_context_pack_ids": expected_origin},
        ),
    ]
    assert retried.verifications[0].superseded is True
    assert retried.verifications[0].superseded_reason == ("Replaced by a newer successful reviewer-pack snapshot.")
    assert retried.llm_coverage.reviewers[0].status == "pass"

    repeated_provider = FakeLLMProvider([])
    repeated, repeated_selected = continue_review_from_report(
        retried,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        provider=repeated_provider,
    )

    assert repeated_selected == []
    assert repeated_provider.reviewed_pack_ids == []
    assert repeated_provider.verified_batch_pack_ids == []
    assert repeated.findings == retried.findings


def test_continue_review_does_not_resurrect_a_replaced_snapshot_candidate(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization boundaries.",
                required=True,
                verify=True,
                verify_profile="verify-a",
            )
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = True
    config.llm.cache_enabled = False
    config.llm.profiles = {
        "verify-a": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-a"),
        "verify-b": LLMProfile(provider=LLMProviderName.FAKE, model="verifier-b"),
    }
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
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The original verifier approved the finding.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    refocused_config = config.model_copy(deep=True)
    refocused_config.reviewers[0].focus = "Authorization and session boundaries."

    clean, clean_selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=refocused_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert [pack.id for pack in clean_selected] == [pack.id]
    assert clean.findings == []
    assert clean.verifications[0].superseded is True
    assert clean.verifications[0].superseded_reason == ("Replaced by a newer successful reviewer-pack snapshot.")

    reconfigured = refocused_config.model_copy(deep=True)
    reconfigured.reviewers[0].verify_profile = "verify-b"
    provider = FakeLLMProvider([], verification_approvals=[True])
    continued, selected = continue_review_from_report(
        clean,
        repo_root=tmp_path,
        config=reconfigured,
        reviewer_ids=["security"],
        provider=provider,
    )

    assert selected == []
    assert provider.reviewed_pack_ids == []
    assert provider.verified_batch_pack_ids == []
    assert continued.findings == []


def test_continue_review_carried_verification_respects_pack_scope_and_cap(
    tmp_path: Path,
) -> None:
    packs = [
        ContextPack(
            id=f"src/auth-{index}.ts#authorize:1",
            file=f"src/auth-{index}.ts",
            file_kind=FileKind.SOURCE,
        )
        for index in range(3)
    ]
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                required=True,
                verify=False,
            )
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = False
    config.llm.cache_enabled = False
    findings = [
        Finding(
            title=f"Authorization bypass {index}",
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.HIGH,
            file=pack.file,
            failure_mode=f"Transfer path {index} bypasses authorization.",
            evidence="No authorization predicate precedes the transfer.",
            suggested_fix="Add a tenant authorization check.",
            suggested_test="Reject cross-tenant transfers.",
            context_pack_id=pack.id,
            reviewer_ids=["security"],
        )
        for index, pack in enumerate(packs)
    ]
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id for pack in packs],
        selected_context_pack_ids=[pack.id for pack in packs],
        deep_selected_context_pack_ids=[pack.id for pack in packs],
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=packs,
        findings=findings,
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            )
            for pack in packs
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )
    current_config = config.model_copy(deep=True)
    current_config.reviewers[0].verify = True
    provider = FakeLLMProvider([], verification_approvals=[True])

    continued, selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        pack_ids={packs[0].id, packs[1].id},
        max_pack_reviews=1,
        provider=provider,
    )

    assert selected == []
    assert len(provider.verified_batch_pack_ids) == 1
    assert set(provider.verified_batch_pack_ids).issubset({packs[0].id, packs[1].id})
    assert packs[2].id not in provider.verified_batch_pack_ids
    assert continued.llm_coverage.reviewers[0].status == "fail"

    second_provider = FakeLLMProvider([], verification_approvals=[True])
    second, second_selected = continue_review_from_report(
        continued,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        pack_ids={packs[0].id, packs[1].id},
        max_pack_reviews=1,
        provider=second_provider,
    )

    assert second_selected == []
    assert len(second_provider.verified_batch_pack_ids) == 1
    assert second_provider.verified_batch_pack_ids[0] != provider.verified_batch_pack_ids[0]
    assert set(second_provider.verified_batch_pack_ids).issubset({packs[0].id, packs[1].id})

    scoped_noop_provider = FakeLLMProvider([])
    scoped_noop, scoped_noop_selected = continue_review_from_report(
        second,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        pack_ids={packs[0].id, packs[1].id},
        max_pack_reviews=1,
        provider=scoped_noop_provider,
    )

    assert scoped_noop_selected == []
    assert scoped_noop_provider.verified_batch_pack_ids == []
    assert scoped_noop.llm_coverage.reviewers[0].status == "fail"

    final_provider = FakeLLMProvider([], verification_approvals=[True])
    completed, completed_selected = continue_review_from_report(
        scoped_noop,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        pack_ids={packs[2].id},
        max_pack_reviews=1,
        provider=final_provider,
    )

    assert completed_selected == []
    assert final_provider.verified_batch_pack_ids == [packs[2].id]
    assert completed.llm_coverage.reviewers[0].status == "pass"

    noop_provider = FakeLLMProvider([])
    noop, noop_selected = continue_review_from_report(
        completed,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        max_pack_reviews=1,
        provider=noop_provider,
    )

    assert noop_selected == []
    assert noop_provider.verified_batch_pack_ids == []
    assert noop.verifications == completed.verifications


def test_continue_review_drops_removed_reviewers_from_persisted_scope(
    tmp_path: Path,
) -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
    )
    initial_config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="ux", verify=False),
            ReviewerConfig(id="security", verify=False),
        ]
    )
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        initial_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        reviewer_scope_ids=["ux"],
    )
    current_config = initial_config.model_copy(deep=True)
    current_config.reviewers = [current_config.reviewers[1]]
    current_config.llm.enabled = True
    current_config.llm.provider = LLMProviderName.FAKE
    current_config.llm.verify = False
    current_config.llm.cache_enabled = False

    continued, _selected = continue_review_from_report(
        initial,
        repo_root=tmp_path,
        config=current_config,
        reviewer_ids=["security"],
        provider=FakeLLMProvider([]),
    )

    assert continued.reviewer_scope_ids == ["security"]
    assert type(continued).model_validate_json(continued.model_dump_json()).reviewer_scope_ids == ["security"]


def test_multi_reviewer_verification_preserves_decision_provenance(
    tmp_path: Path,
) -> None:
    diff_text = """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1 +1 @@
-{"name":"old"}
+{"name":"new","scripts":{"postinstall":"node setup.js"}}
"""
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", focus="Supply-chain execution."),
            ReviewerConfig(id="operations", focus="Deployment safety."),
        ]
    )
    config.llm.enabled = True
    config.llm.provider = LLMProviderName.FAKE
    config.llm.verify = True
    candidate = Finding(
        title="Install hook executes an untrusted local script",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="package.json",
        failure_mode=(
            "Dependency installation executes a local script before operators can validate the package contents."
        ),
        evidence="The changed manifest adds a postinstall command that invokes setup.js.",
        suggested_fix="Remove the lifecycle command or validate the invoked artifact.",
        suggested_test="Install with lifecycle scripts disabled and assert the build succeeds.",
    )
    provider = FakeLLMProvider([candidate], verification_approvals=[True, True])

    report = run_review_pipeline(tmp_path, diff_text, TargetMode.PATCH, config, provider=provider)

    assert len(report.findings) == 1
    assert report.findings[0].reviewer_ids == ["operations", "security"]
    assert len(report.verifications) == 2
    assert {decision.reviewer_id for decision in report.verifications} == {
        "operations",
        "security",
    }


def test_consolidate_findings_deduplicates_test_and_source_root_cause() -> None:
    test_finding = Finding(
        title="Test locks in raw CoreBank TFA method pass-through",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="apps/client-bff/src/modules/auth/application/auth.service.test.ts",
        line=182,
        failure_mode=(
            "The test now requires raw CoreBank tfaMethods objects to pass through "
            "the mobile BFF response, including settings.phoneNumber and upstream internals."
        ),
        evidence=(
            "The expected response includes full CoreBank method objects with settings.phoneNumber, "
            "hint, id, and other upstream fields."
        ),
        suggested_fix="Project a safe mobile DTO and keep phoneNumber/settings out of the response.",
        suggested_test="Assert settings.phoneNumber is absent from result.tfaMethods.",
    )
    source_finding = Finding(
        title="Raw CoreBank TFA method objects are exposed in the login response",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="apps/client-bff/src/modules/auth/application/auth.service.ts",
        line=143,
        failure_mode=(
            "The login response can include raw CoreBank tfaMethods objects, including "
            "settings.phoneNumber, hint, id, and upstream internals."
        ),
        evidence=(
            "The service returns jwtPayload.tfaMethods unchanged after documenting the live "
            "CoreBank object shape with settings.phoneNumber."
        ),
        suggested_fix="Project a safe mobile DTO and omit settings, phoneNumber, IDs, and raw JWT internals.",
        suggested_test="Use an object-shaped SMS fixture and assert the response excludes settings.phoneNumber.",
    )
    distinct_finding = Finding(
        title="Type assertion added at JWT boundary",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="apps/client-bff/src/modules/auth/application/auth.service.ts",
        line=123,
        failure_mode="A type assertion masks an external JWT payload shape change.",
        evidence="The diff adds `m as { type: unknown }`.",
        suggested_fix="Use a type guard.",
        suggested_test="Compile without type assertions.",
    )

    assert consolidate_findings([test_finding, source_finding, distinct_finding]) == [
        source_finding,
        distinct_finding,
    ]


def test_consolidate_findings_preserves_all_reviewer_provenance() -> None:
    first = Finding(
        title="Authorization guard can be bypassed before settlement",
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
        suggested_fix="Move the early return after the ownership authorization guard.",
        suggested_test="Add a denied-account settlement regression test.",
        context_pack_id="tests/settlement.test.ts#settles:1",
        reviewer_ids=["security"],
        reviewer_context_pack_ids={
            "security": ["tests/settlement.test.ts#settles:1"],
        },
    )
    duplicate = first.model_copy(
        update={
            "context_pack_id": "src/settlement.ts#settles:1",
            "reviewer_ids": ["finance"],
            "reviewer_context_pack_ids": {
                "finance": ["src/settlement.ts#settles:1"],
            },
        }
    )

    consolidated = consolidate_findings([first, duplicate])

    assert len(consolidated) == 1
    assert consolidated[0].reviewer_ids == ["finance", "security"]
    assert consolidated[0].reviewer_context_pack_ids == {
        "finance": [duplicate.context_pack_id],
        "security": [first.context_pack_id],
    }


def test_consolidate_findings_does_not_invent_origins_for_legacy_multi_reviewer() -> None:
    legacy = Finding(
        title="Authorization guard can be bypassed before settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/settlement.ts",
        line=42,
        failure_mode="A caller can submit a settlement without the required ownership check.",
        evidence="The changed early return executes before the ownership guard.",
        suggested_fix="Move the early return after the ownership guard.",
        suggested_test="Reject a settlement for an account the caller does not own.",
        context_pack_id="src/settlement.ts#authorize:1",
        reviewer_ids=["finance", "security"],
    )
    current = legacy.model_copy(
        update={
            "context_pack_id": "src/settlement.ts#dispatch:2",
            "reviewer_ids": ["ux"],
            "reviewer_context_pack_ids": {
                "ux": ["src/settlement.ts#dispatch:2"],
            },
        }
    )

    consolidated = consolidate_findings([legacy, current])

    assert len(consolidated) == 1
    assert consolidated[0].reviewer_ids == ["finance", "security", "ux"]
    assert consolidated[0].reviewer_context_pack_ids == {
        "ux": ["src/settlement.ts#dispatch:2"],
    }


def test_consolidate_findings_keeps_single_reviewer_legacy_origin_unknown() -> None:
    legacy = Finding(
        title="Authorization guard can be bypassed before settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/settlement.ts",
        line=42,
        failure_mode="A caller can submit a settlement without the required ownership check.",
        evidence="The changed early return executes before the ownership guard.",
        suggested_fix="Move the early return after the ownership guard.",
        suggested_test="Reject a settlement for an account the caller does not own.",
        context_pack_id="src/settlement.ts#authorize:1",
        reviewer_ids=["security"],
    )
    current = legacy.model_copy(
        update={
            "context_pack_id": "src/settlement.ts#dispatch:2",
            "reviewer_context_pack_ids": {
                "security": ["src/settlement.ts#dispatch:2"],
            },
        }
    )

    consolidated = consolidate_findings([legacy, current])

    assert len(consolidated) == 1
    assert consolidated[0].reviewer_ids == ["security"]
    assert consolidated[0].reviewer_context_pack_ids == {}


def test_consolidate_findings_preserves_normalized_general_with_specialist() -> None:
    general = Finding(
        title="Authorization guard can be bypassed before settlement",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/settlement.ts",
        line=42,
        failure_mode="A caller can submit a settlement without the required ownership check.",
        evidence="The changed early return executes before the ownership guard.",
        suggested_fix="Move the early return after the ownership guard.",
        suggested_test="Reject a settlement for an account the caller does not own.",
        context_pack_id="src/settlement.ts#authorize:1",
        reviewer_context_pack_ids={
            "general": ["src/settlement.ts#authorize:1"],
        },
    )
    security = general.model_copy(
        update={
            "reviewer_ids": ["security"],
            "reviewer_context_pack_ids": {
                "security": ["src/settlement.ts#authorize:1"],
            },
        }
    )

    consolidated = consolidate_findings([general, security])

    assert len(consolidated) == 1
    assert consolidated[0].reviewer_ids == ["general", "security"]
    assert consolidated[0].reviewer_context_pack_ids == {
        "general": ["src/settlement.ts#authorize:1"],
        "security": ["src/settlement.ts#authorize:1"],
    }


def test_consolidate_findings_deduplicates_exact_concise_findings() -> None:
    first = Finding(
        title="Tenant bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        line=42,
        failure_mode="Guard skipped.",
        evidence="Early return.",
        suggested_fix="Run guard.",
        suggested_test="Reject tenant.",
        reviewer_ids=["security"],
    )
    duplicate = first.model_copy(update={"reviewer_ids": ["finance"]})

    consolidated = consolidate_findings([first, duplicate])

    assert len(consolidated) == 1
    assert consolidated[0].reviewer_ids == ["finance", "security"]


def test_consolidate_findings_prefers_an_approved_duplicate() -> None:
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
        reviewer_ids=["security"],
    )
    unverified = approved.model_copy(
        update={
            "title": "Settlement authorization can be bypassed",
            "confidence": FindingConfidence.HIGH,
            "reviewer_ids": ["finance"],
        }
    )

    consolidated = consolidate_findings(
        [approved, unverified],
        preferred_findings=[approved],
    )

    assert len(consolidated) == 1
    assert consolidated[0].title == approved.title
    assert consolidated[0].reviewer_ids == ["finance", "security"]


def test_consolidate_findings_uses_bracketed_query_tokens_for_duplicates() -> None:
    schema_finding = Finding(
        title="`filter[pagination]` is now accepted despite unsupported response shape",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="apps/client-bff/src/modules/transactions/framework/transactions.dto.ts",
        line=37,
        failure_mode=(
            "A request with filter[pagination]=true passes validation and reaches the transactions service, "
            "but CoreBank returns a pagination-wrapped response shape."
        ),
        evidence="The schema declares filter[pagination] and uses passthrough for filter keys.",
        suggested_fix="Block filter[pagination] until the paginated response shape is modeled.",
        suggested_test="Assert filter[pagination]=true is rejected or stripped.",
    )
    adapter_finding = Finding(
        title="Unsafe `filter[pagination]` is forwarded to CoreBank",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="apps/client-bff/src/modules/transactions/infrastructure/corebank-transactions.adapter.ts",
        line=59,
        failure_mode=(
            "The adapter forwards filter[pagination] upstream, so CoreBank can return the pagination-wrapped "
            "response while translateTransactionsList still expects an array."
        ),
        evidence="The loop forwards every filter[ key and does not block filter[pagination].",
        suggested_fix="Drop filter[pagination] in the adapter until the response mapper supports the wrapped shape.",
        suggested_test="Assert buildTransactionsWireQuery omits filter[pagination].",
    )
    conflict_finding = Finding(
        title="Bare and wire-shaped pagination keys can be duplicated",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="apps/client-bff/src/modules/transactions/infrastructure/corebank-transactions.adapter.ts",
        line=51,
        failure_mode="page plus filter[page] produces duplicate query params with conflicting values.",
        evidence="The adapter appends filter[page] from page and then appends existing filter[page].",
        suggested_fix="Choose a conflict policy.",
        suggested_test="Cover page with filter[page].",
    )

    assert consolidate_findings([schema_finding, adapter_finding, conflict_finding]) == [
        schema_finding,
        conflict_finding,
    ]


def test_apply_language_filter_marks_disabled_languages_ignored() -> None:
    diff = parse_unified_diff(
        """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1 +1 @@
-{"name":"old"}
+{"name":"new"}
diff --git a/src/cart.ts b/src/cart.ts
--- a/src/cart.ts
+++ b/src/cart.ts
@@ -1 +1 @@
-export const total = 1;
+export const total = 2;
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    apply_language_filter(diff, ["typescript"])

    assert diff.files[0].is_ignored is True
    assert diff.files[0].ignore_reason == "Language not enabled: json"
    assert diff.files[1].is_ignored is False
