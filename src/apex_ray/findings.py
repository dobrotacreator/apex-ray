import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

from apex_ray.models import ContextPack, Finding, FindingVerification

_TOKEN_RE = re.compile(r"\s+")
_FINDING_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")
_FINDING_CODE_TOKEN_RE = re.compile(r"[a-z][a-z0-9]*(?:\[[a-z0-9]*\])+(?:\[\])?")
_FINDING_STOP_WORDS = {
    "add",
    "added",
    "also",
    "and",
    "any",
    "are",
    "assert",
    "before",
    "but",
    "can",
    "change",
    "changed",
    "changes",
    "code",
    "concrete",
    "context",
    "diff",
    "does",
    "from",
    "has",
    "have",
    "including",
    "instead",
    "into",
    "issue",
    "line",
    "new",
    "not",
    "now",
    "only",
    "public",
    "raw",
    "return",
    "returns",
    "same",
    "should",
    "that",
    "the",
    "this",
    "through",
    "type",
    "updated",
    "using",
    "value",
    "with",
}


def reviewer_origin_pack_ids(finding: Finding, reviewer_id: str) -> set[str]:
    """Return the context packs from which one reviewer produced a finding."""
    if reviewer_id in finding.reviewer_context_pack_ids:
        return {
            context_pack_id for context_pack_id in finding.reviewer_context_pack_ids[reviewer_id] if context_pack_id
        }
    if finding.context_pack_id:
        return {finding.context_pack_id}
    return set()


def reviewer_origins_are_explicit(finding: Finding, reviewer_id: str) -> bool:
    return reviewer_id in finding.reviewer_context_pack_ids


def findings_share_reviewer_origin(
    left: Finding,
    right: Finding,
    reviewer_id: str,
) -> bool:
    return bool(reviewer_origin_pack_ids(left, reviewer_id).intersection(reviewer_origin_pack_ids(right, reviewer_id)))


def merge_finding_reviewer_provenance(
    preferred: Finding,
    findings: Iterable[Finding],
) -> Finding:
    """Merge reviewer identities and their raw context-pack origins."""
    candidates = list(findings)
    provenance_ids = sorted(
        {reviewer_id for finding in candidates for reviewer_id in (finding.reviewer_ids or ["general"])}
    )
    reviewer_ids = [] if provenance_ids == ["general"] else provenance_ids
    merged_origins: dict[str, list[str]] = {}
    candidate_pack_ids = {finding.context_pack_id for finding in candidates if finding.context_pack_id}
    if len(candidate_pack_ids) > 1 or any(finding.reviewer_context_pack_ids for finding in candidates):
        for reviewer_id in provenance_ids:
            reviewer_candidates = [
                finding for finding in candidates if reviewer_id in (finding.reviewer_ids or ["general"])
            ]
            if any(not reviewer_origins_are_explicit(finding, reviewer_id) for finding in reviewer_candidates):
                continue
            origin_pack_ids = {
                context_pack_id
                for finding in reviewer_candidates
                for context_pack_id in reviewer_origin_pack_ids(finding, reviewer_id)
            }
            if origin_pack_ids:
                merged_origins[reviewer_id] = sorted(origin_pack_ids)
    return preferred.model_copy(
        update={
            "reviewer_ids": reviewer_ids,
            "reviewer_context_pack_ids": merged_origins,
        }
    )


def retain_finding_reviewer_provenance(
    finding: Finding,
    reviewer_ids: Iterable[str],
    *,
    origin_pack_ids: dict[str, set[str]] | None = None,
) -> Finding:
    """Keep selected reviewer provenance while preserving explicit origin metadata."""
    retained = list(dict.fromkeys(reviewer_ids))
    named_reviewer_ids = sorted(retained) if any(reviewer_id != "general" for reviewer_id in retained) else []
    retained_origins: dict[str, list[str]] = {}
    for reviewer_id in retained:
        if origin_pack_ids is not None and reviewer_id in origin_pack_ids:
            values = origin_pack_ids[reviewer_id]
        elif reviewer_origins_are_explicit(finding, reviewer_id):
            values = reviewer_origin_pack_ids(finding, reviewer_id)
        else:
            continue
        if values:
            retained_origins[reviewer_id] = sorted(values)
    return finding.model_copy(
        update={
            "reviewer_ids": named_reviewer_ids,
            "reviewer_context_pack_ids": retained_origins,
        }
    )


