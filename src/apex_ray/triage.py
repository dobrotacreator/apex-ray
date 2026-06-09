import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from apex_ray.findings import context_pack_fingerprint, finding_fingerprint
from apex_ray.models import ApexModel, ContextPack, Finding, TriageConfig

TRIAGE_STATE_SCHEMA_VERSION = "finding-triage-state/v1"
TRIAGE_EVENT_SCHEMA_VERSION = "finding-triage-event/v1"

SuppressionVerdict = Literal["false_positive", "not_actionable", "duplicate", "accepted_risk"]
TriageEventKind = Literal[
    "suppression_created",
    "suppression_matched",
    "suppression_stale",
    "suppression_expired",
    "suppression_pruned",
    "suppression_removed",
]


class FindingSnapshot(ApexModel):
    fingerprint: str
    title: str
    severity: str
    confidence: str
    file: str
    line: int | None = None
    context_pack_id: str = ""
    context_pack_fingerprint: str = ""


class FindingSuppression(ApexModel):
    id: str
    finding_fingerprint: str
    context_pack_id: str = ""
    context_pack_fingerprint: str = ""
    file: str
    line: int | None = None
    title: str
    severity: str
    confidence: str
    verdict: SuppressionVerdict = "false_positive"
    reason: str
    target_base_ref: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    last_matched_at: datetime | None = None
    match_count: int = 0
    report_path: str = ""

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_scope_base_ref(cls, data):
        if isinstance(data, dict) and "target_base_ref" not in data and "scope_base_ref" in data:
            return {**data, "target_base_ref": data["scope_base_ref"]}
        return data


class TriageState(ApexModel):
    schema_version: str = TRIAGE_STATE_SCHEMA_VERSION
    suppressions: list[FindingSuppression] = Field(default_factory=list)


class TriageEvent(ApexModel):
    schema_version: str = TRIAGE_EVENT_SCHEMA_VERSION
    event: TriageEventKind
    suppression_id: str
    finding_fingerprint: str
    created_at: datetime
    verdict: str = ""
    reason: str = ""
    suppression_reason: str = ""
    file: str = ""
    line: int | None = None
    title: str = ""
    severity: str = ""
    context_pack_id: str = ""


@dataclass(frozen=True)
class FindingCandidate:
    finding: Finding
    snapshot: FindingSnapshot


@dataclass(frozen=True)
class SuppressedFinding:
    finding: Finding
    suppression: FindingSuppression
    snapshot: FindingSnapshot


@dataclass(frozen=True)
class StaleSuppression:
    finding: Finding
    suppression: FindingSuppression
    snapshot: FindingSnapshot
    reason: str


@dataclass(frozen=True)
class TriagePruneResult:
    state: TriageState
    events: list[TriageEvent]
    expired_count: int
    pruned_count: int


@dataclass(frozen=True)
class TriageApplyResult:
    remaining_findings: list[Finding]
    suppressed_findings: list[SuppressedFinding]
    stale_suppressions: list[StaleSuppression]
    state: TriageState
    events: list[TriageEvent]
    stale_count: int


def load_triage_state(path: Path) -> TriageState:
    if not path.exists():
        return TriageState()
    try:
        state = TriageState.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError, ValidationError:
        return TriageState()
    if state.schema_version != TRIAGE_STATE_SCHEMA_VERSION:
        return TriageState()
    return state


def write_triage_state(path: Path, state: TriageState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, state.model_dump_json(indent=2))


def append_triage_events(path: Path, events: list[TriageEvent], *, retention_days: int | None) -> None:
    if not events and retention_days is None:
        return
    existing = load_triage_events(path)
    combined = [*existing, *events]
    if retention_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        combined = [event for event in combined if _as_utc(event.created_at) >= cutoff]
    if not combined:
        if path.exists():
            _atomic_write_text(path, "")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(event.model_dump_json() + "\n" for event in combined)
    _atomic_write_text(path, text)


def load_triage_events(path: Path) -> list[TriageEvent]:
    if not path.exists():
        return []
    events: list[TriageEvent] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(TriageEvent.model_validate_json(line))
    except OSError, ValidationError:
        return []
    return events


def finding_candidates_for_report(report, findings: list[Finding]) -> list[FindingCandidate]:
    packs_by_id = {pack.id: pack for pack in report.context_packs}
    return [
        FindingCandidate(finding, finding_snapshot(finding, packs_by_id.get(finding.context_pack_id)))
        for finding in findings
    ]


def finding_candidate(finding: Finding, context_pack: ContextPack | None = None) -> FindingCandidate:
    return FindingCandidate(finding, finding_snapshot(finding, context_pack))


