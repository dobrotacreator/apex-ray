from pathlib import Path

from apex_ray.classify import classify_diff
from apex_ray.diff import parse_unified_diff
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
    LLMContextSelection,
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
from apex_ray.report import build_report


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
    initial = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[kept, removed],
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
    assert continued.llm_coverage.quality_gate_status != "fail"
    assert not any("review run(s) failed" in reason for reason in continued.llm_coverage.partial_reasons)


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
    assert not any("review run(s) failed" in reason for reason in continued.llm_coverage.partial_reasons)


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
                id="ux",
                focus="User-facing behavior.",
                paths=["src/ui.py"],
                verify=False,
            )
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
    assert full.llm_coverage.total_context_packs == 2
    assert full.llm_coverage.quality_gate_status == "fail"
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
