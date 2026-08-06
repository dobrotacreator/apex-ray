from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from apex_ray.llm import FakeLLMProvider
from apex_ray.models import (
    ContextPack,
    DiffSummary,
    LLMContextSelection,
    LLMCoverageSummary,
    LLMPackReviewStatus,
    LLMReviewerCoverageSummary,
    LLMRun,
    ProjectProfile,
    ReviewConfig,
    ReviewCoverageCompletion,
    ReviewerConfig,
    ReviewInputSnapshot,
    TargetMode,
)
from apex_ray.pipeline.coverage import continue_review_until_complete, coverage_is_complete
from apex_ray.pipeline.snapshot import (
    ReviewInputSnapshotError,
    capture_review_input_snapshot,
)
from apex_ray.report import build_report


def _detached_snapshot(tmp_path: Path) -> ReviewInputSnapshot:
    return capture_review_input_snapshot(tmp_path, "", TargetMode.PATCH)


def test_coverage_completion_status_is_serialized_and_distinguishes_failures() -> None:
    partial = LLMCoverageSummary(
        enabled=True,
        total_context_packs=2,
        reviewed_context_packs=1,
        unreviewed_context_packs=1,
        partial_severity="minor",
    )
    incomplete = partial.model_copy(
        update={
            "pack_statuses": [
                LLMPackReviewStatus(
                    context_pack_id="src/app.ts#run:1",
                    file="src/app.ts",
                    status="failed_provider",
                    priority="p2",
                )
            ]
        }
    )
    failed_verification = LLMCoverageSummary(
        enabled=True,
        total_context_packs=1,
        reviewed_context_packs=1,
        reviewers=[
            LLMReviewerCoverageSummary(
                reviewer_id="general",
                failed_verify_runs=1,
            )
        ],
    )

    assert partial.completion_status == "partial"
    assert partial.model_dump(mode="json")["completion_status"] == "partial"
    assert incomplete.completion_status == "incomplete"
    assert failed_verification.completion_status == "incomplete"
    assert LLMCoverageSummary(enabled=True).completion_status == "complete"
    assert LLMCoverageSummary().completion_status == "disabled"


def test_optional_reviewer_verification_debt_prevents_false_complete_status() -> None:
    coverage = LLMCoverageSummary(
        enabled=True,
        total_context_packs=1,
        reviewed_context_packs=1,
        reviewers=[
            LLMReviewerCoverageSummary(
                reviewer_id="security",
                status="warn",
                matching_context_packs=1,
                reviewed_context_packs=1,
                matching_context_pack_ids=["src/auth.ts#authorize:1"],
                reviewed_context_pack_ids=["src/auth.ts#authorize:1"],
                reasons=["Reviewer security has unresolved verification subjects."],
            )
        ],
    )

    assert coverage.completion_status == "partial"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "complete", "reviewer_ids": [], "batches": -1, "stop_reason": "complete"},
        {"status": "complete", "reviewer_ids": [], "batches": 1, "stop_reason": "no_progress"},
        {
            "status": "complete",
            "reviewer_ids": ["security", "security"],
            "batches": 1,
            "stop_reason": "complete",
        },
    ],
)
def test_persisted_coverage_completion_rejects_invalid_contract(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ReviewCoverageCompletion.model_validate(payload)


def test_until_complete_runs_bounded_batches_until_reviewer_scope_is_complete(
    tmp_path: Path,
    monkeypatch,
) -> None:
    packs = [
        ContextPack(id="src/a.ts#one:1", file="src/a.ts"),
        ContextPack(id="src/b.ts#two:1", file="src/b.ts"),
    ]
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=packs,
        input_snapshot=_detached_snapshot(tmp_path),
    )
    report.llm_coverage.reviewers = [
        LLMReviewerCoverageSummary(
            reviewer_id="correctness",
            required=True,
            matching_context_packs=2,
            matching_context_pack_ids=[pack.id for pack in packs],
        )
    ]
    calls: list[set[str]] = []

    def fake_continue(current, **kwargs):
        calls.append(set(kwargs["pack_ids"]))
        reviewed = len(calls)
        current.llm_coverage.reviewed_context_packs = reviewed
        current.llm_coverage.unreviewed_context_packs = 2 - reviewed
        current.llm_coverage.reviewed_context_pack_ids = [pack.id for pack in packs[:reviewed]]
        current.llm_coverage.unreviewed_context_pack_ids = [pack.id for pack in packs[reviewed:]]
        current.llm_coverage.partial_severity = "none" if reviewed == 2 else "minor"
        reviewer = current.llm_coverage.reviewers[0]
        reviewer.reviewed_context_packs = reviewed
        reviewer.reviewed_context_pack_ids = [pack.id for pack in packs[:reviewed]]
        reviewer.status = "pass"
        return current, [packs[reviewed - 1]]

    monkeypatch.setattr("apex_ray.pipeline.coverage.continue_review_from_report", fake_continue)

    result = continue_review_until_complete(
        report,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["correctness"],
        batch_size=1,
        max_batches=3,
    )

    assert result.complete is True
    assert result.batches == 2
    assert result.stop_reason == "complete"
    assert calls == [{packs[0].id, packs[1].id}, {packs[1].id}]


