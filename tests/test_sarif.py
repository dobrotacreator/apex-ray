import json

from apex_ray.models import (
    ContextPack,
    DiffStats,
    DiffSummary,
    Finding,
    FindingConfidence,
    FindingSeverity,
    ProjectProfile,
    ReviewConfig,
    RiskSeverity,
    RiskSignal,
    TargetMode,
)
from apex_ray.report import build_report, render_sarif


def _finding(
    *,
    title: str,
    severity: FindingSeverity,
    file: str,
    line: int | None,
    context_pack_id: str = "",
    reviewer_ids: list[str] | None = None,
) -> Finding:
    return Finding(
        title=title,
        severity=severity,
        confidence=FindingConfidence.HIGH,
        file=file,
        line=line,
        failure_mode=f"{title} can fail at runtime.",
        evidence=f"The changed code under /private/work/repo demonstrates {title}.",
        suggested_fix="Preserve the required invariant.",
        suggested_test="Add a regression test.",
        context_pack_id=context_pack_id,
        reviewer_ids=reviewer_ids or [],
    )


def test_render_sarif_is_deterministic_and_maps_levels_locations_and_metadata() -> None:
    config = ReviewConfig()
    findings = [
        _finding(
            title="Low-risk cleanup",
            severity=FindingSeverity.LOW,
            file="/tmp/outside/cleanup.ts",
            line=3,
        ),
        _finding(
            title="Medium behavior regression",
            severity=FindingSeverity.MEDIUM,
            file="src/a file.ts",
            line=None,
        ),
        _finding(
            title="Critical settlement loss",
            severity=FindingSeverity.CRITICAL,
            file="/private/work/repo/src/settlement.ts",
            line=42,
            context_pack_id="src/settlement.ts#settle:42",
            reviewer_ids=["security", "finance", "security"],
        ),
        _finding(
            title="High authorization bypass",
            severity=FindingSeverity.HIGH,
            file="src/auth.ts",
            line=7,
        ),
    ]
    report = build_report(
        ProjectProfile(root="/private/work/repo", is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=4)),
        context_packs=[
            ContextPack(
                id="src/settlement.ts#settle:42",
                file="src/settlement.ts",
                risk_signals=[
                    RiskSignal(
                        kind="policy:settlement",
                        severity=RiskSeverity.CRITICAL,
                        score=97,
                        reason="Financial boundary changed.",
                        file="/private/work/repo/src/settlement.ts",
                        line=42,
                        source="project",
                        rule_id="settlement-boundary",
                        categories=["financial", "correctness"],
                        reviewer_tags=["finance"],
                    )
                ],
            )
        ],
        findings=findings,
    )

    rendered = render_sarif(report)
    reversed_rendered = render_sarif(report.model_copy(update={"findings": list(reversed(findings))}))
    sarif = json.loads(rendered)

    assert rendered == reversed_rendered
    assert rendered.endswith("\n")
    assert sarif["version"] == "2.1.0"
    assert sarif["$schema"] == "https://json.schemastore.org/sarif-2.1.0.json"

    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "Apex Ray"
    assert run["tool"]["driver"]["rules"][0]["id"] == "APEX-RAY-REVIEW"

    results = {result["message"]["text"].splitlines()[0]: result for result in run["results"]}
    assert results["Critical settlement loss"]["level"] == "error"
    assert results["High authorization bypass"]["level"] == "error"
    assert results["Medium behavior regression"]["level"] == "warning"
    assert results["Low-risk cleanup"]["level"] == "note"

    critical_location = results["Critical settlement loss"]["locations"][0]["physicalLocation"]
    assert critical_location == {
        "artifactLocation": {"uri": "src/settlement.ts"},
        "region": {"startColumn": 1, "startLine": 42},
    }
    medium_location = results["Medium behavior regression"]["locations"][0]["physicalLocation"]
    assert medium_location == {"artifactLocation": {"uri": "src/a%20file.ts"}}
    assert "locations" not in results["Low-risk cleanup"]

    properties = results["Critical settlement loss"]["properties"]
    assert properties["severity"] == "critical"
    assert properties["confidence"] == "high"
    assert properties["reviewerIds"] == ["finance", "security"]
    assert properties["riskKinds"] == ["policy:settlement"]
    assert properties["riskSeverities"] == ["critical"]
    assert properties["riskScore"] == 97
    assert properties["riskCategories"] == ["correctness", "financial"]
    assert properties["riskReviewerTags"] == ["finance"]
    assert properties["riskSignals"] == [
        {
            "categories": ["correctness", "financial"],
            "kind": "policy:settlement",
            "reviewerTags": ["finance"],
            "ruleId": "settlement-boundary",
            "score": 97,
            "severity": "critical",
            "source": "project",
        }
    ]
    assert results["Critical settlement loss"]["partialFingerprints"].keys() == {"apexRayFinding/v1"}
    assert "/private/work/repo" not in rendered
    assert "/tmp/outside/cleanup.ts" not in rendered


