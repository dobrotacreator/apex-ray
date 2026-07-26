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


def test_render_sarif_checks_containment_for_mixed_separator_absolute_posix_paths() -> None:
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=2)),
        findings=[
            _finding(
                title="Inside mixed path",
                severity=FindingSeverity.HIGH,
                file=r"/workspace/repo\src\service.ts",
                line=4,
            ),
            _finding(
                title="Outside mixed path",
                severity=FindingSeverity.HIGH,
                file=r"/private/company\secret.ts",
                line=8,
            ),
        ],
    )

    rendered = render_sarif(report)
    results = {
        result["message"]["text"].splitlines()[0]: result for result in json.loads(rendered)["runs"][0]["results"]
    }

    assert results["Inside mixed path"]["locations"] == [
        {
            "physicalLocation": {
                "artifactLocation": {"uri": "src/service.ts"},
                "region": {"startColumn": 1, "startLine": 4},
            }
        }
    ]
    assert "locations" not in results["Outside mixed path"]
    assert "/private/company" not in rendered
    assert r"\private\company" not in rendered


def test_render_sarif_omits_uncontained_windows_rooted_and_drive_relative_paths() -> None:
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=2)),
        findings=[
            _finding(
                title="Root-relative Windows path",
                severity=FindingSeverity.HIGH,
                file=r"\private\company\secret.ts",
                line=4,
            ),
            _finding(
                title="Drive-relative Windows path",
                severity=FindingSeverity.HIGH,
                file=r"C:private\company\secret.ts",
                line=8,
            ),
        ],
    )

    rendered = render_sarif(report)
    results = {
        result["message"]["text"].splitlines()[0]: result for result in json.loads(rendered)["runs"][0]["results"]
    }

    assert "locations" not in results["Root-relative Windows path"]
    assert "locations" not in results["Drive-relative Windows path"]
    assert r"\private\company" not in rendered
    assert "C:private" not in rendered


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


def test_render_sarif_keeps_outside_file_fingerprints_distinct_without_exposing_paths() -> None:
    first = _finding(
        title="Outside duplicate",
        severity=FindingSeverity.HIGH,
        file="/private/tenant-a/service.ts",
        line=8,
    )
    second = first.model_copy(update={"file": "/private/tenant-b/service.ts"})
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=2)),
        findings=[first, second],
    )

    rendered = render_sarif(report)
    results = json.loads(rendered)["runs"][0]["results"]
    fingerprints = {result["partialFingerprints"]["apexRayFinding/v1"] for result in results}

    assert len(fingerprints) == 2
    assert "/private/tenant-a" not in rendered
    assert "/private/tenant-b" not in rendered


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


def test_render_sarif_redacts_windows_root_and_drive_relative_paths_from_finding_text() -> None:
    finding = _finding(
        title="Anchored Windows path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "failure_mode": r"Reads private data from \Users\alice\corp\secret.ts.",
            "evidence": (
                r"Drive-relative trace C:Users\alice\corp\secret.ts. "
                "The prose label C:Users remains useful."
            ),
        }
    )
    report = build_report(
        ProjectProfile(root=r"C:\workspace\repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            stats=DiffStats(files_changed=1),
        ),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert r"\Users\alice" not in message
    assert r"C:Users\alice" not in message
    assert message.count("<absolute-path>") == 2
    assert "The prose label C:Users remains useful." in message


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


def test_render_sarif_does_not_redact_remote_urls_for_repo_root_substrings() -> None:
    finding = _finding(
        title="Root substring URL evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Safe https://example.com/application/status. "
                "Asset https://example.com/app.css. "
                "Local https://example.com/open?file=%2Fapp%2Fsrc%2Fservice.ts."
            )
        }
    )
    report = build_report(
        ProjectProfile(root="/app", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert "https://example.com/application/status." in message
    assert "https://example.com/app.css." in message
    assert message.count("<remote-url-with-local-path>") == 1


def test_render_sarif_redacts_absolute_posix_paths_from_arbitrary_url_query_keys() -> None:
    finding = _finding(
        title="Arbitrary query path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Local https://logs.example/view?location=%2Fhome%2Frunner%2Fprivate%2Ftrace.json. "
                "Safe https://logs.example/view?location=docs%2Ftrace.json."
            )
        }
    )
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert message.count("<remote-url-with-local-path>") == 1
    assert "%2Fhome%2Frunner" not in message
    assert "https://logs.example/view?location=docs%2Ftrace.json." in message


