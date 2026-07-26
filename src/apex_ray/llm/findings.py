import posixpath

from apex_ray.findings import merge_finding_reviewer_provenance
from apex_ray.models import ContextPack, Finding, FindingVerification


def filter_findings_for_context_pack(findings: list[Finding], pack: ContextPack) -> list[Finding]:
    context_files = _context_files(pack)
    filtered: list[Finding] = []
    for finding in findings:
        normalized_file = normalize_context_file(finding.file)
        if finding.context_pack_id and finding.context_pack_id != pack.id:
            continue
        if normalized_file not in context_files:
            continue
        filtered.append(
            finding.model_copy(
                update={
                    "context_pack_id": pack.id,
                    "file": normalized_file,
                    # Reviewer origin metadata is assigned by Apex Ray and must
                    # never be accepted from an untrusted model response.
                    "reviewer_context_pack_ids": {},
                }
            )
        )
    return filtered


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    deduped: dict[tuple[str, str, int | None, str], Finding] = {}
    for finding in findings:
        key = (
            _normalize_for_dedupe(finding.title),
            normalize_context_file(finding.file),
            finding.line,
            _normalize_for_dedupe(finding.failure_mode),
        )
        current = deduped.get(key)
        if current is None:
            deduped[key] = finding
            continue
        preferred = finding if _finding_rank(finding) > _finding_rank(current) else current
        deduped[key] = merge_finding_reviewer_provenance(
            preferred,
            [current, finding],
        )
    return list(deduped.values())


def verification_for_finding(verification: FindingVerification, finding: Finding) -> FindingVerification:
    if verification.finding == finding:
        return verification
    return FindingVerification(
        finding=finding,
        approved=verification.approved,
        confidence=verification.confidence,
        reason=verification.reason,
    )


def normalize_context_file(path: str) -> str:
    return posixpath.normpath(path.strip().replace("\\", "/")).removeprefix("./")


def _context_files(pack: ContextPack) -> set[str]:
    files = {normalize_context_file(pack.file)}
    files.update(normalize_context_file(snippet.file) for snippet in pack.changed_snippets)
    files.update(normalize_context_file(snippet.file) for snippet in pack.reference_snippets)
    files.update(normalize_context_file(snippet.file) for snippet in pack.callee_snippets)
    files.update(normalize_context_file(snippet.file) for snippet in pack.contract_snippets)
    files.update(normalize_context_file(snippet.file) for snippet in pack.metadata_snippets)
    files.update(normalize_context_file(snippet.file) for snippet in pack.related_test_snippets)
    files.update(normalize_context_file(reference.file) for reference in pack.references)
    files.update(normalize_context_file(callee.file) for callee in pack.callees)
    files.update(normalize_context_file(reference.file) for reference in pack.contracts)
    files.update(normalize_context_file(reference.file) for reference in pack.metadata)
    files.update(normalize_context_file(path) for path in pack.related_tests)
    return files


def _normalize_for_dedupe(value: str) -> str:
    return " ".join(value.lower().strip().split())


def _finding_rank(finding: Finding) -> tuple[int, int]:
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    return (
        severity_rank.get(str(finding.severity), 0),
        confidence_rank.get(str(finding.confidence), 0),
    )