def test_render_sarif_relativizes_windows_paths_and_omits_invalid_lines() -> None:
    report = build_report(
        ProjectProfile(root=r"C:\work\repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=2)),
        findings=[
            _finding(
                title="Windows location",
                severity=FindingSeverity.HIGH,
                file=r"C:\work\repo\src\service.ts",
                line=0,
            ),
            _finding(
                title="Escaping location",
                severity=FindingSeverity.MEDIUM,
                file="../outside.ts",
                line=1,
            ),
        ],
    )

    results = {
        result["message"]["text"].splitlines()[0]: result
        for result in json.loads(render_sarif(report))["runs"][0]["results"]
    }

    assert results["Windows location"]["locations"] == [
        {"physicalLocation": {"artifactLocation": {"uri": "src/service.ts"}}}
    ]
    assert "locations" not in results["Escaping location"]


def test_render_sarif_fingerprint_is_independent_of_checkout_root() -> None:
    config = ReviewConfig()
    diff = DiffSummary(
        target_mode=TargetMode.PATCH,
        stats=DiffStats(files_changed=1),
    )
    first = build_report(
        ProjectProfile(root="/home/runner/work/repo", is_git_repo=True),
        config,
        diff,
        findings=[
            _finding(
                title="Stable alert",
                severity=FindingSeverity.HIGH,
                file="/home/runner/work/repo/src/service.ts",
                line=8,
            )
        ],
    )
    second = build_report(
        ProjectProfile(root="/opt/actions/repo", is_git_repo=True),
        config,
        diff,
        findings=[
            _finding(
                title="Stable alert",
                severity=FindingSeverity.HIGH,
                file="/opt/actions/repo/src/service.ts",
                line=8,
            )
        ],
    )

    first_fingerprint = json.loads(render_sarif(first))["runs"][0]["results"][0]["partialFingerprints"]
    second_fingerprint = json.loads(render_sarif(second))["runs"][0]["results"][0]["partialFingerprints"]

    assert first_fingerprint == second_fingerprint


def test_render_sarif_redacts_posix_windows_and_unc_paths_without_corrupting_urls() -> None:
    finding = _finding(
        title="Path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                r"Path:/home/runner/work/repo/src/service.ts, "
                r"Windows C:\runner\repo\src\service.ts, "
                r"UNC \\server\share\src\service.ts. "
                "See https://example.com/review/path."
            )
        }
    )
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            stats=DiffStats(files_changed=1),
        ),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert "/home/runner" not in message
    assert r"C:\runner" not in message
    assert r"\\server\share" not in message
    assert "https://example.com/review/path." in message


def test_render_sarif_redacts_local_paths_embedded_in_urls() -> None:
    finding = _finding(
        title="URL path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Remote path https://example.com/open/private/work/repo/src/service.ts. "
                "Encoded query "
                "https://example.com/open?file=%2Fprivate%2Fwork%2Frepo%2Fsrc%2Fservice.ts. "
                "Other local query "
                "https://example.com/open?path=%2Fopt%2Fother%2Frepo%2Fsecret.ts. "
                "Editor vscode://file/private/work/repo/src/service.ts. "
                "Safe https://example.com/review/path?tab=security."
            )
        }
    )
    report = build_report(
        ProjectProfile(root="/private/work/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            stats=DiffStats(files_changed=1),
        ),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert "/private/work/repo" not in message
    assert "%2Fprivate%2Fwork%2Frepo" not in message
    assert "%2Fopt%2Fother%2Frepo" not in message
    assert message.count("<remote-url-with-local-path>") == 3
    assert "<local-url>" in message
    assert "https://example.com/review/path?tab=security." in message


def test_render_sarif_uses_total_result_order_for_duplicate_fingerprints() -> None:
    first = _finding(
        title="Duplicate fingerprint",
        severity=FindingSeverity.HIGH,
        file="src/service.ts",
        line=12,
        reviewer_ids=["security"],
    )
    second = first.model_copy(
        update={
            "evidence": "Different evidence for the same fingerprint.",
            "reviewer_ids": ["finance"],
        }
    )
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            stats=DiffStats(files_changed=1),
        ),
        findings=[first, second],
    )

    rendered = render_sarif(report)
    reversed_rendered = render_sarif(report.model_copy(update={"findings": [second, first]}))

    assert rendered == reversed_rendered