def test_render_sarif_redacts_repo_paths_from_url_query_keys() -> None:
    finding = _finding(
        title="Query key path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Local "
                "https://logs.example/view?%2Fhome%2Frunner%2Fwork%2Fproject%2Fsecret=value. "
                "Safe https://logs.example/view?project%2Fsecret=value."
            )
        }
    )
    report = build_report(
        ProjectProfile(root="/home/runner/work/project", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert message.count("<remote-url-with-local-path>") == 1
    assert "%2Fhome%2Frunner%2Fwork%2Fproject" not in message
    assert "https://logs.example/view?project%2Fsecret=value." in message


def test_render_sarif_redacts_repo_root_urls_with_terminal_punctuation_or_line_suffix() -> None:
    finding = _finding(
        title="Terminal root path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Root https://logs.example/open/%2Fhome%2Frunner%2Fwork%2Fproject. "
                "Line https://logs.example/open/%2Fhome%2Frunner%2Fwork%2Fproject%3A42. "
                "Safe https://logs.example/open/project.css."
            )
        }
    )
    report = build_report(
        ProjectProfile(root="/home/runner/work/project", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert message.count("<remote-url-with-local-path>") == 2
    assert "%2Fhome%2Frunner%2Fwork%2Fproject" not in message
    assert "https://logs.example/open/project.css." in message


def test_render_sarif_redacts_repeatedly_encoded_repo_paths_in_url_components() -> None:
    finding = _finding(
        title="Nested encoded path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Path https://logs.example/open/%252Fapp%252Fsrc%252Fservice.ts. "
                "Query key https://logs.example/view?%25252Fapp%25252Fsecret=value. "
                "Over-nested https://logs.example/open/%252525252Fapp%252525252Fsecret.ts. "
                "Safe https://logs.example/open/%252Fapp.css."
            )
        }
    )
    report = build_report(
        ProjectProfile(root="/app", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert message.count("<remote-url-with-local-path>") == 3
    assert "%252Fapp%252Fsrc" not in message
    assert "%25252Fapp%25252Fsecret" not in message
    assert "%252525252Fapp%252525252Fsecret" not in message
    assert "https://logs.example/open/%252Fapp.css." in message


def test_render_sarif_redacts_encoded_windows_root_and_drive_relative_url_queries() -> None:
    finding = _finding(
        title="Encoded Windows query evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Root relative "
                "https://example.com/open?path=%5CUsers%5Calice%5Ccorp%5Csecret.ts. "
                "Drive relative "
                "https://example.com/open?file=C%3AUsers%5Calice%5Ccorp%5Csecret.ts. "
                "Safe label https://example.com/open?path=C%3AUsers."
            )
        }
    )
    report = build_report(
        ProjectProfile(root=r"C:\workspace\repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            stats=DiffStats(files_changed=1),
        ),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert message.count("<remote-url-with-local-path>") == 2
    assert r"\Users\alice" not in message
    assert r"C:Users\alice" not in message
    assert "%5CUsers%5Calice" not in message
    assert "C%3AUsers%5Calice" not in message
    assert "https://example.com/open?path=C%3AUsers." in message


def test_render_sarif_redacts_encoded_windows_paths_from_url_paths_and_arbitrary_queries() -> None:
    finding = _finding(
        title="Encoded Windows URL path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Root-relative URL path "
                "https://example.com/open/%5CUsers%5Calice%5Ccorp%5Csecret.ts. "
                "Drive-relative URL path "
                "https://example.com/open/C%3AUsers%5Calice%5Ccorp%5Csecret.ts. "
                "Arbitrary query key "
                "https://example.com/open?redirect=%5CUsers%5Calice%5Ccorp%5Csecret.ts. "
                "Safe URL https://example.com/docs/C%3AUsers."
            )
        }
    )
    report = build_report(
        ProjectProfile(root=r"C:\workspace\repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            stats=DiffStats(files_changed=1),
        ),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert message.count("<remote-url-with-local-path>") == 3
    assert "%5CUsers%5Calice" not in message
    assert "C%3AUsers%5Calice" not in message
    assert "https://example.com/docs/C%3AUsers." in message


def test_render_sarif_redacts_encoded_windows_paths_from_url_authority_and_fragment() -> None:
    finding = _finding(
        title="Encoded Windows URL metadata",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Authority "
                "https://C%3AUsers%5Calice%5Ccorp%5Csecret.ts@example.com/open. "
                "Fragment "
                "https://example.com/open#file=C%3AUsers%5Calice%5Ccorp%5Csecret.ts. "
                "Safe fragment https://example.com/docs#label=C%3AUsers."
            )
        }
    )
    report = build_report(
        ProjectProfile(root=r"C:\workspace\repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            stats=DiffStats(files_changed=1),
        ),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert message.count("<remote-url-with-local-path>") == 2
    assert "C%3AUsers%5Calice" not in message
    assert "https://example.com/docs#label=C%3AUsers." in message


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
