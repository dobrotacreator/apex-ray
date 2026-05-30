from pathlib import Path

from apex_ray.classify import classify_diff
from apex_ray.diff import parse_unified_diff
from apex_ray.llm import FakeLLMProvider
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
    LLMProviderName,
    LLMRun,
    ProjectProfile,
    ReviewConfig,
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


def test_select_llm_context_packs_does_not_let_noisy_tests_crowd_out_source() -> None:
    noisy_test_pack = ContextPack(
        id="src/cart.test.ts#test:1",
        file="src/cart.test.ts",
        file_kind=FileKind.TEST,
        risk_signals=[
            RiskSignal(
                kind="persistence",
                severity=RiskSeverity.HIGH,
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
    first_b = ContextPack(id="src/b.ts#first", file="src/b.ts", file_kind=FileKind.SOURCE)

    selected = select_llm_context_packs(
        [first_a, second_a, third_a, first_b],
        [
            ChangedFile(old_path="src/a.ts", new_path="src/a.ts", file_kind=FileKind.SOURCE),
            ChangedFile(old_path="src/b.ts", new_path="src/b.ts", file_kind=FileKind.SOURCE),
        ],
        max_packs=2,
    )

    assert selected == [first_a, first_b]


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
        max_packs=1,
        max_input_tokens=None,
    )

    assert selection.selected_context_pack_ids == ["src/b.ts#file", "src/a.ts#file", "src/a.test.ts#file"]
    assert selection.deep_selected_context_pack_ids == ["src/a.ts#file"]
    assert selection.shallow_selected_context_pack_ids == ["src/b.ts#file", "src/a.test.ts#file"]
    assert selection.unselected_context_pack_ids == []
    assert [stage.stage for stage in selection.stages] == ["deep", "shallow"]


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
