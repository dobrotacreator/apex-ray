import json
from pathlib import Path

from apex_ray.models import (
    AnalyzerCoverageSignal,
    AnalyzerMetrics,
    AnalyzerResult,
    AnalyzerShardMetrics,
    AnalyzerWarningSummary,
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
    TargetMode,
)
from apex_ray.report import build_report
from apex_ray.telemetry import append_review_telemetry, load_review_telemetry, render_review_telemetry_summary


def test_review_telemetry_round_trip(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.telemetry.path_mode = "anonymized"
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.WORKTREE, stats=DiffStats(files_changed=1, additions=3)),
        context_packs=[ContextPack(id="src/cart.ts#file", file="src/cart.ts", file_kind=FileKind.SOURCE)],
        llm_runs=[
            LLMRun(
                provider="fake",
                context_pack_id="src/cart.ts#file",
                status="ok",
                duration_ms=9,
                input_chars=400,
                estimated_input_tokens=100,
                actual_input_tokens=80,
                actual_output_tokens=20,
                actual_total_tokens=100,
                estimated_saved_input_tokens=25,
                usage_source="unit",
                cache_hits=1,
            )
        ],
        stage_durations_ms={"diff": 2, "analyzers": 7, "llm": 9, "total": 25},
    )
    telemetry_path = tmp_path / ".apex-ray" / "telemetry" / "review-runs.jsonl"

    append_review_telemetry(
        report,
        telemetry_path,
        source_repo=tmp_path,
        duration_ms=25,
        output_path=tmp_path / "review.md",
        json_output_path=tmp_path / "review.json",
    )
    entries = load_review_telemetry(telemetry_path)
    summary = render_review_telemetry_summary(entries)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["target_mode"] == "worktree"
    assert entry["schema_version"] == "review-telemetry/v2"
    assert "source_repo" not in entry
    assert len(entry["repo_id"]) == 16
    assert entry["output_path"] == "review.md"
    assert entry["duration_ms"] == 25
    assert entry["files_changed"] == 1
    assert entry["llm_estimated_input_tokens"] == 100
    assert entry["llm_actual_total_tokens"] == 100
    assert entry["llm_estimated_saved_input_tokens"] == 25
    assert entry["llm_usage_sources"] == ["unit"]
    assert entry["llm_cache_hits"] == 1
    assert entry["llm_input_estimate_ratio"] == 0.8
    assert entry["stage_durations_ms"] == {"analyzers": 7, "diff": 2, "llm": 9, "total": 25}
    assert entry["pack_status_counts"] == {"reviewed_deep": 1}
    assert "Latest LLM tokens: `~100`" in summary
    assert "Latest actual LLM tokens: `100`" in summary


