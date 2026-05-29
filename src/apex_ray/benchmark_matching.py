from typing import Any

from apex_ray.benchmark_models import ExpectedContext, ExpectedContextResult, ExpectedFinding, ExpectedFindingResult
from apex_ray.models import AnalyzerReference, ContextPack, Finding


def match_expected_findings(
    expected_findings: list[ExpectedFinding],
    findings: list[Finding],
) -> tuple[list[ExpectedFindingResult], list[Finding]]:
    candidate_indices = [
        [index for index, finding in enumerate(findings) if _finding_matches_expected(expected, finding)]
        for expected in expected_findings
    ]
    matched_finding_to_expected: dict[int, int] = {}

    def assign(expected_index: int, seen_findings: set[int]) -> bool:
        for finding_index in candidate_indices[expected_index]:
            if finding_index in seen_findings:
                continue
            seen_findings.add(finding_index)
            current_expected = matched_finding_to_expected.get(finding_index)
            if current_expected is None or assign(current_expected, seen_findings):
                matched_finding_to_expected[finding_index] = expected_index
                return True
        return False

    expected_order = sorted(
        range(len(expected_findings)),
        key=lambda index: (len(candidate_indices[index]), index),
    )
    for expected_index in expected_order:
        if candidate_indices[expected_index]:
            assign(expected_index, set())

    matched_expected_to_finding = {
        expected_index: finding_index for finding_index, expected_index in matched_finding_to_expected.items()
    }
    results = [
        ExpectedFindingResult(
            expected=expected,
            matched=index in matched_expected_to_finding,
            matched_title=(
                findings[matched_expected_to_finding[index]].title if index in matched_expected_to_finding else None
            ),
        )
        for index, expected in enumerate(expected_findings)
    ]
    unused = [finding for index, finding in enumerate(findings) if index not in matched_finding_to_expected]
    return results, unused


def match_expected_context(expected: ExpectedContext, packs: list[ContextPack]) -> ExpectedContextResult:
    for pack in packs:
        if expected.pack_file and pack.file != expected.pack_file:
            continue
        if expected.pack_id_contains and expected.pack_id_contains not in pack.id:
            continue
        if expected.related_test and expected.related_test not in pack.related_tests:
            continue
        if expected.related_test_index is not None:
            if expected.related_test is None:
                continue
            if expected.related_test_index >= len(pack.related_tests):
                continue
            if pack.related_tests[expected.related_test_index] != expected.related_test:
                continue
        if _expects_related_test_snippet(expected) and not any(
            _related_test_snippet_matches(expected, snippet) for snippet in pack.related_test_snippets
        ):
            continue
        if _expects_reference(expected) and not any(
            _reference_matches(expected, reference) for reference in pack.references
        ):
            continue
        if expected.reference_snippet_contains and not any(
            expected.reference_snippet_contains in snippet.code for snippet in pack.reference_snippets
        ):
            continue
        if _expects_callee(expected) and not any(_callee_matches(expected, callee) for callee in pack.callees):
            continue
        if expected.callee_snippet_contains and not any(
            expected.callee_snippet_contains in snippet.code for snippet in pack.callee_snippets
        ):
            continue
        if _expects_contract(expected) and not any(
            _contract_matches(expected, contract) for contract in pack.contracts
        ):
            continue
        if expected.contract_snippet_contains and not any(
            expected.contract_snippet_contains in snippet.code for snippet in pack.contract_snippets
        ):
            continue
        if _expects_metadata(expected) and not any(
            _metadata_matches(expected, reference) for reference in pack.metadata
        ):
            continue
        if expected.metadata_snippet_contains and not any(
            expected.metadata_snippet_contains in snippet.code for snippet in pack.metadata_snippets
        ):
            continue
        return ExpectedContextResult(expected=expected, matched=True, matched_pack_id=pack.id)
    return ExpectedContextResult(expected=expected, matched=False)


def _finding_matches_expected(expected: ExpectedFinding, finding: Finding) -> bool:
    if expected.file and finding.file != expected.file:
        return False
    if expected.line is not None and finding.line != expected.line:
        return False
    if expected.line_min is not None and (finding.line is None or finding.line < expected.line_min):
        return False
    if expected.line_max is not None and (finding.line is None or finding.line > expected.line_max):
        return False
    if expected.title_contains and expected.title_contains.lower() not in finding.title.lower():
        return False
    if expected.severity and finding.severity != expected.severity:
        return False
    if expected.confidence and finding.confidence != expected.confidence:
        return False
    if expected.failure_mode_contains and expected.failure_mode_contains.lower() not in finding.failure_mode.lower():
        return False
    if expected.evidence_contains and expected.evidence_contains.lower() not in finding.evidence.lower():
        return False
    if expected.suggested_fix_contains and expected.suggested_fix_contains.lower() not in finding.suggested_fix.lower():
        return False
    if (
        expected.suggested_test_contains
        and expected.suggested_test_contains.lower() not in finding.suggested_test.lower()
    ):
        return False
    return True


def _expects_related_test_snippet(expected: ExpectedContext) -> bool:
    return bool(expected.related_test_snippet_contains or expected.related_test_snippet_start_min)


def _related_test_snippet_matches(expected: ExpectedContext, snippet: Any) -> bool:
    if expected.related_test and snippet.file != expected.related_test:
        return False
    if expected.related_test_snippet_contains and expected.related_test_snippet_contains not in snippet.code:
        return False
    if expected.related_test_snippet_start_min and snippet.start_line < expected.related_test_snippet_start_min:
        return False
    return True


def _expects_reference(expected: ExpectedContext) -> bool:
    return bool(expected.reference_file or expected.reference_kind or expected.reference_text_contains)


def _reference_matches(expected: ExpectedContext, reference: AnalyzerReference) -> bool:
    if expected.reference_file and reference.file != expected.reference_file:
        return False
    if expected.reference_kind and reference.kind != expected.reference_kind:
        return False
    if expected.reference_text_contains and expected.reference_text_contains not in reference.text:
        return False
    return True


def _expects_callee(expected: ExpectedContext) -> bool:
    return bool(expected.callee_file or expected.callee_kind or expected.callee_text_contains)


def _callee_matches(expected: ExpectedContext, callee: AnalyzerReference) -> bool:
    if expected.callee_file and callee.file != expected.callee_file:
        return False
    if expected.callee_kind and callee.kind != expected.callee_kind:
        return False
    if expected.callee_text_contains and expected.callee_text_contains not in callee.text:
        return False
    return True


def _expects_contract(expected: ExpectedContext) -> bool:
    return bool(expected.contract_file or expected.contract_kind or expected.contract_text_contains)


def _contract_matches(expected: ExpectedContext, contract: AnalyzerReference) -> bool:
    if expected.contract_file and contract.file != expected.contract_file:
        return False
    if expected.contract_kind and contract.kind != expected.contract_kind:
        return False
    if expected.contract_text_contains and expected.contract_text_contains not in contract.text:
        return False
    return True


def _expects_metadata(expected: ExpectedContext) -> bool:
    return bool(expected.metadata_file or expected.metadata_kind or expected.metadata_text_contains)


def _metadata_matches(expected: ExpectedContext, reference: AnalyzerReference) -> bool:
    if expected.metadata_file and reference.file != expected.metadata_file:
        return False
    if expected.metadata_kind and reference.kind != expected.metadata_kind:
        return False
    if expected.metadata_text_contains and expected.metadata_text_contains not in reference.text:
        return False
    return True
