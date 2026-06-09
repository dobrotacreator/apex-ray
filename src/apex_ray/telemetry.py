import json
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apex_ray import __version__
from apex_ray.models import ReviewReport

DEFAULT_REVIEW_TELEMETRY_PATH = ".apex-ray/telemetry/review-runs.jsonl"


class TelemetryError(RuntimeError):
    pass


def append_review_telemetry(
    report: ReviewReport,
    telemetry_path: Path,
    *,
    source_repo: Path,
    duration_ms: int,
    output_path: Path | None = None,
    json_output_path: Path | None = None,
    html_output_path: Path | None = None,
    triage_counts: Mapping[str, int] | None = None,
) -> Path:
    coverage = report.llm_coverage
    entry: dict[str, Any] = {
        "schema_version": "review-telemetry/v1",
        "run_id": uuid.uuid4().hex,
        "created_at": _now_iso(),
        "version": __version__,
        "source_repo": str(source_repo),
        "target_mode": report.diff.target_mode,
        "base": report.diff.base,
        "duration_ms": duration_ms,
        "output_path": str(output_path) if output_path else None,
        "json_output_path": str(json_output_path) if json_output_path else None,
        "html_output_path": str(html_output_path) if html_output_path else None,
        "files_changed": report.diff.stats.files_changed,
        "additions": report.diff.stats.additions,
        "deletions": report.diff.stats.deletions,
        "ignored_files": report.diff.stats.ignored_files,
        "findings_count": len(report.findings),
        "verified_findings_count": sum(1 for verification in report.verifications if verification.approved),
        "context_packs_count": len(report.context_packs),
        "llm_enabled": coverage.enabled,
        "llm_verify_enabled": coverage.verify_enabled,
        "llm_coverage_mode": coverage.coverage_mode,
        "llm_max_packs": coverage.max_packs,
        "llm_max_deep_packs": coverage.max_deep_packs,
        "llm_max_input_tokens": coverage.max_input_tokens,
        "reviewed_context_packs_count": coverage.reviewed_context_packs,
        "unreviewed_context_packs_count": coverage.unreviewed_context_packs,
        "coverage_ratio": coverage.coverage_ratio,
        "source_changed_line_coverage_ratio": coverage.source_changed_line_coverage_ratio,
        "high_risk_coverage_ratio": coverage.high_risk_coverage_ratio,
        "partial_severity": coverage.partial_severity,
        "coverage_quality_gate_status": coverage.quality_gate_status,
        "residual_p0_context_packs_count": len(coverage.residual_risk_p0_context_pack_ids),
        "residual_p1_context_packs_count": len(coverage.residual_risk_p1_context_pack_ids),
        "coverage_todos_count": len(coverage.coverage_todos),
        "llm_runs_count": len(report.llm_runs),
        "llm_review_runs_count": coverage.review_runs,
        "llm_verify_runs_count": coverage.verify_runs,
        "failed_llm_review_runs_count": coverage.failed_review_runs,
        "failed_llm_verify_runs_count": coverage.failed_verify_runs,
        "llm_duration_ms": coverage.total_duration_ms,
        "llm_input_chars": coverage.input_chars,
        "llm_estimated_input_tokens": coverage.estimated_input_tokens,
        "llm_actual_input_tokens": coverage.actual_input_tokens,
        "llm_actual_cached_input_tokens": coverage.actual_cached_input_tokens,
        "llm_actual_output_tokens": coverage.actual_output_tokens,
        "llm_actual_reasoning_output_tokens": coverage.actual_reasoning_output_tokens,
        "llm_actual_total_tokens": coverage.actual_total_tokens,
        "llm_actual_cache_read_input_tokens": coverage.actual_cache_read_input_tokens,
        "llm_actual_cache_creation_input_tokens": coverage.actual_cache_creation_input_tokens,
        "llm_estimated_saved_input_tokens": coverage.estimated_saved_input_tokens,
        "llm_estimated_cost_usd": coverage.estimated_cost_usd,
        "llm_usage_sources": coverage.usage_sources,
        "llm_cache_hits": coverage.cache_hits,
        "llm_cache_misses": coverage.cache_misses,
        "llm_run_status_counts": coverage.run_status_counts,
        "pack_status_counts": dict(sorted(Counter(status.status for status in coverage.pack_statuses).items())),
        "routes": [route.model_dump(mode="json", exclude_none=True) for route in coverage.routes],
    }
    if triage_counts:
        entry.update(triage_counts)
    try:
        telemetry_path.parent.mkdir(parents=True, exist_ok=True)
        with telemetry_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError as exc:
        raise TelemetryError(f"Unable to append review telemetry {telemetry_path}: {exc}") from exc
    return telemetry_path


