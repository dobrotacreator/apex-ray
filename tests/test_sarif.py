import hashlib
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
        ProjectProfile(root=r"C:\repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=3)),
        findings=[
            _finding(
                title="Root-relative Windows path",
                severity=FindingSeverity.HIGH,
                file=r"\other\secret.py",
                line=4,
            ),
            _finding(
                title="Drive-relative Windows path",
                severity=FindingSeverity.HIGH,
                file=r"C:secret.py",
                line=8,
            ),
            _finding(
                title="Forward-slash rooted path",
                severity=FindingSeverity.HIGH,
                file="/other/secret.py",
                line=12,
            ),
        ],
    )

    rendered = render_sarif(report)
    results = {
        result["message"]["text"].splitlines()[0]: result for result in json.loads(rendered)["runs"][0]["results"]
    }

    assert "locations" not in results["Root-relative Windows path"]
    assert "locations" not in results["Drive-relative Windows path"]
    assert "locations" not in results["Forward-slash rooted path"]
    assert r"\other\secret.py" not in rendered
    assert "C:secret.py" not in rendered
    assert "/other/secret.py" not in rendered


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
    outside_discriminator = hashlib.sha256(f"\0{first.file}".encode()).hexdigest()[:20]
    colliding_relative = first.model_copy(update={"file": f"<outside-repository:{outside_discriminator}>"})
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=3)),
        findings=[first, second, colliding_relative],
    )

    rendered = render_sarif(report)
    results = json.loads(rendered)["runs"][0]["results"]
    fingerprints = {result["partialFingerprints"]["apexRayFinding/v1"] for result in results}

    assert len(fingerprints) == 3
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


