from apex_ray.models import (
    AnalyzerSymbol,
    ContextPack,
    ContextPackStats,
    DiffStats,
    DiffSummary,
    FileKind,
    Finding,
    FindingConfidence,
    FindingSeverity,
    LLMRun,
    MemoryCard,
    MemoryKind,
    MemoryMatch,
    MemoryOmission,
    ProjectProfile,
    ReviewConfig,
    RiskSeverity,
    RiskSignal,
    TargetMode,
)
from apex_ray.report import build_report, render_markdown


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
    assert "apex-ray review --continue-from <report.json> --only-pack" in markdown
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