def finding_fingerprint(finding: Finding) -> str:
    payload = "|".join(
        [
            _normalize_path(finding.file),
            str(finding.line or ""),
            _compact_text(finding.title).lower(),
            _compact_text(finding.failure_mode).lower()[:500],
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"apex-{digest}"


def finding_matches_any(finding: Finding, candidates: Iterable[Finding]) -> bool:
    identity = finding_decision_identity(finding)
    return any(
        _verification_scope_is_compatible(finding, candidate)
        and (identity == finding_decision_identity(candidate) or findings_are_duplicates(finding, candidate))
        for candidate in candidates
    )


def verification_subject_matches_any(
    finding: Finding,
    candidates: Iterable[Finding],
) -> bool:
    subject_identity = _verification_subject_identity(finding)
    return any(
        _verification_scope_is_compatible(finding, candidate)
        and (
            subject_identity == _verification_subject_identity(candidate) or findings_are_duplicates(finding, candidate)
        )
        for candidate in candidates
    )


def active_verifications(
    verifications: Iterable[FindingVerification],
) -> list[FindingVerification]:
    """Return the latest non-superseded decision per reviewer and finding."""
    active: list[FindingVerification] = []
    for verification in verifications:
        if verification.superseded:
            continue
        active = [
            current
            for current in active
            if current.reviewer_id != verification.reviewer_id
            or not verification_decisions_match(current, verification)
        ]
        active.append(verification)
    return active


def verified_report_findings(
    findings: Iterable[Finding],
    verifications: Iterable[FindingVerification],
) -> list[Finding]:
    """Return report findings approved by an active reviewer decision.

    Consolidation can preserve reviewer provenance while choosing a canonical
    finding from a different context pack. Exact decisions therefore take
    precedence for each reviewer, with a duplicate/provenance fallback only
    when the report representation has no exact decision.
    """
    history = list(verifications)
    current_verifications = active_verifications(history)
    return [
        finding
        for finding in findings
        if any(
            verification.approved
            for verification in _matching_verifications_for_finding(
                finding,
                current_verifications,
            )
        )
    ]


def matching_active_verifications(
    finding: Finding,
    verifications: Iterable[FindingVerification],
    *,
    reviewer_id: str | None = None,
) -> list[FindingVerification]:
    """Resolve active decisions for one report finding with exact-first semantics."""
    current_verifications = active_verifications(verifications)
    return _matching_verifications_for_finding(
        finding,
        current_verifications,
        reviewer_id=reviewer_id,
    )


def _matching_verifications_for_finding(
    finding: Finding,
    current_verifications: list[FindingVerification],
    *,
    reviewer_id: str | None = None,
) -> list[FindingVerification]:
    if finding.reviewer_ids:
        provenance = set(finding.reviewer_ids)
    elif finding.reviewer_context_pack_ids:
        provenance = set(finding.reviewer_context_pack_ids)
    else:
        # Reports created before reviewer provenance was persisted still need
        # all of their reviewer decisions to remain effective.
        provenance = {verification.reviewer_id for verification in current_verifications}
    if reviewer_id is not None:
        provenance.intersection_update({reviewer_id})
    matched: list[FindingVerification] = []
    for candidate_reviewer_id in provenance:
        reviewer_decisions = [
            verification for verification in current_verifications if verification.reviewer_id == candidate_reviewer_id
        ]
        exact_decisions = [
            verification
            for verification in reviewer_decisions
            if finding_decision_identity(verification.finding) == finding_decision_identity(finding)
        ]
        if exact_decisions:
            matched.extend(exact_decisions)
            continue
        content_decisions = [
            verification
            for verification in reviewer_decisions
            if _has_cross_pack_reviewer_lineage(
                finding,
                verification.finding,
                candidate_reviewer_id,
            )
            and _cross_pack_verification_identity(finding) == _cross_pack_verification_identity(verification.finding)
        ]
        if content_decisions:
            matched.extend(content_decisions)
            continue
        scoped_decisions = [
            verification
            for verification in reviewer_decisions
            if verification_subject_matches_any(finding, [verification.finding])
        ]
        if scoped_decisions:
            matched.extend(scoped_decisions)
            continue
        matched.extend(
            verification
            for verification in reviewer_decisions
            if _has_cross_pack_reviewer_lineage(
                finding,
                verification.finding,
                candidate_reviewer_id,
            )
            and findings_are_duplicates(finding, verification.finding)
        )
    return matched


def _has_cross_pack_reviewer_lineage(
    finding: Finding,
    verification_finding: Finding,
    reviewer_id: str,
) -> bool:
    if finding.context_pack_id == verification_finding.context_pack_id:
        return False
    if findings_share_reviewer_origin(
        finding,
        verification_finding,
        reviewer_id,
    ):
        return True
    # Reports written before reviewer origin packs were persisted only retain
    # multi-reviewer provenance. Preserve their established cross-pack match.
    return not reviewer_origins_are_explicit(finding, reviewer_id) and len(finding.reviewer_ids) > 1


def inactive_verifications(
    verifications: Iterable[FindingVerification],
) -> list[FindingVerification]:
    history = list(verifications)
    active_ids = {id(verification) for verification in active_verifications(history)}
    return [verification for verification in history if id(verification) not in active_ids]


def unresolved_verifications(
    verifications: Iterable[FindingVerification],
) -> list[FindingVerification]:
    """Return nonterminal failed/reset decisions without a newer active resolution."""
    history = list(verifications)
    active = active_verifications(history)
    active_indexes = {
        id(verification): index
        for index, verification in enumerate(history)
        if any(verification is candidate for candidate in active)
    }
    return [
        verification
        for index, verification in enumerate(history)
        if verification.superseded
        and not verification_is_terminally_replaced(verification)
        and not any(
            active_verification.reviewer_id == verification.reviewer_id
            and active_indexes[id(active_verification)] > index
            and verification_decisions_match(verification, active_verification)
            for active_verification in active
        )
    ]


def verification_decisions_match(
    left: FindingVerification,
    right: FindingVerification,
) -> bool:
    """Match decisions while preserving distinct candidates in one review snapshot."""
    if left.review_snapshot_id is None or right.review_snapshot_id is None:
        return _verification_subject_identity(left.finding) == _verification_subject_identity(
            right.finding
        ) or _verification_decisions_share_explicit_cross_pack_origin(left, right)
    if left.review_snapshot_id is not None and left.review_snapshot_id == right.review_snapshot_id:
        return finding_decision_identity(left.finding) == finding_decision_identity(right.finding)
    if _verification_decisions_share_explicit_cross_pack_origin(left, right):
        return True
    return verification_subject_matches_any(left.finding, [right.finding])


def _verification_decisions_share_explicit_cross_pack_origin(
    left: FindingVerification,
    right: FindingVerification,
) -> bool:
    return (
        left.reviewer_id == right.reviewer_id
        and left.finding.context_pack_id != right.finding.context_pack_id
        and (
            reviewer_origins_are_explicit(left.finding, left.reviewer_id)
            or reviewer_origins_are_explicit(right.finding, right.reviewer_id)
        )
        and findings_share_reviewer_origin(
            left.finding,
            right.finding,
            left.reviewer_id,
        )
        and _cross_pack_verification_identity(left.finding) == _cross_pack_verification_identity(right.finding)
    )


def historical_verifications(
    verifications: Iterable[FindingVerification],
) -> list[FindingVerification]:
    """Return inactive verification decisions that are no longer pending."""
    history = list(verifications)
    unresolved_ids = {id(verification) for verification in unresolved_verifications(history)}
    return [verification for verification in inactive_verifications(history) if id(verification) not in unresolved_ids]


def verification_candidate_counts(
    findings: Iterable[Finding],
    verifications: Iterable[FindingVerification],
) -> dict[tuple[str, str], int]:
    """Count retained raw finding candidates per reviewer and context pack."""
    return {key: len(value) for key, value in verification_candidates_by_reviewer_pack(findings, verifications).items()}


def verification_candidates_by_reviewer_pack(
    findings: Iterable[Finding],
    verifications: Iterable[FindingVerification],
) -> dict[tuple[str, str], list[Finding]]:
    """Return distinct retained verification candidates by reviewer and pack."""
    candidates: dict[
        tuple[str, str],
        list[tuple[Finding, str | None]],
    ] = {}

    def add(
        reviewer_id: str,
        finding: Finding,
        review_snapshot_id: str | None = None,
    ) -> None:
        if not finding.context_pack_id:
            return
        key = (reviewer_id, finding.context_pack_id)
        current = candidates.setdefault(key, [])
        if any(
            finding_decision_identity(finding) == finding_decision_identity(existing_finding)
            for existing_finding, _existing_snapshot_id in current
        ):
            return
        if review_snapshot_id is None and verification_subject_matches_any(
            finding,
            [existing_finding for existing_finding, _existing_snapshot_id in current],
        ):
            return
        current.append((finding, review_snapshot_id))

    for finding in findings:
        for reviewer_id in finding.reviewer_ids or ["general"]:
            add(reviewer_id, finding)
    for verification in verifications:
        if verification_is_terminally_replaced(verification):
            continue
        add(
            verification.reviewer_id,
            verification.finding,
            verification.review_snapshot_id,
        )
    return {key: [finding for finding, _review_snapshot_id in value] for key, value in candidates.items()}


def unresolved_verification_candidate_pack_ids(
    findings: Iterable[Finding],
    verifications: Iterable[FindingVerification],
) -> set[tuple[str, str]]:
    """Return reviewer-pack keys with at least one candidate lacking a decision."""
    history = list(verifications)
    current_verifications = active_verifications(history)
    unresolved = {
        (verification.reviewer_id, verification.finding.context_pack_id)
        for verification in unresolved_verifications(history)
        if verification.finding.context_pack_id
    }
    for (reviewer_id, context_pack_id), candidates in verification_candidates_by_reviewer_pack(
        findings,
        history,
    ).items():
        if any(
            not _matching_verifications_for_finding(
                finding,
                current_verifications,
                reviewer_id=reviewer_id,
            )
            for finding in candidates
        ):
            unresolved.add((reviewer_id, context_pack_id))
    return unresolved


def verification_is_terminally_replaced(verification: FindingVerification) -> bool:
    return verification.superseded and (verification.superseded_reason or "").startswith("Replaced by")


def finding_decision_identity(finding: Finding) -> tuple[object, ...]:
    return (
        str(finding.severity),
        finding.title,
        finding.file,
        finding.line,
        finding.failure_mode,
        finding.evidence,
        finding.context_pack_id,
    )


def findings_are_duplicates(left: Finding, right: Finding) -> bool:
    if finding_decision_identity(left) == finding_decision_identity(right):
        return True
    if left.severity != right.severity:
        return False
    return _finding_subjects_are_duplicates(left, right)


def _finding_subjects_are_duplicates(left: Finding, right: Finding) -> bool:
    left_tokens = _finding_tokens(left)
    right_tokens = _finding_tokens(right)
    if min(len(left_tokens), len(right_tokens)) < 8:
        return False
    overlap = len(left_tokens & right_tokens)
    containment = overlap / min(len(left_tokens), len(right_tokens))
    jaccard = overlap / len(left_tokens | right_tokens)
    if containment >= 0.54 and jaccard >= 0.35:
        return True
    if _finding_code_tokens(left) & _finding_code_tokens(right) and overlap >= 8 and jaccard >= 0.20:
        return True
    return overlap >= 24 and containment >= 0.48 and jaccard >= 0.30


def _verification_subject_identity(finding: Finding) -> tuple[object, ...]:
    return (
        finding.title,
        finding.file,
        finding.line,
        finding.failure_mode,
        finding.evidence,
        finding.context_pack_id,
    )


def _cross_pack_verification_identity(finding: Finding) -> tuple[object, ...]:
    return (
        str(finding.severity),
        finding.title,
        finding.failure_mode,
        finding.evidence,
    )


def _verification_scope_is_compatible(left: Finding, right: Finding) -> bool:
    if _normalized_finding_file(left) != _normalized_finding_file(right):
        return False
    if left.context_pack_id and right.context_pack_id and left.context_pack_id != right.context_pack_id:
        return False
    if left.line is None or right.line is None:
        return left.line == right.line
    return abs(left.line - right.line) <= 3


def _normalized_finding_file(finding: Finding) -> str:
    return finding.file.replace("\\", "/")


def context_pack_fingerprint(pack: ContextPack | None) -> str:
    if pack is None:
        return ""
    return payload_fingerprint(pack.model_dump(mode="json"))


def payload_fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").removeprefix("./")


def _compact_text(value: str) -> str:
    return _TOKEN_RE.sub(" ", value.strip())


def _finding_tokens(finding: Finding) -> set[str]:
    text = "\n".join(
        [
            finding.title,
            finding.failure_mode,
            finding.evidence,
            finding.suggested_fix,
        ]
    ).lower()
    tokens = {
        token for token in _FINDING_TOKEN_RE.findall(text) if token not in _FINDING_STOP_WORDS and not token.isdigit()
    }
    tokens.update(_finding_code_tokens(finding))
    return tokens


def _finding_code_tokens(finding: Finding) -> set[str]:
    text = "\n".join(
        [
            finding.title,
            finding.failure_mode,
            finding.evidence,
            finding.suggested_fix,
        ]
    ).lower()
    return set(_FINDING_CODE_TOKEN_RE.findall(text))
