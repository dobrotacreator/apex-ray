import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from apex_ray import __version__
from apex_ray.llm.cache import REVIEW_PROMPT_VERSION, REVIEW_SHALLOW_PROMPT_VERSION, VERIFIER_PROMPT_VERSION
from apex_ray.models import (
    ApexModel,
    ContextPack,
    Finding,
    FindingConfidence,
    PrePushGateConfig,
    ReviewConfig,
    ReviewReport,
)

STATE_SCHEMA_VERSION = "pre-push-state/v1"


class CarriedFinding(ApexModel):
    finding: Finding
    context_pack: ContextPack | None = None
    status: Literal["still_present", "uncertain"] = "still_present"
    first_seen_report: str = ""
    last_seen_report: str = ""
    resolution_reason: str = ""
    resolution_confidence: FindingConfidence | None = None
    relevant_files: list[str] = Field(default_factory=list)


class CoverageDebt(ApexModel):
    quality_gate_failed: bool = False
    partial_blocked: bool = False
    reasons: list[str] = Field(default_factory=list)
    partial_severity: str = "none"
    quality_gate_status: str = "pass"


class PrePushGateState(ApexModel):
    schema_version: str = STATE_SCHEMA_VERSION
    repo_root: str
    base_ref: str
    merge_base_sha: str
    head_sha: str
    config_fingerprint: str
    report_path: str
    json_path: str
    generated_at: datetime
    active_findings: list[CarriedFinding] = Field(default_factory=list)
    coverage_debt: CoverageDebt = Field(default_factory=CoverageDebt)
    reviewed_context_pack_ids: list[str] = Field(default_factory=list)
    context_pack_fingerprints: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class IncrementalEligibility:
    eligible: bool
    reason: str = ""


def resolve_state_path(root: Path, config: PrePushGateConfig) -> Path:
    configured = Path(config.incremental_retry.state_path)
    return configured if configured.is_absolute() else root / configured


def load_pre_push_state(path: Path) -> PrePushGateState | None:
    if not path.exists():
        return None
    try:
        return PrePushGateState.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError, ValidationError:
        return None


def write_pre_push_state(path: Path, state: PrePushGateState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")


def config_fingerprint(config: ReviewConfig, gate_config: PrePushGateConfig) -> str:
    payload = {
        "version": __version__,
        "review_config": config.model_dump(mode="json"),
        "gate_policy": gate_config.model_dump(mode="json"),
        "prompt_versions": {
            "review": REVIEW_PROMPT_VERSION,
            "review_shallow": REVIEW_SHALLOW_PROMPT_VERSION,
            "verify": VERIFIER_PROMPT_VERSION,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def check_incremental_eligibility(
    state: PrePushGateState | None,
    *,
    repo_root: Path,
    base_ref: str,
    merge_base_sha: str,
    config_hash: str,
    previous_head_exists: bool,
) -> IncrementalEligibility:
    if state is None:
        return IncrementalEligibility(False, "no previous pre-push state")
    if state.schema_version != STATE_SCHEMA_VERSION:
        return IncrementalEligibility(False, "unsupported pre-push state schema")
    if Path(state.repo_root) != repo_root:
        return IncrementalEligibility(False, "previous state belongs to a different repo root")
    if state.base_ref != base_ref:
        return IncrementalEligibility(False, "previous state used a different base ref")
    if state.merge_base_sha != merge_base_sha:
        return IncrementalEligibility(False, "merge-base changed")
    if state.config_fingerprint != config_hash:
        return IncrementalEligibility(False, "review config, rules, memory, prompt, model, or gate policy changed")
    if not previous_head_exists:
        return IncrementalEligibility(False, "previous gate HEAD is not available locally")
    return IncrementalEligibility(True)


def build_pre_push_state(
    *,
    repo_root: Path,
    base_ref: str,
    merge_base_sha: str,
    head_sha: str,
    config_hash: str,
    report: ReviewReport,
    report_path: Path,
    json_path: Path,
    active_findings: list[CarriedFinding],
    coverage_debt: CoverageDebt,
) -> PrePushGateState:
    return PrePushGateState(
        repo_root=str(repo_root),
        base_ref=base_ref,
        merge_base_sha=merge_base_sha,
        head_sha=head_sha,
        config_fingerprint=config_hash,
        report_path=str(report_path),
        json_path=str(json_path),
        generated_at=report.generated_at,
        active_findings=active_findings,
        coverage_debt=coverage_debt,
        reviewed_context_pack_ids=report.llm_coverage.reviewed_context_pack_ids,
        context_pack_fingerprints={
            pack.id: context_pack_fingerprint(pack.model_dump(mode="json")) for pack in report.context_packs
        },
    )


def current_blocking_findings(
    report: ReviewReport,
    blocking_findings: list[Finding],
    *,
    report_path: Path,
) -> list[CarriedFinding]:
    packs_by_id = {pack.id: pack for pack in report.context_packs}
    return [
        CarriedFinding(
            finding=finding,
            context_pack=packs_by_id.get(finding.context_pack_id),
            status="still_present",
            first_seen_report=str(report_path),
            last_seen_report=str(report_path),
            relevant_files=relevant_files_for_finding(report, finding),
        )
        for finding in blocking_findings
    ]


def coverage_debt_from_decision(
    report: ReviewReport,
    *,
    quality_gate_failed: bool,
    partial_blocked: bool,
    reasons: list[str],
) -> CoverageDebt:
    return CoverageDebt(
        quality_gate_failed=quality_gate_failed,
        partial_blocked=partial_blocked,
        reasons=reasons if quality_gate_failed or partial_blocked else [],
        partial_severity=report.llm_coverage.partial_severity,
        quality_gate_status=report.llm_coverage.quality_gate_status,
    )


def relevant_files_for_finding(report: ReviewReport, finding: Finding) -> list[str]:
    files = {finding.file}
    if finding.context_pack_id and "#" in finding.context_pack_id:
        files.add(finding.context_pack_id.split("#", 1)[0])
    pack = next((candidate for candidate in report.context_packs if candidate.id == finding.context_pack_id), None)
    if pack is not None:
        files.add(pack.file)
        files.update(pack.related_tests)
        for reference in [*pack.references, *pack.callees, *pack.contracts, *pack.metadata]:
            files.add(reference.file)
        for snippet in [
            *pack.reference_snippets,
            *pack.callee_snippets,
            *pack.contract_snippets,
            *pack.metadata_snippets,
            *pack.related_test_snippets,
        ]:
            files.add(snippet.file)
    return sorted(file for file in files if file)


def changed_paths(report: ReviewReport) -> set[str]:
    paths: set[str] = set()
    for file in report.diff.files:
        if file.old_path:
            paths.add(file.old_path)
        if file.new_path:
            paths.add(file.new_path)
        paths.add(file.path)
    return paths


def finding_key(finding: Finding) -> tuple[object, ...]:
    return (
        str(finding.severity),
        finding.title,
        finding.file,
        finding.line,
        finding.failure_mode,
    )


def dedupe_carried_findings(findings: list[CarriedFinding]) -> list[CarriedFinding]:
    seen: set[tuple[object, ...]] = set()
    deduped: list[CarriedFinding] = []
    for carried in findings:
        key = finding_key(carried.finding)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(carried)
    return deduped


def context_pack_fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
