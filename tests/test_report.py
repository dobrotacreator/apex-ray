from apex_ray.gates import evaluate_pre_push_gate
from apex_ray.invocation import ApexRayLauncher
from apex_ray.models import (
    AnalyzerResult,
    AnalyzerSymbol,
    AnalyzerWarningSummary,
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
    LLMCoverageMode,
    LLMCoverageSummary,
    LLMCoverageTodo,
    LLMReviewerCoverageSummary,
    LLMRun,
    MemoryCard,
    MemoryKind,
    MemoryMatch,
    MemoryOmission,
    ProjectProfile,
    ReviewConfig,
    ReviewCoverageCompletion,
    ReviewerConfig,
    ReviewReport,
    RiskSeverity,
    RiskSignal,
    TargetMode,
)
from apex_ray.report import build_report, render_html, render_markdown
from apex_ray.report.coverage import continue_command_for_pack, render_coverage_summary_lines
from apex_ray.report.coverage_breakdown import pack_residual_priority


def test_report_renders_warning_multiplicity_and_loads_legacy_analyzer_fields() -> None:
    analyzer_result = AnalyzerResult(
        language="typescript",
        projectRoot="/repo",
        warnings=["TypeScript workspace index is partial."],
        warningSummaries=[
            AnalyzerWarningSummary(
                message="TypeScript workspace index is partial.",
                occurrences=3,
                shardIndexes=[1, 2, 3],
            )
        ],
        partial=True,
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
        analyzer_results=[analyzer_result],
    )

    assert "repeated 3 times; shards 1, 2, 3" in render_markdown(report)
    assert "repeated 3 times; shards 1, 2, 3" in render_html(report)

    legacy_payload = report.model_dump(mode="json")
    legacy_result = legacy_payload["analyzer_results"][0]
    legacy_result.pop("warning_summaries")
    legacy_result.pop("coverage")
    legacy_result.pop("metrics")
    round_tripped = ReviewReport.model_validate(legacy_payload)

    assert round_tripped.schema_version == "review-report/v1"
    assert round_tripped.analyzer_results[0].warnings == ["TypeScript workspace index is partial."]
    assert round_tripped.analyzer_results[0].warning_summaries == []
    assert round_tripped.analyzer_results[0].coverage is None
    assert round_tripped.analyzer_results[0].metrics is None


def test_render_markdown_explains_project_risk_and_focused_reviewers() -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", name="Security", focus="Authorization boundaries.", verify=False),
            ReviewerConfig(id="finance", name="Finance", focus="Money movement.", verify=False),
        ]
    )
    config.llm.enabled = True
    finding = Finding(
        title="Settlement can bypass authorization",
        severity=FindingSeverity.CRITICAL,
        confidence=FindingConfidence.HIGH,
        file="src/settlement.ts",
        line=42,
        failure_mode="An untrusted caller can settle another account.",
        evidence="The changed branch returns before the ownership check.",
        suggested_fix="Run the ownership check before settlement.",
        suggested_test="Add a cross-account settlement test.",
        context_pack_id="src/settlement.ts#settle:42",
        reviewer_ids=["finance", "security"],
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(
            target_mode=TargetMode.PATCH,
            stats=DiffStats(files_changed=1),
            files=[
                ChangedFile(
                    old_path="src/settlement.ts",
                    new_path="src/settlement.ts",
                    file_kind=FileKind.SOURCE,
                    risk_signals=[
                        RiskSignal(
                            kind="policy:settlement",
                            severity=RiskSeverity.CRITICAL,
                            score=98,
                            reason="Project risk policy matched: Settlement boundary.",
                            file="src/settlement.ts",
                            line=42,
                            source="project",
                            rule_id="settlement",
                            categories=["money_movement"],
                            reviewer_tags=["finance"],
                            guidance="Preserve idempotency and authorization.",
                        )
                    ],
                )
            ],
        ),
        context_packs=[
            ContextPack(
                id="src/settlement.ts#settle:42",
                file="src/settlement.ts",
                file_kind=FileKind.SOURCE,
            )
        ],
        findings=[finding],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id="src/settlement.ts#settle:42",
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="finance",
                context_pack_id="src/settlement.ts#settle:42",
                status="ok",
                duration_ms=1,
            ),
        ],
        reviewer_selections={
            "security": LLMContextSelection(
                total_context_pack_ids=["src/settlement.ts#settle:42"],
                selected_context_pack_ids=["src/settlement.ts#settle:42"],
                deep_selected_context_pack_ids=["src/settlement.ts#settle:42"],
            ),
            "finance": LLMContextSelection(
                total_context_pack_ids=["src/settlement.ts#settle:42"],
                selected_context_pack_ids=["src/settlement.ts#settle:42"],
                deep_selected_context_pack_ids=["src/settlement.ts#settle:42"],
            ),
        },
    )
    report.coverage_completion = ReviewCoverageCompletion(
        status="complete",
        reviewer_ids=["security"],
        batches=2,
        stop_reason="complete",
    )

    markdown = render_markdown(report)

    assert "### Critical" in markdown
    assert "`policy:settlement`" in markdown
    assert "score: `98`" in markdown
    assert "Reviewer tags: `finance`" in markdown
    assert "Guidance: Preserve idempotency and authorization." in markdown
    assert "Reviewers: `finance`, `security`" in markdown
    assert "## Focused Reviewers" in markdown
    assert "`security` (Security)" in markdown
    assert "`finance` (Finance)" in markdown
    assert "- Completion status: `complete`" in markdown
    assert "- Bounded completion: `complete`" in markdown
    assert "- Completion scope: `security`" in markdown
    assert "- Completion batches: `2`" in markdown
    assert "- Completion stop reason: `complete`" in markdown
    assert "- Reviewer assignments: `2` of `2`" in markdown


def test_render_markdown_counts_empty_reviewer_ids_as_general() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig()
    config.llm.enabled = True
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts")],
        findings=[
            Finding(
                title="Authorization bypass",
                severity=FindingSeverity.HIGH,
                confidence=FindingConfidence.HIGH,
                file="src/auth.ts",
                failure_mode="A request can bypass authorization.",
                evidence="The changed branch returns before the guard.",
                suggested_fix="Run the guard first.",
                suggested_test="Reject an unauthorized request.",
                context_pack_id=pack_id,
                reviewer_context_pack_ids={"general": [pack_id]},
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="general",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            )
        ],
        reviewer_selections={"general": selection},
    )

    markdown = render_markdown(report)

    assert "`general` (general)" in markdown
    assert "Deep/shallow selected: `1`/`0`; findings: `1`; failed runs: `0`" in markdown


def test_builtin_auth_heuristic_does_not_make_test_only_packs_p0() -> None:
    source_pack = ContextPack(
        id="src/auth.ts#authorize:1",
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="auth",
                severity=RiskSeverity.HIGH,
                reason="Authorization-sensitive code changed.",
                file="src/auth.ts",
            )
        ],
    )
    test_pack = ContextPack(
        id="tests/auth.test.ts#authorize:1",
        file="tests/auth.test.ts",
        file_kind=FileKind.TEST,
        risk_signals=[
            RiskSignal(
                kind="auth",
                severity=RiskSeverity.HIGH,
                reason="Authorization-sensitive test text changed.",
                file="tests/auth.test.ts",
            )
        ],
    )
    config = ReviewConfig()
    config.llm.enabled = True

    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[source_pack, test_pack],
    )

    priorities = {status.context_pack_id: status.priority for status in report.llm_coverage.pack_statuses}
    assert priorities[source_pack.id] == "p0"
    assert priorities[test_pack.id] == "p1"


