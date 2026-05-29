import json
import multiprocessing
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Literal, TypedDict

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is not available on Windows.
    fcntl = None

from apex_ray import git
from apex_ray.config import ConfigError, load_config
from apex_ray.invocation import ReviewOverrides, apply_review_overrides
from apex_ray.models import (
    LLMCoverageMode,
    LLMProviderName,
    TargetMode,
)
from apex_ray.pipeline import run_review_pipeline
from apex_ray.pr_eval import github as _github_helpers
from apex_ray.pr_eval import state as _run_state
from apex_ray.pr_eval.greptile import (
    greptile_findings_from_comments as _greptile_findings_from_comments,
)
from apex_ray.pr_eval.greptile import (
    parse_iso as _parse_iso,
)
from apex_ray.pr_eval.matching import (
    apex_extra_label_counts as _apex_extra_label_counts,
)
from apex_ray.pr_eval.matching import (
    apex_finding_fingerprint as apex_finding_fingerprint,
)
from apex_ray.pr_eval.matching import (
    apply_greptile_labels as _apply_greptile_labels,
)
from apex_ray.pr_eval.matching import (
    blocking_extra_findings as _blocking_extra_findings,
)
from apex_ray.pr_eval.matching import (
    match_greptile_to_apex as match_greptile_to_apex,
)
from apex_ray.pr_eval.models import (
    DEFAULT_FIRST_PASS_WINDOW_MINUTES,
    GreptileComment,
    GreptileFinding,
    PullRequestEvalCaptureResult,
    PullRequestEvalCase,
    PullRequestEvalRunReport,
    PullRequestEvalRunResult,
)
from apex_ray.pr_eval.models import (
    PrEvalCaseStatus as PrEvalCaseStatus,
)
from apex_ray.pr_eval.models import (
    PrEvalFindingMatch as PrEvalFindingMatch,
)
from apex_ray.pr_eval.report import (
    memory_suggestions_from_pr_eval_report as memory_suggestions_from_pr_eval_report,
)
from apex_ray.pr_eval.report import (
    render_pr_eval_report as render_pr_eval_report,
)
from apex_ray.pr_eval.report import (
    render_pr_eval_telemetry_summary as render_pr_eval_telemetry_summary,
)
from apex_ray.pr_eval.state import CASE_STATUS_FILENAME
from apex_ray.pr_eval.store import PrEvalError as PrEvalError
from apex_ray.pr_eval.store import append_pr_eval_telemetry as append_pr_eval_telemetry
from apex_ray.pr_eval.store import atomic_write_text as _atomic_write_text
from apex_ray.pr_eval.store import load_pr_eval_case as load_pr_eval_case
from apex_ray.pr_eval.store import load_pr_eval_labels as load_pr_eval_labels
from apex_ray.pr_eval.store import load_pr_eval_run_report as load_pr_eval_run_report
from apex_ray.pr_eval.store import load_pr_eval_run_result as load_pr_eval_run_result
from apex_ray.pr_eval.store import load_pr_eval_telemetry as load_pr_eval_telemetry
from apex_ray.pr_eval.store import now_iso as _now_iso
from apex_ray.pr_eval.store import pr_eval_label_path as pr_eval_label_path
from apex_ray.pr_eval.store import write_pr_eval_label_templates as write_pr_eval_label_templates
from apex_ray.report import render_markdown

DEFAULT_LABELS_DIR = ".apex-ray/eval/labels"
DEFAULT_TELEMETRY_PATH = ".apex-ray/eval/telemetry/pr-eval-runs.jsonl"


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
    return _run_state.build_pr_eval_run_report(results)


def _load_resumable_pr_eval_result(output_dir: Path, run_fingerprint: str) -> PullRequestEvalRunResult | None:
    return _run_state.load_resumable_pr_eval_result(output_dir, run_fingerprint)


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
    return _run_state.failed_pr_eval_result(
        case,
        output_dir,
        status=status,
        phase=phase,
        error=error,
        started_at=started_at,
        elapsed_ms=elapsed_ms,
        run_fingerprint=run_fingerprint,
    )


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
    _run_state.write_case_status(
        path,
        case,
        status=status,
        phase=phase,
        started_at=started_at,
        ended_at=ended_at,
        elapsed_ms=elapsed_ms,
        error=error,
        eval_result_path=eval_result_path,
        report_path=report_path,
        run_fingerprint=run_fingerprint,
    )


def _read_case_status(path: Path):
    return _run_state.read_case_status(path)


def _read_case_status_phase(path: Path) -> str:
    return _run_state.read_case_status_phase(path)


def _read_case_status_started_at(path: Path) -> str:
    return _run_state.read_case_status_started_at(path)


def _read_case_status_error(path: Path) -> str:
    return _run_state.read_case_status_error(path)


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
    return _run_state.pr_eval_case_run_fingerprint(case, run_kwargs)


