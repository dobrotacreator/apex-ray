from collections.abc import Iterable

from apex_ray.findings import (
    finding_decision_identity,
    findings_are_duplicates,
    merge_finding_reviewer_provenance,
)
from apex_ray.models import Finding


def consolidate_findings(
    findings: list[Finding],
    *,
    preferred_findings: Iterable[Finding] = (),
) -> list[Finding]:
    preferred_identities = {finding_decision_identity(finding) for finding in preferred_findings}
    consolidated: list[Finding] = []
    for finding in findings:
        duplicate_index = next(
            (index for index, existing in enumerate(consolidated) if findings_are_duplicates(existing, finding)),
            None,
        )
        if duplicate_index is None:
            consolidated.append(finding)
            continue
        existing = consolidated[duplicate_index]
        preferred = (
            finding
            if _finding_preference_key(finding, preferred_identities)
            > _finding_preference_key(existing, preferred_identities)
            else existing
        )
        consolidated[duplicate_index] = merge_finding_reviewer_provenance(
            preferred,
            [existing, finding],
        )
    return consolidated


def _finding_preference_key(
    finding: Finding,
    preferred_identities: set[tuple[object, ...]] | None = None,
) -> tuple[int, int, int, int]:
    return (
        1 if finding_decision_identity(finding) in (preferred_identities or set()) else 0,
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