def test_until_complete_stops_after_a_batch_without_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pack = ContextPack(id="src/a.ts#one:1", file="src/a.ts")
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        input_snapshot=_detached_snapshot(tmp_path),
    )
    calls = 0

    def fake_continue(current, **_kwargs):
        nonlocal calls
        calls += 1
        return current, [pack]

    monkeypatch.setattr("apex_ray.pipeline.coverage.continue_review_from_report", fake_continue)

    result = continue_review_until_complete(
        report,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=None,
        batch_size=1,
        max_batches=5,
    )

    assert result.complete is False
    assert result.batches == 1
    assert result.stop_reason == "no_progress"
    assert calls == 1


def test_until_complete_rebases_changed_reviewer_config_before_declaring_complete(
    tmp_path: Path,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    original_config = ReviewConfig(
        reviewers=[ReviewerConfig(id="correctness", focus="Original correctness focus.", verify=False)]
    )
    original_config.llm.enabled = True
    original_config.llm.cache_enabled = False
    selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        original_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="correctness",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"correctness": selection},
        reviewer_scope_ids=["correctness"],
        input_snapshot=_detached_snapshot(tmp_path),
    )
    changed_config = original_config.model_copy(deep=True)
    changed_config.reviewers[0].focus = "Changed correctness focus."
    provider = FakeLLMProvider([])

    result = continue_review_until_complete(
        report,
        repo_root=tmp_path,
        config=changed_config,
        reviewer_ids=["correctness"],
        batch_size=1,
        max_batches=2,
        provider=provider,
    )

    assert result.complete is True
    assert result.batches == 1
    assert provider.reviewed_pack_ids == [pack.id]
    assert result.report.config.reviewers[0].focus == "Changed correctness focus."


def test_explicit_reviewer_completion_ignores_global_debt_outside_its_matching_scope(
    tmp_path: Path,
) -> None:
    reviewed = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    unrelated = ContextPack(id="src/ui.ts#render:1", file="src/ui.ts")
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[reviewed, unrelated],
        input_snapshot=_detached_snapshot(tmp_path),
    )
    report.llm_coverage.reviewed_context_packs = 1
    report.llm_coverage.unreviewed_context_packs = 1
    report.llm_coverage.reviewed_context_pack_ids = [reviewed.id]
    report.llm_coverage.unreviewed_context_pack_ids = [unrelated.id]
    report.llm_coverage.partial_severity = "minor"
    report.llm_coverage.reviewers = [
        LLMReviewerCoverageSummary(
            reviewer_id="security",
            status="pass",
            matching_context_packs=1,
            reviewed_context_packs=1,
            matching_context_pack_ids=[reviewed.id],
            reviewed_context_pack_ids=[reviewed.id],
        )
    ]

    assert report.llm_coverage.completion_status == "partial"
    assert coverage_is_complete(report, reviewer_ids=["security"]) is True

    result = continue_review_until_complete(
        report,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security"],
        batch_size=1,
        max_batches=2,
    )

    assert result.complete is True
    assert result.batches == 0


def test_until_complete_upgrades_shallow_high_risk_packs_to_deep(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        input_snapshot=_detached_snapshot(tmp_path),
    )
    report.llm_coverage.reviewed_context_packs = 1
    report.llm_coverage.unreviewed_context_packs = 0
    report.llm_coverage.reviewed_context_pack_ids = [pack.id]
    report.llm_coverage.shallow_only_high_risk_context_pack_ids = [pack.id]
    report.llm_coverage.partial_severity = "major"
    seen: dict[str, object] = {}

    def fake_continue(current, **kwargs):
        seen.update(kwargs)
        current.llm_coverage.shallow_only_high_risk_context_pack_ids = []
        current.llm_coverage.partial_severity = "none"
        return current, [pack]

    monkeypatch.setattr("apex_ray.pipeline.coverage.continue_review_from_report", fake_continue)

    result = continue_review_until_complete(
        report,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=None,
        batch_size=1,
        max_batches=2,
    )

    assert result.complete is True
    assert seen["pack_ids"] == {pack.id}
    assert seen["only_unreviewed"] is True
    assert seen["force_review_pack_ids"] == {pack.id}
    assert seen["review_depth"] == "deep"