def test_render_sarif_redacts_remote_url_credentials_and_sensitive_queries() -> None:
    finding = _finding(
        title="URL credential evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Database postgresql://alice:S3cretValue@db.example.com/app. "
                "Signed artifact "
                "https://artifacts.example.com/build.zip?"
                "X-Amz-Algorithm=AWS4-HMAC-SHA256&"
                "X-Amz-Signature=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef. "
                "Token callback https://api.example.com/callback?access_token=callback-secret. "
                "Safe username-only URL https://alice@profiles.example.com/public. "
                "Safe https://example.com/review/path?tab=security."
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

    assert "postgresql://alice" not in message
    assert "S3cretValue" not in message
    assert "0123456789abcdef" not in message
    assert "callback-secret" not in message
    assert "<remote-url-with-credentials>" in message
    assert message.count("<remote-url-with-sensitive-query>") == 2
    assert "https://alice@profiles.example.com/public." in message
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
                "Host https://app.example.com/status. "
                "Safe https://example.com/application/status. "
                "Asset https://example.com/app.css. "
                "Nested "
                "https://example.com/open?next=https%3A%2F%2Fservice.example%2Fapp%2Fcallback. "
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

    assert "https://app.example.com/status." in message
    assert "https://example.com/application/status." in message
    assert "https://example.com/app.css." in message
    assert "https://example.com/open?next=https%3A%2F%2Fservice.example%2Fapp%2Fcallback." in message
    assert message.count("<remote-url-with-local-path>") == 1


def test_render_sarif_redacts_local_paths_from_arbitrary_url_query_parameters() -> None:
    finding = _finding(
        title="Arbitrary query path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Local https://logs.example/view?location=%2Fhome%2Frunner%2Fprivate%2Ftrace.json. "
                "Drive "
                "https://logs.example/view?uri=C%3A%5CUsers%5Calice%5Cprivate%5Ctrace.json. "
                "UNC "
                "https://logs.example/view?target=%5C%5Cserver%5Cshare%5Cprivate%5Ctrace.json. "
                "Nested local "
                "https://logs.example/view?next=https%3A%2F%2Fservice.example%2Fview"
                "%3Flocation%3D%2Fhome%2Falice%2Fprivate%2Ftrace.json. "
                "Nested path "
                "https://logs.example/view?next=https%3A%2F%2Fservice.example%2Fopen"
                "%2F%252Fworkspace%252Frepo%252Fprivate%252Ftrace.json. "
                "Safe relative https://logs.example/view?location=docs%2Ftrace.json. "
                "Safe remote "
                "https://logs.example/view?next=https%3A%2F%2Fservice.example%2Fdocs%2Ftrace.json."
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

    assert message.count("<remote-url-with-local-path>") == 5
    assert "%2Fhome%2Frunner" not in message
    assert "C%3A%5CUsers%5Calice" not in message
    assert "%5C%5Cserver%5Cshare" not in message
    assert "%2Fhome%2Falice" not in message
    assert "%252Fworkspace%252Frepo" not in message
    assert "https://logs.example/view?location=docs%2Ftrace.json." in message
    assert "https://logs.example/view?next=https%3A%2F%2Fservice.example%2Fdocs%2Ftrace.json." in message


def test_render_sarif_redacts_embedded_encoded_posix_path_from_arbitrary_query_value() -> None:
    finding = _finding(
        title="Embedded query path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Local "
                "https://logs.example/view?message=trace%20at%20%2Fhome%2Falice%2Fsecret.py. "
                "Safe "
                "https://logs.example/view?message=trace%20at%20docs%2Fsecret.py."
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
    assert "%2Fhome%2Falice%2Fsecret.py" not in message
    assert "https://logs.example/view?message=trace%20at%20docs%2Fsecret.py." in message


def test_render_sarif_redacts_local_path_behind_encoded_url_delimiter() -> None:
    finding = _finding(
        title="Encoded delimiter path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Local "
                "https://logs.example/open%3Ffile=%2Fhome%2Falice%2Fsecret.py. "
                "Safe "
                "https://logs.example/open%3Ffile=docs%2Fsecret.py."
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
    assert "%2Fhome%2Falice%2Fsecret.py" not in message
    assert "https://logs.example/open%3Ffile=docs%2Fsecret.py." in message


def test_render_sarif_redacts_absolute_posix_paths_from_url_fragments() -> None:
    finding = _finding(
        title="Fragment path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Local https://logs.example/open#%2Fhome%2Falice%2Fsecret.py. "
                "Encoded delimiter "
                "https://logs.example/open%23%2Fhome%2Falice%2Fsecret.py. "
                "Safe https://logs.example/open#docs%2Fsecret.py. "
                "SPA https://app.example/#/dashboard. "
                "Docs https://docs.example/#/guides/getting-started."
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

    assert message.count("<remote-url-with-local-path>") == 2
    assert "%2Fhome%2Falice%2Fsecret.py" not in message
    assert "https://logs.example/open#docs%2Fsecret.py." in message
    assert "https://app.example/#/dashboard." in message
    assert "https://docs.example/#/guides/getting-started." in message


def test_render_sarif_redacts_explicit_absolute_posix_paths_from_url_paths() -> None:
    finding = _finding(
        title="URL route path evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Encoded "
                "https://logs.example/open/%2Fhome%2Falice%2Fsecret.py. "
                "Raw https://logs.example/open//home/alice/secret.py. "
                "System https://logs.example/open/%2Fetc%2Fpasswd. "
                "FHS https://logs.example/open/%2Fusr%2Flocal%2Fshare%2Fconfig.conf. "
                "Runtime https://logs.example/open/%2Frun%2Fservice%2Fconfig.json. "
                "Proc https://logs.example/open/%2Fproc%2Fself%2Fstatus. "
                "Safe https://logs.example/open/docs%2Fguide.md. "
                "Proxy https://logs.example/proxy//cdn.example.com/assets/app.js. "
                "Route https://logs.example/open//docs/guides/getting-started."
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

    assert message.count("<remote-url-with-local-path>") == 6
    assert "%2Fhome%2Falice" not in message
    assert "//home/alice" not in message
    assert "%2Fetc%2Fpasswd" not in message
    assert "%2Fusr%2Flocal" not in message
    assert "%2Frun%2Fservice" not in message
    assert "%2Fproc%2Fself" not in message
    assert "https://logs.example/open/docs%2Fguide.md." in message
    assert "https://logs.example/proxy//cdn.example.com/assets/app.js." in message
    assert "https://logs.example/open//docs/guides/getting-started." in message


def test_render_sarif_redacts_encoded_custom_mount_paths_without_treating_raw_authorities_as_local() -> None:
    finding = _finding(
        title="Custom mount URL evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Encoded https://logs.example/open/%2Facme%2Fprivate%2Fsecret.py. "
                "Custom "
                "https://logs.example/open/%2Fcustom-mount%2Ftenant%2Fcredentials.json. "
                "Dotted https://logs.example/open/%2Fsrv.prod%2Ftenant%2Fcredentials.json. "
                "Dot directory https://logs.example/open/%2F.company%2Ftenant%2Fsecret.py. "
                "Proxy https://logs.example/proxy//service/assets/app.js. "
                "Localhost https://logs.example/proxy//localhost/api/result.json."
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

    assert message.count("<remote-url-with-local-path>") == 4
    assert "%2Facme%2Fprivate%2Fsecret.py" not in message
    assert "%2Fcustom-mount%2Ftenant%2Fcredentials.json" not in message
    assert "%2Fsrv.prod%2Ftenant%2Fcredentials.json" not in message
    assert "%2F.company%2Ftenant%2Fsecret.py" not in message
    assert "https://logs.example/proxy//service/assets/app.js." in message
    assert "https://logs.example/proxy//localhost/api/result.json." in message


def test_render_sarif_redacts_delimited_absolute_paths_in_url_paths_and_fragments() -> None:
    finding = _finding(
        title="Delimited URL evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Parameter https://logs.example/open;file=%2Fhome%2Falice%2Fsecret.py. "
                "Segment https://logs.example/open/file=%2Fhome%2Falice%2Fsecret.py. "
                "Custom "
                "https://logs.example/open;next=%2Fcustom-mount%2Fsecret.json. "
                "Fragment "
                "https://logs.example/open#file=%2Fhome%2Falice%2Fsecret.py. "
                "Fragment prose "
                "https://logs.example/open#trace=at%20%2Fcustom-mount%2Fsecret.json. "
                "Fragment root "
                "https://logs.example/open#%2Fcustom-mount%2Ftenant%2Fcredentials. "
                "Nested fragment root "
                "https://logs.example/open#%252Fcustom-mount%252Ftenant%252Fcredentials. "
                "Path pipe "
                "https://logs.example/open/path|%2Fhome%2Falice%2Fsecret.py. "
                "Fragment pipe "
                "https://logs.example/open#trace|%2Fhome%2Falice%2Fsecret.py. "
                "Safe parameter https://logs.example/open;page=docs%2Fguide.md. "
                "Safe fragment https://logs.example/open#file=docs%2Fguide.md. "
                "Safe SPA https://logs.example/open#/dashboard. "
                "Safe encoded SPA https://logs.example/open#%2Fdashboard. "
                "Safe pipe segment https://logs.example/open/asset|docs%2Fguide.md."
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

    assert message.count("<remote-url-with-local-path>") == 9
    assert "%2Fhome%2Falice%2Fsecret.py" not in message
    assert "%2Fcustom-mount%2Fsecret.json" not in message
    assert "%2Fcustom-mount%2Ftenant%2Fcredentials" not in message
    assert "%252Fcustom-mount%252Ftenant%252Fcredentials" not in message
    assert "https://logs.example/open;page=docs%2Fguide.md." in message
    assert "https://logs.example/open#file=docs%2Fguide.md." in message
    assert "https://logs.example/open#/dashboard." in message
    assert "https://logs.example/open#%2Fdashboard." in message
    assert "https://logs.example/open/asset|docs%2Fguide.md." in message


def test_render_sarif_redacts_authorityless_urls_with_absolute_local_paths() -> None:
    finding = _finding(
        title="Authorityless URL evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Git git+file:///home/alice/private/repo. "
                "Filesystem filesystem:///custom-mount/tenant/secret.json. "
                "Malformed https:///etc/passwd. "
                "Safe https://example.com/home/alice."
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

    assert message.count("<remote-url-with-local-path>") == 3
    assert "git+file:///home/alice/private/repo" not in message
    assert "filesystem:///custom-mount/tenant/secret.json" not in message
    assert "https:///etc/passwd" not in message
    assert "https://example.com/home/alice." in message


def test_render_sarif_distinguishes_encoded_nested_remote_and_local_schemes() -> None:
    finding = _finding(
        title="Encoded nested scheme evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Remote "
                "https://logs.example/proxy/https:%2F%2Fservice.example/assets/app.js. "
                "Nested remote "
                "https://logs.example/proxy/http:%252F%252Fservice.example/assets/app.js. "
                "Fragment "
                "https://logs.example/open#https:%2F%2Fservice.example/assets/app.js. "
                "Parameter "
                "https://logs.example/open;next=https:%2F%2Fservice.example%2Fassets%2Fapp.js. "
                "Query "
                "https://logs.example/open?next=https:%2F%2Fservice.example%2Fassets%2Fapp.js. "
                "Local "
                "https://logs.example/proxy/file:%2F%2F%2Fhome%2Falice%2Fsecret.py."
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
    assert "https:%2F%2Fservice.example/assets/app.js." in message
    assert "http:%252F%252Fservice.example/assets/app.js." in message
    assert "#https:%2F%2Fservice.example/assets/app.js." in message
    assert ";next=https:%2F%2Fservice.example%2Fassets%2Fapp.js." in message
    assert "?next=https:%2F%2Fservice.example%2Fassets%2Fapp.js." in message
    assert "file:%2F%2F%2Fhome%2Falice%2Fsecret.py" not in message


def test_render_sarif_redacts_local_path_in_mixed_nested_url_query_prose() -> None:
    finding = _finding(
        title="Mixed nested URL query evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Local "
                "https://logs.example/view?next=https%3A%2F%2Fservice.example%2Fcallback"
                "%20at%20%2Fhome%2Falice%2Fsecret.py. "
                "Safe "
                "https://logs.example/view?next=https%3A%2F%2Fservice.example%2Fcallback."
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
    assert "%2Fhome%2Falice%2Fsecret.py" not in message
    assert "https://logs.example/view?next=https%3A%2F%2Fservice.example%2Fcallback." in message


def test_render_sarif_redacts_fully_encoded_local_references_from_prose() -> None:
    finding = _finding(
        title="Encoded prose reference evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(
        update={
            "evidence": (
                "Path path=%2Fhome%2Falice%2Fsecret.py. "
                "Local URL uri=file%3A%2F%2F%2Fhome%2Falice%2Fsecret.py. "
                "Remote URL "
                "url=https%3A%2F%2Flogs.example%2Fopen"
                "%3Ffile%3D%2Fworkspace%2Frepo%2Fsecret.py. "
                "Safe docs%2Fguide.md."
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

    assert message.count("<encoded-local-reference>") == 3
    assert "%2Fhome%2Falice" not in message
    assert "%2Fworkspace%2Frepo" not in message
    assert "Safe docs%2Fguide.md." in message


def test_render_sarif_handles_long_safe_unbroken_evidence_linearly() -> None:
    safe_token = "a" * 100_000
    finding = _finding(
        title="Long evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(update={"evidence": safe_token})
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert safe_token in message


def test_render_sarif_handles_many_embedded_remote_scheme_delimiters_linearly() -> None:
    safe_url = "https://logs.example/open/" + ("ab://" * 10_000) + "tail"
    finding = _finding(
        title="Long URL evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(update={"evidence": safe_url})
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert safe_url in message


def test_render_sarif_handles_many_safe_double_slash_route_components_linearly() -> None:
    safe_url = "https://logs.example/open" + ("//docs" * 10_000) + "/tail"
    finding = _finding(
        title="Long route evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(update={"evidence": safe_url})
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert safe_url in message


def test_render_sarif_preserves_encoded_relative_path_after_length_expanding_unicode() -> None:
    safe_url = f"https://logs.example/open/{'ß' * 100}%2Fhome%2Falice%2Fsecret.py"
    finding = _finding(
        title="Unicode URL evidence",
        severity=FindingSeverity.MEDIUM,
        file="src/service.ts",
        line=5,
    ).model_copy(update={"evidence": safe_url})
    report = build_report(
        ProjectProfile(root="/workspace/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding],
    )

    message = json.loads(render_sarif(report))["runs"][0]["results"][0]["message"]["text"]

    assert safe_url in message


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
                "Safe https://logs.example/open/assets%252Fstyles.css."
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
    assert "https://logs.example/open/assets%252Fstyles.css." in message


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
                "Windows authority "
                "https://C%3AUsers%5Calice%5Ccorp%5Csecret.ts@example.com/open. "
                "POSIX authority "
                "https://%2Fhome%2Falice%2Fsecret.py@example.com/open. "
                "POSIX userinfo "
                "https://user:%2Fhome%2Falice%2Fsecret.py@example.com/open. "
                "System authority https://%2Fetc%2Fpasswd@example.com/open. "
                "Fragment "
                "https://example.com/open#file=C%3AUsers%5Calice%5Ccorp%5Csecret.ts. "
                "Safe fragment https://example.com/docs#label=C%3AUsers. "
                "Safe userinfo https://user:docs%2Fguide@example.com/open."
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

    assert message.count("<remote-url-with-local-path>") == 5
    assert "C%3AUsers%5Calice" not in message
    assert "%2Fhome%2Falice" not in message
    assert "%2Fetc%2Fpasswd" not in message
    assert "https://example.com/docs#label=C%3AUsers." in message
    assert "https://user:docs%2Fguide@example.com/open." in message


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
