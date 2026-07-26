from apex_ray.models import (
    ContextPack,
    DiffSummary,
    FileKind,
    Finding,
    FindingConfidence,
    FindingSeverity,
    FindingVerification,
    LLMContextSelection,
    LLMRun,
    ProjectProfile,
    ReviewConfig,
    ReviewerConfig,
    TargetMode,
)
from apex_ray.report import build_report, render_html


def test_render_html_attributes_findings_routes_and_coverage_to_reviewers() -> None:
    pack_id = "src/settlement.ts#settle:42"
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(
                id="security",
                name="Security <Lead>",
                focus="Authorization & trust boundaries.",
                required=True,
            ),
            ReviewerConfig(id="finance", name="Finance", focus="Money movement."),
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
        context_packs=[ContextPack(id=pack_id, file="src/settlement.ts", file_kind=FileKind.SOURCE)],
        findings=[
            finding := Finding(
                title="Settlement bypass",
                severity=FindingSeverity.CRITICAL,
                confidence=FindingConfidence.HIGH,
                file="src/settlement.ts",
                line=42,
                failure_mode="Ownership is not checked.",
                evidence="The settlement branch returns early.",
                suggested_fix="Check ownership first.",
                suggested_test="Add a cross-account test.",
                context_pack_id=pack_id,
                reviewer_ids=["security", "<unsafe&reviewer>"],
            )
        ],
        verifications=[
            FindingVerification(
                finding=finding,
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="The ownership bypass is reproducible.",
                reviewer_id="security",
            ),
            FindingVerification(
                finding=finding,
                approved=False,
                confidence=FindingConfidence.MEDIUM,
                reason="Needs <manual> confirmation.",
            ),
        ],
        llm_runs=[
            LLMRun(
                provider="openai_api",
                model="gpt-review",
                reviewer_id="security",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=10,
                estimated_input_tokens=123,
                actual_input_tokens=55,
                actual_output_tokens=22,
                actual_total_tokens=77,
                estimated_cost_usd=0.012345,
                findings_count=1,
            ),
            LLMRun(
                provider="anthropic_api",
                model="claude-review",
                reviewer_id="finance",
                context_pack_id=pack_id,
                status="provider_error",
                duration_ms=20,
                estimated_input_tokens=50,
                error="temporary failure",
            ),
        ],
        reviewer_selections={"security": selection, "finance": selection},
    )

    html = render_html(report)

    assert "<h2>Focused Reviewers</h2>" in html
    assert "Security &lt;Lead&gt;" in html
    assert "Authorization &amp; trust boundaries." in html
    assert "<code>required</code>" in html
    assert "<code>optional</code>" in html
    assert "<code>pass</code>" in html
    assert "<code>warn</code>" in html
    assert "1 selected / 1 reviewed / 1 matching" in html
    assert "1 selected / 0 reviewed / 1 matching" in html
    assert "Verification: <code>enabled</code>" in html
    assert "~123 estimated input tokens; 77 actual tokens; $0.012345 estimated cost" in html
    assert "Reviewer <code>security</code>" in html
    assert "Reviewer <code>finance</code>" in html
    assert "<h2>Verifier Decisions</h2>" in html
    assert "Reviewer <code>security</code>: 1 approved / 0 rejected" in html
    assert "Reviewer <code>general</code>: 0 approved / 1 rejected" in html
    assert "<td><code>security</code></td>" in html
    assert "<td><code>approved</code></td>" in html
    assert "<td><code>rejected</code></td>" in html
    assert "The ownership bypass is reproducible." in html
    assert "Needs &lt;manual&gt; confirmation." in html
    assert ("<strong>Reviewers:</strong> <code>security</code>, <code>&lt;unsafe&amp;reviewer&gt;</code>") in html
    assert "<unsafe&reviewer>" not in html
    assert "<Lead>" not in html
    assert "<manual>" not in html