def test_current_high_risk_outranks_lower_archived_residual_priority() -> None:
    pack = ContextPack(
        id="src/payments.ts#capture:1",
        file="src/payments.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="financial",
                severity=RiskSeverity.HIGH,
                reason="Money movement changed.",
                file="src/payments.ts",
            )
        ],
    )

    assert pack_residual_priority(pack, archived_priority="p2") == "p0"


def test_render_markdown_lists_legacy_configured_reviewers_when_selection_data_is_missing() -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                name="Security",
                focus="Authorization boundaries.",
            ),
            ReviewerConfig(
                id="ux",
                name="UX",
                focus="User-facing behavior.",
            ),
        ]
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        reviewer_selections=None,
    )

    markdown = render_markdown(report)

    assert "`security` (Security) - Authorization boundaries." in markdown
    assert "`ux` (UX) - User-facing behavior." in markdown
    assert "selection data was not recorded" in markdown
    assert "No focused reviewer passes configured." not in markdown


def test_render_markdown_filters_configured_reviewers_by_explicit_scope_when_selections_are_missing() -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                name="Security",
                focus="Authorization boundaries.",
                required=True,
            ),
            ReviewerConfig(
                id="ux",
                name="UX",
                focus="User-facing behavior.",
            ),
        ]
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        reviewer_selections=None,
        reviewer_scope_ids=["ux"],
    )

    markdown = render_markdown(report)

    assert "`ux` (UX) - User-facing behavior." in markdown
    assert "`security`" not in markdown
    assert [reviewer.reviewer_id for reviewer in report.llm_coverage.reviewers] == ["ux"]


def test_reviewer_coverage_exposes_effective_independent_limits() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                coverage_mode=LLMCoverageMode.FAST,
                review_depth="deep",
                max_packs=7,
                max_deep_packs=5,
                max_input_tokens=42_000,
                verify=False,
            )
        ]
    )
    config.llm.enabled = True
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts")],
        reviewer_selections={"security": selection},
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            )
        ],
    )

    reviewer = report.llm_coverage.reviewers[0]
    markdown = render_markdown(report)

    assert reviewer.coverage_mode == "fast"
    assert reviewer.review_depth == "deep"
    assert reviewer.max_packs == 7
    assert reviewer.max_deep_packs == 5
    assert reviewer.max_input_tokens == 42_000
    assert "Effective limits: mode `fast`, depth `deep`, packs `7`, deep `5`, input `~42000` tokens" in markdown


def test_render_markdown_includes_missing_required_reviewer_when_other_selection_exists() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                name="Security",
                required=True,
                verify=False,
            ),
            ReviewerConfig(
                id="ux",
                name="UX",
                verify=False,
            ),
        ]
    )
    config.llm.enabled = True
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[
            ContextPack(
                id=pack_id,
                file="src/auth.ts",
                file_kind=FileKind.SOURCE,
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="ux",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            )
        ],
        reviewer_selections={"ux": selection},
    )

    markdown = render_markdown(report)

    assert "`security` (Security)" in markdown
    assert "Status: `fail`, required" in markdown
    assert "Selection data: `missing`" in markdown
    assert "Required reviewer security did not review" in markdown
    assert "`ux` (UX)" in markdown
    assert "Status: `pass`" in markdown


def test_render_markdown_preserves_legacy_verifier_counters_and_adds_unique_summary() -> None:
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
        ProjectProfile(root="/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Confirmed.",
            ),
            FindingVerification(
                finding=finding,
                reviewer_id="correctness",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Independently confirmed.",
            ),
            FindingVerification(
                finding=finding,
                reviewer_id="general",
                approved=False,
                confidence=FindingConfidence.MEDIUM,
                reason="Needs another reproduction.",
            ),
        ],
    )

    markdown = render_markdown(report)

    assert "- Unique verified findings: `1`" in markdown
    assert "- Approved: `2`" in markdown
    assert "- Rejected: `1`" in markdown
    assert "Approved reviewer decisions" not in markdown
    assert "Rejected reviewer decisions" not in markdown


def test_missing_reviewer_selections_keep_required_reviewer_coverage_and_applicability() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization.",
                paths=["src/**"],
                required=True,
            ),
            ReviewerConfig(
                id="finance",
                focus="Money movement.",
                paths=["payments/**"],
                required=True,
            ),
            ReviewerConfig(
                id="correctness",
                focus="Runtime behavior.",
                paths=["src/**"],
                required=True,
            ),
        ]
    )
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE)],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="failed_auth",
                duration_ms=1,
            )
        ],
        reviewer_selections=None,
    )

    coverage = {reviewer.reviewer_id: reviewer for reviewer in report.llm_coverage.reviewers}

    assert set(coverage) == {"correctness", "finance", "security"}
    assert coverage["security"].status == "fail"
    assert coverage["security"].matching_context_pack_ids == [pack_id]
    assert coverage["security"].selected_context_pack_ids == [pack_id]
    assert coverage["correctness"].status == "fail"
    assert coverage["correctness"].matching_context_pack_ids == [pack_id]
    assert coverage["correctness"].selected_context_pack_ids == [pack_id]
    assert coverage["finance"].status == "not_applicable"
    assert coverage["finance"].matching_context_pack_ids == []
    assert report.llm_coverage.quality_gate_status == "fail"
    assert any("Required reviewer security" in reason for reason in report.llm_coverage.quality_gate_reasons)


def test_missing_selections_fail_closed_for_every_required_reviewer_pack() -> None:
    first_pack_id = "src/auth.ts#authorize:1"
    second_pack_id = "src/session.ts#refresh:1"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization and session safety.",
                paths=["src/**"],
                required=True,
            ),
            ReviewerConfig(
                id="ux",
                focus="User experience.",
                paths=["src/**"],
            ),
        ]
    )
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[
            ContextPack(id=first_pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE),
            ContextPack(id=second_pack_id, file="src/session.ts", file_kind=FileKind.SOURCE),
        ],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=first_pack_id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                provider="openai_api",
                reviewer_id="ux",
                context_pack_id=second_pack_id,
                status="ok",
                duration_ms=1,
            ),
        ],
        reviewer_selections=None,
    )

    coverage = {reviewer.reviewer_id: reviewer for reviewer in report.llm_coverage.reviewers}
    assert coverage["security"].status == "fail"
    assert coverage["security"].selected_context_pack_ids == [
        first_pack_id,
        second_pack_id,
    ]
    assert coverage["security"].reviewed_context_pack_ids == [first_pack_id]
    assert any(
        todo.reviewer_id == "security" and todo.context_pack_id == second_pack_id
        for todo in report.llm_coverage.coverage_todos
    )
    assert report.llm_coverage.quality_gate_status == "fail"


