import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

from apex_ray.models import ContextPack, Finding

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
    if left.severity != right.severity:
        return False
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