def test_review_telemetry_exports_path_free_analyzer_stage_and_shard_aggregates(
    tmp_path: Path,
) -> None:
    analyzer_result = AnalyzerResult(
        language="typescript",
        projectRoot="/sensitive/repo",
        warnings=["Sensitive analyzer warning for src/private.ts"],
        warningSummaries=[
            AnalyzerWarningSummary(
                message="Sensitive analyzer warning for src/private.ts",
                occurrences=4,
                shardIndexes=[1, 2],
            )
        ],
        partial=True,
        coverage=AnalyzerCoverageSignal(
            partial=True,
            reasonCodes=["workspace_index_partial"],
            scopes=["workspace_index"],
            failedFileCount=1,
        ),
        metrics=AnalyzerMetrics(
            wallDurationMs=25,
            stageDurationsMs={
                "manifest": 2,
                "workspace_index": 8,
                "/sensitive/repo/src/private.ts": 99,
            },
            shards=[
                AnalyzerShardMetrics(
                    index=1,
                    total=2,
                    status="partial",
                    wallDurationMs=11,
                    stageDurationsMs={"workspace_index": 4, "src/private.ts": 99},
                    changedFileCount=3,
                    analyzedFileCount=3,
                    warningCount=2,
                    partialReasonCodes=["workspace_index_partial"],
                    indexCacheHits=5,
                    indexCacheMisses=7,
                ),
                AnalyzerShardMetrics(
                    index=2,
                    total=2,
                    status="timeout",
                    wallDurationMs=12,
                    changedFileCount=2,
                    failedFileCount=1,
                    warningCount=2,
                    partialReasonCodes=["shard_timeout", "changed_file_analysis_incomplete"],
                ),
            ],
        ),
        failedFiles=["src/private.ts"],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
        analyzer_results=[analyzer_result],
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(report, telemetry_path, source_repo=tmp_path, duration_ms=30)

    analyzer = load_review_telemetry(telemetry_path)[0]["analyzers"][0]
    assert analyzer == {
        "language": "typescript",
        "partial": True,
        "partial_reason_codes": ["workspace_index_partial"],
        "partial_scopes": ["workspace_index"],
        "failed_file_count": 1,
        "warning_count": 4,
        "unique_warning_count": 1,
        "wall_duration_ms": 25,
        "stage_durations_ms": {"manifest": 2, "workspace_index": 8},
        "shards": [
            {
                "index": 1,
                "total": 2,
                "status": "partial",
                "wall_duration_ms": 11,
                "stage_durations_ms": {"workspace_index": 4},
                "changed_file_count": 3,
                "analyzed_file_count": 3,
                "failed_file_count": 0,
                "warning_count": 2,
                "partial_reason_codes": ["workspace_index_partial"],
                "index_cache_hits": 5,
                "index_cache_misses": 7,
            },
            {
                "index": 2,
                "total": 2,
                "status": "timeout",
                "wall_duration_ms": 12,
                "stage_durations_ms": {},
                "changed_file_count": 2,
                "analyzed_file_count": 0,
                "failed_file_count": 1,
                "warning_count": 2,
                "partial_reason_codes": ["shard_timeout", "changed_file_analysis_incomplete"],
                "index_cache_hits": 0,
                "index_cache_misses": 0,
            },
        ],
    }
    serialized = json.dumps(analyzer)
    assert "/sensitive/repo" not in serialized
    assert "src/private.ts" not in serialized
    assert "Sensitive analyzer warning" not in serialized


def test_review_telemetry_preserves_structured_failed_file_count(tmp_path: Path) -> None:
    analyzer_result = AnalyzerResult(
        language="typescript",
        projectRoot=str(tmp_path),
        partial=True,
        coverage=AnalyzerCoverageSignal(
            partial=True,
            reasonCodes=["changed_file_analysis_incomplete"],
            scopes=["changed_files"],
            failedFileCount=3,
        ),
        failedFiles=["src/retained.ts"],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
        analyzer_results=[analyzer_result],
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(report, telemetry_path, source_repo=tmp_path, duration_ms=1)

    analyzer = load_review_telemetry(telemetry_path)[0]["analyzers"][0]
    assert analyzer["failed_file_count"] == 3


def test_review_telemetry_counts_legacy_and_structured_warnings(tmp_path: Path) -> None:
    analyzer_result = AnalyzerResult(
        language="typescript",
        projectRoot=str(tmp_path),
        warnings=["repeated", "repeated", "legacy only"],
        warningSummaries=[
            AnalyzerWarningSummary(
                message="repeated",
                occurrences=4,
                shardIndexes=[1, 2],
            )
        ],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
        analyzer_results=[analyzer_result],
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(report, telemetry_path, source_repo=tmp_path, duration_ms=1)

    analyzer = load_review_telemetry(telemetry_path)[0]["analyzers"][0]
    assert analyzer["warning_count"] == 5
    assert analyzer["unique_warning_count"] == 2


def test_review_telemetry_input_ratio_includes_provider_split_cache_tokens(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[ContextPack(id="src/cart.ts#file", file="src/cart.ts")],
        llm_runs=[
            LLMRun(
                provider="anthropic_api",
                context_pack_id="src/cart.ts#file",
                status="ok",
                duration_ms=1,
                estimated_input_tokens=200,
                actual_input_tokens=10,
                actual_cached_input_tokens=90,
                actual_output_tokens=20,
                actual_total_tokens=220,
                actual_cache_read_input_tokens=90,
                actual_cache_creation_input_tokens=100,
                usage_source="anthropic_api",
            )
        ],
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(report, telemetry_path, source_repo=tmp_path, duration_ms=1)

    entry = load_review_telemetry(telemetry_path)[0]
    assert entry["llm_input_estimate_ratio"] == 1.0


def test_review_telemetry_summary_uses_effective_input_for_legacy_provider_entries() -> None:
    entries = [
        {
            "schema_version": "review-telemetry/v1",
            "created_at": "2026-07-01T00:00:00Z",
            "run_id": "anthropic",
            "llm_estimated_input_tokens": 200,
            "llm_actual_input_tokens": 10,
            "llm_actual_cached_input_tokens": 90,
            "llm_actual_cache_read_input_tokens": 90,
            "llm_actual_cache_creation_input_tokens": 100,
            "llm_actual_output_tokens": 20,
            "llm_actual_total_tokens": 220,
        },
        {
            "schema_version": "review-telemetry/v1",
            "created_at": "2026-07-02T00:00:00Z",
            "run_id": "openai",
            "llm_estimated_input_tokens": 100,
            "llm_actual_input_tokens": 100,
            "llm_actual_cached_input_tokens": 60,
            "llm_actual_cache_read_input_tokens": 60,
            "llm_actual_cache_creation_input_tokens": 0,
            "llm_actual_output_tokens": 20,
            "llm_actual_total_tokens": 120,
        },
    ]

    summary = render_review_telemetry_summary(entries)

    assert "Aggregate input estimate ratio: `1.00x`" in summary


def test_review_telemetry_counts_canonical_verified_findings_and_reviewer_decisions(
    tmp_path: Path,
) -> None:
    finding = Finding(
        title="Duplicate settlement defect",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/settlement.ts",
        line=42,
        failure_mode="Two reviewer passes identify the same loss of idempotency.",
        evidence="The changed write no longer uses the idempotency key.",
        suggested_fix="Restore the idempotent write.",
        suggested_test="Retry settlement with the same key.",
    )
    verified_variant = finding.model_copy(
        update={
            "title": "Settlement retry can create a duplicate transfer",
            "line": 43,
        }
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding.model_copy(update={"reviewer_ids": ["finance", "security"]})],
        verifications=[
            FindingVerification(
                finding=verified_variant.model_copy(update={"reviewer_ids": ["security"]}),
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Confirmed by the security pass.",
            ),
            FindingVerification(
                finding=verified_variant.model_copy(update={"reviewer_ids": ["finance"]}),
                reviewer_id="finance",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Confirmed by the finance pass.",
            ),
        ],
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(
        report,
        telemetry_path,
        source_repo=tmp_path,
        duration_ms=10,
    )
    entry = load_review_telemetry(telemetry_path)[0]

    assert entry["verified_findings_count"] == 1
    assert entry["verification_decisions_count"] == 2
    assert entry["approved_verification_decisions_count"] == 2
    assert entry["active_approved_verification_decisions_count"] == 2
    assert entry["active_rejected_verification_decisions_count"] == 0


def test_review_telemetry_counts_legacy_replaced_decision_as_superseded(
    tmp_path: Path,
) -> None:
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        failure_mode="A caller can bypass authorization.",
        evidence="The changed branch skips the ownership check.",
        suggested_fix="Check ownership before returning.",
        suggested_test="Add a cross-account authorization test.",
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
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
                reason="The later verifier rejected the finding.",
            ),
        ],
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(
        report,
        telemetry_path,
        source_repo=tmp_path,
        duration_ms=10,
    )
    entry = load_review_telemetry(telemetry_path)[0]

    assert entry["verified_findings_count"] == 0
    assert entry["verification_decisions_count"] == 2
    assert entry["approved_verification_decisions_count"] == 1
    assert entry["active_verification_decisions_count"] == 1
    assert entry["active_approved_verification_decisions_count"] == 0
    assert entry["active_rejected_verification_decisions_count"] == 1
    assert entry["unresolved_verification_decisions_count"] == 0
    assert entry["superseded_verification_decisions_count"] == 1


def test_review_telemetry_counts_failed_verification_as_unresolved(
    tmp_path: Path,
) -> None:
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        failure_mode="A caller can bypass authorization.",
        evidence="The changed branch skips the ownership check.",
        suggested_fix="Check ownership before returning.",
        suggested_test="Add a cross-account authorization test.",
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=False,
                confidence=FindingConfidence.LOW,
                reason="The verifier was unavailable.",
                superseded=True,
                superseded_reason="Verification run did not complete successfully (failed_provider).",
            )
        ],
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(
        report,
        telemetry_path,
        source_repo=tmp_path,
        duration_ms=10,
    )
    entry = load_review_telemetry(telemetry_path)[0]

    assert entry["verification_decisions_count"] == 1
    assert entry["approved_verification_decisions_count"] == 0
    assert entry["active_verification_decisions_count"] == 0
    assert entry["active_approved_verification_decisions_count"] == 0
    assert entry["active_rejected_verification_decisions_count"] == 0
    assert entry["unresolved_verification_decisions_count"] == 1
    assert entry["superseded_verification_decisions_count"] == 0


