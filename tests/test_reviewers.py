from pathlib import Path

import pytest

from apex_ray.llm import (
    FakeLLMProvider,
    estimate_review_input_tokens,
    review_config_for_pack,
    review_context_packs,
)
from apex_ray.llm.prompts import build_review_prompt
from apex_ray.llm.review import verify_findings
from apex_ray.models import (
    ContextPack,
    DiffSummary,
    FileKind,
    Finding,
    FindingConfidence,
    FindingSeverity,
    LLMConfig,
    LLMProfile,
    ReviewerConfig,
    RiskSeverity,
    RiskSignal,
    TargetMode,
)
from apex_ray.pipeline.runner import _plan_reviewer_context_selection
from apex_ray.reviewers import (
    ReviewerConfigError,
    effective_reviewers,
    llm_config_for_reviewer,
    pack_for_reviewer,
    reviewer_matches_pack,
)


def test_effective_reviewers_preserves_legacy_general_review() -> None:
    reviewers = effective_reviewers([])
    pack = ContextPack(id="src/service.ts#run:1", file="src/service.ts")

    assert len(reviewers) == 1
    assert reviewers[0].id == "general"
    assert reviewers[0].name == "General review"
    assert reviewers[0].required is False
    assert pack_for_reviewer(pack, reviewers[0]) is pack


def test_effective_reviewers_filters_requested_ids_in_requested_order() -> None:
    reviewers = [
        ReviewerConfig(id="security", focus="Security."),
        ReviewerConfig(id="finance", focus="Financial correctness."),
        ReviewerConfig(id="disabled", enabled=False),
    ]

    selected = effective_reviewers(reviewers, ["finance", "security"])

    assert [reviewer.id for reviewer in selected] == ["finance", "security"]
    with pytest.raises(ReviewerConfigError, match="Unknown or disabled reviewer: disabled"):
        effective_reviewers(reviewers, ["disabled"])


def test_reviewer_matches_pack_uses_path_kind_and_risk_tags() -> None:
    reviewer = ReviewerConfig(
        id="finance",
        paths=["src/**"],
        exclude_paths=["src/**/*.test.ts"],
        file_kinds=[FileKind.SOURCE],
        risk=["external_io"],
        risk_tags=["finance"],
    )
    matching = ContextPack(
        id="src/settlement.ts#settle:1",
        file="src/settlement.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="policy:settlement",
                severity="critical",
                reason="Settlement boundary.",
                file="src/settlement.ts",
                reviewer_tags=["finance"],
            )
        ],
    )
    unrelated = ContextPack(
        id="src/theme.ts#theme:1",
        file="src/theme.ts",
        file_kind=FileKind.SOURCE,
    )

    assert reviewer_matches_pack(reviewer, matching) is True
    assert reviewer_matches_pack(reviewer, unrelated) is False


def test_reviewer_double_star_exclusion_matches_zero_or_more_directories() -> None:
    reviewer = ReviewerConfig(id="source", exclude_paths=["src/**/*.test.ts"])

    assert (
        reviewer_matches_pack(
            reviewer,
            ContextPack(id="root-test", file=r".\src\auth.test.ts"),
        )
        is False
    )
    assert (
        reviewer_matches_pack(
            reviewer,
            ContextPack(id="nested-test", file="src/unit/auth.test.ts"),
        )
        is False
    )
    assert (
        reviewer_matches_pack(
            reviewer,
            ContextPack(id="source", file="src/auth.ts"),
        )
        is True
    )


def test_reviewer_exact_path_with_brackets_matches_literal_route_path() -> None:
    path = "src/pages/[id].ts"
    reviewer = ReviewerConfig(id="route-review", paths=[path])

    assert reviewer_matches_pack(reviewer, ContextPack(id="route", file=path)) is True


def test_llm_config_for_reviewer_applies_profile_verify_and_budget_overrides() -> None:
    config = LLMConfig(
        profiles={
            "broad": LLMProfile(model="broad-model"),
            "strong": LLMProfile(model="strong-model"),
        }
    )
    reviewer = ReviewerConfig(
        id="security",
        profile="broad",
        verify_profile="strong",
        coverage_mode="exhaustive",
        max_packs=20,
        max_deep_packs=12,
        max_input_tokens=90_000,
        verify=False,
    )

    resolved = llm_config_for_reviewer(config, reviewer)

    assert resolved.routing.review_profile == "broad"
    assert resolved.routing.verify_profile == "strong"
    assert resolved.coverage_mode == "exhaustive"
    assert resolved.max_packs == 20
    assert resolved.max_deep_packs == 12
    assert resolved.max_input_tokens == 90_000
    assert resolved.verify is False
    assert config.routing.review_profile is None


