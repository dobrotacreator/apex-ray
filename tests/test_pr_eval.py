import subprocess
import sys
import time
from multiprocessing import get_context
from pathlib import Path

import yaml

from apex_ray import pr_eval
from apex_ray.models import Finding, FindingConfidence, FindingSeverity
from apex_ray.pr_eval import (
    GreptileComment,
    GreptileFinding,
    PrEvalCaseStatus,
    PrEvalFindingMatch,
    PullRequestEvalRunReport,
    PullRequestEvalRunResult,
    apex_finding_fingerprint,
    append_pr_eval_telemetry,
    capture_pr_eval_cases,
    load_pr_eval_case,
    load_pr_eval_labels,
    load_pr_eval_run_report,
    load_pr_eval_telemetry,
    match_greptile_to_apex,
    memory_suggestions_from_pr_eval_report,
    render_pr_eval_telemetry_summary,
    write_pr_eval_label_templates,
)


def test_greptile_findings_keep_first_pass_inline_comments_and_ignore_edited_summary() -> None:
    comments = [
        GreptileComment(
            id="summary",
            source="issue_comment",
            author="greptile-apps[bot]",
            body=_summary_body("src/cart.ts", 12, "Cart total drops quantity"),
            created_at="2026-01-01T00:00:00Z",
            includes_created_edit=True,
        ),
        GreptileComment(
            id="inline",
            source="review_comment",
            author="greptile-apps[bot]",
            body="![P1](https://example.test/badges/p1.svg) **Cart total drops quantity**\n\nUse price * quantity.",
            file="src/cart.ts",
            line=12,
            original_line=12,
            created_at="2026-01-01T00:01:00Z",
        ),
    ]

    findings = pr_eval._greptile_findings_from_comments(comments, first_pass_window_minutes=15)

    assert len(findings) == 2
    summary, inline = findings
    assert summary.source == "summary_issue"
    assert summary.first_pass is False
    assert inline.source == "review_comment"
    assert inline.first_pass is True
    assert inline.severity == "P1"
    assert inline.title == "Cart total drops quantity"


