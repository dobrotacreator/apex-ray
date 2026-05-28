from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Literal, TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is not available on Windows.
    fcntl = None

from apex_ray import git
from apex_ray.config import ConfigError, load_config
from apex_ray.models import (
    Finding,
    LLMCoverageMode,
    LLMProviderName,
    LLMRoutingConfig,
    TargetMode,
)
from apex_ray.pipeline import run_review_pipeline
from apex_ray.report import render_markdown

GREPTILE_AUTHOR_PREFIXES = ("greptile", "greptile-apps")
DEFAULT_FIRST_PASS_WINDOW_MINUTES = 15
DEFAULT_MAX_BODY_CHARS = 4000
DEFAULT_LABELS_DIR = ".apex-ray/eval/labels"
DEFAULT_TELEMETRY_PATH = ".apex-ray/eval/telemetry/pr-eval-runs.jsonl"
CASE_STATUS_FILENAME = "case-status.json"
_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


class PrEvalError(RuntimeError):
    pass


class _PrEvalRunKwargs(TypedDict):
    repo_root: Path
    llm_enabled: bool
    provider_override: LLMProviderName | None
    model_override: str | None
    verify_override: bool | None
    cache_enabled: bool | None
    refresh_cache: bool
    cache_dir: Path | None
    llm_jobs: int | None
    llm_coverage_mode: str | None
    llm_max_deep_packs: int | None
    llm_max_input_tokens: int | None
    analyzer_timeout_seconds: int | None
    allow_extra_findings: bool
    labels_dir: Path | None


class StrictPrEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GreptileFinding(StrictPrEvalModel):
    id: str
    source: Literal["review_comment", "summary_issue"]
    title: str
    body: str
    severity: str | None = None
    file: str | None = None
    line: int | None = None
    original_line: int | None = None
    url: str | None = None
    commit_id: str | None = None
    original_commit_id: str | None = None
    created_at: str
    updated_at: str | None = None
    first_pass: bool = True


class GreptileComment(StrictPrEvalModel):
    id: str
    source: Literal["issue_comment", "review_comment", "review"]
    author: str
    body: str
    file: str | None = None
    line: int | None = None
    original_line: int | None = None
    url: str | None = None
    commit_id: str | None = None
    original_commit_id: str | None = None
    created_at: str
    updated_at: str | None = None
    includes_created_edit: bool = False


class PullRequestEvalCase(StrictPrEvalModel):
    number: int
    title: str
    url: str
    base_ref_name: str
    head_ref_name: str
    base_sha: str
    head_sha: str
    replay_base_sha: str | None = None
    replay_head_sha: str | None = None
    merge_commit_sha: str | None = None
    created_at: str
    merged_at: str | None = None
    first_greptile_at: str | None = None
    first_pass_window_minutes: int = DEFAULT_FIRST_PASS_WINDOW_MINUTES
    diff_path: str = "pr.diff"
    greptile_comments_path: str = "greptile-comments.json"
    greptile_findings: list[GreptileFinding] = Field(default_factory=list)


class PullRequestEvalCaptureResult(StrictPrEvalModel):
    output_dir: str
    cases: list[PullRequestEvalCase] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PrEvalFindingMatch(StrictPrEvalModel):
    greptile_finding: GreptileFinding
    matched: bool
    matched_apex_title: str | None = None
    matched_apex_file: str | None = None
    matched_apex_line: int | None = None
    score: float = 0.0


class PrEvalGreptileFindingLabel(StrictPrEvalModel):
    verdict: Literal["valid", "not_issue", "out_of_scope", "unknown"] = "valid"
    title: str = ""
    file: str | None = None
    line: int | None = None
    notes: str = ""


class PrEvalApexFindingLabel(StrictPrEvalModel):
    verdict: Literal["unknown", "true_positive", "false_positive", "duplicate", "not_actionable"] = "unknown"
    title: str = ""
    file: str | None = None
    line: int | None = None
    notes: str = ""


class PrEvalLabels(StrictPrEvalModel):
    pr: int
    updated_at: str = ""
    case_status: Literal["active", "quarantined"] = "active"
    case_status_reason: str = ""
    greptile_findings: dict[str, PrEvalGreptileFindingLabel] = Field(default_factory=dict)
    apex_findings: dict[str, PrEvalApexFindingLabel] = Field(default_factory=dict)


class PrEvalCaseStatus(StrictPrEvalModel):
    number: int
    title: str = ""
    status: Literal["pending", "running", "succeeded", "partial", "failed", "timed_out", "quarantined", "skipped"]
    phase: str = ""
    started_at: str = ""
    updated_at: str = ""
    ended_at: str | None = None
    elapsed_ms: int = 0
    error: str | None = None
    eval_result_path: str | None = None
    report_path: str | None = None
    run_fingerprint: str | None = None