def load_review_telemetry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TelemetryError(f"Invalid telemetry entry {path}:{line_number}: {exc}") from exc
                if not isinstance(raw, dict):
                    raise TelemetryError(f"Invalid telemetry entry {path}:{line_number}: expected object")
                entries.append(raw)
    except OSError as exc:
        raise TelemetryError(f"Unable to read review telemetry {path}: {exc}") from exc
    return entries


def render_review_telemetry_summary(entries: list[dict[str, Any]]) -> str:
    lines = ["# Apex Ray Review Telemetry", ""]
    if not entries:
        lines.append("No telemetry entries found.")
        lines.append("")
        return "\n".join(lines)

    latest = entries[-1]
    tokens = sum(_int(entry.get("llm_estimated_input_tokens")) for entry in entries)
    actual_tokens = sum(_int(entry.get("llm_actual_total_tokens")) for entry in entries)
    saved_tokens = sum(_int(entry.get("llm_estimated_saved_input_tokens")) for entry in entries)
    duration_ms = sum(_int(entry.get("duration_ms")) for entry in entries)
    llm_duration_ms = sum(_int(entry.get("llm_duration_ms")) for entry in entries)
    failed_llm_runs = sum(
        _int(entry.get("failed_llm_review_runs_count")) + _int(entry.get("failed_llm_verify_runs_count"))
        for entry in entries
    )
    partial_counts = Counter(str(entry.get("partial_severity") or "none") for entry in entries)
    lines.extend(
        [
            f"- Runs: `{len(entries)}`",
            f"- Latest run: `{latest.get('created_at')}` (`{latest.get('run_id')}`)",
            f"- Latest target: `{latest.get('target_mode')}`",
            f"- Latest findings: `{latest.get('findings_count', 0)}`",
            f"- Latest partial severity: `{latest.get('partial_severity', 'none')}`",
            f"- Latest coverage: `{_float(latest.get('coverage_ratio')):.1%}`",
            f"- Latest high-risk coverage: `{_float(latest.get('high_risk_coverage_ratio')):.1%}`",
            f"- Latest LLM tokens: `~{latest.get('llm_estimated_input_tokens', 0)}`",
            f"- Latest actual LLM tokens: `{latest.get('llm_actual_total_tokens', 0)}`",
            f"- Total LLM tokens: `~{tokens}`",
            f"- Total actual LLM tokens: `{actual_tokens}`",
            f"- Estimated saved input tokens: `~{saved_tokens}`",
            f"- Average LLM tokens/run: `~{tokens // len(entries)}`",
            f"- Total wall time: `{duration_ms}ms`",
            f"- Average wall time/run: `{duration_ms // len(entries)}ms`",
            f"- Total LLM duration: `{llm_duration_ms}ms`",
            f"- Failed LLM runs: `{failed_llm_runs}`",
            f"- Partial severity counts: `{dict(sorted(partial_counts.items()))}`",
            "",
            "## Recent Runs",
            "",
        ]
    )
    for entry in entries[-20:]:
        lines.append(
            f"- `{entry.get('created_at')}` `{entry.get('run_id')}` - "
            f"target `{entry.get('target_mode')}`, findings `{entry.get('findings_count', 0)}`, "
            f"coverage `{_float(entry.get('coverage_ratio')):.1%}`, "
            f"partial `{entry.get('partial_severity', 'none')}`, "
            f"tokens `~{entry.get('llm_estimated_input_tokens', 0)}`"
            f"/`{entry.get('llm_actual_total_tokens', 0)}` actual, "
            f"duration `{entry.get('duration_ms', 0)}ms`"
        )
    lines.append("")
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _float(value: object) -> float:
    return value if isinstance(value, int | float) else 0.0