def test_pr_diff_from_git_uses_clean_unified_diff(tmp_path: Path) -> None:
    _git(["init"], tmp_path)
    _git(["config", "user.email", "apex@example.test"], tmp_path)
    _git(["config", "user.name", "Apex Test"], tmp_path)
    (tmp_path / "cart.ts").write_text("export const total = item.price;\n", encoding="utf-8")
    _git(["add", "cart.ts"], tmp_path)
    _git(["commit", "-m", "base"], tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    (tmp_path / "cart.ts").write_text("export const total = item.price * item.quantity;\n", encoding="utf-8")
    _git(["commit", "-am", "head"], tmp_path)
    head_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    diff = pr_eval._pr_diff_from_git(tmp_path, "org/repo", 1, base_sha, head_sha)

    assert "diff --git a/cart.ts b/cart.ts" in diff
    assert "+export const total = item.price * item.quantity;" in diff
    assert "\nFrom " not in diff


def test_overlay_current_apex_config_excludes_local_runtime_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    apex = source / ".apex-ray"
    (apex / "rules").mkdir(parents=True)
    (apex / "cache").mkdir()
    (apex / "telemetry").mkdir()
    (apex / "reports").mkdir()
    (apex / "evals" / "runs").mkdir(parents=True)
    worktree.mkdir()
    (apex / "config.yml").write_text("review:\n", encoding="utf-8")
    (apex / "config.local.yml").write_text("review:\n  llm:\n    jobs: 8\n", encoding="utf-8")
    (apex / "rules" / "rule.md").write_text("---\nid: r\n---\nbody\n", encoding="utf-8")
    (apex / "cache" / "x").write_text("cache\n", encoding="utf-8")
    (apex / "telemetry" / "x").write_text("telemetry\n", encoding="utf-8")
    (apex / "reports" / "x").write_text("report\n", encoding="utf-8")
    (apex / "evals" / "runs" / "x").write_text("run\n", encoding="utf-8")

    pr_eval._overlay_current_apex_config(source, worktree)

    copied = worktree / ".apex-ray"
    assert (copied / "config.yml").exists()
    assert (copied / "rules" / "rule.md").exists()
    assert not (copied / "config.local.yml").exists()
    assert not (copied / "cache").exists()
    assert not (copied / "telemetry").exists()
    assert not (copied / "reports").exists()
    assert not (copied / "evals").exists()


def test_pr_diff_from_git_falls_back_to_clean_gh_diff(tmp_path: Path, monkeypatch) -> None:
    def fail_commit_fetch(_repo: Path, _sha: str, *, pr_number: int | None = None) -> None:
        raise pr_eval.PrEvalError("no merge base")

    def fake_gh_text(args: list[str], cwd: Path) -> str:
        assert args == ["pr", "diff", "735"]
        assert cwd == tmp_path
        return "diff --git a/src/cart.ts b/src/cart.ts\n"

    monkeypatch.setattr(pr_eval, "_ensure_commit_available", fail_commit_fetch)
    monkeypatch.setattr(pr_eval, "_github_compare_diff", lambda *_args: "")
    monkeypatch.setattr(pr_eval, "_run_gh_text", fake_gh_text)

    assert pr_eval._pr_diff_from_git(
        tmp_path,
        "org/repo",
        735,
        "base",
        "head",
        allow_pr_diff_fallback=True,
    ).startswith("diff --git")


def test_match_greptile_to_apex_uses_file_line_and_issue_tokens() -> None:
    greptile = [
        GreptileFinding(
            id="g1",
            source="review_comment",
            title="FOR UPDATE SKIP LOCKED lock released before status update",
            body="The row lock is released before the job status is updated.",
            file="src/jobs.ts",
            line=56,
            created_at="2026-01-01T00:00:00Z",
        )
    ]
    apex = [
        Finding(
            title="Row lock is released before status update",
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.HIGH,
            file="src/jobs.ts",
            line=57,
            failure_mode="SKIP LOCKED claim and status update are split across transactions.",
            evidence="The selected job is returned before the status write happens.",
            suggested_fix="Update the job inside the same transaction as the claim.",
            suggested_test="Add a concurrent worker claim test.",
        ),
        Finding(
            title="Unrelated issue",
            severity=FindingSeverity.LOW,
            confidence=FindingConfidence.LOW,
            file="src/other.ts",
            line=1,
            failure_mode="Unrelated.",
            evidence="Unrelated.",
            suggested_fix="None.",
            suggested_test="None.",
        ),
    ]

    matches, extra = match_greptile_to_apex(greptile, apex)

    assert matches[0].matched is True
    assert matches[0].matched_apex_title == "Row lock is released before status update"
    assert extra == [apex[1]]


def test_match_greptile_to_apex_rejects_same_file_far_line_weak_text_match() -> None:
    greptile = [
        GreptileFinding(
            id="g1",
            source="review_comment",
            title="Route53 hosted zone ID pattern is too narrow",
            body="The regex only accepts one fixed hosted zone ID length.",
            file="scripts/check-hardcoded-cloud-ids.ts",
            line=9,
            created_at="2026-01-01T00:00:00Z",
        )
    ]
    apex = [
        Finding(
            title="Segment-based path skips miss top-level ignored directories",
            severity=FindingSeverity.MEDIUM,
            confidence=FindingConfidence.MEDIUM,
            file="scripts/check-hardcoded-cloud-ids.ts",
            line=24,
            failure_mode="Top-level ignored directories can still be scanned.",
            evidence="The skip logic checks slash-delimited segments only.",
            suggested_fix="Normalize ignored paths before matching.",
            suggested_test="Add root-level fixture directory coverage.",
        )
    ]

    matches, extra = match_greptile_to_apex(greptile, apex)

    assert matches[0].matched is False
    assert extra == apex


def test_capture_pr_eval_cases_writes_manifest_and_greptile_comments(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    output = tmp_path / "cases"

    monkeypatch.setattr(pr_eval, "_github_name_with_owner", lambda _repo: "org/repo")
    monkeypatch.setattr(
        pr_eval,
        "_load_prs",
        lambda _repo, _numbers, _limit: [
            {
                "number": 12,
                "title": "Fix cart total",
                "url": "https://github.com/org/repo/pull/12",
                "baseRefName": "main",
                "headRefName": "feature/cart",
                "baseRefOid": "base-sha",
                "headRefOid": "head-sha",
                "createdAt": "2026-01-01T00:00:00Z",
                "mergedAt": "2026-01-02T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        pr_eval,
        "_pr_diff_from_git",
        lambda _repo, _owner, _number, _base, _head, allow_pr_diff_fallback=False: (
            "diff --git a/src/cart.ts b/src/cart.ts\n"
        ),
    )
    monkeypatch.setattr(pr_eval, "_load_pr_commit_oids", lambda _repo, _number: ["head-sha"])
    monkeypatch.setattr(pr_eval, "_github_commit_first_parent", lambda _owner, _sha, _repo: "replay-base-sha")
    monkeypatch.setattr(
        pr_eval,
        "_load_greptile_comments",
        lambda _owner_repo, _number, _repo: [
            GreptileComment(
                id="inline",
                source="review_comment",
                author="greptile-apps[bot]",
                body="**Cart total drops quantity**",
                file="src/cart.ts",
                line=12,
                original_line=12,
                original_commit_id="head-sha",
                created_at="2026-01-01T00:01:00Z",
            )
        ],
    )

    result = capture_pr_eval_cases(source_repo=repo, output_dir=output, pr_numbers=[12])

    assert result.warnings == []
    assert (output / "pr-12" / "pr.diff").read_text(encoding="utf-8").startswith("diff --git")
    manifest = yaml.safe_load((output / "pr-12" / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["number"] == 12
    assert manifest["greptile_findings"][0]["file"] == "src/cart.ts"
    case = load_pr_eval_case(output / "pr-12" / "manifest.yml")
    assert case.head_sha == "head-sha"
    assert case.replay_base_sha == "replay-base-sha"
    assert case.replay_head_sha == "head-sha"
    assert case.greptile_findings[0].first_pass is True


def test_run_gh_api_paginated_array_flattens_slurped_pages(tmp_path: Path, monkeypatch) -> None:
    def fake_run_gh_json(args: list[str], cwd: Path):
        assert args == ["api", "repos/org/repo/pulls/1/comments", "--paginate", "--slurp"]
        assert cwd == tmp_path
        return [[{"id": 1}], [{"id": 2}, "ignored"]]

    monkeypatch.setattr(pr_eval, "_run_gh_json", fake_run_gh_json)

    assert pr_eval._run_gh_api_paginated_array("repos/org/repo/pulls/1/comments", tmp_path) == [
        {"id": 1},
        {"id": 2},
    ]


def test_memory_suggestions_from_pr_eval_report_drafts_missed_greptile_cards() -> None:
    greptile = GreptileFinding(
        id="g1",
        source="review_comment",
        title="Missing pre-TFA session guard",
        body=(
            "**Missing pre-TFA session guard** `resendPreTfaCode` does not check "
            "`session.profiles` before using `session.corebank.token`."
        ),
        severity="P1",
        file="apps/client-bff/src/modules/auth/application/auth.service.ts",
        line=528,
        created_at="2026-01-01T00:00:00Z",
    )
    report = PullRequestEvalRunReport(
        cases=[
            PullRequestEvalRunResult(
                number=727,
                title="feat(client-bff): add pre-TFA SMS resend endpoint",
                url="https://github.com/org/repo/pull/727",
                passed=False,
                greptile_findings_count=1,
                apex_findings_count=0,
                matched_greptile_findings=0,
                missed_greptile_findings=1,
                extra_apex_findings=0,
                context_packs_count=1,
                llm_runs_count=1,
                report_path="apex-report.json",
                markdown_path="apex-report.md",
                matches=[PrEvalFindingMatch(greptile_finding=greptile, matched=False)],
            )
        ],
        total=1,
        passed=0,
        failed=1,
    )

    suggestions = memory_suggestions_from_pr_eval_report(report)

    assert "id: greptile-pr-727-missing-pre-tfa-session-guard" in suggestions
    assert "severity: high" in suggestions
    assert "apps/client-bff/src/modules/auth/application/auth.service.ts" in suggestions
    assert "resendPreTfaCode" in suggestions
    assert "session.profiles" in suggestions


def test_load_pr_eval_run_report_accepts_saved_computed_totals(tmp_path: Path) -> None:
    report = PullRequestEvalRunReport(cases=[], total=0, passed=0, failed=0)
    path = tmp_path / "pr-eval-report.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    loaded = load_pr_eval_run_report(path)

    assert loaded.total == 0
    assert loaded.greptile_findings_total == 0


def test_pr_eval_label_templates_capture_extra_apex_findings(tmp_path: Path) -> None:
    extra = Finding(
        title="Raw upstream payload escapes boundary",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/auth.ts",
        line=42,
        failure_mode="The changed response returns raw upstream fields.",
        evidence="The diff returns payload directly.",
        suggested_fix="Project a DTO.",
        suggested_test="Assert raw fields are absent.",
    )
    greptile = GreptileFinding(
        id="g1",
        source="review_comment",
        title="Missing DTO projection",
        body="Return a safe DTO.",
        file="src/auth.ts",
        line=42,
        created_at="2026-01-01T00:00:00Z",
    )
    report = PullRequestEvalRunReport(
        cases=[
            PullRequestEvalRunResult(
                number=1,
                title="Auth fix",
                url="https://github.com/org/repo/pull/1",
                passed=False,
                greptile_findings_count=1,
                apex_findings_count=1,
                matched_greptile_findings=0,
                missed_greptile_findings=1,
                extra_apex_findings=1,
                context_packs_count=1,
                llm_runs_count=1,
                report_path="apex-report.json",
                markdown_path="apex-report.md",
                matches=[PrEvalFindingMatch(greptile_finding=greptile, matched=False)],
                extra_findings=[extra],
            )
        ],
        total=1,
        passed=0,
        failed=1,
    )

    written = write_pr_eval_label_templates(report, tmp_path)
    labels = load_pr_eval_labels(tmp_path, 1)

    assert written == [tmp_path / "pr-1.yml"]
    assert labels.greptile_findings["g1"].verdict == "valid"
    assert labels.apex_findings[apex_finding_fingerprint(extra)].verdict == "unknown"


def test_pr_eval_telemetry_round_trip(tmp_path: Path) -> None:
    report = PullRequestEvalRunReport(
        cases=[
            PullRequestEvalRunResult(
                number=2,
                title="Payments fix",
                url="https://github.com/org/repo/pull/2",
                passed=True,
                greptile_findings_count=1,
                apex_findings_count=1,
                matched_greptile_findings=1,
                missed_greptile_findings=0,
                extra_apex_findings=0,
                context_packs_count=3,
                llm_runs_count=4,
                llm_estimated_input_tokens=900,
                triaged_extra_duplicates=2,
                triaged_extra_not_actionable=1,
                report_path="apex-report.json",
                markdown_path="apex-report.md",
            )
        ],
        total=1,
        passed=1,
        failed=0,
    )
    telemetry_path = tmp_path / "telemetry.jsonl"

    append_pr_eval_telemetry(report, telemetry_path, source_repo=tmp_path, output_dir=tmp_path / "run")
    entries = load_pr_eval_telemetry(telemetry_path)
    summary = render_pr_eval_telemetry_summary(entries)

    assert len(entries) == 1
    assert entries[0].matched_greptile_findings_total == 1
    assert entries[0].triaged_extra_duplicates_total == 2
    assert entries[0].cases[0].triaged_extra_not_actionable == 1
    assert entries[0].cases[0].llm_estimated_input_tokens == 900
    assert "Latest matched Greptile findings: `1/1`" in summary
    assert "Latest triaged extra duplicates: `2`" in summary


def test_run_pr_eval_cases_resume_skips_completed_case(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    cases = tmp_path / "cases"
    case_dir = _write_eval_case(cases, 1)
    output = tmp_path / "run"
    result_dir = output / "pr-1"
    result_dir.mkdir(parents=True)
    existing = PullRequestEvalRunResult(
        number=1,
        title="PR 1",
        url="https://github.com/org/repo/pull/1",
        passed=True,
        greptile_findings_count=0,
        apex_findings_count=0,
        matched_greptile_findings=0,
        missed_greptile_findings=0,
        extra_apex_findings=0,
        context_packs_count=0,
        llm_runs_count=0,
        report_path="",
        markdown_path="",
    )
    case = load_pr_eval_case(case_dir / "manifest.yml")
    run_fingerprint = pr_eval._pr_eval_case_run_fingerprint(case, _default_run_kwargs(repo))
    existing.run_fingerprint = run_fingerprint
    (result_dir / "eval-result.json").write_text(existing.model_dump_json(), encoding="utf-8")
    status = PrEvalCaseStatus(
        number=1,
        title="PR 1",
        status="succeeded",
        phase="done",
        run_fingerprint=run_fingerprint,
    )
    (result_dir / pr_eval.CASE_STATUS_FILENAME).write_text(status.model_dump_json(), encoding="utf-8")

    def fail_run(*_args: object, **_kwargs: object) -> PullRequestEvalRunResult:
        raise AssertionError("case should have been resumed")

    monkeypatch.setattr(pr_eval, "_run_one_pr_eval_case", fail_run)

    report = pr_eval.run_pr_eval_cases(source_repo=repo, cases_dir=cases, output_dir=output, resume=True)

    assert report.total == 1
    assert report.cases[0].number == 1
    assert case_dir.exists()


def test_run_pr_eval_cases_resume_reruns_when_run_fingerprint_changes(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    cases = tmp_path / "cases"
    _write_eval_case(cases, 4)
    output = tmp_path / "run"
    result_dir = output / "pr-4"
    result_dir.mkdir(parents=True)
    existing = PullRequestEvalRunResult(
        number=4,
        title="PR 4",
        url="https://github.com/org/repo/pull/4",
        passed=True,
        run_fingerprint="old",
        greptile_findings_count=0,
        apex_findings_count=0,
        matched_greptile_findings=0,
        missed_greptile_findings=0,
        extra_apex_findings=0,
        context_packs_count=0,
        llm_runs_count=0,
        report_path="",
        markdown_path="",
    )
    (result_dir / "eval-result.json").write_text(existing.model_dump_json(), encoding="utf-8")
    status = PrEvalCaseStatus(number=4, title="PR 4", status="succeeded", phase="done", run_fingerprint="old")
    (result_dir / pr_eval.CASE_STATUS_FILENAME).write_text(status.model_dump_json(), encoding="utf-8")
    calls = 0

    def fake_run(**kwargs: object) -> PullRequestEvalRunResult:
        nonlocal calls
        calls += 1
        case = kwargs["case"]
        assert isinstance(case, pr_eval.PullRequestEvalCase)
        return PullRequestEvalRunResult(
            number=case.number,
            title=case.title,
            url=case.url,
            passed=True,
            greptile_findings_count=0,
            apex_findings_count=0,
            matched_greptile_findings=0,
            missed_greptile_findings=0,
            extra_apex_findings=0,
            context_packs_count=0,
            llm_runs_count=0,
            report_path="",
            markdown_path="",
        )

    monkeypatch.setattr(pr_eval, "_run_one_pr_eval_case", fake_run)

    report = pr_eval.run_pr_eval_cases(
        source_repo=repo,
        cases_dir=cases,
        output_dir=output,
        resume=True,
        llm_enabled=True,
    )

    assert calls == 1
    assert report.cases[0].number == 4


def test_run_pr_eval_cases_stale_running_status_reruns_case(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    cases = tmp_path / "cases"
    _write_eval_case(cases, 2)
    output = tmp_path / "run"
    result_dir = output / "pr-2"
    result_dir.mkdir(parents=True)
    stale = PrEvalCaseStatus(number=2, title="PR 2", status="running", phase="pipeline")
    (result_dir / pr_eval.CASE_STATUS_FILENAME).write_text(stale.model_dump_json(), encoding="utf-8")
    calls = 0

    def fake_run(**kwargs: object) -> PullRequestEvalRunResult:
        nonlocal calls
        calls += 1
        case = kwargs["case"]
        assert isinstance(case, pr_eval.PullRequestEvalCase)
        return PullRequestEvalRunResult(
            number=case.number,
            title=case.title,
            url=case.url,
            passed=True,
            greptile_findings_count=0,
            apex_findings_count=0,
            matched_greptile_findings=0,
            missed_greptile_findings=0,
            extra_apex_findings=0,
            context_packs_count=0,
            llm_runs_count=0,
            report_path="",
            markdown_path="",
        )

    monkeypatch.setattr(pr_eval, "_run_one_pr_eval_case", fake_run)

    report = pr_eval.run_pr_eval_cases(source_repo=repo, cases_dir=cases, output_dir=output, resume=True)

    assert calls == 1
    assert report.cases[0].number == 2


def test_run_pr_eval_cases_resolves_relative_cache_dir_against_source_repo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    cases = tmp_path / "cases"
    _write_eval_case(cases, 5)
    seen_cache_dir: Path | None = None

    def fake_run(**kwargs: object) -> PullRequestEvalRunResult:
        nonlocal seen_cache_dir
        seen_cache_dir = kwargs["cache_dir"]
        case = kwargs["case"]
        assert isinstance(case, pr_eval.PullRequestEvalCase)
        return PullRequestEvalRunResult(
            number=case.number,
            title=case.title,
            url=case.url,
            passed=True,
            greptile_findings_count=0,
            apex_findings_count=0,
            matched_greptile_findings=0,
            missed_greptile_findings=0,
            extra_apex_findings=0,
            context_packs_count=0,
            llm_runs_count=0,
            report_path="",
            markdown_path="",
        )

    monkeypatch.setattr(pr_eval, "_run_one_pr_eval_case", fake_run)

    pr_eval.run_pr_eval_cases(
        source_repo=repo,
        cases_dir=cases,
        output_dir=tmp_path / "run",
        cache_dir=Path(".apex-ray/cache/llm"),
    )

    assert seen_cache_dir == repo / ".apex-ray/cache/llm"


def test_quarantined_pr_eval_label_skips_pipeline(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    cases = tmp_path / "cases"
    _write_eval_case(cases, 3)
    labels = tmp_path / "labels"
    labels.mkdir()
    (labels / "pr-3.yml").write_text(
        "pr: 3\ncase_status: quarantined\ncase_status_reason: flaky historical PR\n",
        encoding="utf-8",
    )

    def fail_pipeline(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("quarantined case should not run pipeline")

    monkeypatch.setattr(pr_eval, "run_review_pipeline", fail_pipeline)

    report = pr_eval.run_pr_eval_cases(
        source_repo=repo,
        cases_dir=cases,
        output_dir=tmp_path / "run",
        labels_dir=labels,
    )

    assert report.quarantined == 1
    assert report.passed == 1
    assert report.cases[0].scored is False
    assert report.cases[0].status == "quarantined"


def test_run_pr_eval_cases_supervised_parallel_preserves_case_order(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    cases = tmp_path / "cases"
    _write_eval_case(cases, 6)
    _write_eval_case(cases, 7)
    labels = tmp_path / "labels"
    labels.mkdir()
    for number in (6, 7):
        (labels / f"pr-{number}.yml").write_text(
            f"pr: {number}\ncase_status: quarantined\ncase_status_reason: skip fixture\n",
            encoding="utf-8",
        )

    report = pr_eval.run_pr_eval_cases(
        source_repo=repo,
        cases_dir=cases,
        output_dir=tmp_path / "run",
        labels_dir=labels,
        case_jobs=2,
    )

    assert [case.number for case in report.cases] == [6, 7]
    assert report.quarantined == 2
    assert report.passed == 2


def test_terminate_case_worker_kills_child_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    context = get_context("spawn")
    proc = context.Process(target=_spawn_child_marker_after_parent_timeout, args=(marker,))
    proc.start()
    time.sleep(0.5)

    pr_eval._terminate_case_worker(proc, grace_seconds=0.2)
    time.sleep(1.0)

    assert not proc.is_alive()
    assert not marker.exists()


def _default_run_kwargs(repo: Path) -> dict[str, object]:
    return {
        "repo_root": repo.resolve(),
        "llm_enabled": False,
        "provider_override": None,
        "model_override": None,
        "verify_override": None,
        "cache_enabled": None,
        "refresh_cache": False,
        "cache_dir": None,
        "llm_jobs": None,
        "llm_coverage_mode": None,
        "llm_max_deep_packs": None,
        "llm_max_input_tokens": None,
        "analyzer_timeout_seconds": None,
        "allow_extra_findings": False,
        "labels_dir": None,
    }


def _spawn_child_marker_after_parent_timeout(marker: Path) -> None:
    pr_eval._become_process_group_leader()
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import pathlib, sys, time; "
                "time.sleep(0.8); "
                "pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
            ),
            str(marker),
        ]
    )
    time.sleep(10)


def _summary_body(file: str, line: int, title: str) -> str:
    return (
        "<details><summary>Prompt To Fix All With AI</summary>\n\n"
        "### Issue 1 of 1\n"
        f"{file}:{line}\n"
        f"**{title}**\n"
        "Use price * quantity.\n"
        "</details>"
    )


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write_eval_case(root: Path, number: int) -> Path:
    case_dir = root / f"pr-{number}"
    case_dir.mkdir(parents=True)
    (case_dir / "pr.diff").write_text(
        "diff --git a/src/cart.ts b/src/cart.ts\n--- a/src/cart.ts\n+++ b/src/cart.ts\n@@ -1 +1 @@\n-export const total = 1;\n+export const total = 2;\n",
        encoding="utf-8",
    )
    case = pr_eval.PullRequestEvalCase(
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/org/repo/pull/{number}",
        base_ref_name="main",
        head_ref_name=f"branch-{number}",
        base_sha="base",
        head_sha="head",
        created_at="2026-01-01T00:00:00Z",
    )
    (case_dir / "manifest.yml").write_text(
        yaml.safe_dump(case.model_dump(mode="json", exclude_none=True), sort_keys=False),
        encoding="utf-8",
    )
    return case_dir