def finding_snapshot(finding: Finding, context_pack: ContextPack | None = None) -> FindingSnapshot:
    return FindingSnapshot(
        fingerprint=finding_fingerprint(finding),
        title=finding.title,
        severity=str(finding.severity),
        confidence=str(finding.confidence),
        file=finding.file,
        line=finding.line,
        context_pack_id=finding.context_pack_id,
        context_pack_fingerprint=context_pack_fingerprint(context_pack),
    )


def create_suppression(
    *,
    snapshot: FindingSnapshot,
    reason: str,
    config: TriageConfig,
    now: datetime | None = None,
    verdict: SuppressionVerdict = "false_positive",
    expires_at: datetime | None = None,
    target_base_ref: str | None = None,
    report_path: Path | None = None,
) -> tuple[FindingSuppression, TriageEvent]:
    created_at = _as_utc(now or datetime.now(UTC))
    effective_expires_at = expires_at or created_at + timedelta(days=config.default_expiry_days)
    suppression = FindingSuppression(
        id=f"sup-{uuid.uuid4().hex[:12]}",
        finding_fingerprint=snapshot.fingerprint,
        context_pack_id=snapshot.context_pack_id,
        context_pack_fingerprint=snapshot.context_pack_fingerprint,
        file=snapshot.file,
        line=snapshot.line,
        title=snapshot.title,
        severity=snapshot.severity,
        confidence=snapshot.confidence,
        verdict=verdict,
        reason=reason,
        target_base_ref=target_base_ref,
        created_at=created_at,
        expires_at=effective_expires_at,
        report_path=str(report_path) if report_path else "",
    )
    return suppression, triage_event("suppression_created", suppression, created_at, reason=reason)


def add_or_replace_suppression(state: TriageState, suppression: FindingSuppression) -> TriageState:
    kept = [
        existing
        for existing in state.suppressions
        if not (
            existing.finding_fingerprint == suppression.finding_fingerprint
            and existing.context_pack_id == suppression.context_pack_id
            and existing.target_base_ref == suppression.target_base_ref
        )
    ]
    return TriageState(suppressions=[*kept, suppression])


def remove_suppressions(
    state: TriageState, selector: str, *, now: datetime | None = None
) -> tuple[TriageState, list[TriageEvent]]:
    removed = [
        suppression
        for suppression in state.suppressions
        if suppression.id == selector or suppression.finding_fingerprint == selector
    ]
    kept = [suppression for suppression in state.suppressions if suppression not in removed]
    timestamp = _as_utc(now or datetime.now(UTC))
    events = [
        triage_event("suppression_removed", suppression, timestamp, reason="Removed by user.")
        for suppression in removed
    ]
    return TriageState(suppressions=kept), events


def prune_triage_state(
    state: TriageState,
    config: TriageConfig,
    *,
    now: datetime | None = None,
) -> TriagePruneResult:
    timestamp = _as_utc(now or datetime.now(UTC))
    active: list[FindingSuppression] = []
    events: list[TriageEvent] = []
    expired_count = 0
    for suppression in state.suppressions:
        if _is_expired(suppression, timestamp):
            expired_count += 1
            events.append(triage_event("suppression_expired", suppression, timestamp, reason="Suppression expired."))
        else:
            active.append(suppression)

    pruned_count = 0
    if len(active) > config.max_active_suppressions:
        active = sorted(active, key=lambda suppression: suppression.last_matched_at or suppression.created_at)
        prune_count = len(active) - config.max_active_suppressions
        stale = active[:prune_count]
        active = active[prune_count:]
        pruned_count = len(stale)
        events.extend(
            triage_event("suppression_pruned", suppression, timestamp, reason="Active suppression limit exceeded.")
            for suppression in stale
        )
    return TriagePruneResult(TriageState(suppressions=active), events, expired_count, pruned_count)


