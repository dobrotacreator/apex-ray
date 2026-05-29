import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from apex_ray.pr_eval.matching import apex_finding_fingerprint
from apex_ray.pr_eval.models import (
    PrEvalApexFindingLabel,
    PrEvalGreptileFindingLabel,
    PrEvalLabels,
    PrEvalTelemetryCase,
    PrEvalTelemetryEntry,
    PullRequestEvalCase,
    PullRequestEvalRunReport,
    PullRequestEvalRunResult,
)


class PrEvalError(RuntimeError):
    pass


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
            updated_at=now_iso(),
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
        atomic_write_text(
            path,
            yaml.safe_dump(labels.model_dump(mode="json", exclude_none=True), sort_keys=False),
        )
        written.append(path)
    return written


def append_pr_eval_telemetry(
    report: PullRequestEvalRunReport,
    telemetry_path: Path,
    *,
    source_repo: Path,
    output_dir: Path,
) -> Path:
    entry = PrEvalTelemetryEntry(
        run_id=uuid.uuid4().hex,
        created_at=now_iso(),
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


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