def test_review_telemetry_aggregates_reviewer_outcomes_without_source_paths(tmp_path: Path) -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", focus="Security."),
            ReviewerConfig(id="finance", focus="Finance."),
        ]
    )
    config.llm.enabled = True
    config.telemetry.path_mode = "anonymized"
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[ContextPack(id="src/payments.ts#file", file="src/payments.ts")],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id="src/payments.ts#file",
                status="ok",
                duration_ms=10,
                findings_count=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id="src/payments.ts#file",
                status="failed_quota",
                duration_ms=5,
            ),
        ],
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(report, telemetry_path, source_repo=tmp_path, duration_ms=20)

    entry = load_review_telemetry(telemetry_path)[0]
    assert entry["reviewers"] == {
        "finance": {
            "failed_runs": 1,
            "findings": 0,
            "runs": 1,
            "verify_enabled": True,
        },
        "security": {
            "failed_runs": 0,
            "findings": 1,
            "runs": 1,
            "verify_enabled": True,
        },
    }
    assert str(tmp_path) not in telemetry_path.read_text(encoding="utf-8")


def test_review_telemetry_includes_required_reviewer_with_no_runs(
    tmp_path: Path,
) -> None:
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
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[
            ContextPack(
                id="src/auth.ts#authorize:1",
                file="src/auth.ts",
            )
        ],
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(
        report,
        telemetry_path,
        source_repo=tmp_path,
        duration_ms=1,
    )
    entry = load_review_telemetry(telemetry_path)[0]

    assert entry["reviewers"]["security"] == {
        "failed_runs": 0,
        "findings": 0,
        "runs": 0,
        "verify_enabled": False,
    }


def test_review_telemetry_can_opt_in_to_full_local_paths(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.telemetry.path_mode = "full"
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    telemetry_path = tmp_path / "full.jsonl"

    append_review_telemetry(report, telemetry_path, source_repo=tmp_path, duration_ms=1)

    entry = load_review_telemetry(telemetry_path)[0]
    assert entry["source_repo"] == str(tmp_path)


def test_review_telemetry_jsonl_is_compact(tmp_path: Path) -> None:
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=0)),
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(report, telemetry_path, source_repo=tmp_path, duration_ms=1)

    line = telemetry_path.read_text(encoding="utf-8").strip()
    assert "\n" not in line
    assert json.loads(line)["context_packs_count"] == 0