def apply_suppressions(
    candidates: list[FindingCandidate],
    state: TriageState,
    *,
    target_base_ref: str | None = None,
    now: datetime | None = None,
) -> TriageApplyResult:
    timestamp = _as_utc(now or datetime.now(UTC))
    suppressions = list(state.suppressions)
    remaining: list[Finding] = []
    suppressed: list[SuppressedFinding] = []
    stale: list[StaleSuppression] = []
    events: list[TriageEvent] = []
    stale_ids: set[str] = set()
    updated_by_id: dict[str, FindingSuppression] = {}

    for candidate in candidates:
        match = _matching_suppression(
            candidate.snapshot,
            suppressions,
            target_base_ref=target_base_ref,
            now=timestamp,
        )
        if match is None:
            remaining.append(candidate.finding)
            continue
        suppression, stale_reason = match
        if stale_reason:
            if suppression.id not in stale_ids:
                stale_ids.add(suppression.id)
                stale.append(StaleSuppression(candidate.finding, suppression, candidate.snapshot, stale_reason))
                events.append(triage_event("suppression_stale", suppression, timestamp, reason=stale_reason))
            remaining.append(candidate.finding)
            continue
        updated = suppression.model_copy(
            update={
                "last_matched_at": timestamp,
                "match_count": suppression.match_count + 1,
            }
        )
        updated_by_id[suppression.id] = updated
        suppressed.append(SuppressedFinding(candidate.finding, updated, candidate.snapshot))
        events.append(triage_event("suppression_matched", updated, timestamp, reason=updated.reason))

    next_suppressions = [
        updated_by_id.get(suppression.id, suppression)
        for suppression in suppressions
        if suppression.id not in stale_ids
    ]
    return TriageApplyResult(
        remaining_findings=remaining,
        suppressed_findings=suppressed,
        stale_suppressions=stale,
        state=TriageState(suppressions=next_suppressions),
        events=events,
        stale_count=len(stale_ids),
    )


def render_triage_snapshot(
    *,
    suppressed_findings: list[SuppressedFinding],
    stale_suppressions: list[StaleSuppression] | None = None,
    active_suppressions: list[FindingSuppression],
    stale_count: int,
    expired_count: int,
    pruned_count: int,
) -> str:
    payload = {
        "schema_version": "finding-triage-snapshot/v1",
        "created_at": datetime.now(UTC).isoformat(),
        "suppressed_findings": [
            {
                "suppression_id": item.suppression.id,
                "finding_fingerprint": item.snapshot.fingerprint,
                "verdict": item.suppression.verdict,
                "reason": item.suppression.reason,
                "file": item.snapshot.file,
                "line": item.snapshot.line,
                "title": item.snapshot.title,
                "severity": item.snapshot.severity,
                "context_pack_id": item.snapshot.context_pack_id,
                "expires_at": item.suppression.expires_at.isoformat() if item.suppression.expires_at else None,
            }
            for item in suppressed_findings
        ],
        "stale_suppressions": [
            {
                "suppression_id": item.suppression.id,
                "finding_fingerprint": item.snapshot.fingerprint,
                "stale_reason": item.reason,
                "prior_reason": item.suppression.reason,
                "verdict": item.suppression.verdict,
                "file": item.snapshot.file,
                "line": item.snapshot.line,
                "title": item.snapshot.title,
                "severity": item.snapshot.severity,
                "context_pack_id": item.snapshot.context_pack_id,
                "previous_context_pack_fingerprint": item.suppression.context_pack_fingerprint,
                "current_context_pack_fingerprint": item.snapshot.context_pack_fingerprint,
            }
            for item in (stale_suppressions or [])
        ],
        "active_suppressions_count": len(active_suppressions),
        "stale_suppressions_count": stale_count,
        "expired_suppressions_count": expired_count,
        "pruned_suppressions_count": pruned_count,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def triage_event(
    event: TriageEventKind,
    suppression: FindingSuppression,
    created_at: datetime,
    *,
    reason: str = "",
) -> TriageEvent:
    return TriageEvent(
        event=event,
        suppression_id=suppression.id,
        finding_fingerprint=suppression.finding_fingerprint,
        created_at=created_at,
        verdict=suppression.verdict,
        reason=reason,
        suppression_reason=suppression.reason,
        file=suppression.file,
        line=suppression.line,
        title=suppression.title,
        severity=suppression.severity,
        context_pack_id=suppression.context_pack_id,
    )


def _matching_suppression(
    snapshot: FindingSnapshot,
    suppressions: list[FindingSuppression],
    *,
    target_base_ref: str | None,
    now: datetime,
) -> tuple[FindingSuppression, str] | None:
    for suppression in suppressions:
        if suppression.finding_fingerprint != snapshot.fingerprint:
            continue
        if _is_expired(suppression, now):
            continue
        if suppression.target_base_ref and target_base_ref and suppression.target_base_ref != target_base_ref:
            continue
        if suppression.context_pack_fingerprint and snapshot.context_pack_fingerprint:
            if suppression.context_pack_fingerprint != snapshot.context_pack_fingerprint:
                return suppression, "Context pack changed since suppression was created."
        return suppression, ""
    return None


def _is_expired(suppression: FindingSuppression, now: datetime) -> bool:
    return suppression.expires_at is not None and _as_utc(suppression.expires_at) <= now


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