def test_public_completion_api_rejects_legacy_report_without_snapshot(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=False),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
    )

    with pytest.raises(ReviewInputSnapshotError, match="review-input snapshot"):
        continue_review_until_complete(
            report,
            repo_root=tmp_path,
            config=config,
            reviewer_ids=None,
            batch_size=1,
            max_batches=1,
        )


def test_until_complete_rebases_new_reviewer_scope_even_when_config_is_unchanged(tmp_path: Path) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", paths=["src/**"], verify=False),
            ReviewerConfig(id="correctness", paths=["src/**"], verify=False),
        ]
    )
    config.llm.enabled = True
    config.llm.cache_enabled = False
    security_selection = LLMContextSelection(
        total_context_pack_ids=[pack.id],
        selected_context_pack_ids=[pack.id],
        deep_selected_context_pack_ids=[pack.id],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=False),
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
            )
        ],
        reviewer_selections={"security": security_selection},
        reviewer_scope_ids=["security"],
        input_snapshot=_detached_snapshot(tmp_path),
    )
    provider = FakeLLMProvider([])

    result = continue_review_until_complete(
        report,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["correctness"],
        batch_size=1,
        max_batches=2,
        provider=provider,
    )

    assert result.complete is True
    assert result.batches == 1
    assert provider.reviewed_pack_ids == [pack.id]


def test_until_complete_clears_stale_completion_before_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=False),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        input_snapshot=_detached_snapshot(tmp_path),
    )
    report.coverage_completion = ReviewCoverageCompletion(
        status="complete",
        reviewer_ids=["security"],
        batches=1,
        stop_reason="complete",
    )
    report.llm_coverage.enabled = True
    report.llm_coverage.total_context_packs = 1
    report.llm_coverage.unreviewed_context_packs = 1
    report.llm_coverage.unreviewed_context_pack_ids = [pack.id]
    report.llm_coverage.partial_severity = "minor"
    checkpoints: list[ReviewCoverageCompletion | None] = []

    def fake_continue(current, **_kwargs):
        current.llm_coverage.reviewed_context_packs = 1
        current.llm_coverage.unreviewed_context_packs = 0
        current.llm_coverage.reviewed_context_pack_ids = [pack.id]
        current.llm_coverage.unreviewed_context_pack_ids = []
        current.llm_coverage.partial_severity = "none"
        return current, [pack]

    monkeypatch.setattr("apex_ray.pipeline.coverage.continue_review_from_report", fake_continue)

    result = continue_review_until_complete(
        report,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=None,
        batch_size=1,
        max_batches=1,
        on_batch=lambda current, _batch: checkpoints.append(current.coverage_completion),
    )

    assert result.complete is True
    assert checkpoints == [None]


def test_until_complete_deep_upgrades_overlapping_pack_only_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", paths=["src/**"], verify=False),
            ReviewerConfig(id="correctness", paths=["src/**"], verify=False),
        ]
    )
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=False),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        input_snapshot=_detached_snapshot(tmp_path),
    )
    report.llm_coverage.enabled = True
    report.llm_coverage.total_context_packs = 1
    report.llm_coverage.reviewed_context_packs = 1
    report.llm_coverage.reviewed_context_pack_ids = [pack.id]
    report.llm_coverage.shallow_only_high_risk_context_pack_ids = [pack.id]
    report.llm_coverage.partial_severity = "major"
    report.llm_coverage.reviewers = [
        LLMReviewerCoverageSummary(
            reviewer_id=reviewer_id,
            status="pass",
            matching_context_packs=1,
            selected_context_packs=1,
            reviewed_context_packs=1,
            matching_context_pack_ids=[pack.id],
            selected_context_pack_ids=[pack.id],
            reviewed_context_pack_ids=[pack.id],
        )
        for reviewer_id in ("security", "correctness")
    ]
    seen_reviewer_ids: list[list[str] | None] = []

    def fake_continue(current, **kwargs):
        seen_reviewer_ids.append(kwargs["reviewer_ids"])
        current.llm_coverage.shallow_only_high_risk_context_pack_ids = []
        current.llm_coverage.partial_severity = "none"
        return current, [pack]

    monkeypatch.setattr("apex_ray.pipeline.coverage.continue_review_from_report", fake_continue)

    result = continue_review_until_complete(
        report,
        repo_root=tmp_path,
        config=config,
        reviewer_ids=["security", "correctness"],
        batch_size=16,
        max_batches=1,
    )

    assert result.complete is True
    assert seen_reviewer_ids == [["security"]]