def test_pack_for_reviewer_adds_prompt_context_without_mutating_report_pack() -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        impact_notes=["Authorization entry point changed."],
        risk_signals=[
            RiskSignal(
                kind="auth",
                severity=RiskSeverity.HIGH,
                reason="Authorization changed.",
                file="src/auth.ts",
            )
        ],
    )
    reviewer = ReviewerConfig(
        id="security",
        name="Security reviewer",
        focus="Authorization boundaries.",
        instructions=["Prefer exploitable issues."],
    )

    scoped = pack_for_reviewer(pack, reviewer)

    assert scoped is not pack
    assert pack.reviewer is None
    assert scoped.reviewer is not None
    assert scoped.reviewer.id == "security"
    assert scoped.reviewer.focus == "Authorization boundaries."
    assert scoped.reviewer.instructions == ["Prefer exploitable issues."]
    assert scoped.impact_notes is pack.impact_notes
    assert scoped.risk_signals is pack.risk_signals
    assert scoped.stats is pack.stats
    prompt = build_review_prompt(scoped)
    assert "Focused reviewer: Security reviewer (`security`)" in prompt
    assert "Authorization boundaries." in prompt
    assert "Prefer exploitable issues." in prompt
    assert "Do not turn the focused pass into a duplicate general review." in prompt


def test_reviewer_prompt_is_included_in_selection_token_budget() -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    reviewer = ReviewerConfig(
        id="security",
        focus="authorization boundary " * 1_000,
    )
    config = LLMConfig(
        provider="fake",
        max_packs=1,
        max_deep_packs=1,
        max_input_tokens=1_000,
    )

    selection = _plan_reviewer_context_selection(
        [pack],
        DiffSummary(target_mode=TargetMode.WORKTREE),
        config,
        reviewer,
        max_pack_chars=40_000,
    )

    assert selection.selected_context_pack_ids == []
    assert selection.over_token_budget_context_pack_ids == [pack.id]


def test_reviewer_selection_budget_uses_effective_routed_provider() -> None:
    pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        diff_snippet=["+" + ("x" * 16_000)],
    )
    reviewer = ReviewerConfig(
        id="security",
        profile="cli",
        focus="Authorization boundaries.",
    )
    base_config = LLMConfig(
        provider="openai_api",
        profiles={
            "cli": LLMProfile(
                provider="codex_cli",
                model="review-model",
            )
        },
        max_packs=1,
        max_deep_packs=1,
    )
    config = llm_config_for_reviewer(base_config, reviewer)
    focused_pack = pack_for_reviewer(pack, reviewer)
    routed_config, _profile, _reason = review_config_for_pack(config, focused_pack)
    direct_api_tokens = estimate_review_input_tokens(
        focused_pack,
        provider=config.provider,
    )
    routed_tokens = estimate_review_input_tokens(
        focused_pack,
        provider=routed_config.provider,
    )
    assert direct_api_tokens < routed_tokens
    config.max_input_tokens = (direct_api_tokens + routed_tokens) // 2

    selection = _plan_reviewer_context_selection(
        [pack],
        DiffSummary(target_mode=TargetMode.WORKTREE),
        config,
        reviewer,
        max_pack_chars=40_000,
    )

    assert selection.selected_context_pack_ids == []
    assert selection.over_token_budget_context_pack_ids == [pack.id]


def test_review_context_packs_records_focused_reviewer_provenance() -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    candidate = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        failure_mode="A caller can skip the permission check.",
        evidence="The changed branch returns before authorization.",
        suggested_fix="Keep the authorization guard before the return.",
        suggested_test="Add a denied-role regression test.",
    )
    provider = FakeLLMProvider([candidate])
    reviewer = ReviewerConfig(id="security", focus="Authorization boundaries.")

    findings, runs = review_context_packs(
        [pack],
        LLMConfig(provider="fake"),
        Path("."),
        provider=provider,
        reviewer=reviewer,
    )

    assert findings[0].reviewer_ids == ["security"]
    assert runs[0].reviewer_id == "security"
    assert pack.reviewer is None


def test_verify_findings_records_focused_reviewer_provenance(tmp_path: Path) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        failure_mode="A caller can skip the permission check.",
        evidence="The changed branch returns before authorization.",
        suggested_fix="Keep the authorization guard before the return.",
        suggested_test="Add a denied-role regression test.",
        context_pack_id=pack.id,
        reviewer_ids=["security"],
    )
    reviewer = ReviewerConfig(id="security", focus="Authorization boundaries.")

    approved, verifications, runs = verify_findings(
        [finding],
        [pack],
        LLMConfig(provider="fake", cache_dir=str(tmp_path / "cache")),
        tmp_path,
        provider=FakeLLMProvider(verification_approvals=[True]),
        reviewer=reviewer,
    )

    assert approved == [finding]
    assert verifications[0].finding.reviewer_ids == ["security"]
    assert runs[0].reviewer_id == "security"
    assert pack.reviewer is None
