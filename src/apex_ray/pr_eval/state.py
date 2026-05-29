import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from apex_ray.pr_eval.models import (
    PrEvalCaseStatus,
    PullRequestEvalCase,
    PullRequestEvalRunReport,
    PullRequestEvalRunResult,
)
from apex_ray.pr_eval.store import (
    PrEvalError,
    atomic_write_text,
    load_pr_eval_run_result,
    now_iso,
    pr_eval_label_path,
)

CASE_STATUS_FILENAME = "case-status.json"


def build_pr_eval_run_report(results: list[PullRequestEvalRunResult]) -> PullRequestEvalRunReport:
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


def load_resumable_pr_eval_result(output_dir: Path, run_fingerprint: str) -> PullRequestEvalRunResult | None:
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


def failed_pr_eval_result(
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
    finished_at = now_iso()
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
    atomic_write_text(output_dir / "eval-result.json", result.model_dump_json(indent=2))
    write_case_status(
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


def write_case_status(
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
    existing = read_case_status(path)
    status_model = PrEvalCaseStatus(
        number=case.number,
        title=case.title,
        status=status,
        phase=phase,
        started_at=started_at or (existing.started_at if existing else now_iso()),
        updated_at=now_iso(),
        ended_at=ended_at,
        elapsed_ms=elapsed_ms,
        error=error,
        eval_result_path=eval_result_path or (existing.eval_result_path if existing else None),
        report_path=report_path or (existing.report_path if existing else None),
        run_fingerprint=run_fingerprint or (existing.run_fingerprint if existing else None),
    )
    atomic_write_text(path, status_model.model_dump_json(indent=2))


def read_case_status(path: Path) -> PrEvalCaseStatus | None:
    if not path.exists():
        return None
    try:
        return PrEvalCaseStatus.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError, ValidationError:
        return None


def read_case_status_phase(path: Path) -> str:
    status = read_case_status(path)
    return status.phase if status else ""


def read_case_status_started_at(path: Path) -> str:
    status = read_case_status(path)
    return status.started_at if status else ""


def read_case_status_error(path: Path) -> str:
    status = read_case_status(path)
    return status.error if status and status.error else ""


def pr_eval_case_run_fingerprint(case: PullRequestEvalCase, run_kwargs: Mapping[str, Any]) -> str:
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


def warnings_indicate_partial_analysis(warnings: list[str]) -> bool:
    partial_markers = (
        "partial TypeScript analyzer result",
        "TypeScript analyzer unavailable",
        "TypeScript analyzer failed",
        "TypeScript analyzer timed out",
    )
    return any(any(marker in warning for marker in partial_markers) for warning in warnings)