class PullRequestEvalRunResult(StrictPrEvalModel):
    number: int
    title: str
    url: str
    passed: bool
    status: Literal["succeeded", "partial", "failed", "timed_out", "quarantined", "skipped"] = "succeeded"
    scored: bool = True
    analysis_partial: bool = False
    coverage_partial_severity: str = "none"
    coverage_quality_gate_status: str = "disabled"
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    error_message: str | None = None
    status_path: str | None = None
    run_fingerprint: str | None = None
    greptile_findings_count: int
    ignored_greptile_findings: int = 0
    apex_findings_count: int
    matched_greptile_findings: int
    missed_greptile_findings: int
    extra_apex_findings: int
    triaged_extra_true_positives: int = 0
    triaged_extra_false_positives: int = 0
    triaged_extra_duplicates: int = 0
    triaged_extra_not_actionable: int = 0
    triaged_extra_unknown: int = 0
    context_packs_count: int
    reviewed_context_packs_count: int = 0
    unreviewed_context_packs_count: int = 0
    residual_p0_context_packs_count: int = 0
    residual_p1_context_packs_count: int = 0
    failed_llm_review_runs_count: int = 0
    failed_llm_verify_runs_count: int = 0
    llm_coverage_ratio: float = 0.0
    source_changed_line_coverage_ratio: float = 0.0
    high_risk_coverage_ratio: float = 0.0
    llm_runs_count: int
    llm_duration_ms: int = 0
    llm_input_chars: int = 0
    llm_estimated_input_tokens: int = 0
    llm_cache_hits: int = 0
    llm_cache_misses: int = 0
    report_path: str
    markdown_path: str
    labels_path: str | None = None
    matches: list[PrEvalFindingMatch] = Field(default_factory=list)
    extra_findings: list[Finding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PullRequestEvalRunReport(StrictPrEvalModel):
    model_config = ConfigDict(extra="ignore")

    cases: list[PullRequestEvalRunResult]
    total: int
    passed: int
    failed: int
    partial: int = 0
    timed_out: int = 0
    quarantined: int = 0
    skipped: int = 0

    @computed_field
    @property
    def greptile_findings_total(self) -> int:
        return sum(case.greptile_findings_count for case in self.cases if case.scored)

    @computed_field
    @property
    def matched_greptile_findings_total(self) -> int:
        return sum(case.matched_greptile_findings for case in self.cases if case.scored)

    @computed_field
    @property
    def missed_greptile_findings_total(self) -> int:
        return sum(case.missed_greptile_findings for case in self.cases if case.scored)

    @computed_field
    @property
    def extra_apex_findings_total(self) -> int:
        return sum(case.extra_apex_findings for case in self.cases if case.scored)

    @computed_field
    @property
    def triaged_extra_true_positives_total(self) -> int:
        return sum(case.triaged_extra_true_positives for case in self.cases)

    @computed_field
    @property
    def triaged_extra_false_positives_total(self) -> int:
        return sum(case.triaged_extra_false_positives for case in self.cases)

    @computed_field
    @property
    def triaged_extra_duplicates_total(self) -> int:
        return sum(case.triaged_extra_duplicates for case in self.cases)

    @computed_field
    @property
    def triaged_extra_not_actionable_total(self) -> int:
        return sum(case.triaged_extra_not_actionable for case in self.cases)

    @computed_field
    @property
    def triaged_extra_unknown_total(self) -> int:
        return sum(case.triaged_extra_unknown for case in self.cases)

    @computed_field
    @property
    def estimated_input_tokens_total(self) -> int:
        return sum(case.llm_estimated_input_tokens for case in self.cases)


class PrEvalTelemetryCase(StrictPrEvalModel):
    number: int
    passed: bool
    status: str = "succeeded"
    scored: bool = True
    duration_ms: int = 0
    coverage_partial_severity: str = "none"
    coverage_quality_gate_status: str = "disabled"
    greptile_findings_count: int
    ignored_greptile_findings: int = 0
    matched_greptile_findings: int
    missed_greptile_findings: int
    extra_apex_findings: int
    triaged_extra_true_positives: int = 0
    triaged_extra_false_positives: int = 0
    triaged_extra_duplicates: int = 0
    triaged_extra_not_actionable: int = 0
    triaged_extra_unknown: int = 0
    context_packs_count: int
    reviewed_context_packs_count: int = 0
    unreviewed_context_packs_count: int = 0
    residual_p0_context_packs_count: int = 0
    residual_p1_context_packs_count: int = 0
    failed_llm_review_runs_count: int = 0
    failed_llm_verify_runs_count: int = 0
    llm_coverage_ratio: float = 0.0
    source_changed_line_coverage_ratio: float = 0.0
    high_risk_coverage_ratio: float = 0.0
    llm_runs_count: int
    llm_duration_ms: int = 0
    llm_estimated_input_tokens: int = 0
    llm_cache_hits: int = 0
    llm_cache_misses: int = 0


class PrEvalTelemetryEntry(StrictPrEvalModel):
    run_id: str
    created_at: str
    source_repo: str
    output_dir: str
    total: int
    passed: int
    failed: int
    partial: int = 0
    timed_out: int = 0
    quarantined: int = 0
    greptile_findings_total: int
    matched_greptile_findings_total: int
    missed_greptile_findings_total: int
    extra_apex_findings_total: int
    triaged_extra_true_positives_total: int = 0
    triaged_extra_false_positives_total: int = 0
    triaged_extra_duplicates_total: int = 0
    triaged_extra_not_actionable_total: int = 0
    triaged_extra_unknown_total: int = 0
    estimated_input_tokens_total: int = 0
    cases: list[PrEvalTelemetryCase] = Field(default_factory=list)


def capture_pr_eval_cases(
    *,
    source_repo: Path,
    output_dir: Path,
    pr_numbers: list[int] | None = None,
    limit: int = 10,
    first_pass_window_minutes: int = DEFAULT_FIRST_PASS_WINDOW_MINUTES,
    overwrite: bool = False,
) -> PullRequestEvalCaptureResult:
    repo_root = git.repo_root(source_repo) or source_repo.resolve()
    if not git.is_git_repo(repo_root):
        raise PrEvalError(f"Source repo is not a git repository: {source_repo}")
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise PrEvalError(f"Output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    owner_repo = _github_name_with_owner(repo_root)
    prs = _load_prs(repo_root, pr_numbers, limit)
    warnings: list[str] = []
    cases: list[PullRequestEvalCase] = []

    for pr in prs:
        number = int(pr["number"])
        case_dir = output_dir / f"pr-{number}"
        case_dir.mkdir(parents=True, exist_ok=True)

        base_sha = str(pr.get("baseRefOid") or "")
        head_sha = str(pr.get("headRefOid") or "")
        comments = _load_greptile_comments(owner_repo, number, repo_root)
        raw_comments_path = case_dir / "greptile-comments.json"
        raw_comments_path.write_text(
            json.dumps([comment.model_dump(mode="json") for comment in comments], indent=2), encoding="utf-8"
        )

        findings = _greptile_findings_from_comments(comments, first_pass_window_minutes)
        pr_commit_oids = _load_pr_commit_oids(repo_root, number)
        replay_head_sha = _replay_head_sha_from_findings(findings) or head_sha
        replay_base_sha = _replay_base_sha(repo_root, owner_repo, pr_commit_oids, replay_head_sha, base_sha)
        diff_text = _pr_diff_from_git(
            repo_root,
            owner_repo,
            number,
            replay_base_sha,
            replay_head_sha,
            allow_pr_diff_fallback=replay_head_sha == head_sha and replay_base_sha == base_sha,
        )
        diff_path = case_dir / "pr.diff"
        diff_path.write_text(diff_text, encoding="utf-8")

        first_at = min((_parse_iso(comment.created_at) for comment in comments), default=None)
        case = PullRequestEvalCase(
            number=number,
            title=str(pr.get("title") or ""),
            url=str(pr.get("url") or ""),
            base_ref_name=str(pr.get("baseRefName") or ""),
            head_ref_name=str(pr.get("headRefName") or ""),
            base_sha=base_sha,
            head_sha=head_sha,
            replay_base_sha=replay_base_sha,
            replay_head_sha=replay_head_sha,
            merge_commit_sha=_merge_commit_oid(pr),
            created_at=str(pr.get("createdAt") or ""),
            merged_at=pr.get("mergedAt"),
            first_greptile_at=first_at.isoformat().replace("+00:00", "Z") if first_at else None,
            first_pass_window_minutes=first_pass_window_minutes,
            greptile_findings=findings,
        )
        _write_case_manifest(case_dir / "manifest.yml", case)
        cases.append(case)
        if not any(finding.first_pass for finding in findings):
            warnings.append(f"PR #{number}: no first-pass Greptile findings captured")

    result = PullRequestEvalCaptureResult(output_dir=str(output_dir), cases=cases, warnings=warnings)
    _atomic_write_text(output_dir / "capture-summary.json", result.model_dump_json(indent=2))
    return result


def run_pr_eval_cases(
    *,
    source_repo: Path,
    cases_dir: Path,
    output_dir: Path,
    pr_numbers: list[int] | None = None,
    llm_enabled: bool = False,
    provider_override: LLMProviderName | None = None,
    model_override: str | None = None,
    verify_override: bool | None = None,
    cache_enabled: bool | None = None,
    refresh_cache: bool = False,
    cache_dir: Path | None = None,
    llm_jobs: int | None = None,
    llm_coverage_mode: str | None = None,
    llm_max_deep_packs: int | None = None,
    llm_max_input_tokens: int | None = None,
    analyzer_timeout_seconds: int | None = None,
    allow_extra_findings: bool = False,
    labels_dir: Path | None = None,
    telemetry_path: Path | None = None,
    limit: int | None = None,
    resume: bool = False,
    case_jobs: int = 1,
    case_timeout_seconds: int | None = None,
) -> PullRequestEvalRunReport:
    repo_root = git.repo_root(source_repo) or source_repo.resolve()
    if not git.is_git_repo(repo_root):
        raise PrEvalError(f"Source repo is not a git repository: {source_repo}")
    if labels_dir is not None and not labels_dir.is_absolute():
        labels_dir = repo_root / labels_dir
    if telemetry_path is not None and not telemetry_path.is_absolute():
        telemetry_path = repo_root / telemetry_path
    if cache_dir is not None and not cache_dir.is_absolute():
        cache_dir = repo_root / cache_dir
    manifests = _case_manifest_paths(cases_dir, pr_numbers, limit)
    output_dir.mkdir(parents=True, exist_ok=True)

    case_jobs = max(1, case_jobs)
    run_kwargs: _PrEvalRunKwargs = {
        "repo_root": repo_root,
        "llm_enabled": llm_enabled,
        "provider_override": provider_override,
        "model_override": model_override,
        "verify_override": verify_override,
        "cache_enabled": cache_enabled,
        "refresh_cache": refresh_cache,
        "cache_dir": cache_dir,
        "llm_jobs": llm_jobs,
        "llm_coverage_mode": llm_coverage_mode,
        "llm_max_deep_packs": llm_max_deep_packs,
        "llm_max_input_tokens": llm_max_input_tokens,
        "analyzer_timeout_seconds": analyzer_timeout_seconds,
        "allow_extra_findings": allow_extra_findings,
        "labels_dir": labels_dir,
    }
    case_specs = [(manifest_path, load_pr_eval_case(manifest_path)) for manifest_path in manifests]
    results: list[PullRequestEvalRunResult | None] = [None] * len(case_specs)
    pending: list[tuple[int, Path, PullRequestEvalCase]] = []
    for index, (manifest_path, case) in enumerate(case_specs):
        case_output_dir = output_dir / f"pr-{case.number}"
        run_fingerprint = _pr_eval_case_run_fingerprint(case, run_kwargs)
        resumed = _load_resumable_pr_eval_result(case_output_dir, run_fingerprint) if resume else None
        if resumed is not None:
            results[index] = resumed
            continue
        pending.append((index, manifest_path, case))

    if case_jobs > 1 or case_timeout_seconds is not None:
        _run_pr_eval_cases_supervised(
            pending,
            output_dir=output_dir,
            results=results,
            case_jobs=case_jobs,
            case_timeout_seconds=case_timeout_seconds,
            run_kwargs=run_kwargs,
        )
    else:
        for index, manifest_path, case in pending:
            case_output_dir = output_dir / f"pr-{case.number}"
            try:
                results[index] = _run_one_pr_eval_case(
                    case_dir=manifest_path.parent,
                    case=case,
                    output_dir=case_output_dir,
                    status_path=case_output_dir / CASE_STATUS_FILENAME,
                    run_fingerprint=_pr_eval_case_run_fingerprint(case, run_kwargs),
                    **run_kwargs,
                )
            except Exception as exc:
                results[index] = _failed_pr_eval_result(
                    case,
                    case_output_dir,
                    status="failed",
                    phase=_read_case_status_phase(case_output_dir / CASE_STATUS_FILENAME) or "case",
                    error=str(exc),
                    run_fingerprint=_pr_eval_case_run_fingerprint(case, run_kwargs),
                )

    completed_results = [result for result in results if result is not None]
    report = _build_pr_eval_run_report(completed_results)
    _atomic_write_text(output_dir / "pr-eval-report.json", report.model_dump_json(indent=2))
    _atomic_write_text(output_dir / "pr-eval-report.md", render_pr_eval_report(report))
    if telemetry_path is not None:
        append_pr_eval_telemetry(report, telemetry_path, source_repo=repo_root, output_dir=output_dir)
    return report


def _run_pr_eval_cases_supervised(
    pending: list[tuple[int, Path, PullRequestEvalCase]],
    *,
    output_dir: Path,
    results: list[PullRequestEvalRunResult | None],
    case_jobs: int,
    case_timeout_seconds: int | None,
    run_kwargs: _PrEvalRunKwargs,
) -> None:
    context = multiprocessing.get_context("spawn")
    active: dict[int, dict[str, Any]] = {}
    pending_queue = list(pending)

    while pending_queue or active:
        while pending_queue and len(active) < case_jobs:
            index, manifest_path, case = pending_queue.pop(0)
            case_output_dir = output_dir / f"pr-{case.number}"
            case_output_dir.mkdir(parents=True, exist_ok=True)
            status_path = case_output_dir / CASE_STATUS_FILENAME
            run_fingerprint = _pr_eval_case_run_fingerprint(case, run_kwargs)
            _write_case_status(
                status_path,
                case,
                status="running",
                phase="queued",
                started_at=_now_iso(),
                run_fingerprint=run_fingerprint,
            )
            proc: BaseProcess = context.Process(
                target=_run_pr_eval_case_worker,
                kwargs={
                    "manifest_path": manifest_path,
                    "case_data": case.model_dump(mode="json"),
                    "output_dir": case_output_dir,
                    "status_path": status_path,
                    "run_fingerprint": run_fingerprint,
                    "run_kwargs": run_kwargs,
                },
            )
            proc.start()
            active[proc.pid or id(proc)] = {
                "process": proc,
                "index": index,
                "case": case,
                "output_dir": case_output_dir,
                "status_path": status_path,
                "started": time.monotonic(),
                "run_fingerprint": run_fingerprint,
            }

        finished: list[int] = []
        for key, item in active.items():
            proc: BaseProcess = item["process"]
            case: PullRequestEvalCase = item["case"]
            case_output_dir: Path = item["output_dir"]
            status_path: Path = item["status_path"]
            run_fingerprint: str = item["run_fingerprint"]
            elapsed = time.monotonic() - item["started"]
            if proc.is_alive() and case_timeout_seconds is not None and elapsed > case_timeout_seconds:
                _terminate_case_worker(proc)
                results[item["index"]] = _failed_pr_eval_result(
                    case,
                    case_output_dir,
                    status="timed_out",
                    phase=_read_case_status_phase(status_path) or "case",
                    error=f"PR eval case timed out after {case_timeout_seconds}s",
                    started_at=_read_case_status_started_at(status_path),
                    elapsed_ms=round(elapsed * 1000),
                    run_fingerprint=run_fingerprint,
                )
                finished.append(key)
                continue
            if not proc.is_alive():
                proc.join()
                result_path = case_output_dir / "eval-result.json"
                if proc.exitcode == 0 and result_path.exists():
                    result = load_pr_eval_run_result(result_path)
                    if result.run_fingerprint == run_fingerprint:
                        results[item["index"]] = result
                    else:
                        results[item["index"]] = _failed_pr_eval_result(
                            case,
                            case_output_dir,
                            status="failed",
                            phase=_read_case_status_phase(status_path) or "case",
                            error="case worker produced a stale eval-result fingerprint",
                            started_at=_read_case_status_started_at(status_path),
                            elapsed_ms=round(elapsed * 1000),
                            run_fingerprint=run_fingerprint,
                        )
                else:
                    error = _read_case_status_error(status_path) or f"case worker exited with {proc.exitcode}"
                    results[item["index"]] = _failed_pr_eval_result(
                        case,
                        case_output_dir,
                        status="failed",
                        phase=_read_case_status_phase(status_path) or "case",
                        error=error,
                        started_at=_read_case_status_started_at(status_path),
                        elapsed_ms=round(elapsed * 1000),
                        run_fingerprint=_pr_eval_case_run_fingerprint(case, run_kwargs),
                    )
                finished.append(key)
        for key in finished:
            active.pop(key, None)
        if active:
            time.sleep(0.1)


def _run_pr_eval_case_worker(
    *,
    manifest_path: Path,
    case_data: dict[str, Any],
    output_dir: Path,
    status_path: Path,
    run_fingerprint: str,
    run_kwargs: _PrEvalRunKwargs,
) -> None:
    _become_process_group_leader()
    case = PullRequestEvalCase.model_validate(case_data)
    try:
        _run_one_pr_eval_case(
            case_dir=manifest_path.parent,
            case=case,
            output_dir=output_dir,
            status_path=status_path,
            run_fingerprint=run_fingerprint,
            **run_kwargs,
        )
    except Exception as exc:
        _failed_pr_eval_result(
            case,
            output_dir,
            status="failed",
            phase=_read_case_status_phase(status_path) or "case",
            error=str(exc),
            started_at=_read_case_status_started_at(status_path),
            run_fingerprint=run_fingerprint,
        )


def _build_pr_eval_run_report(results: list[PullRequestEvalRunResult]) -> PullRequestEvalRunReport:
    return PullRequestEvalRunReport(
        cases=results,
        total=len(results),
        passed=sum(1 for result in results if result.passed),
        failed=sum(1 for result in results if not result.passed),
        partial=sum(1 for result in results if result.status == "partial"),
        timed_out=sum(1 for result in results if result.status == "timed_out"),
        quarantined=sum(1 for result in results if result.status == "quarantined"),
        skipped=sum(1 for result in results if result.status == "skipped"),
    )


def _load_resumable_pr_eval_result(output_dir: Path, run_fingerprint: str) -> PullRequestEvalRunResult | None:
    status_path = output_dir / CASE_STATUS_FILENAME
    result_path = output_dir / "eval-result.json"
    if not status_path.exists() or not result_path.exists():
        return None
    try:
        status = PrEvalCaseStatus.model_validate_json(status_path.read_text(encoding="utf-8"))
        result = load_pr_eval_run_result(result_path)
    except OSError, ValidationError, PrEvalError:
        return None
    if status.status not in {"succeeded", "partial", "quarantined", "skipped"}:
        return None
    if result.status not in {"succeeded", "partial", "quarantined", "skipped"}:
        return None
    if status.run_fingerprint != run_fingerprint or result.run_fingerprint != run_fingerprint:
        return None
    return result


def _failed_pr_eval_result(
    case: PullRequestEvalCase,
    output_dir: Path,
    *,
    status: Literal["failed", "timed_out", "quarantined", "skipped"],
    phase: str,
    error: str | None = None,
    started_at: str = "",
    elapsed_ms: int = 0,
    run_fingerprint: str | None = None,
) -> PullRequestEvalRunResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    status_path = output_dir / CASE_STATUS_FILENAME
    finished_at = _now_iso()
    if not started_at:
        started_at = finished_at
    first_pass_greptile_findings = [finding for finding in case.greptile_findings if finding.first_pass]
    scored = status not in {"timed_out", "quarantined", "skipped"}
    missed = len(first_pass_greptile_findings) if scored else 0
    result = PullRequestEvalRunResult(
        number=case.number,
        title=case.title,
        url=case.url,
        passed=status in {"quarantined", "skipped"},
        status=status,
        scored=scored,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=elapsed_ms,
        error_message=error,
        status_path=str(status_path),
        run_fingerprint=run_fingerprint,
        greptile_findings_count=len(first_pass_greptile_findings) if scored else 0,
        apex_findings_count=0,
        matched_greptile_findings=0,
        missed_greptile_findings=missed,
        extra_apex_findings=0,
        context_packs_count=0,
        llm_runs_count=0,
        report_path="",
        markdown_path="",
        warnings=[error] if error else [],
    )
    _atomic_write_text(output_dir / "eval-result.json", result.model_dump_json(indent=2))
    _write_case_status(
        status_path,
        case,
        status=status,
        phase=phase,
        started_at=started_at,
        ended_at=finished_at,
        elapsed_ms=elapsed_ms,
        error=error,
        eval_result_path=str(output_dir / "eval-result.json"),
        run_fingerprint=run_fingerprint,
    )
    return result


def _write_case_status(
    path: Path,
    case: PullRequestEvalCase,
    *,
    status: Literal["pending", "running", "succeeded", "partial", "failed", "timed_out", "quarantined", "skipped"],
    phase: str,
    started_at: str = "",
    ended_at: str | None = None,
    elapsed_ms: int = 0,
    error: str | None = None,
    eval_result_path: str | None = None,
    report_path: str | None = None,
    run_fingerprint: str | None = None,
) -> None:
    existing = _read_case_status(path)
    status_model = PrEvalCaseStatus(
        number=case.number,
        title=case.title,
        status=status,
        phase=phase,
        started_at=started_at or (existing.started_at if existing else _now_iso()),
        updated_at=_now_iso(),
        ended_at=ended_at,
        elapsed_ms=elapsed_ms,
        error=error,
        eval_result_path=eval_result_path or (existing.eval_result_path if existing else None),
        report_path=report_path or (existing.report_path if existing else None),
        run_fingerprint=run_fingerprint or (existing.run_fingerprint if existing else None),
    )
    _atomic_write_text(path, status_model.model_dump_json(indent=2))


def _read_case_status(path: Path) -> PrEvalCaseStatus | None:
    if not path.exists():
        return None
    try:
        return PrEvalCaseStatus.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError, ValidationError:
        return None


def _read_case_status_phase(path: Path) -> str:
    status = _read_case_status(path)
    return status.phase if status else ""


def _read_case_status_started_at(path: Path) -> str:
    status = _read_case_status(path)
    return status.started_at if status else ""


def _read_case_status_error(path: Path) -> str:
    status = _read_case_status(path)
    return status.error if status and status.error else ""


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _become_process_group_leader() -> None:
    if os.name != "posix":
        return
    try:
        os.setsid()
    except OSError:
        pass


def _terminate_case_worker(proc: BaseProcess, *, grace_seconds: float = 2.0) -> None:
    pid = proc.pid
    if pid is not None and os.name == "posix":
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.terminate()
        except OSError:
            proc.terminate()
    else:
        proc.terminate()
    proc.join(timeout=grace_seconds)
    if not proc.is_alive():
        return
    if pid is not None and os.name == "posix":
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            proc.kill()
        except OSError:
            proc.kill()
    else:
        proc.kill()
    proc.join(timeout=grace_seconds)


def _pr_eval_case_run_fingerprint(case: PullRequestEvalCase, run_kwargs: Mapping[str, Any]) -> str:
    labels_dir = run_kwargs.get("labels_dir")
    payload = {
        "case": case.model_dump(mode="json"),
        "run": {key: _fingerprint_value(value) for key, value in run_kwargs.items() if key not in {"repo_root"}},
        "label_digest": _label_file_digest(labels_dir, case.number) if isinstance(labels_dir, Path) else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _label_file_digest(labels_dir: Path, pr_number: int) -> str | None:
    path = pr_eval_label_path(labels_dir, pr_number)
    if not path.exists():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def load_pr_eval_case(path: Path) -> PullRequestEvalCase:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise PrEvalError(f"Unable to read PR eval manifest {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PrEvalError(f"Invalid YAML in PR eval manifest {path}: {exc}") from exc
    try:
        return PullRequestEvalCase.model_validate(raw)
    except ValidationError as exc:
        raise PrEvalError(f"Invalid PR eval manifest {path}: {exc}") from exc


def load_pr_eval_run_report(path: Path) -> PullRequestEvalRunReport:
    report_path = path / "pr-eval-report.json" if path.is_dir() else path
    try:
        return PullRequestEvalRunReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PrEvalError(f"Unable to read PR eval report {report_path}: {exc}") from exc
    except ValidationError as exc:
        raise PrEvalError(f"Invalid PR eval report {report_path}: {exc}") from exc


def load_pr_eval_run_result(path: Path) -> PullRequestEvalRunResult:
    try:
        return PullRequestEvalRunResult.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PrEvalError(f"Unable to read PR eval result {path}: {exc}") from exc
    except ValidationError as exc:
        raise PrEvalError(f"Invalid PR eval result {path}: {exc}") from exc


def render_pr_eval_report(report: PullRequestEvalRunReport) -> str:
    lines = [
        "# Apex Ray PR Replay Eval",
        "",
        f"- Cases: `{report.total}`",
        f"- Passed: `{report.passed}`",
        f"- Failed: `{report.failed}`",
        f"- Partial: `{report.partial}`",
        f"- Timed out: `{report.timed_out}`",
        f"- Quarantined: `{report.quarantined}`",
        f"- Skipped: `{report.skipped}`",
        f"- Greptile findings: `{report.greptile_findings_total}`",
        f"- Matched Greptile findings: `{report.matched_greptile_findings_total}`",
        f"- Missed Greptile findings: `{report.missed_greptile_findings_total}`",
        f"- Extra Apex findings: `{report.extra_apex_findings_total}`",
        f"- Triaged extra true positives: `{report.triaged_extra_true_positives_total}`",
        f"- Triaged extra false positives: `{report.triaged_extra_false_positives_total}`",
        f"- Triaged extra duplicates: `{report.triaged_extra_duplicates_total}`",
        f"- Triaged extra not actionable: `{report.triaged_extra_not_actionable_total}`",
        f"- Triaged extra unknown: `{report.triaged_extra_unknown_total}`",
        f"- Estimated input tokens: `~{report.estimated_input_tokens_total}`",
        "",
        "## Cases",
        "",
    ]
    if not report.cases:
        lines.append("No PR eval cases were run.")
        lines.append("")
        return "\n".join(lines)
    for case in report.cases:
        lines.append(f"### PR #{case.number}: {case.title}")
        lines.append("")
        lines.append(f"- Status: `{case.status}`")
        lines.append(f"- Gate: `{'pass' if case.passed else 'fail'}`")
        if not case.scored:
            lines.append("- Scored: `false`")
        if case.error_message:
            lines.append(f"- Error: {case.error_message}")
        lines.append(f"- URL: {case.url}")
        lines.append(f"- Greptile findings: `{case.greptile_findings_count}`")
        if case.ignored_greptile_findings:
            lines.append(f"- Ignored Greptile findings by labels: `{case.ignored_greptile_findings}`")
        lines.append(f"- Matched/missed: `{case.matched_greptile_findings}` / `{case.missed_greptile_findings}`")
        lines.append(f"- Apex findings: `{case.apex_findings_count}`")
        lines.append(f"- Extra Apex findings: `{case.extra_apex_findings}`")
        lines.append(f"- Coverage partial severity: `{case.coverage_partial_severity}`")
        lines.append(f"- Coverage gate: `{case.coverage_quality_gate_status}`")
        if case.labels_path:
            lines.append(f"- Labels: `{case.labels_path}`")
        if case.extra_apex_findings:
            lines.append(
                "- Extra triage: "
                f"`{case.triaged_extra_true_positives}` true positive / "
                f"`{case.triaged_extra_false_positives}` false positive / "
                f"`{case.triaged_extra_duplicates}` duplicate / "
                f"`{case.triaged_extra_not_actionable}` not actionable / "
                f"`{case.triaged_extra_unknown}` unknown"
            )
        lines.append(f"- Context packs: `{case.context_packs_count}`")
        lines.append(
            f"- Reviewed/unreviewed context packs: `{case.reviewed_context_packs_count}` / "
            f"`{case.unreviewed_context_packs_count}`"
        )
        lines.append(
            f"- Residual P0/P1 packs: `{case.residual_p0_context_packs_count}` / "
            f"`{case.residual_p1_context_packs_count}`"
        )
        lines.append(
            f"- LLM coverage: `{case.llm_coverage_ratio:.1%}`, source lines "
            f"`{case.source_changed_line_coverage_ratio:.1%}`, high-risk `{case.high_risk_coverage_ratio:.1%}`"
        )
        lines.append(f"- LLM runs: `{case.llm_runs_count}`")
        lines.append(
            f"- Failed LLM review/verifier runs: `{case.failed_llm_review_runs_count}` / "
            f"`{case.failed_llm_verify_runs_count}`"
        )
        lines.append(f"- LLM duration: `{case.llm_duration_ms}ms`")
        lines.append(f"- Estimated input: `{case.llm_input_chars}` chars (`~{case.llm_estimated_input_tokens}` tokens)")
        if case.matches:
            lines.append("- Greptile match details:")
            for match in case.matches:
                marker = "matched" if match.matched else "missed"
                location = _format_location(match.greptile_finding.file, match.greptile_finding.line)
                matched = f" -> {match.matched_apex_title}" if match.matched_apex_title else ""
                lines.append(
                    f"  - `{marker}` `{match.greptile_finding.severity or 'n/a'}` "
                    f"{match.greptile_finding.title} at `{location}`{matched}"
                )
        if case.extra_findings:
            lines.append("- Extra Apex findings:")
            for finding in case.extra_findings:
                location = _format_location(finding.file, finding.line)
                lines.append(f"  - `{finding.severity}` {finding.title} at `{location}`")
        if case.warnings:
            lines.append("- Warnings:")
            for warning in case.warnings:
                lines.append(f"  - {warning}")
        lines.append("")
    return "\n".join(lines)


def memory_suggestions_from_pr_eval_report(report: PullRequestEvalRunReport) -> str:
    lines = [
        "# Apex Ray Memory Suggestions From PR Replay",
        "",
        "Review these suggestions before committing them under `.apex-ray/memory/`.",
        "They are generated from missed first-pass Greptile findings and should be edited into stable project knowledge.",
        "",
    ]
    missed = [(case, match.greptile_finding) for case in report.cases for match in case.matches if not match.matched]
    if not missed:
        lines.append("No missed Greptile findings were available for memory suggestions.")
        lines.append("")
        return "\n".join(lines)

    seen: set[str] = set()
    for case, finding in missed:
        base_slug = _slugify(f"greptile-pr-{case.number}-{finding.title}") or f"greptile-pr-{case.number}"
        slug = _dedupe_slug(base_slug, seen)
        severity = _memory_severity_from_greptile(finding.severity)
        title = finding.title or "Historical Greptile finding"
        frontmatter: dict[str, Any] = {
            "id": slug,
            "title": title,
            "kind": "bug_pattern",
            "severity": severity,
        }
        if finding.file:
            frontmatter["paths"] = [finding.file]
        triggers = _memory_triggers_from_finding(finding)
        if triggers:
            frontmatter["triggers"] = {"text": triggers}
        body = _memory_body_from_greptile_finding(case, finding)
        lines.extend(
            [
                f"## {title}",
                "",
                "```md",
                "---",
                yaml.safe_dump(frontmatter, sort_keys=False).strip(),
                "---",
                body,
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def match_greptile_to_apex(
    greptile_findings: list[GreptileFinding],
    apex_findings: list[Finding],
) -> tuple[list[PrEvalFindingMatch], list[Finding]]:
    matches: list[PrEvalFindingMatch] = []
    used_apex: set[int] = set()
    for greptile_finding in greptile_findings:
        best_index = None
        best_score = 0.0
        for index, apex_finding in enumerate(apex_findings):
            if index in used_apex:
                continue
            score = _finding_similarity(greptile_finding, apex_finding)
            if score > best_score:
                best_index = index
                best_score = score
        if best_index is not None and best_score >= 0.28:
            used_apex.add(best_index)
            apex_finding = apex_findings[best_index]
            matches.append(
                PrEvalFindingMatch(
                    greptile_finding=greptile_finding,
                    matched=True,
                    matched_apex_title=apex_finding.title,
                    matched_apex_file=apex_finding.file,
                    matched_apex_line=apex_finding.line,
                    score=round(best_score, 4),
                )
            )
        else:
            matches.append(
                PrEvalFindingMatch(greptile_finding=greptile_finding, matched=False, score=round(best_score, 4))
            )
    extra = [finding for index, finding in enumerate(apex_findings) if index not in used_apex]
    return matches, extra


def pr_eval_label_path(labels_dir: Path, pr_number: int) -> Path:
    return labels_dir / f"pr-{pr_number}.yml"


def load_pr_eval_labels(labels_dir: Path | None, pr_number: int) -> PrEvalLabels:
    if labels_dir is None:
        return PrEvalLabels(pr=pr_number)
    path = pr_eval_label_path(labels_dir, pr_number)
    if not path.exists():
        return PrEvalLabels(pr=pr_number)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise PrEvalError(f"Unable to read PR eval labels {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PrEvalError(f"Invalid YAML in PR eval labels {path}: {exc}") from exc
    try:
        labels = PrEvalLabels.model_validate(raw)
    except ValidationError as exc:
        raise PrEvalError(f"Invalid PR eval labels {path}: {exc}") from exc
    if labels.pr != pr_number:
        raise PrEvalError(f"Invalid PR eval labels {path}: expected pr={pr_number}, got pr={labels.pr}")
    return labels


def write_pr_eval_label_templates(
    report: PullRequestEvalRunReport,
    output_dir: Path,
    *,
    overwrite: bool = False,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for case in report.cases:
        path = pr_eval_label_path(output_dir, case.number)
        if path.exists() and not overwrite:
            continue
        labels = PrEvalLabels(
            pr=case.number,
            updated_at=_now_iso(),
            greptile_findings={
                match.greptile_finding.id: PrEvalGreptileFindingLabel(
                    verdict="valid",
                    title=match.greptile_finding.title,
                    file=match.greptile_finding.file,
                    line=match.greptile_finding.line,
                )
                for match in case.matches
            },
            apex_findings={
                apex_finding_fingerprint(finding): PrEvalApexFindingLabel(
                    verdict="unknown",
                    title=finding.title,
                    file=finding.file,
                    line=finding.line,
                )
                for finding in case.extra_findings
            },
        )
        _atomic_write_text(
            path,
            yaml.safe_dump(labels.model_dump(mode="json", exclude_none=True), sort_keys=False),
        )
        written.append(path)
    return written


def apex_finding_fingerprint(finding: Finding) -> str:
    payload = "|".join(
        [
            _normalize_path(finding.file),
            str(finding.line or ""),
            _clean_text(finding.title).lower(),
            _clean_text(finding.failure_mode).lower()[:500],
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"apex-{digest}"


def append_pr_eval_telemetry(
    report: PullRequestEvalRunReport,
    telemetry_path: Path,
    *,
    source_repo: Path,
    output_dir: Path,
) -> Path:
    entry = PrEvalTelemetryEntry(
        run_id=uuid.uuid4().hex,
        created_at=_now_iso(),
        source_repo=str(source_repo),
        output_dir=str(output_dir),
        total=report.total,
        passed=report.passed,
        failed=report.failed,
        partial=report.partial,
        timed_out=report.timed_out,
        quarantined=report.quarantined,
        greptile_findings_total=report.greptile_findings_total,
        matched_greptile_findings_total=report.matched_greptile_findings_total,
        missed_greptile_findings_total=report.missed_greptile_findings_total,
        extra_apex_findings_total=report.extra_apex_findings_total,
        triaged_extra_true_positives_total=report.triaged_extra_true_positives_total,
        triaged_extra_false_positives_total=report.triaged_extra_false_positives_total,
        triaged_extra_duplicates_total=report.triaged_extra_duplicates_total,
        triaged_extra_not_actionable_total=report.triaged_extra_not_actionable_total,
        triaged_extra_unknown_total=report.triaged_extra_unknown_total,
        estimated_input_tokens_total=report.estimated_input_tokens_total,
        cases=[
            PrEvalTelemetryCase(
                number=case.number,
                passed=case.passed,
                status=case.status,
                scored=case.scored,
                duration_ms=case.duration_ms,
                coverage_partial_severity=case.coverage_partial_severity,
                coverage_quality_gate_status=case.coverage_quality_gate_status,
                greptile_findings_count=case.greptile_findings_count,
                ignored_greptile_findings=case.ignored_greptile_findings,
                matched_greptile_findings=case.matched_greptile_findings,
                missed_greptile_findings=case.missed_greptile_findings,
                extra_apex_findings=case.extra_apex_findings,
                triaged_extra_true_positives=case.triaged_extra_true_positives,
                triaged_extra_false_positives=case.triaged_extra_false_positives,
                triaged_extra_duplicates=case.triaged_extra_duplicates,
                triaged_extra_not_actionable=case.triaged_extra_not_actionable,
                triaged_extra_unknown=case.triaged_extra_unknown,
                context_packs_count=case.context_packs_count,
                reviewed_context_packs_count=case.reviewed_context_packs_count,
                unreviewed_context_packs_count=case.unreviewed_context_packs_count,
                residual_p0_context_packs_count=case.residual_p0_context_packs_count,
                residual_p1_context_packs_count=case.residual_p1_context_packs_count,
                failed_llm_review_runs_count=case.failed_llm_review_runs_count,
                failed_llm_verify_runs_count=case.failed_llm_verify_runs_count,
                llm_coverage_ratio=case.llm_coverage_ratio,
                source_changed_line_coverage_ratio=case.source_changed_line_coverage_ratio,
                high_risk_coverage_ratio=case.high_risk_coverage_ratio,
                llm_runs_count=case.llm_runs_count,
                llm_duration_ms=case.llm_duration_ms,
                llm_estimated_input_tokens=case.llm_estimated_input_tokens,
                llm_cache_hits=case.llm_cache_hits,
                llm_cache_misses=case.llm_cache_misses,
            )
            for case in report.cases
        ],
    )
    telemetry_path.parent.mkdir(parents=True, exist_ok=True)
    with telemetry_path.open("a", encoding="utf-8") as handle:
        handle.write(entry.model_dump_json() + "\n")
    return telemetry_path


def load_pr_eval_telemetry(path: Path) -> list[PrEvalTelemetryEntry]:
    if not path.exists():
        return []
    entries: list[PrEvalTelemetryEntry] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PrEvalError(f"Unable to read PR eval telemetry {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entries.append(PrEvalTelemetryEntry.model_validate_json(line))
        except ValidationError as exc:
            raise PrEvalError(f"Invalid telemetry entry {path}:{line_number}: {exc}") from exc
    return entries


def render_pr_eval_telemetry_summary(entries: list[PrEvalTelemetryEntry]) -> str:
    lines = ["# Apex Ray PR Eval Telemetry", ""]
    if not entries:
        lines.append("No telemetry entries found.")
        lines.append("")
        return "\n".join(lines)
    latest = entries[-1]
    latest_failed_llm_runs = sum(
        case.failed_llm_review_runs_count + case.failed_llm_verify_runs_count for case in latest.cases
    )
    latest_unreviewed_packs = sum(case.unreviewed_context_packs_count for case in latest.cases)
    latest_llm_duration_ms = sum(case.llm_duration_ms for case in latest.cases)
    lines.extend(
        [
            f"- Runs: `{len(entries)}`",
            f"- Latest run: `{latest.created_at}` (`{latest.run_id}`)",
            f"- Latest cases: `{latest.total}`",
            f"- Latest matched Greptile findings: `{latest.matched_greptile_findings_total}/{latest.greptile_findings_total}`",
            f"- Latest missed Greptile findings: `{latest.missed_greptile_findings_total}`",
            f"- Latest extra Apex findings: `{latest.extra_apex_findings_total}`",
            f"- Latest triaged extra true positives: `{latest.triaged_extra_true_positives_total}`",
            f"- Latest triaged extra false positives: `{latest.triaged_extra_false_positives_total}`",
            f"- Latest triaged extra duplicates: `{latest.triaged_extra_duplicates_total}`",
            f"- Latest triaged extra not actionable: `{latest.triaged_extra_not_actionable_total}`",
            f"- Latest estimated input tokens: `~{latest.estimated_input_tokens_total}`",
            f"- Latest LLM duration: `{latest_llm_duration_ms}ms`",
            f"- Latest unreviewed context packs: `{latest_unreviewed_packs}`",
            f"- Latest failed LLM runs: `{latest_failed_llm_runs}`",
            "",
            "## Recent Runs",
            "",
        ]
    )
    for entry in entries[-20:]:
        lines.append(
            f"- `{entry.created_at}` `{entry.run_id}` - cases `{entry.total}`, "
            f"matched `{entry.matched_greptile_findings_total}/{entry.greptile_findings_total}`, "
            f"missed `{entry.missed_greptile_findings_total}`, extra `{entry.extra_apex_findings_total}`, "
            f"tokens `~{entry.estimated_input_tokens_total}`, "
            f"unreviewed packs `{sum(case.unreviewed_context_packs_count for case in entry.cases)}`, "
            f"failed LLM runs `{sum(case.failed_llm_review_runs_count + case.failed_llm_verify_runs_count for case in entry.cases)}`"
        )
    lines.append("")
    return "\n".join(lines)


def _apply_greptile_labels(
    findings: list[GreptileFinding],
    labels: PrEvalLabels | None,
) -> list[GreptileFinding]:
    if labels is None:
        return findings
    filtered: list[GreptileFinding] = []
    for finding in findings:
        label = labels.greptile_findings.get(finding.id)
        verdict = label.verdict if label else "valid"
        if verdict in {"not_issue", "out_of_scope"}:
            continue
        filtered.append(finding)
    return filtered


def _apex_extra_label_counts(
    findings: list[Finding],
    labels: PrEvalLabels | None,
) -> dict[str, int]:
    counts = {
        "true_positive": 0,
        "false_positive": 0,
        "duplicate": 0,
        "not_actionable": 0,
        "unknown": 0,
    }
    for finding in findings:
        verdict = _apex_label_verdict(finding, labels)
        counts[verdict] += 1
    return counts


def _blocking_extra_findings(findings: list[Finding], labels: PrEvalLabels | None) -> list[Finding]:
    return [
        finding for finding in findings if _apex_label_verdict(finding, labels) not in {"true_positive", "duplicate"}
    ]


def _apex_label_verdict(finding: Finding, labels: PrEvalLabels | None) -> str:
    if labels is None:
        return "unknown"
    label = labels.apex_findings.get(apex_finding_fingerprint(finding))
    return label.verdict if label else "unknown"


def _warnings_indicate_partial_analysis(warnings: list[str]) -> bool:
    partial_markers = (
        "partial TypeScript analyzer result",
        "TypeScript analyzer unavailable",
        "TypeScript analyzer failed",
        "TypeScript analyzer timed out",
    )
    return any(any(marker in warning for marker in partial_markers) for warning in warnings)


@contextmanager
def _git_worktree_lock(repo_root: Path):
    lock_path = _git_common_dir(repo_root) / "apex-ray-pr-eval-worktree.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _git_common_dir(repo_root: Path) -> Path:
    proc = git.run_git(["rev-parse", "--git-common-dir"], cwd=repo_root, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return repo_root / ".git"
    common_dir = Path(proc.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = repo_root / common_dir
    return common_dir.resolve()


def _run_one_pr_eval_case(
    *,
    repo_root: Path,
    case_dir: Path,
    case: PullRequestEvalCase,
    output_dir: Path,
    llm_enabled: bool,
    provider_override: LLMProviderName | None,
    model_override: str | None,
    verify_override: bool | None,
    cache_enabled: bool | None,
    refresh_cache: bool,
    cache_dir: Path | None,
    llm_jobs: int | None,
    llm_coverage_mode: str | None,
    llm_max_deep_packs: int | None,
    llm_max_input_tokens: int | None,
    analyzer_timeout_seconds: int | None,
    allow_extra_findings: bool,
    labels_dir: Path | None,
    status_path: Path | None = None,
    run_fingerprint: str | None = None,
) -> PullRequestEvalRunResult:
    started_monotonic = time.monotonic()
    started_at = _now_iso()
    output_dir.mkdir(parents=True, exist_ok=True)
    if status_path is not None:
        _write_case_status(
            status_path,
            case,
            status="running",
            phase="prepare",
            started_at=started_at,
            run_fingerprint=run_fingerprint,
        )
    labels = load_pr_eval_labels(labels_dir, case.number) if labels_dir is not None else None
    labels_path = pr_eval_label_path(labels_dir, case.number) if labels_dir is not None else None
    if labels is not None and labels.case_status == "quarantined":
        return _failed_pr_eval_result(
            case,
            output_dir,
            status="quarantined",
            phase="labels",
            error=labels.case_status_reason or "PR eval case quarantined by labels.",
            started_at=started_at,
            run_fingerprint=run_fingerprint,
        )
    diff_path = case_dir / case.diff_path
    if not diff_path.exists():
        raise PrEvalError(f"PR #{case.number}: diff file does not exist: {diff_path}")
    with tempfile.TemporaryDirectory(prefix=f"apex-ray-pr-{case.number}-") as tmp:
        worktree = Path(tmp) / "repo"
        replay_head_sha = case.replay_head_sha or case.head_sha
        if status_path is not None:
            _write_case_status(
                status_path,
                case,
                status="running",
                phase="worktree",
                started_at=started_at,
                run_fingerprint=run_fingerprint,
            )
        with _git_worktree_lock(repo_root):
            _ensure_commit_available(repo_root, replay_head_sha)
            git.run_git(["worktree", "add", "--detach", "--force", str(worktree), replay_head_sha], cwd=repo_root)
        try:
            if status_path is not None:
                _write_case_status(
                    status_path,
                    case,
                    status="running",
                    phase="config",
                    started_at=started_at,
                    run_fingerprint=run_fingerprint,
                )
            _overlay_current_apex_config(repo_root, worktree)
            config_path = worktree / ".apex-ray" / "config.yml"
            try:
                config, loaded_config_path = load_config(worktree, config_path if config_path.exists() else None)
            except ConfigError as exc:
                raise PrEvalError(f"PR #{case.number}: invalid Apex Ray config: {exc}") from exc
            config.llm.enabled = llm_enabled
            if provider_override:
                config.llm.provider = provider_override
            if model_override:
                config.llm.model = model_override
                config.llm.profiles = {}
                config.llm.routing = LLMRoutingConfig()
            if verify_override is not None:
                config.llm.verify = verify_override
            if cache_enabled is not None:
                config.llm.cache_enabled = cache_enabled and config.llm.cache_enabled
            if refresh_cache:
                config.llm.refresh_cache = True
            if cache_dir:
                config.llm.cache_dir = str(cache_dir)
            elif config.llm.cache_enabled and not config.llm.cache_dir:
                config.llm.cache_dir = str(repo_root / ".apex-ray" / "cache" / "llm")
            if llm_jobs is not None:
                config.llm.jobs = llm_jobs
            if llm_coverage_mode is not None:
                try:
                    config.llm.coverage_mode = LLMCoverageMode(llm_coverage_mode)
                except ValueError as exc:
                    raise PrEvalError(f"PR #{case.number}: unsupported LLM coverage mode: {llm_coverage_mode}") from exc
            if llm_max_deep_packs is not None:
                config.llm.max_deep_packs = llm_max_deep_packs
            if llm_max_input_tokens is not None:
                config.llm.max_input_tokens = llm_max_input_tokens
            if analyzer_timeout_seconds is not None:
                config.analyzer.timeout_seconds = analyzer_timeout_seconds

            if status_path is not None:
                _write_case_status(
                    status_path,
                    case,
                    status="running",
                    phase="pipeline",
                    started_at=started_at,
                    run_fingerprint=run_fingerprint,
                )
            report = run_review_pipeline(
                worktree,
                diff_path.read_text(encoding="utf-8"),
                TargetMode.PATCH,
                config,
                config_path=loaded_config_path,
            )
        finally:
            with _git_worktree_lock(repo_root):
                git.run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo_root, check=False)

    report_json_path = output_dir / "apex-report.json"
    report_md_path = output_dir / "apex-report.md"
    _atomic_write_text(report_json_path, report.model_dump_json(indent=2))
    _atomic_write_text(report_md_path, render_markdown(report))

    if status_path is not None:
        _write_case_status(
            status_path,
            case,
            status="running",
            phase="scoring",
            started_at=started_at,
            run_fingerprint=run_fingerprint,
        )
    first_pass_greptile_findings = [finding for finding in case.greptile_findings if finding.first_pass]
    greptile_findings = _apply_greptile_labels(first_pass_greptile_findings, labels)
    ignored_greptile_findings = len(first_pass_greptile_findings) - len(greptile_findings)
    matches, extra = match_greptile_to_apex(greptile_findings, report.findings)
    extra_labels = _apex_extra_label_counts(extra, labels)
    blocking_extra = _blocking_extra_findings(extra, labels)
    warnings = [*report.diff.warnings]
    for analyzer_result in report.analyzer_results:
        warnings.extend(analyzer_result.warnings)
    analysis_partial = _warnings_indicate_partial_analysis(warnings)
    coverage_partial = report.llm_coverage.partial_severity != "none"
    result_status: Literal["succeeded", "partial"] = "partial" if analysis_partial or coverage_partial else "succeeded"
    finished_at = _now_iso()
    elapsed_ms = round((time.monotonic() - started_monotonic) * 1000)
    result = PullRequestEvalRunResult(
        number=case.number,
        title=case.title,
        url=case.url,
        passed=all(match.matched for match in matches) and (allow_extra_findings or not blocking_extra),
        status=result_status,
        analysis_partial=analysis_partial,
        coverage_partial_severity=report.llm_coverage.partial_severity,
        coverage_quality_gate_status=report.llm_coverage.quality_gate_status,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=elapsed_ms,
        status_path=str(status_path) if status_path else None,
        run_fingerprint=run_fingerprint,
        greptile_findings_count=len(greptile_findings),
        ignored_greptile_findings=ignored_greptile_findings,
        apex_findings_count=len(report.findings),
        matched_greptile_findings=sum(1 for match in matches if match.matched),
        missed_greptile_findings=sum(1 for match in matches if not match.matched),
        extra_apex_findings=len(extra),
        triaged_extra_true_positives=extra_labels["true_positive"],
        triaged_extra_false_positives=extra_labels["false_positive"],
        triaged_extra_duplicates=extra_labels["duplicate"],
        triaged_extra_not_actionable=extra_labels["not_actionable"],
        triaged_extra_unknown=extra_labels["unknown"],
        context_packs_count=len(report.context_packs),
        reviewed_context_packs_count=report.llm_coverage.reviewed_context_packs,
        unreviewed_context_packs_count=report.llm_coverage.unreviewed_context_packs,
        residual_p0_context_packs_count=len(report.llm_coverage.residual_risk_p0_context_pack_ids),
        residual_p1_context_packs_count=len(report.llm_coverage.residual_risk_p1_context_pack_ids),
        failed_llm_review_runs_count=report.llm_coverage.failed_review_runs,
        failed_llm_verify_runs_count=report.llm_coverage.failed_verify_runs,
        llm_coverage_ratio=report.llm_coverage.coverage_ratio,
        source_changed_line_coverage_ratio=report.llm_coverage.source_changed_line_coverage_ratio,
        high_risk_coverage_ratio=report.llm_coverage.high_risk_coverage_ratio,
        llm_runs_count=len(report.llm_runs),
        llm_duration_ms=report.llm_coverage.total_duration_ms,
        llm_input_chars=report.llm_coverage.input_chars,
        llm_estimated_input_tokens=report.llm_coverage.estimated_input_tokens,
        llm_cache_hits=report.llm_coverage.cache_hits,
        llm_cache_misses=report.llm_coverage.cache_misses,
        report_path=str(report_json_path),
        markdown_path=str(report_md_path),
        labels_path=str(labels_path) if labels_path and labels_path.exists() else None,
        matches=matches,
        extra_findings=extra,
        warnings=warnings,
    )
    eval_result_path = output_dir / "eval-result.json"
    _atomic_write_text(eval_result_path, result.model_dump_json(indent=2))
    if status_path is not None:
        _write_case_status(
            status_path,
            case,
            status=result_status,
            phase="done",
            started_at=started_at,
            ended_at=finished_at,
            elapsed_ms=elapsed_ms,
            eval_result_path=str(eval_result_path),
            report_path=str(report_json_path),
            run_fingerprint=run_fingerprint,
        )
    return result


def _load_prs(repo_root: Path, pr_numbers: list[int] | None, limit: int) -> list[dict[str, Any]]:
    fields = "number,title,url,author,baseRefName,headRefName,baseRefOid,headRefOid,mergeCommit,createdAt,mergedAt"
    if pr_numbers:
        return [_run_gh_json(["pr", "view", str(number), "--json", fields], repo_root) for number in pr_numbers]
    return _run_gh_json(["pr", "list", "--state", "merged", "--limit", str(limit), "--json", fields], repo_root)


def _load_pr_commit_oids(repo_root: Path, number: int) -> list[str]:
    data = _run_gh_json(["pr", "view", str(number), "--json", "commits"], repo_root)
    commits = data.get("commits", [])
    if not isinstance(commits, list):
        return []
    return [str(commit.get("oid")) for commit in commits if isinstance(commit, dict) and commit.get("oid")]


def _load_greptile_comments(owner_repo: str, number: int, repo_root: Path) -> list[GreptileComment]:
    pr = _run_gh_json(["pr", "view", str(number), "--json", "comments,reviews"], repo_root)
    comments: list[GreptileComment] = []
    for raw in pr.get("comments", []):
        author = _author_login(raw)
        if not _is_greptile_author(author):
            continue
        comments.append(
            GreptileComment(
                id=str(raw.get("id") or raw.get("url") or f"issue-{len(comments)}"),
                source="issue_comment",
                author=author,
                body=str(raw.get("body") or ""),
                url=raw.get("url"),
                created_at=str(raw.get("createdAt") or ""),
                updated_at=raw.get("updatedAt"),
                includes_created_edit=bool(raw.get("includesCreatedEdit", False)),
            )
        )
    for raw in pr.get("reviews", []):
        author = _author_login(raw)
        if not _is_greptile_author(author):
            continue
        comments.append(
            GreptileComment(
                id=str(raw.get("id") or f"review-{len(comments)}"),
                source="review",
                author=author,
                body=str(raw.get("body") or ""),
                url=raw.get("url"),
                commit_id=(raw.get("commit") or {}).get("oid") if isinstance(raw.get("commit"), dict) else None,
                created_at=str(raw.get("submittedAt") or ""),
            )
        )
    review_comments = _run_gh_api_paginated_array(f"repos/{owner_repo}/pulls/{number}/comments", repo_root)
    for raw in review_comments:
        author = _author_login(raw)
        if not _is_greptile_author(author):
            continue
        comments.append(
            GreptileComment(
                id=str(raw.get("id") or raw.get("html_url") or f"review-comment-{len(comments)}"),
                source="review_comment",
                author=author,
                body=str(raw.get("body") or ""),
                file=raw.get("path"),
                line=raw.get("line"),
                original_line=raw.get("original_line"),
                url=raw.get("html_url"),
                commit_id=raw.get("commit_id"),
                original_commit_id=raw.get("original_commit_id"),
                created_at=str(raw.get("created_at") or ""),
                updated_at=raw.get("updated_at"),
            )
        )
    return sorted(comments, key=lambda item: (_parse_iso(item.created_at), item.source, item.id))


def _greptile_findings_from_comments(
    comments: list[GreptileComment],
    first_pass_window_minutes: int,
) -> list[GreptileFinding]:
    first_at = min((_parse_iso(comment.created_at) for comment in comments), default=None)
    first_pass_cutoff = first_at + timedelta(minutes=first_pass_window_minutes) if first_at else None
    findings: list[GreptileFinding] = []
    for comment in comments:
        created_at = _parse_iso(comment.created_at)
        first_pass = first_pass_cutoff is None or created_at <= first_pass_cutoff
        if comment.source == "review_comment":
            findings.append(_finding_from_review_comment(comment, first_pass))
        elif comment.source == "issue_comment":
            findings.extend(_findings_from_summary_comment(comment, first_pass and not comment.includes_created_edit))
    return findings


def _finding_from_review_comment(comment: GreptileComment, first_pass: bool) -> GreptileFinding:
    return GreptileFinding(
        id=comment.id,
        source="review_comment",
        title=_greptile_title(comment.body),
        body=_trim_body(_strip_prompt_details(comment.body)),
        severity=_greptile_priority(comment.body),
        file=comment.file,
        line=comment.line or comment.original_line,
        original_line=comment.original_line,
        url=comment.url,
        commit_id=comment.commit_id,
        original_commit_id=comment.original_commit_id,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
        first_pass=first_pass,
    )


def _findings_from_summary_comment(comment: GreptileComment, first_pass: bool) -> list[GreptileFinding]:
    findings: list[GreptileFinding] = []
    prompt_match = re.search(
        r"<details><summary>Prompt To Fix All With AI</summary>(?P<body>.*?)</details>",
        comment.body,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not prompt_match:
        return findings
    body = prompt_match.group("body")
    issue_pattern = re.compile(
        r"### Issue (?P<index>\d+) of \d+\s*\n(?P<file>[^:\n`]+):(?P<line>\d+)(?:-\d+)?\s*\n(?P<issue>.*?)(?=\n### Issue \d+ of \d+|\n`````|$)",
        flags=re.DOTALL,
    )
    for match in issue_pattern.finditer(body):
        issue_body = match.group("issue").strip()
        findings.append(
            GreptileFinding(
                id=f"{comment.id}:summary-issue-{match.group('index')}",
                source="summary_issue",
                title=_greptile_title(issue_body),
                body=_trim_body(issue_body),
                file=match.group("file").strip(),
                line=int(match.group("line")),
                url=comment.url,
                created_at=comment.created_at,
                updated_at=comment.updated_at,
                first_pass=first_pass,
            )
        )
    return findings


def _github_name_with_owner(repo_root: Path) -> str:
    data = _run_gh_json(["repo", "view", "--json", "nameWithOwner"], repo_root)
    value = data.get("nameWithOwner")
    if not value:
        raise PrEvalError("Unable to resolve GitHub repository nameWithOwner via gh.")
    return str(value)


def _run_gh_json(args: list[str], cwd: Path) -> Any:
    proc = _run_gh(args, cwd)
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise PrEvalError(f"Unable to parse gh JSON output for {' '.join(args)}: {exc}") from exc


def _run_gh_api_paginated_array(path: str, cwd: Path) -> list[dict[str, Any]]:
    payload = _run_gh_json(["api", path, "--paginate", "--slurp"], cwd)
    if isinstance(payload, list) and all(isinstance(page, list) for page in payload):
        return [item for page in payload for item in page if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise PrEvalError(f"Expected gh api {path} to return a JSON array.")


def _run_gh_text(args: list[str], cwd: Path) -> str:
    return _run_gh(args, cwd).stdout


def _run_gh(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    if shutil.which("gh") is None:
        raise PrEvalError("GitHub CLI `gh` is not available.")
    proc = subprocess.run(["gh", *args], cwd=cwd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        details = proc.stderr.strip() or proc.stdout.strip()
        raise PrEvalError(f"gh {' '.join(args)} failed ({proc.returncode}): {details}")
    return proc


def _merge_commit_oid(pr: dict[str, Any]) -> str | None:
    merge_commit = pr.get("mergeCommit")
    if isinstance(merge_commit, dict):
        return merge_commit.get("oid")
    return None


def _write_case_manifest(path: Path, case: PullRequestEvalCase) -> None:
    _atomic_write_text(
        path,
        yaml.safe_dump(case.model_dump(mode="json", exclude_none=True), sort_keys=False),
    )


def _case_manifest_paths(cases_dir: Path, pr_numbers: list[int] | None, limit: int | None) -> list[Path]:
    if pr_numbers:
        paths = [cases_dir / f"pr-{number}" / "manifest.yml" for number in pr_numbers]
    else:
        paths = sorted(cases_dir.glob("pr-*/manifest.yml"), key=lambda path: _pr_number_from_case_path(path))
    if limit is not None:
        paths = paths[:limit]
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise PrEvalError("Missing PR eval manifest(s): " + ", ".join(str(path) for path in missing))
    return paths


def _pr_number_from_case_path(path: Path) -> int:
    match = re.search(r"pr-(\d+)", path.as_posix())
    return int(match.group(1)) if match else 0


def _replay_head_sha_from_findings(findings: list[GreptileFinding]) -> str | None:
    first_pass_shas = [
        finding.original_commit_id or finding.commit_id
        for finding in findings
        if finding.first_pass and (finding.original_commit_id or finding.commit_id)
    ]
    if not first_pass_shas:
        return None
    return first_pass_shas[0]


def _replay_base_sha(
    repo_root: Path,
    owner_repo: str,
    pr_commit_oids: list[str],
    replay_head_sha: str,
    default_base_sha: str,
) -> str:
    if replay_head_sha not in pr_commit_oids or not pr_commit_oids:
        return default_base_sha
    first_pr_commit = pr_commit_oids[0]
    return _github_commit_first_parent(owner_repo, first_pr_commit, repo_root) or default_base_sha


def _github_commit_first_parent(owner_repo: str, sha: str, repo_root: Path) -> str | None:
    data = _run_gh_json(["api", f"repos/{owner_repo}/commits/{sha}"], repo_root)
    parents = data.get("parents", [])
    if isinstance(parents, list) and parents and isinstance(parents[0], dict):
        parent = parents[0].get("sha")
        return str(parent) if parent else None
    return None


def _pr_diff_from_git(
    repo_root: Path,
    owner_repo: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    *,
    allow_pr_diff_fallback: bool = False,
) -> str:
    if not base_sha or not head_sha:
        raise PrEvalError(f"PR #{pr_number}: missing base/head commit SHA for diff capture.")
    errors: list[str] = []
    try:
        _ensure_commit_available(repo_root, base_sha, pr_number=pr_number)
        _ensure_commit_available(repo_root, head_sha, pr_number=pr_number)
        proc = git.run_git(
            ["diff", "--no-ext-diff", "--find-renames", "--find-copies", f"{base_sha}...{head_sha}"],
            cwd=repo_root,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        errors.append(proc.stderr.strip() or f"git diff {base_sha}...{head_sha} returned {proc.returncode}")
    except PrEvalError as exc:
        errors.append(str(exc))
    diff = _github_compare_diff(owner_repo, base_sha, head_sha, repo_root)
    if not diff.strip() and allow_pr_diff_fallback:
        diff = _run_gh_text(["pr", "diff", str(pr_number)], repo_root)
    if not diff.strip():
        detail = "; ".join(error for error in errors if error)
        suffix = f" Local git diff failed first: {detail}" if detail else ""
        raise PrEvalError(f"PR #{pr_number}: captured diff is empty.{suffix}")
    return diff


def _github_compare_diff(owner_repo: str, base_sha: str, head_sha: str, repo_root: Path) -> str:
    try:
        return _run_gh_text(
            [
                "api",
                f"repos/{owner_repo}/compare/{base_sha}...{head_sha}",
                "-H",
                "Accept: application/vnd.github.v3.diff",
            ],
            repo_root,
        )
    except PrEvalError:
        return ""


def _ensure_commit_available(repo_root: Path, sha: str, *, pr_number: int | None = None) -> None:
    if git.run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_root, check=False).returncode == 0:
        return
    fetch_attempts = [["fetch", "origin", sha, "--depth=1"]]
    if pr_number is not None:
        fetch_attempts.extend(
            [
                ["fetch", "origin", f"+refs/pull/{pr_number}/head:refs/apex-ray/pr-eval/{pr_number}/head", "--depth=1"],
                [
                    "fetch",
                    "origin",
                    f"+refs/pull/{pr_number}/merge:refs/apex-ray/pr-eval/{pr_number}/merge",
                    "--depth=1",
                ],
            ]
        )
    errors: list[str] = []
    for args in fetch_attempts:
        proc = git.run_git(args, cwd=repo_root, check=False)
        if (
            proc.returncode == 0
            and git.run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_root, check=False).returncode == 0
        ):
            return
        if proc.returncode != 0:
            errors.append(proc.stderr.strip() or f"git {' '.join(args)} failed with {proc.returncode}")
    detail = "; ".join(error for error in errors if error)
    suffix = f": {detail}" if detail else ""
    raise PrEvalError(f"Commit {sha} is not available locally and could not be fetched{suffix}")


def _overlay_current_apex_config(source_repo: Path, worktree: Path) -> None:
    source = source_repo / ".apex-ray"
    if not source.exists():
        return
    target = worktree / ".apex-ray"
    if target.exists():
        shutil.rmtree(target)
    ignore = shutil.ignore_patterns("cache", "config.local.yml", "telemetry", "reports", "eval", "evals")
    shutil.copytree(source, target, ignore=ignore)


def _is_greptile_author(author: str) -> bool:
    normalized = author.removesuffix("[bot]").lower()
    return any(normalized.startswith(prefix) for prefix in GREPTILE_AUTHOR_PREFIXES)


def _author_login(raw: dict[str, Any]) -> str:
    user = raw.get("user")
    if isinstance(user, dict):
        return str(user.get("login") or "")
    author = raw.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or "")
    return ""


def _parse_iso(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _greptile_title(body: str) -> str:
    for pattern in (
        r"\*\*([^*\n]+)\*\*",
        r"### [^\n]*\n+\*\*([^*\n]+)\*\*",
        r"Comment:\s*\n\*\*([^*\n]+)\*\*",
    ):
        match = re.search(pattern, body)
        if match:
            return _clean_text(match.group(1))[:180]
    first = next((line.strip() for line in body.splitlines() if line.strip()), "Greptile finding")
    first = re.sub(r"<[^>]+>", "", first)
    return _clean_text(first)[:180] or "Greptile finding"


def _greptile_priority(body: str) -> str | None:
    match = re.search(r'alt="(P\d)"|badges/(p\d)\.svg', body, flags=re.IGNORECASE)
    if not match:
        return None
    return (match.group(1) or match.group(2)).upper()


def _strip_prompt_details(body: str) -> str:
    return re.sub(
        r"<details><summary>Prompt To Fix With AI</summary>.*?</details>", "", body, flags=re.DOTALL | re.IGNORECASE
    ).strip()


def _trim_body(body: str) -> str:
    return _clean_text(body)[:DEFAULT_MAX_BODY_CHARS]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").removeprefix("./")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _memory_body_from_greptile_finding(case: PullRequestEvalRunResult, finding: GreptileFinding) -> str:
    body = _clean_greptile_body(finding.body)
    location = _format_location(finding.file, finding.line)
    parts = [
        f"Historical Greptile first-pass finding from PR #{case.number}: {case.title}",
        f"PR: {case.url}",
        f"Location: {location}",
        "",
        f"Pattern to check: {body or finding.title}",
        "",
        "When similar code changes, verify the diff preserves the invariant and includes a regression test.",
    ]
    return "\n".join(parts)


def _clean_greptile_body(value: str) -> str:
    text = _strip_prompt_details(value)
    text = re.sub(r"!\[[^\]]*]\([^)]+\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return _trim_body(text)


def _memory_severity_from_greptile(severity: str | None) -> str:
    normalized = (severity or "").upper()
    if normalized in {"P0", "P1"}:
        return "high"
    if normalized == "P2":
        return "medium"
    return "low" if normalized == "P3" else "medium"


def _memory_triggers_from_finding(finding: GreptileFinding) -> list[str]:
    text = " ".join([finding.title, finding.body])
    code_terms = []
    for term in re.findall(r"`([^`\n]{3,80})`", text):
        cleaned = _clean_text(term)
        if cleaned and cleaned not in code_terms:
            code_terms.append(cleaned)
    return code_terms[:8]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:96].strip("-")


def _dedupe_slug(slug: str, seen: set[str]) -> str:
    if slug not in seen:
        seen.add(slug)
        return slug
    suffix = 2
    while f"{slug}-{suffix}" in seen:
        suffix += 1
    deduped = f"{slug}-{suffix}"
    seen.add(deduped)
    return deduped


def _finding_similarity(greptile_finding: GreptileFinding, apex_finding: Finding) -> float:
    score = 0.0
    if greptile_finding.file and apex_finding.file == greptile_finding.file:
        score += 0.3
    elif greptile_finding.file:
        return 0.0
    greptile_text = " ".join([greptile_finding.title, greptile_finding.body])
    apex_text = " ".join([apex_finding.title, apex_finding.failure_mode, apex_finding.evidence])
    token_overlap = _token_jaccard(greptile_text, apex_text)
    line_close = False
    if greptile_finding.line and apex_finding.line:
        delta = abs(greptile_finding.line - apex_finding.line)
        if delta == 0:
            score += 0.2
            line_close = True
        elif delta <= 5:
            score += 0.1
            line_close = True
        elif delta <= 20:
            if token_overlap < 0.08:
                return 0.0
            score += 0.05
            line_close = True
    if greptile_finding.file and not line_close and token_overlap < 0.12:
        return 0.0
    score += 0.5 * token_overlap
    return score


def _token_jaccard(left: str, right: str) -> float:
    left_tokens = _important_tokens(left)
    right_tokens = _important_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _important_tokens(value: str) -> set[str]:
    stop = {
        "the",
        "and",
        "or",
        "to",
        "a",
        "an",
        "of",
        "in",
        "is",
        "are",
        "with",
        "for",
        "this",
        "that",
        "it",
        "be",
        "not",
        "no",
        "on",
        "line",
        "comment",
        "issue",
        "fix",
    }
    return {token.lower() for token in _TOKEN_RE.findall(value) if len(token) >= 3 and token.lower() not in stop}


def _format_location(file: str | None, line: int | None) -> str:
    if not file:
        return "n/a"
    return f"{file}:{line}" if line else file