def test_partial_reviewer_selections_fail_closed_for_missing_required_reviewer() -> None:
    pack_id = "src/auth.ts#authorize:1"
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", paths=["src/**"], required=True),
            ReviewerConfig(id="ux", paths=["src/**"]),
        ]
    )
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE)],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="ux",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            )
        ],
        reviewer_selections={"ux": selection},
    )

    coverage = {reviewer.reviewer_id: reviewer for reviewer in report.llm_coverage.reviewers}

    assert coverage["ux"].status == "pass"
    assert coverage["security"].status == "fail"
    assert coverage["security"].matching_context_pack_ids == [pack_id]
    assert coverage["security"].selected_context_pack_ids == [pack_id]
    assert coverage["security"].reviewed_context_pack_ids == []
    assert report.llm_coverage.quality_gate_status == "fail"


def test_partial_reviewer_selections_honor_persisted_explicit_scope() -> None:
    pack_id = "src/auth.ts#authorize:1"
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", paths=["src/**"], required=True),
            ReviewerConfig(id="ux", paths=["src/**"]),
        ]
    )
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE)],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="ux",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            )
        ],
        reviewer_selections={"ux": selection},
        reviewer_scope_ids=["ux"],
    )

    assert report.reviewer_scope_ids == ["ux"]
    assert [reviewer.reviewer_id for reviewer in report.llm_coverage.reviewers] == ["ux"]
    assert report.llm_coverage.reviewers[0].status == "pass"
    assert report.llm_coverage.quality_gate_status == "pass"
    assert type(report).model_validate_json(report.model_dump_json()).reviewer_scope_ids == ["ux"]


def test_legacy_serialized_report_without_reviewer_scope_loads_as_full_scope() -> None:
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        ReviewConfig(reviewers=[ReviewerConfig(id="security", required=True)]),
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    payload = report.model_dump(mode="json")
    payload.pop("reviewer_scope_ids")

    loaded = type(report).model_validate(payload)

    assert loaded.reviewer_scope_ids is None


def test_disabled_llm_keeps_configured_required_reviewers_not_applicable() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", paths=["src/**"], required=True),
        ]
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE)],
        reviewer_selections=None,
    )

    assert report.llm_coverage.quality_gate_status == "disabled"
    assert report.llm_coverage.reviewers[0].status == "not_applicable"
    assert report.llm_coverage.reviewers[0].reasons == []
    assert all(todo.reviewer_id != "security" for todo in report.llm_coverage.coverage_todos)
    assert "- Completion status: `disabled`" in render_markdown(report)


def test_required_reviewer_failure_fails_union_coverage_gate() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", focus="Authorization.", required=True),
            ReviewerConfig(id="ux", focus="User experience."),
        ]
    )
    config.llm.enabled = True
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )

    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE)],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="failed_auth",
                duration_ms=1,
                error="invalid credentials",
            ),
            LLMRun(
                provider="openai_api",
                reviewer_id="ux",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection, "ux": selection},
    )

    assert report.llm_coverage.coverage_ratio == 1.0
    assert report.llm_coverage.quality_gate_status == "fail"
    assert any("Required reviewer security" in reason for reason in report.llm_coverage.quality_gate_reasons)
    reviewer_coverage = {reviewer.reviewer_id: reviewer for reviewer in report.llm_coverage.reviewers}
    assert reviewer_coverage["security"].status == "fail"
    assert reviewer_coverage["ux"].status == "pass"


def test_reviewer_assignment_debt_is_not_hidden_by_union_pack_coverage() -> None:
    first_pack_id = "src/auth.ts#authorize:1"
    second_pack_id = "src/session.ts#refresh:1"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="security", paths=["src/**"], required=True),
            ReviewerConfig(id="ux", paths=["src/**"]),
        ]
    )
    config.llm.enabled = True
    security_selection = LLMContextSelection(
        total_context_pack_ids=[first_pack_id, second_pack_id],
        selected_context_pack_ids=[first_pack_id],
        deep_selected_context_pack_ids=[first_pack_id],
        unselected_context_pack_ids=[second_pack_id],
        skipped_context_pack_reasons={second_pack_id: "not selected by reviewer pack cap"},
    )
    ux_selection = LLMContextSelection(
        total_context_pack_ids=[first_pack_id, second_pack_id],
        selected_context_pack_ids=[second_pack_id],
        deep_selected_context_pack_ids=[second_pack_id],
        unselected_context_pack_ids=[first_pack_id],
        skipped_context_pack_reasons={first_pack_id: "not selected by reviewer pack cap"},
    )

    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[
            ContextPack(id=first_pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE),
            ContextPack(id=second_pack_id, file="src/session.ts", file_kind=FileKind.SOURCE),
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=first_pack_id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="ux",
                context_pack_id=second_pack_id,
                status="ok",
                duration_ms=1,
            ),
        ],
        reviewer_selections={"security": security_selection, "ux": ux_selection},
    )

    reviewer_coverage = {reviewer.reviewer_id: reviewer for reviewer in report.llm_coverage.reviewers}
    assert report.llm_coverage.coverage_ratio == 1.0
    assert reviewer_coverage["security"].status == "pass"
    assert reviewer_coverage["security"].reviewed_context_pack_ids == [first_pack_id]
    assert reviewer_coverage["ux"].status == "pass"
    assert reviewer_coverage["ux"].reviewed_context_pack_ids == [second_pack_id]
    assert report.llm_coverage.completion_status == "partial"
    assert report.llm_coverage.quality_gate_status == "pass"
    assert {(todo.reviewer_id, todo.context_pack_id) for todo in report.llm_coverage.coverage_todos} == {
        ("security", second_pack_id),
        ("ux", first_pack_id),
    }


def test_applicable_required_reviewer_with_zero_selected_packs_fails_closed() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", paths=["src/**"], required=True)])
    config.llm.enabled = True
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[],
        unselected_context_pack_ids=[pack_id],
        skipped_context_pack_reasons={pack_id: "not selected by reviewer token budget"},
    )

    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE)],
        reviewer_selections={"security": selection},
    )

    reviewer = report.llm_coverage.reviewers[0]
    assert reviewer.status == "fail"
    assert any("selected no context packs" in reason for reason in reviewer.reasons)
    assert report.llm_coverage.quality_gate_status == "fail"
    assert report.llm_coverage.completion_status == "incomplete"
    assert any(
        todo.reviewer_id == "security" and todo.context_pack_id == pack_id
        for todo in report.llm_coverage.coverage_todos
    )


def test_console_coverage_summary_separates_assignment_and_high_risk_depth_debt() -> None:
    coverage = LLMCoverageSummary(
        enabled=True,
        total_context_packs=1,
        reviewed_context_packs=1,
        high_risk_context_packs=1,
        reviewed_high_risk_context_packs=1,
        shallow_only_high_risk_context_pack_ids=["src/auth.ts#authorize:1"],
        reviewers=[
            LLMReviewerCoverageSummary(
                reviewer_id="correctness",
                matching_context_packs=2,
                reviewed_context_packs=1,
            )
        ],
        coverage_todos=[
            LLMCoverageTodo(
                context_pack_id="src/auth.ts#authorize:1",
                file="src/auth.ts",
                reviewer_id="correctness",
                priority="p1",
            )
        ],
    )

    lines = render_coverage_summary_lines(coverage)

    assert "High-risk coverage: PARTIAL - 1/1; depth debt: 1 shallow-only" in lines
    assert "Remaining: P0 0, P1 0, P2 0 globally unreviewed" in lines
    assert "Reviewer assignment debt: P0 0, P1 1, P2 0" in lines


def test_shallow_only_high_risk_pack_has_an_actionable_deep_continuation_todo() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="baseline", paths=["docs/**"], required=True, verify=False),
            ReviewerConfig(id="security", risk=["auth"], verify=False),
        ]
    )
    config.llm.enabled = True
    pack = ContextPack(
        id=pack_id,
        file="src/auth.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="auth",
                severity=RiskSeverity.HIGH,
                reason="Authorization boundary changed.",
                file="src/auth.ts",
            )
        ],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        shallow_selected_context_pack_ids=[pack_id],
    )

    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        llm_runs=[
            LLMRun(
                kind="review_shallow",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            )
        ],
        reviewer_selections={"security": selection},
    )

    assert report.llm_coverage.shallow_only_high_risk_context_pack_ids == [pack_id]
    assert [(todo.context_pack_id, todo.reviewer_id) for todo in report.llm_coverage.coverage_todos] == [
        (pack_id, "security")
    ]
    todo = report.llm_coverage.coverage_todos[0]
    assert "reviewed only shallowly" in todo.reason
    assert "--only-pack" in todo.suggested_command
    assert "--reviewer security" in todo.suggested_command
    assert "--include-reviewed --continue-review-depth deep" in todo.suggested_command
    assert "Reviewer assignment debt: P0 0, P1 0, P2 0" in render_coverage_summary_lines(report.llm_coverage)


def test_continue_command_for_pack_can_use_the_locked_runtime_launcher() -> None:
    command = continue_command_for_pack(
        "src/auth.ts#authorize:1",
        ".apex-ray/reports/pre-push.json",
        "security",
        json_output_path=".apex-ray/reports/pre-push.json",
        review_depth_upgrade=True,
        launcher_version="0.1.17",
        platform_name="posix",
    )

    assert command == (
        "uvx --python 3.14 apex-ray@0.1.17 review --continue-from .apex-ray/reports/pre-push.json "
        "--only-pack 'src/auth.ts#authorize:1' --llm --reviewer security --include-reviewed "
        "--continue-review-depth deep --json .apex-ray/reports/pre-push.json"
    )


def test_continue_command_for_pack_can_use_the_source_runtime_launcher() -> None:
    command = continue_command_for_pack(
        "src/auth.ts#authorize:1",
        ".apex-ray/reports/pre-push.json",
        "security",
        launcher=ApexRayLauncher.source(),
        platform_name="posix",
    )

    assert command.startswith("uv run --locked apex-ray review --continue-from .apex-ray/reports/pre-push.json ")
    assert "--only-pack 'src/auth.ts#authorize:1' --llm --reviewer security" in command


def test_successful_reviewer_retry_clears_prior_review_and_verification_failures() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig(reviewers=[ReviewerConfig(id="security", focus="Authorization.", required=True)])
    config.llm.enabled = True
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )

    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE)],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="failed_auth",
                duration_ms=1,
            ),
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="provider_error",
                duration_ms=1,
            ),
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
                findings_count=0,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )

    reviewer = report.llm_coverage.reviewers[0]
    assert reviewer.status == "pass"
    assert reviewer.failed_review_runs == 0
    assert reviewer.failed_verify_runs == 0
    assert report.llm_coverage.quality_gate_status == "pass"
    assert report.llm_coverage.partial_severity == "none"
    assert report.llm_coverage.completion_status == "complete"


def test_reviewer_usage_excludes_superseded_disabled_verify_and_out_of_scope_runs() -> None:
    selected_pack_id = "src/auth.ts#authorize:1"
    outside_pack_id = "src/outside.ts#legacy:1"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization.",
                paths=["src/auth.ts"],
                verify=False,
            )
        ]
    )
    config.llm.enabled = True
    selection = LLMContextSelection(
        total_context_pack_ids=[selected_pack_id],
        selected_context_pack_ids=[selected_pack_id],
        deep_selected_context_pack_ids=[selected_pack_id],
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[
            ContextPack(id=selected_pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE),
            ContextPack(id=outside_pack_id, file="src/outside.ts", file_kind=FileKind.SOURCE),
        ],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=selected_pack_id,
                status="failed_auth",
                duration_ms=1,
                estimated_input_tokens=10,
                actual_total_tokens=10,
                estimated_cost_usd=0.1,
            ),
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=selected_pack_id,
                status="ok",
                duration_ms=1,
                estimated_input_tokens=20,
                actual_total_tokens=20,
                estimated_cost_usd=0.2,
            ),
            LLMRun(
                kind="verify",
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=selected_pack_id,
                status="provider_error",
                duration_ms=1,
                estimated_input_tokens=40,
                actual_total_tokens=40,
                estimated_cost_usd=0.4,
            ),
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=outside_pack_id,
                status="ok",
                duration_ms=1,
                estimated_input_tokens=80,
                actual_total_tokens=80,
                estimated_cost_usd=0.8,
            ),
        ],
        reviewer_selections={"security": selection},
    )

    reviewer = report.llm_coverage.reviewers[0]
    assert reviewer.status == "pass"
    assert reviewer.failed_review_runs == 0
    assert reviewer.failed_verify_runs == 0
    assert reviewer.estimated_input_tokens == 20
    assert reviewer.actual_total_tokens == 20
    assert reviewer.estimated_cost_usd == 0.2


def test_unselected_pack_verifier_failure_does_not_fail_reviewer_coverage() -> None:
    selected_pack_id = "src/auth.ts#authorize:1"
    unselected_pack_id = "docs/session-refresh.md#file"
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
    selection = LLMContextSelection(
        total_context_pack_ids=[selected_pack_id, unselected_pack_id],
        selected_context_pack_ids=[selected_pack_id],
        deep_selected_context_pack_ids=[selected_pack_id],
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[
            ContextPack(id=selected_pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE),
            ContextPack(id=unselected_pack_id, file="docs/session-refresh.md", file_kind=FileKind.DOCS),
        ],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=selected_pack_id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                kind="verify",
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=unselected_pack_id,
                status="failed_rate_limit",
                duration_ms=1,
                estimated_input_tokens=40,
                actual_total_tokens=40,
            ),
        ],
        reviewer_selections={"security": selection},
    )

    reviewer = report.llm_coverage.reviewers[0]
    assert reviewer.status == "pass"
    assert reviewer.failed_verify_runs == 0
    assert reviewer.actual_total_tokens == 0
    assert all("failed verification run" not in reason for reason in reviewer.reasons)
    assert report.llm_coverage.partial_severity == "minor"
    assert report.llm_coverage.completion_status == "partial"
    assert all("verifier run" not in reason for reason in report.llm_coverage.partial_reasons)


def test_sibling_verifier_failure_is_not_hidden_by_later_success() -> None:
    pack_id = "src/auth.ts#authorize:1"
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
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )

    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE)],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
                findings_count=2,
            ),
            LLMRun(
                kind="verify",
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=pack_id,
                profile="strong",
                status="failed_rate_limit",
                duration_ms=1,
            ),
            LLMRun(
                kind="verify",
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=pack_id,
                profile="default",
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )

    reviewer = report.llm_coverage.reviewers[0]
    assert reviewer.status == "fail"
    assert reviewer.failed_verify_runs == 1
    assert report.llm_coverage.quality_gate_status == "fail"
    assert report.llm_coverage.partial_severity == "critical"
    assert any("failed verification run" in reason for reason in reviewer.reasons)
    assert report.llm_coverage.coverage_todos[0].reviewer_id == "security"
    assert "--reviewer security" in report.llm_coverage.coverage_todos[0].suggested_command


def test_active_decisions_must_cover_every_reported_finding_without_verify_run() -> None:
    pack_id = "src/auth.ts#authorize:1"
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
    findings = [
        Finding(
            title=f"Authorization bypass {index}",
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.HIGH,
            file="src/auth.ts",
            line=10 + (index * 90),
            failure_mode=f"Authorization path {index} bypasses its tenant guard.",
            evidence=f"Branch {index} executes before the tenant check.",
            suggested_fix="Move the branch after the tenant check.",
            suggested_test=f"Reject unauthorized path {index}.",
            context_pack_id=pack_id,
            reviewer_ids=["security"],
        )
        for index in range(2)
    ]
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )

    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[
            ContextPack(
                id=pack_id,
                file="src/auth.ts",
                file_kind=FileKind.SOURCE,
            )
        ],
        findings=findings,
        verifications=[
            FindingVerification(
                finding=findings[0],
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Only the first finding was verified.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
                findings_count=2,
            )
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )

    reviewer = report.llm_coverage.reviewers[0]
    assert reviewer.status == "fail"
    assert report.llm_coverage.quality_gate_status == "fail"
    assert any("unresolved verification subjects" in reason for reason in reviewer.reasons)
    assert [(todo.context_pack_id, todo.reviewer_id) for todo in report.llm_coverage.coverage_todos] == [
        (pack_id, "security")
    ]


def test_cross_pack_consolidation_does_not_create_false_verification_debt() -> None:
    source_pack_id = "src/auth.ts#authorize:1"
    test_pack_id = "src/auth.test.ts#authorize:1"
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
    finding = Finding(
        title="Authorization guard can be bypassed",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        failure_mode="A transfer path executes before the tenant authorization guard.",
        evidence="The changed branch returns before tenant ownership is checked.",
        suggested_fix="Move the branch after the authorization guard.",
        suggested_test="Reject a transfer for another tenant.",
        context_pack_id=source_pack_id,
        reviewer_ids=["security"],
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[source_pack_id, test_pack_id],
        selected_context_pack_ids=[source_pack_id, test_pack_id],
        deep_selected_context_pack_ids=[source_pack_id, test_pack_id],
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[
            ContextPack(
                id=source_pack_id,
                file="src/auth.ts",
                file_kind=FileKind.SOURCE,
            ),
            ContextPack(
                id=test_pack_id,
                file="src/auth.test.ts",
                file_kind=FileKind.TEST,
            ),
        ],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                reviewer_id="security",
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The canonical root cause was verified.",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=source_pack_id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=test_pack_id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=source_pack_id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )

    assert report.llm_coverage.reviewers[0].status == "pass"
    assert report.llm_coverage.quality_gate_status == "pass"
    assert report.llm_coverage.coverage_todos == []


def test_failed_selected_reviewer_keeps_reviewer_scoped_continuation() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization.",
                required=True,
                verify=False,
            ),
            ReviewerConfig(
                id="finance",
                focus="Financial impact.",
                required=True,
                verify=False,
            ),
        ]
    )
    config.llm.enabled = True
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )

    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts", file_kind=FileKind.SOURCE)],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                reviewer_id="finance",
                context_pack_id=pack_id,
                status="failed_rate_limit",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"finance": selection},
    )

    assert [(todo.context_pack_id, todo.reviewer_id) for todo in report.llm_coverage.coverage_todos] == [
        (pack_id, "finance"),
        (pack_id, "security"),
    ]
    assert all(
        f"--reviewer {reviewer_id}" in todo.suggested_command
        for todo, reviewer_id in zip(
            report.llm_coverage.coverage_todos,
            ("finance", "security"),
            strict=True,
        )
    )


def test_reviewer_verify_override_drives_reported_verification_mode() -> None:
    pack_id = "src/auth.ts#authorize:1"
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                focus="Authorization.",
                verify=True,
            )
        ]
    )
    config.llm.enabled = True
    config.llm.verify = False
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts")],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )

    assert report.llm_coverage.verify_enabled is True
    assert report.llm_coverage.reviewers[0].verify_enabled is True

    disabled_config = config.model_copy(deep=True)
    disabled_config.llm.verify = True
    disabled_config.reviewers[0].verify = False
    disabled = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        disabled_config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[ContextPack(id=pack_id, file="src/auth.ts")],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="provider_error",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"security": selection},
    )

    assert disabled.llm_coverage.verify_enabled is False
    assert disabled.llm_coverage.reviewers[0].verify_enabled is False
    assert disabled.llm_coverage.reviewers[0].status == "pass"
    assert disabled.llm_coverage.quality_gate_status == "pass"
    assert disabled.llm_coverage.partial_severity == "none"


def test_legacy_general_pack_cap_does_not_become_required_reviewer_failure() -> None:
    pack_ids = [f"src/file-{index}.ts#module:1" for index in range(65)]
    selected_ids = pack_ids[:64]
    config = ReviewConfig()
    config.llm.enabled = True
    selection = LLMContextSelection(
        total_context_pack_ids=pack_ids,
        selected_context_pack_ids=selected_ids,
        deep_selected_context_pack_ids=selected_ids,
        unselected_context_pack_ids=[pack_ids[-1]],
        skipped_context_pack_reasons={pack_ids[-1]: "not selected by LLM pack cap"},
    )

    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[
            ContextPack(id=pack_id, file=pack_id.split("#", 1)[0], file_kind=FileKind.SOURCE) for pack_id in pack_ids
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="general",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            )
            for pack_id in selected_ids
        ],
        llm_selection=selection,
        reviewer_selections={"general": selection},
    )

    assert report.llm_coverage.quality_gate_status == "warn"
    assert report.llm_coverage.partial_severity != "critical"
    assert report.llm_coverage.reviewers[0].required is False


def test_unresolved_general_verification_debt_blocks_the_default_quality_gate() -> None:
    pack_id = "src/auth.ts#authorize:1"
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.verify = True
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        line=42,
        failure_mode="A transfer can bypass its tenant guard.",
        evidence="The changed branch runs before tenant authorization.",
        suggested_fix="Run the tenant guard before dispatch.",
        suggested_test="Reject a cross-tenant transfer.",
        context_pack_id=pack_id,
    )
    selection = LLMContextSelection(
        total_context_pack_ids=[pack_id],
        selected_context_pack_ids=[pack_id],
        deep_selected_context_pack_ids=[pack_id],
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[
            ContextPack(
                id=pack_id,
                file="src/auth.ts",
                file_kind=FileKind.SOURCE,
            )
        ],
        verifications=[
            FindingVerification(
                finding=finding,
                approved=False,
                confidence=FindingConfidence.LOW,
                reason="The verifier was unavailable.",
                superseded=True,
                superseded_reason="Verification run did not complete successfully (failed_provider).",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                reviewer_id="general",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
                findings_count=1,
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="general",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            ),
        ],
        llm_selection=selection,
        reviewer_selections={"general": selection},
    )

    assert report.findings == []
    assert report.llm_coverage.reviewers[0].status == "warn"
    assert report.llm_coverage.quality_gate_status == "fail"
    assert report.llm_coverage.partial_severity == "critical"
    assert report.llm_coverage.completion_status == "partial"
    assert "unresolved verification subjects" in " ".join(report.llm_coverage.quality_gate_reasons)
    assert "Reviewer assignment debt: P0 0, P1 0, P2 0" in render_coverage_summary_lines(report.llm_coverage)
    assert evaluate_pre_push_gate(report, config.gates.pre_push).blocked is True
    markdown = render_markdown(report)
    assert "- Unresolved verification decisions: `1`" in markdown
    assert "### Unresolved" in markdown
    assert "Superseded historical decisions" not in markdown

    disabled_config = config.model_copy(deep=True)
    disabled_config.llm.verify = False
    disabled = build_report(
        report.project,
        disabled_config,
        report.diff,
        context_packs=report.context_packs,
        verifications=report.verifications,
        llm_runs=report.llm_runs,
        llm_selection=selection,
        reviewer_selections={"general": selection},
    )
    assert disabled.llm_coverage.reviewers[0].status == "pass"
    assert disabled.llm_coverage.quality_gate_status == "pass"
    assert disabled.llm_coverage.partial_severity == "none"
    assert disabled.llm_coverage.completion_status == "complete"
    assert evaluate_pre_push_gate(disabled, disabled_config.gates.pre_push).blocked is False

    llm_disabled_config = config.model_copy(deep=True)
    llm_disabled_config.llm.enabled = False
    llm_disabled = build_report(
        report.project,
        llm_disabled_config,
        report.diff,
        context_packs=report.context_packs,
        verifications=report.verifications,
        llm_runs=report.llm_runs,
        llm_selection=selection,
        reviewer_selections={"general": selection},
    )
    assert llm_disabled.llm_coverage.reviewers[0].status == "not_applicable"
    assert llm_disabled.llm_coverage.quality_gate_status == "disabled"
    assert llm_disabled.llm_coverage.partial_severity == "none"
    assert evaluate_pre_push_gate(llm_disabled, llm_disabled_config.gates.pre_push).blocked is False


def test_render_markdown_summarizes_llm_pack_selection() -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=2)),
        context_packs=[
            ContextPack(id="src/cart.ts#calculateTotal:1", file="src/cart.ts", file_kind=FileKind.SOURCE),
            ContextPack(id="src/cart.test.ts#test:1", file="src/cart.test.ts", file_kind=FileKind.TEST),
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                model="codex-cheap",
                effort="low",
                profile="cheap",
                route_reason="profile:cheap",
                context_pack_id="src/cart.ts#calculateTotal:1",
                status="ok",
                duration_ms=1,
            )
        ],
    )

    markdown = render_markdown(report)
    data = report.model_dump(mode="json")

    assert "- Review context packs: `1` of `2`" in markdown
    assert "- Skipped context packs: `1`" in markdown
    assert "## LLM Coverage" in markdown
    assert "- Completion status: `partial`" in markdown
    assert "effort: `low`" in markdown
    assert "- Unreviewed context packs: `1`" in markdown
    assert "- Slice coverage:" in markdown
    assert "`source` - reviewed `1/1`" in markdown
    assert "`tests` - reviewed `0/1`" in markdown
    assert data["llm_coverage"]["slice_coverage"][0]["slice"] == "source"
    assert data["llm_coverage"]["slice_coverage"][0]["reviewed_context_packs"] == 1
    assert data["llm_coverage"]["slice_coverage"][1]["slice"] == "tests"
    assert data["llm_coverage"]["slice_coverage"][1]["unreviewed_context_packs"] == 1
    assert "profile: `cheap`" in markdown
    assert "model: `codex-cheap`" in markdown
    assert "route: `profile:cheap`" in markdown


def test_render_markdown_exposes_llm_coverage_blind_spots() -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.max_packs = 1
    config.context.max_pack_chars = 100
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=3)),
        context_packs=[
            ContextPack(
                id="src/auth.ts#login:1",
                file="src/auth.ts",
                stats=ContextPackStats(estimated_chars=80),
            ),
            ContextPack(
                id="src/report.ts#large:1",
                file="src/report.ts",
                stats=ContextPackStats(
                    estimated_chars=120,
                    truncated=True,
                    truncation_notes=[
                        "truncated longest reference snippet to fit context budget",
                        "truncated longest reference snippet to fit context budget",
                    ],
                ),
            ),
        ],
        findings=[
            Finding(
                title="Auth response leaks raw payload",
                severity=FindingSeverity.HIGH,
                confidence=FindingConfidence.HIGH,
                file="src/auth.ts",
                line=10,
                failure_mode="The response exposes raw upstream fields.",
                evidence="The diff returns payload directly.",
                suggested_fix="Project an explicit DTO.",
                suggested_test="Assert upstream-only fields are absent.",
                context_pack_id="src/auth.ts#login:1",
            )
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                model="codex-cheap",
                profile="cheap",
                route_reason="profile:cheap",
                context_pack_id="src/auth.ts#login:1",
                status="ok",
                duration_ms=11,
                input_chars=4000,
                estimated_input_tokens=1000,
                cache_hit=True,
                cache_key="abc",
            ),
            LLMRun(
                kind="verify",
                provider="fake",
                model="codex-strong",
                profile="strong",
                route_reason="profile:strong",
                context_pack_id="src/auth.ts#login:1",
                status="ok",
                duration_ms=17,
                input_chars=1200,
                estimated_input_tokens=300,
                cache_key="def",
                findings_count=1,
            ),
        ],
    )

    markdown = render_markdown(report)
    data = report.model_dump(mode="json")

    assert "- Over-budget packs: `1`" in markdown
    assert "- Truncated packs: `1`" in markdown
    assert "truncated longest reference snippet to fit context budget (x2)" in markdown
    assert "- Estimated LLM input: `5200` chars (`~1300` tokens)" in markdown
    assert "review/fake, status: `ok`, profile: `cheap`, model: `codex-cheap`" in markdown
    assert "verify/fake, status: `ok`, profile: `strong`, model: `codex-strong`" in markdown
    assert "`src/report.ts#large:1` - over context budget" in markdown
    assert "Context pack: `src/auth.ts#login:1`" in markdown
    assert data["llm_coverage"]["reviewed_context_packs"] == 1
    assert data["llm_coverage"]["estimated_input_tokens"] == 1300
    assert data["llm_coverage"]["cache_hits"] == 1
    assert data["llm_coverage"]["cache_misses"] == 1
    assert data["llm_coverage"]["unreviewed_context_pack_ids"] == ["src/report.ts#large:1"]
    assert data["llm_coverage"]["unreviewed_context_pack_reasons"] == {"src/report.ts#large:1": "over context budget"}
    assert data["llm_coverage"]["over_budget_context_pack_ids"] == ["src/report.ts#large:1"]


def test_report_uses_explicit_batch_cache_counters() -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[ContextPack(id="src/auth.ts#file", file="src/auth.ts")],
        llm_runs=[
            LLMRun(
                kind="verify",
                provider="fake",
                model="codex-strong",
                profile="strong",
                context_pack_id="src/auth.ts#file",
                status="ok",
                duration_ms=1,
                cache_hits=2,
                cache_misses=1,
            )
        ],
    )

    data = report.model_dump(mode="json")

    assert data["llm_coverage"]["cache_hits"] == 2
    assert data["llm_coverage"]["cache_misses"] == 1
    assert data["llm_coverage"]["routes"][0]["cache_hits"] == 2
    assert data["llm_coverage"]["routes"][0]["cache_misses"] == 1


def test_report_aggregates_provider_reported_usage() -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[ContextPack(id="src/auth.ts#file", file="src/auth.ts")],
        llm_runs=[
            LLMRun(
                provider="codex_cli",
                model="gpt-5-codex",
                context_pack_id="src/auth.ts#file",
                status="ok",
                duration_ms=12,
                input_chars=400,
                estimated_input_tokens=100,
                actual_input_tokens=90,
                actual_cached_input_tokens=30,
                actual_output_tokens=12,
                actual_reasoning_output_tokens=8,
                actual_total_tokens=110,
                estimated_saved_input_tokens=50,
                estimated_cost_usd=0.001,
                usage_source="codex_cli_json",
            )
        ],
    )

    markdown = render_markdown(report)
    data = report.model_dump(mode="json")
    coverage = data["llm_coverage"]
    route = coverage["routes"][0]

    assert "- Provider-reported LLM tokens: `110` total" in markdown
    assert "- Estimated cache-saved input: `~50` tokens" in markdown
    assert coverage["actual_total_tokens"] == 110
    assert coverage["actual_cached_input_tokens"] == 30
    assert coverage["estimated_saved_input_tokens"] == 50
    assert coverage["estimated_cost_usd"] == 0.001
    assert coverage["usage_sources"] == ["codex_cli_json"]
    assert route["actual_total_tokens"] == 110
    assert route["estimated_saved_input_tokens"] == 50
    assert route["usage_sources"] == ["codex_cli_json"]


def test_report_summarizes_repo_memory() -> None:
    config = ReviewConfig(
        memory_definitions=[
            MemoryCard(
                id="cart-total",
                title="Preserve cart totals",
                kind=MemoryKind.INVARIANT,
                body="Cart totals must include quantity.",
            )
        ]
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[
            ContextPack(
                id="src/cart.ts#calculateTotal:1",
                file="src/cart.ts",
                memory_matches=[
                    MemoryMatch(
                        id="cart-total",
                        title="Preserve cart totals",
                        kind=MemoryKind.INVARIANT,
                        severity=FindingSeverity.HIGH,
                        applies_to="both",
                        rendered="[memory:cart-total] Preserve cart totals\nCart totals must include quantity.",
                        prompt_chars=75,
                    )
                ],
                memory_omissions=[
                    MemoryOmission(
                        id="cart-fp",
                        title="Cart FP",
                        kind=MemoryKind.FALSE_POSITIVE,
                        reason="memory character budget exceeded",
                    )
                ],
                stats=ContextPackStats(memory_cards=1, memory_chars=75, estimated_chars=200),
            )
        ],
    )

    markdown = render_markdown(report)
    data = report.model_dump(mode="json")

    assert "## Memory" in markdown
    assert "- Loaded cards: `1`" in markdown
    assert "- Applied card ids: `cart-total`" in markdown
    assert "`cart-fp` - memory character budget exceeded" in markdown
    assert "memory: `1`" in markdown
    assert data["memory_summary"] == {
        "enabled": True,
        "loaded_cards": 1,
        "matched_cards": 2,
        "applied_cards": 1,
        "omitted_cards": 1,
        "applied_card_ids": ["cart-total"],
        "omitted_card_reasons": {"cart-fp": "memory character budget exceeded"},
        "total_prompt_chars": 75,
    }


def test_report_json_exposes_residual_risk_gate_and_file_coverage() -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.max_packs = 1
    config.context.max_pack_chars = 100
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=2)),
        context_packs=[
            ContextPack(
                id="src/auth.ts#cluster:login,logout",
                file="src/auth.ts",
                file_kind=FileKind.SOURCE,
                stats=ContextPackStats(estimated_chars=80),
            ),
            ContextPack(
                id="src/payments.ts#file",
                file="src/payments.ts",
                file_kind=FileKind.SOURCE,
                risk_signals=[
                    RiskSignal(
                        kind="persistence",
                        severity=RiskSeverity.HIGH,
                        reason="State mutation changed.",
                        file="src/payments.ts",
                    )
                ],
                stats=ContextPackStats(estimated_chars=120),
            ),
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                context_pack_id="src/auth.ts#cluster:login,logout",
                status="ok",
                duration_ms=1,
            )
        ],
    )

    markdown = render_markdown(report)
    data = report.model_dump(mode="json")

    assert "- Coverage quality gate: `fail`" in markdown
    assert "- Residual P0 packs: `1`" in markdown
    assert data["llm_coverage"]["coverage_ratio"] == 0.5
    assert data["llm_coverage"]["quality_gate_status"] == "fail"
    assert data["llm_coverage"]["partial_severity"] == "critical"
    assert data["llm_coverage"]["residual_risk_p0_context_pack_ids"] == ["src/payments.ts#file"]
    assert data["llm_coverage"]["residual_risk_context_packs"][0]["priority"] == "p0"
    assert data["llm_coverage"]["pack_statuses"][1]["status"] == "skipped_context_too_large"
    assert data["llm_coverage"]["coverage_todos"][0]["context_pack_id"] == "src/payments.ts#file"
    assert data["llm_coverage"]["coverage_todos"][0]["priority"] == "p0"
    assert "Coverage todo:" in markdown
    assert "apex-ray review --continue-from '<report.json>' --only-pack" in markdown
    assert data["llm_coverage"]["cluster_context_packs"] == 1
    assert data["llm_coverage"]["file_context_packs"] == 1
    assert data["llm_coverage"]["file_coverage"] == [
        {
            "file": "src/auth.ts",
            "file_kind": "source",
            "total_context_packs": 1,
            "reviewed_context_packs": 1,
            "unreviewed_context_packs": 0,
            "cluster_context_packs": 1,
            "file_context_packs": 0,
            "symbol_context_packs": 0,
            "over_budget_context_packs": 0,
            "truncated_context_packs": 0,
            "risk_by_severity": {},
            "residual_priority": None,
            "reviewed_changed_lines": [],
            "unreviewed_changed_lines": [],
            "reviewed_changed_symbols": [],
            "unreviewed_changed_symbols": [],
            "reviewed_context_pack_ids": ["src/auth.ts#cluster:login,logout"],
            "unreviewed_context_pack_ids": [],
        },
        {
            "file": "src/payments.ts",
            "file_kind": "source",
            "total_context_packs": 1,
            "reviewed_context_packs": 0,
            "unreviewed_context_packs": 1,
            "cluster_context_packs": 0,
            "file_context_packs": 1,
            "symbol_context_packs": 0,
            "over_budget_context_packs": 1,
            "truncated_context_packs": 0,
            "risk_by_severity": {"high": 1},
            "residual_priority": "p0",
            "reviewed_changed_lines": [],
            "unreviewed_changed_lines": [],
            "reviewed_changed_symbols": [],
            "unreviewed_changed_symbols": [],
            "reviewed_context_pack_ids": [],
            "unreviewed_context_pack_ids": ["src/payments.ts#file"],
        },
    ]


def test_report_counts_shallow_review_and_applies_coverage_thresholds() -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.min_source_line_coverage = 0.75
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[
            ContextPack(
                id="src/payments.ts#authorize:1",
                file="src/payments.ts",
                file_kind=FileKind.SOURCE,
                changed_lines=[(10, 19)],
            ),
            ContextPack(
                id="src/payments.ts#capture:1",
                file="src/payments.ts",
                file_kind=FileKind.SOURCE,
                changed_lines=[(20, 29)],
            ),
        ],
        llm_runs=[
            LLMRun(
                kind="review_shallow",
                provider="fake",
                context_pack_id="src/payments.ts#authorize:1",
                status="ok",
                duration_ms=1,
            )
        ],
    )

    markdown = render_markdown(report)
    data = report.model_dump(mode="json")

    assert "- Deep/shallow reviewed packs: `0` / `1`" in markdown
    assert "- Source changed-line coverage: `50.0%`" in markdown
    assert data["llm_coverage"]["reviewed_context_packs"] == 1
    assert data["llm_coverage"]["shallow_reviewed_context_pack_ids"] == ["src/payments.ts#authorize:1"]
    assert data["llm_coverage"]["source_changed_line_coverage_ratio"] == 0.5
    assert data["llm_coverage"]["quality_gate_status"] == "fail"
    assert data["llm_coverage"]["quality_gate_reasons"][-1] == (
        "Source changed-line coverage below threshold: 50.0% < 75.0%"
    )


def test_report_treats_failed_review_run_as_unreviewed_pack() -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
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
                provider="fake",
                context_pack_id="src/auth.ts#login:1",
                status="failed_quota",
                duration_ms=12,
                error="usage limit",
            )
        ],
    )

    data = report.model_dump(mode="json")

    assert data["llm_coverage"]["reviewed_context_packs"] == 0
    assert data["llm_coverage"]["failed_review_runs"] == 1
    assert data["llm_coverage"]["partial_severity"] == "critical"
    assert "1 review run(s) failed (failed_quota: 1)" in data["llm_coverage"]["partial_reasons"]
    assert data["llm_coverage"]["unreviewed_context_pack_reasons"] == {"src/auth.ts#login:1": "failed_quota"}
    assert data["llm_coverage"]["pack_statuses"][0]["status"] == "failed_quota"


def test_report_coverage_thresholds_do_not_fail_when_denominator_is_empty() -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    config.llm.min_source_line_coverage = 1.0
    config.llm.min_high_risk_coverage = 1.0
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[
            ContextPack(
                id="README.md#file",
                file="README.md",
                file_kind=FileKind.DOCS,
            )
        ],
        llm_runs=[
            LLMRun(
                kind="review_shallow",
                provider="fake",
                context_pack_id="README.md#file",
                status="ok",
                duration_ms=1,
            )
        ],
    )

    data = report.model_dump(mode="json")

    assert data["llm_coverage"]["source_changed_line_coverage_ratio"] == 1.0
    assert data["llm_coverage"]["high_risk_coverage_ratio"] == 1.0
    assert data["llm_coverage"]["quality_gate_status"] == "pass"


def test_report_file_coverage_exposes_reviewed_and_unreviewed_changed_ranges() -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[
            ContextPack(
                id="src/payments.ts#authorizePayment:1",
                file="src/payments.ts",
                file_kind=FileKind.SOURCE,
                changed_lines=[(10, 12)],
                symbol=AnalyzerSymbol(name="authorizePayment", kind="function", startLine=10, endLine=20),
            ),
            ContextPack(
                id="src/payments.ts#runSettlement:2",
                file="src/payments.ts",
                file_kind=FileKind.SOURCE,
                changed_lines=[(50, 53)],
                symbol=AnalyzerSymbol(name="runSettlement", kind="function", startLine=45, endLine=60),
            ),
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                context_pack_id="src/payments.ts#authorizePayment:1",
                status="ok",
                duration_ms=1,
            )
        ],
    )

    file_coverage = report.model_dump(mode="json")["llm_coverage"]["file_coverage"][0]
    markdown = render_markdown(report)

    assert file_coverage["reviewed_changed_lines"] == [[10, 12]]
    assert file_coverage["unreviewed_changed_lines"] == [[50, 53]]
    assert file_coverage["reviewed_changed_symbols"] == ["authorizePayment"]
    assert file_coverage["unreviewed_changed_symbols"] == ["runSettlement"]
    assert "changed lines: `3` reviewed / `4` unreviewed" in markdown


def test_report_file_coverage_subtracts_reviewed_overlapping_ranges() -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        context_packs=[
            ContextPack(
                id="src/payments.ts#cluster:authorizePayment",
                file="src/payments.ts",
                file_kind=FileKind.SOURCE,
                changed_lines=[(10, 20)],
                symbol=AnalyzerSymbol(name="authorizePayment", kind="function", startLine=10, endLine=20),
            ),
            ContextPack(
                id="src/payments.ts#authorizePayment:1",
                file="src/payments.ts",
                file_kind=FileKind.SOURCE,
                changed_lines=[(15, 16)],
                symbol=AnalyzerSymbol(name="authorizePayment", kind="function", startLine=15, endLine=16),
            ),
            ContextPack(
                id="src/payments.ts#runSettlement:2",
                file="src/payments.ts",
                file_kind=FileKind.SOURCE,
                changed_lines=[(30, 33)],
                symbol=AnalyzerSymbol(name="runSettlement", kind="function", startLine=30, endLine=33),
            ),
        ],
        llm_runs=[
            LLMRun(
                provider="fake",
                context_pack_id="src/payments.ts#cluster:authorizePayment",
                status="ok",
                duration_ms=1,
            )
        ],
    )

    file_coverage = report.model_dump(mode="json")["llm_coverage"]["file_coverage"][0]

    assert file_coverage["reviewed_changed_lines"] == [[10, 20]]
    assert file_coverage["unreviewed_changed_lines"] == [[30, 33]]
    assert file_coverage["reviewed_changed_symbols"] == ["authorizePayment"]
    assert file_coverage["unreviewed_changed_symbols"] == ["runSettlement"]