def _warnings_indicate_partial_analysis(warnings: list[str]) -> bool:
    return _run_state.warnings_indicate_partial_analysis(warnings)


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
            parsed_coverage_mode = None
            if llm_coverage_mode is not None:
                try:
                    parsed_coverage_mode = LLMCoverageMode(llm_coverage_mode)
                except ValueError as exc:
                    raise PrEvalError(f"PR #{case.number}: unsupported LLM coverage mode: {llm_coverage_mode}") from exc
            config = apply_review_overrides(
                config,
                ReviewOverrides(
                    llm_enabled=llm_enabled,
                    provider=provider_override,
                    model=model_override,
                    verify=verify_override,
                    cache_allowed=cache_enabled,
                    refresh_cache=refresh_cache,
                    cache_dir=cache_dir,
                    default_cache_dir=repo_root / ".apex-ray" / "cache" / "llm",
                    llm_jobs=llm_jobs,
                    coverage_mode=parsed_coverage_mode,
                    max_deep_packs=llm_max_deep_packs,
                    max_input_tokens=llm_max_input_tokens,
                    analyzer_timeout_seconds=analyzer_timeout_seconds,
                ),
            )

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
    return _github_helpers.load_prs(repo_root, pr_numbers, limit, run_gh_json=_run_gh_json)


def _load_pr_commit_oids(repo_root: Path, number: int) -> list[str]:
    return _github_helpers.load_pr_commit_oids(repo_root, number, run_gh_json=_run_gh_json)


def _load_greptile_comments(owner_repo: str, number: int, repo_root: Path) -> list[GreptileComment]:
    return _github_helpers.load_greptile_comments(
        owner_repo,
        number,
        repo_root,
        run_gh_json=_run_gh_json,
        run_gh_api_paginated_array=_run_gh_api_paginated_array,
    )


def _github_name_with_owner(repo_root: Path) -> str:
    return _github_helpers.github_name_with_owner(repo_root, run_gh_json=_run_gh_json)


def _run_gh_json(args: list[str], cwd: Path) -> Any:
    return _github_helpers.run_gh_json_default(args, cwd)


def _run_gh_api_paginated_array(path: str, cwd: Path) -> list[dict[str, Any]]:
    return _github_helpers.run_gh_api_paginated_array_default(path, cwd, run_gh_json=_run_gh_json)


def _run_gh_text(args: list[str], cwd: Path) -> str:
    return _github_helpers.run_gh_text_default(args, cwd)


def _run_gh(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return _github_helpers.run_gh(args, cwd)


def _merge_commit_oid(pr: dict[str, Any]) -> str | None:
    return _github_helpers.merge_commit_oid(pr)


def _write_case_manifest(path: Path, case: PullRequestEvalCase) -> None:
    _github_helpers.write_case_manifest(path, case)


def _case_manifest_paths(cases_dir: Path, pr_numbers: list[int] | None, limit: int | None) -> list[Path]:
    return _github_helpers.case_manifest_paths(cases_dir, pr_numbers, limit)


def _pr_number_from_case_path(path: Path) -> int:
    return _github_helpers.pr_number_from_case_path(path)


def _replay_head_sha_from_findings(findings: list[GreptileFinding]) -> str | None:
    return _github_helpers.replay_head_sha_from_findings(findings)


def _replay_base_sha(
    repo_root: Path,
    owner_repo: str,
    pr_commit_oids: list[str],
    replay_head_sha: str,
    default_base_sha: str,
) -> str:
    return _github_helpers.replay_base_sha(
        repo_root,
        owner_repo,
        pr_commit_oids,
        replay_head_sha,
        default_base_sha,
        github_commit_first_parent=_github_commit_first_parent,
    )


def _github_commit_first_parent(owner_repo: str, sha: str, repo_root: Path) -> str | None:
    return _github_helpers.github_commit_first_parent_default(owner_repo, sha, repo_root, run_gh_json=_run_gh_json)


def _pr_diff_from_git(
    repo_root: Path,
    owner_repo: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    *,
    allow_pr_diff_fallback: bool = False,
) -> str:
    return _github_helpers.pr_diff_from_git(
        repo_root,
        owner_repo,
        pr_number,
        base_sha,
        head_sha,
        allow_pr_diff_fallback=allow_pr_diff_fallback,
        ensure_commit_available=_ensure_commit_available,
        github_compare_diff=_github_compare_diff,
        run_gh_text=_run_gh_text,
    )


def _github_compare_diff(owner_repo: str, base_sha: str, head_sha: str, repo_root: Path) -> str:
    return _github_helpers.github_compare_diff_default(
        owner_repo,
        base_sha,
        head_sha,
        repo_root,
        run_gh_text=_run_gh_text,
    )


def _ensure_commit_available(repo_root: Path, sha: str, *, pr_number: int | None = None) -> None:
    _github_helpers.ensure_commit_available_default(repo_root, sha, pr_number=pr_number)


def _overlay_current_apex_config(source_repo: Path, worktree: Path) -> None:
    _github_helpers.overlay_current_apex_config(source_repo, worktree)
