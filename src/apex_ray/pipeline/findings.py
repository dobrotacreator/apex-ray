import re

from apex_ray.models import Finding

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


def consolidate_findings(findings: list[Finding]) -> list[Finding]:
    consolidated: list[Finding] = []
    for finding in findings:
        duplicate_index = next(
            (index for index, existing in enumerate(consolidated) if _findings_are_duplicates(existing, finding)),
            None,
        )
        if duplicate_index is None:
            consolidated.append(finding)
            continue
        if _finding_preference_key(finding) > _finding_preference_key(consolidated[duplicate_index]):
            consolidated[duplicate_index] = finding
    return consolidated


def _findings_are_duplicates(left: Finding, right: Finding) -> bool:
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


def _finding_preference_key(finding: Finding) -> tuple[int, int, int]:
    return (
        0 if _is_test_path(finding.file) else 1,
        {"low": 1, "medium": 2, "high": 3}.get(str(finding.confidence), 0),
        1 if finding.line is not None else 0,
    )


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        ".test." in normalized
        or ".spec." in normalized
        or "/__tests__/" in normalized
        or normalized.startswith("__tests__/")
    )
