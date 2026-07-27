import fnmatch
import hashlib
import json
from collections.abc import Iterable, MutableMapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from apex_ray.discovery import LANGUAGE_EXTENSIONS
from apex_ray.memory import pack_prompt_payload
from apex_ray.models import ChangedFile, ContextPack, Finding, ReviewReport
from apex_ray.resolution import (
    context_pack_resolution_paths,
    novel_resolution_file_is_reviewable,
    resolution_file_is_reviewable,
)

LANGUAGE_HINTS = {
    "javascript": "TypeScript/JavaScript",
    "python": "Python",
    "typescript": "TypeScript/JavaScript",
}
RESOLUTION_PROMPT_MAX_CHARS = 256_000
_RESOLUTION_FINDING_MAX_CHARS = 24_000
_RESOLUTION_PREVIOUS_PACK_MAX_CHARS = 48_000
_RESOLUTION_METADATA_MAX_CHARS = 12_000
_RESOLUTION_DIFF_MAX_CHARS = 64_000
_RESOLUTION_CONTEXT_PACKS_MAX_CHARS = 84_000
_RESOLUTION_DIFF_WARNINGS_MAX_CHARS = 12_000
_RESOLUTION_DIFF_FILE_LIMIT = 20
_RESOLUTION_CONTEXT_PACK_LIMIT = 8
_RESOLUTION_FALLBACK_DIFF_FILE_LIMIT = 4
_RESOLUTION_FALLBACK_CONTEXT_PACK_LIMIT = 2
_RESOLUTION_DIFF_WARNING_LIMIT = 8
_RESOLUTION_DIFF_WARNING_MAX_CHARS = 1_024


@dataclass(frozen=True)
class RenderedReviewPrompt:
    text: str
    _prompt_payload: dict[str, object] = field(repr=False)
    payload_chars: int

    @property
    def prompt_payload(self) -> dict[str, object]:
        # Keep cached render artifacts immutable to callers. The returned
        # payload is short-lived while deriving the durable LLM cache key.
        return deepcopy(self._prompt_payload)


@dataclass(frozen=True)
class _ResolutionWarningEvidence:
    text: str
    total: int
    omitted: int
    truncated: int
    char_truncated: bool
    incomplete: bool


type ReviewPromptCacheKey = tuple[str, Literal["deep", "shallow"], str]
type ReviewPromptCache = MutableMapping[ReviewPromptCacheKey, RenderedReviewPrompt]


def review_prompt_cache_key(
    pack: ContextPack,
    review_depth: Literal["deep", "shallow"],
) -> tuple[str, Literal["deep", "shallow"], str]:
    render_identity = hashlib.sha256(
        pack.model_dump_json().encode("utf-8"),
    )
    return (pack.id, review_depth, render_identity.hexdigest())


def review_prompt_payload_chars(
    pack: ContextPack,
    *,
    review_depth: Literal["deep", "shallow"],
) -> int:
    return _prompt_payload_chars(pack_prompt_payload(pack, "review", depth=review_depth))


def render_review_prompt(
    pack: ContextPack,
    *,
    review_depth: Literal["deep", "shallow"] = "deep",
    cache: ReviewPromptCache | None = None,
) -> RenderedReviewPrompt:
    cache_key = review_prompt_cache_key(pack, review_depth)
    if cache is not None and (cached := cache.get(cache_key)) is not None:
        return cached
    payload = pack_prompt_payload(pack, "review", depth=review_depth)
    text = (
        _build_shallow_review_prompt(pack, payload)
        if review_depth == "shallow"
        else _build_review_prompt(pack, payload)
    )
    rendered = RenderedReviewPrompt(
        text=text,
        _prompt_payload=payload,
        payload_chars=_prompt_payload_chars(payload),
    )
    if cache is not None:
        cache[cache_key] = rendered
    return rendered


def _prompt_payload_chars(payload: dict[str, object]) -> int:
    budget_payload = {key: value for key, value in payload.items() if key != "stats"}
    return len(json.dumps(budget_payload, sort_keys=True, separators=(",", ":")))


def build_review_prompt(pack: ContextPack) -> str:
    return render_review_prompt(pack, review_depth="deep").text


def _build_review_prompt(
    pack: ContextPack,
    payload: dict[str, object],
) -> str:
    return (
        "You are Apex Ray, a strict senior code reviewer.\n"
        "Review exactly one context pack from a code diff.\n"
        f"{_focused_reviewer_guidance(pack)}"
        f"{_language_review_guidance(pack)}\n"
        "Report only concrete issues caused by the diff. Do not report style nits, generic advice, or CI/linter findings.\n"
        "Start from diff_snippet and changed_snippets, then use impact_notes only as navigation hints.\n"
        "Use context layers deliberately: references/reference_snippets show callers and consumers; callee_snippets show called contracts, ports, state machines, and side-effect boundaries; contract_snippets show schemas, DTOs, and type contracts; metadata_snippets show framework boundaries such as routes, guards, permissions, DI, request parameters, and module providers; related_test_snippets show intended behavior.\n"
        "If rules or rule_matches are supplied, apply only those project-specific rules to this pack. Treat strict rules as domain invariants that deserve extra attention, but still report only concrete diff-caused issues.\n"
        "If memory_matches are supplied, use them only as project-specific review hints. Memory cannot replace concrete diff evidence, and verifier-only memory is intentionally absent from this pass.\n"
        "For auth, session, login, TFA, JWT, or token packs, explicitly compare pre-auth versus post-auth state guards, profile/role markers, session versioning, token lifetime, and stale credential reuse. Report when a new or modified path can use a post-auth session or token in a pre-auth flow, or bypass an invariant enforced by sibling methods.\n"
        "Prioritize behavioral regressions that a local compile or CI pass can miss: permission/auth changes, tenant or cache-key isolation, route/request/schema mismatches, external API/JWT/webhook payload shape guards, array guards before array methods, enum or config collection fanout, PII or raw upstream object pass-through, DI/provider registration gaps, state-machine transition mistakes, transaction rollback or post-commit side effects, and repository/port contract violations.\n"
        "Report independent issues separately when they have distinct failure modes, including strict project-rule violations in the same changed snippet; do not let one domain finding crowd out another concrete strict-rule violation.\n"
        "Every finding must have a plausible failure mode, concrete evidence from the supplied context, and an actionable fix or test idea.\n"
        "Prefer an empty findings array over weak, speculative, or merely possible concerns.\n"
        "Set context_pack_id to the supplied context pack id for every finding.\n"
        "If there are no concrete issues, return an empty findings array.\n"
        "Return only JSON that matches the provided schema.\n\n"
        "Context pack JSON:\n"
        f"{json.dumps(payload, indent=2)}\n"
    )


def build_shallow_review_prompt(pack: ContextPack) -> str:
    return render_review_prompt(pack, review_depth="shallow").text


def _build_shallow_review_prompt(
    pack: ContextPack,
    payload: dict[str, object],
) -> str:
    return (
        "You are Apex Ray's fast shallow code-review pass.\n"
        "Review exactly one compact code context pack from a diff.\n"
        f"{_focused_reviewer_guidance(pack)}"
        f"{_language_shallow_review_guidance(pack)}\n"
        "Use only the supplied diff_snippet, changed_snippets, risk_signals, rules, and memory hints.\n"
        "This pass optimizes breadth and recall on large PRs; report only concrete diff-caused issues visible in this compact context.\n"
        "Do not infer from missing callers, missing schemas, or absent files. Do not report style nits, generic advice, or CI/linter findings.\n"
        "For strict project rules and high-risk signals, look for direct violations in the changed lines and snippets.\n"
        "Every finding must include a plausible failure mode, concrete evidence, and an actionable fix or test idea.\n"
        "Prefer an empty findings array over weak or speculative concerns.\n"
        "Set context_pack_id to the supplied context pack id for every finding.\n"
        "Return only JSON that matches the provided schema.\n\n"
        "Compact context pack JSON:\n"
        f"{json.dumps(payload, indent=2)}\n"
    )


def build_verifier_prompt(finding: Finding, pack: ContextPack) -> str:
    finding_payload = finding.model_dump(mode="json")
    pack_payload = pack_prompt_payload(pack, "verify")
    return (
        "You are Apex Ray's verification pass for AI code review findings.\n"
        "Decide whether the finding should be published.\n"
        "Approve only if the issue is caused by the diff, has concrete evidence in the context pack, is actionable, and is not a style nit or generic advice.\n"
        "Approve concrete diff-caused violations of supplied strict project rules when the changed code clearly violates the rule; a strict safety, boundary, or project-policy violation is actionable even when the immediate failure mode is policy drift or future boundary risk rather than a current runtime exception.\n"
        "Still reject generic style preferences that are not tied to a supplied strict rule or concrete behavioral risk.\n"
        "Treat impact_notes as navigation hints only; reject if the concrete diff/snippet evidence does not support the finding.\n"
        "Reject if context_pack_id differs from the supplied context pack id, or if finding.file is not present in any supplied context layer: changed snippets, references, callees, contracts, metadata, or related tests.\n"
        "Use context layers deliberately: references show consumers, callees show called contracts and side-effect boundaries, contracts show schemas/DTO/type requirements, metadata shows framework/route/permission/DI boundaries, and related tests show intended behavior.\n"
        f"{_language_verifier_guidance(pack)}\n"
        "If rules or rule_matches are supplied, use them as project-specific review criteria. A strict rule can establish review significance when the diff and snippets concretely show a violation, but it cannot replace missing evidence that the changed code exists or that an external behavior assumption is true.\n"
        "If memory_matches are supplied, use them as project-specific calibration, including known false-positive and severity-calibration entries. Reject findings that match known false positives unless the diff evidence materially differs.\n"
        "When consumers, contracts, metadata, or related tests are supplied, approve only when the failure mode is connected to at least one concrete supplied layer and the changed code can realistically trigger it.\n"
        "Reject the finding if it is speculative, contradicted by context, already handled by the changed code, lacks a plausible failure mode, or depends on missing assumptions.\n"
        "Return only JSON that matches the provided schema.\n\n"
        "Candidate finding JSON:\n"
        f"{json.dumps(finding_payload, indent=2)}\n\n"
        "Context pack JSON:\n"
        f"{json.dumps(pack_payload, indent=2)}\n"
    )


def build_verifier_batch_prompt(findings: list[Finding], pack: ContextPack) -> str:
    findings_payload = [finding.model_dump(mode="json") for finding in findings]
    pack_payload = pack_prompt_payload(pack, "verify")
    return (
        "You are Apex Ray's batched verification pass for AI code review findings.\n"
        "Decide whether each candidate finding should be published.\n"
        "Return one decision per input finding in decisions[], using finding_index to point at the zero-based index in candidate_findings.\n"
        "Approve only if the issue is caused by the diff, has concrete evidence in the context pack, is actionable, and is not a style nit or generic advice.\n"
        "Approve concrete diff-caused violations of supplied strict project rules when the changed code clearly violates the rule; a strict safety, boundary, or project-policy violation is actionable even when the immediate failure mode is policy drift or future boundary risk rather than a current runtime exception.\n"
        "Still reject generic style preferences that are not tied to a supplied strict rule or concrete behavioral risk.\n"
        "Treat impact_notes as navigation hints only; reject if the concrete diff/snippet evidence does not support the finding.\n"
        "Reject if context_pack_id differs from the supplied context pack id, or if finding.file is not present in any supplied context layer: changed snippets, references, callees, contracts, metadata, or related tests.\n"
        "Use context layers deliberately: references show consumers, callees show called contracts and side-effect boundaries, contracts show schemas/DTO/type requirements, metadata shows framework/route/permission/DI boundaries, and related tests show intended behavior.\n"
        f"{_language_verifier_guidance(pack)}\n"
        "If rules or rule_matches are supplied, use them as project-specific review criteria. A strict rule can establish review significance when the diff and snippets concretely show a violation, but it cannot replace missing evidence that the changed code exists or that an external behavior assumption is true.\n"
        "If memory_matches are supplied, use them as project-specific calibration, including known false-positive and severity-calibration entries. Reject findings that match known false positives unless the diff evidence materially differs.\n"
        "When consumers, contracts, metadata, or related tests are supplied, approve only when the failure mode is connected to at least one concrete supplied layer and the changed code can realistically trigger it.\n"
        "Reject a finding if it is speculative, contradicted by context, already handled by the changed code, lacks a plausible failure mode, or depends on missing assumptions.\n"
        "Evaluate each candidate independently; approving one finding must not make a weaker sibling finding pass.\n"
        "Return only JSON that matches the provided schema.\n\n"
        "Candidate findings JSON:\n"
        f"{json.dumps({'candidate_findings': findings_payload}, indent=2)}\n\n"
        "Context pack JSON:\n"
        f"{json.dumps(pack_payload, indent=2)}\n"
    )


def build_resolution_prompt(
    finding: Finding,
    previous_pack: ContextPack | None,
    delta_report: ReviewReport,
) -> str:
    previous_payload = pack_prompt_payload(previous_pack, "verify") if previous_pack is not None else None
    primary_paths, relevant_literal_paths, relevant_glob_patterns = _resolution_relevant_paths(
        finding,
        previous_pack,
    )
    provenance_ids = _resolution_provenance_ids(finding, previous_pack)
    relevant_diff_files = sorted(
        (
            changed_file
            for changed_file in delta_report.diff.files
            if any(
                _resolution_path_matches(
                    path,
                    relevant_literal_paths,
                    relevant_glob_patterns,
                )
                for path in _resolution_changed_file_paths(changed_file)
            )
        ),
        key=lambda changed_file: _resolution_diff_rank(changed_file, primary_paths),
    )
    relevant_diff_file_ids = {id(changed_file) for changed_file in relevant_diff_files}
    graph_connected_pack_ids = {
        id(pack)
        for pack in delta_report.context_packs
        if any(
            _resolution_path_matches(
                path,
                relevant_literal_paths,
                relevant_glob_patterns,
            )
            for path in context_pack_resolution_paths(pack)
        )
    }
    graph_connected_file_paths = {
        _normalize_resolution_path(pack.file)
        for pack in delta_report.context_packs
        if id(pack) in graph_connected_pack_ids
    }
    unanchored_diff_files = [
        changed_file for changed_file in delta_report.diff.files if id(changed_file) not in relevant_diff_file_ids
    ]
    fallback_diff_files = sorted(
        (
            changed_file
            for changed_file in unanchored_diff_files
            if resolution_file_is_reviewable(changed_file)
            and (
                novel_resolution_file_is_reviewable(changed_file)
                or bool(graph_connected_file_paths.intersection(_resolution_changed_file_paths(changed_file)))
            )
        ),
        key=lambda changed_file: _resolution_fallback_diff_rank(
            changed_file,
            graph_connected_file_paths=graph_connected_file_paths,
        ),
    )
    selected_fallback_diff_files = fallback_diff_files[:_RESOLUTION_FALLBACK_DIFF_FILE_LIMIT]
    relevant_diff_limit = _RESOLUTION_DIFF_FILE_LIMIT - len(selected_fallback_diff_files)
    selected_relevant_diff_files = relevant_diff_files[:relevant_diff_limit]
    selected_diff_files = [*selected_relevant_diff_files, *selected_fallback_diff_files]
    current_relevant_literal_paths = {
        *relevant_literal_paths,
        *[
            path
            for changed_file in selected_relevant_diff_files
            for path in _resolution_changed_file_paths(changed_file)
        ],
    }
    identity_tokens_by_file = _resolution_identity_tokens_by_file(
        provenance_ids,
        selected_relevant_diff_files,
    )
    relevant_packs = sorted(
        (
            pack
            for pack in delta_report.context_packs
            if pack.id in provenance_ids
            or _resolution_path_matches(
                pack.file,
                current_relevant_literal_paths,
                relevant_glob_patterns,
            )
        ),
        key=lambda pack: _resolution_pack_rank(
            pack,
            finding=finding,
            primary_paths=primary_paths,
            provenance_ids=provenance_ids,
            identity_tokens_by_file=identity_tokens_by_file,
        ),
    )
    relevant_pack_ids = {id(pack) for pack in relevant_packs}
    fallback_diff_paths = {
        path for changed_file in selected_fallback_diff_files for path in _resolution_changed_file_paths(changed_file)
    }
    fallback_pack_candidates = sorted(
        (
            pack
            for pack in delta_report.context_packs
            if id(pack) not in relevant_pack_ids and _normalize_resolution_path(pack.file) in fallback_diff_paths
        ),
        key=lambda pack: _resolution_fallback_pack_rank(
            pack,
            graph_connected=id(pack) in graph_connected_pack_ids,
        ),
    )
    selected_fallback_packs = _select_resolution_fallback_packs(
        fallback_pack_candidates,
        limit=_RESOLUTION_FALLBACK_CONTEXT_PACK_LIMIT,
    )
    relevant_pack_limit = _RESOLUTION_CONTEXT_PACK_LIMIT - len(selected_fallback_packs)
    selected_relevant_packs = relevant_packs[:relevant_pack_limit]
    selected_packs = [*selected_relevant_packs, *selected_fallback_packs]
    finding_text, finding_char_truncated = _bounded_resolution_json(
        finding.model_dump(mode="json"),
        max_chars=_RESOLUTION_FINDING_MAX_CHARS,
        identity={
            "title": finding.title,
            "severity": finding.severity,
            "confidence": finding.confidence,
            "file": finding.file,
            "line": finding.line,
            "failure_mode": finding.failure_mode,
            "evidence": finding.evidence,
            "suggested_fix": finding.suggested_fix,
            "suggested_test": finding.suggested_test,
            "context_pack_id": finding.context_pack_id,
        },
    )
    previous_text, previous_char_truncated = _bounded_resolution_json(
        previous_payload,
        max_chars=_RESOLUTION_PREVIOUS_PACK_MAX_CHARS,
        identity=(
            {"file": previous_pack.file, "context_pack_id": previous_pack.id}
            if previous_pack is not None
            else {"context_pack_id": None}
        ),
    )
    warning_evidence = _build_resolution_warning_evidence(delta_report)
    diff_payload = {
        "base": delta_report.diff.base,
        "target_mode": delta_report.diff.target_mode,
        "files": [changed_file.model_dump(mode="json") for changed_file in selected_diff_files],
        "stats": {
            "files_changed": len(selected_diff_files),
            "additions": sum(changed_file.additions for changed_file in selected_diff_files),
            "deletions": sum(changed_file.deletions for changed_file in selected_diff_files),
            "ignored_files": sum(changed_file.is_ignored for changed_file in selected_diff_files),
        },
    }
    diff_text, diff_char_truncated = _bounded_resolution_json(
        diff_payload,
        max_chars=_RESOLUTION_DIFF_MAX_CHARS,
        identity={
            "selected_files": [
                path for changed_file in selected_diff_files for path in _resolution_changed_file_paths(changed_file)
            ]
        },
    )
    diff_warning_evidence_incomplete = warning_evidence.incomplete
    pack_payloads = [pack_prompt_payload(pack, "verify") for pack in selected_packs]
    packs_text, packs_char_truncated = _bounded_resolution_json(
        pack_payloads,
        max_chars=_RESOLUTION_CONTEXT_PACKS_MAX_CHARS,
        identity={"selected_context_pack_ids": [pack.id for pack in selected_packs]},
    )
    unselected_unanchored_diff_files = len(fallback_diff_files) - len(selected_fallback_diff_files)
    excluded_ineligible_fallback_diff_files = len(unanchored_diff_files) - len(fallback_diff_files)
    unselected_unanchored_context_packs = (
        len(delta_report.context_packs) - len(relevant_packs) - len(selected_fallback_packs)
    )
    omitted_relevant_diff_files = len(relevant_diff_files) - len(selected_relevant_diff_files)
    omitted_relevant_context_packs = len(relevant_packs) - len(selected_relevant_packs)
    char_truncated_sections = [
        name
        for name, truncated in (
            ("current_context_packs", packs_char_truncated),
            ("current_diff", diff_char_truncated),
            ("current_diff_warnings", warning_evidence.char_truncated),
            ("previous_context_pack", previous_char_truncated),
            ("previous_finding", finding_char_truncated),
        )
        if truncated
    ]
    truncation_reasons = [
        reason
        for reason, applies in (
            ("unanchored_diff_files_unselected", unselected_unanchored_diff_files > 0),
            (
                "ineligible_fallback_diff_files_excluded",
                excluded_ineligible_fallback_diff_files > 0,
            ),
            (
                "unanchored_context_packs_unselected",
                unselected_unanchored_context_packs > 0,
            ),
            ("relevant_diff_file_count_limited", omitted_relevant_diff_files > 0),
            ("relevant_context_pack_count_limited", omitted_relevant_context_packs > 0),
            ("fallback_diff_evidence_included", bool(selected_fallback_diff_files)),
            ("fallback_context_evidence_included", bool(selected_fallback_packs)),
            ("global_diff_warnings_present", bool(delta_report.diff.warnings)),
            ("global_diff_warning_count_limited", warning_evidence.omitted > 0),
            ("global_diff_warning_chars_limited", warning_evidence.truncated > 0),
            ("global_diff_warning_evidence_incomplete", diff_warning_evidence_incomplete),
            ("section_char_budget_applied", bool(char_truncated_sections)),
        )
        if applies
    ]
    metadata = {
        "applied": bool(truncation_reasons),
        "max_prompt_chars": RESOLUTION_PROMPT_MAX_CHARS,
        "relevant_literal_paths": _bounded_resolution_labels(current_relevant_literal_paths),
        "relevant_glob_patterns": _bounded_resolution_labels(relevant_glob_patterns),
        "selected_diff_files": _bounded_resolution_labels(
            path for changed_file in selected_diff_files for path in _resolution_changed_file_paths(changed_file)
        ),
        "selected_context_pack_ids": _bounded_resolution_labels(pack.id for pack in selected_packs),
        "selected_fallback_diff_files": _bounded_resolution_labels(
            path
            for changed_file in selected_fallback_diff_files
            for path in _resolution_changed_file_paths(changed_file)
        ),
        "selected_fallback_context_pack_ids": _bounded_resolution_labels(pack.id for pack in selected_fallback_packs),
        "unselected_unanchored_diff_files": unselected_unanchored_diff_files,
        "excluded_ineligible_fallback_diff_files": excluded_ineligible_fallback_diff_files,
        "unselected_unanchored_context_packs": unselected_unanchored_context_packs,
        "omitted_relevant_diff_files": omitted_relevant_diff_files,
        "omitted_relevant_context_packs": omitted_relevant_context_packs,
        "global_diff_warnings_count": warning_evidence.total,
        "omitted_global_diff_warnings": warning_evidence.omitted,
        "truncated_global_diff_warnings": warning_evidence.truncated,
        "diff_warning_evidence_incomplete": diff_warning_evidence_incomplete,
        "char_truncated_sections": char_truncated_sections,
        "reasons": truncation_reasons,
    }
    metadata_text, metadata_char_truncated = _bounded_resolution_json(
        metadata,
        max_chars=_RESOLUTION_METADATA_MAX_CHARS,
        identity={"applied": metadata["applied"], "reasons": truncation_reasons},
    )
    if metadata_char_truncated:
        metadata["applied"] = True
        metadata["char_truncated_sections"] = [*char_truncated_sections, "truncation_metadata"]
        metadata["reasons"] = [*truncation_reasons, "section_char_budget_applied"]
        metadata_text, _ = _bounded_resolution_json(
            metadata,
            max_chars=_RESOLUTION_METADATA_MAX_CHARS,
            identity={"applied": True, "reasons": metadata["reasons"]},
        )
    prompt = (
        "You are Apex Ray's strict pre-push retry resolution pass.\n"
        "Decide whether a previously verified blocking code-review finding is resolved in the current snapshot.\n"
        "Return status `resolved` only when the supplied delta and current context prove that the failure mode no longer applies.\n"
        "Return `still_present` when the same failure mode remains visible or the delta leaves the relevant code unchanged.\n"
        "Return `uncertain` when the supplied context is insufficient, ambiguous, or the fix may be elsewhere.\n"
        "Do not mark resolved merely because the new delta review produced no findings.\n"
        "Treat previous_context_pack as historical evidence for what was blocked, and the relevant current diff and context packs as the only new evidence.\n"
        "The current evidence is relevance-filtered, includes a small bounded fallback for previously unknown fix surfaces, and may be count- or character-truncated. Inspect truncation_metadata and return `uncertain` whenever omitted evidence is needed to prove resolution.\n"
        "Diff warnings are global reliability evidence. Return `uncertain` if a supplied warning makes the relevant proof incomplete. When diff_warning_evidence_incomplete is true, return `uncertain` unconditionally.\n"
        "Prefer `uncertain` over `resolved` when proof is incomplete. `still_present` and `uncertain` both continue to block the gate.\n"
        "Return only JSON that matches the provided schema.\n\n"
        "Previous blocking finding JSON:\n"
        f"{finding_text}\n\n"
        "Previous context pack JSON:\n"
        f"{previous_text}\n\n"
        "Delta report truncation_metadata JSON:\n"
        f"{metadata_text}\n\n"
        "Relevant current diff JSON:\n"
        f"{diff_text}\n\n"
        "Global current diff warnings JSON:\n"
        f"{warning_evidence.text}\n\n"
        "Relevant current context packs JSON:\n"
        f"{packs_text}\n"
    )
    if len(prompt) > RESOLUTION_PROMPT_MAX_CHARS:
        raise RuntimeError(
            "Resolution prompt exceeded its internal hard character budget; "
            "returning it could exceed the configured LLM provider input limit."
        )
    return prompt


def _resolution_relevant_paths(
    finding: Finding,
    previous_pack: ContextPack | None,
) -> tuple[set[str], set[str], set[str]]:
    primary_paths = {
        _normalize_resolution_path(path)
        for path in [
            finding.file,
            *[_resolution_path_from_pack_id(pack_id) for pack_id in _resolution_provenance_ids(finding, previous_pack)],
            previous_pack.file if previous_pack is not None else "",
        ]
        if path
    }
    relevant_literal_paths = set(primary_paths)
    relevant_glob_patterns: set[str] = set()
    if previous_pack is None:
        return primary_paths, relevant_literal_paths, relevant_glob_patterns
    relevant_literal_paths.update(_normalize_resolution_path(path) for path in previous_pack.related_tests if path)
    for rule in previous_pack.rule_matches:
        relevant_glob_patterns.update(_normalize_resolution_path(path) for path in rule.resolution_surfaces if path)
    for reference in [
        *previous_pack.references,
        *previous_pack.callees,
        *previous_pack.contracts,
        *previous_pack.metadata,
    ]:
        if reference.file:
            relevant_literal_paths.add(_normalize_resolution_path(reference.file))
    for snippet in [
        *previous_pack.reference_snippets,
        *previous_pack.callee_snippets,
        *previous_pack.contract_snippets,
        *previous_pack.metadata_snippets,
        *previous_pack.related_test_snippets,
    ]:
        if snippet.file:
            relevant_literal_paths.add(_normalize_resolution_path(snippet.file))
    return primary_paths, relevant_literal_paths, relevant_glob_patterns


def _resolution_provenance_ids(
    finding: Finding,
    previous_pack: ContextPack | None,
) -> set[str]:
    pack_ids = {
        finding.context_pack_id,
        previous_pack.id if previous_pack is not None else "",
        *[pack_id for reviewer_pack_ids in finding.reviewer_context_pack_ids.values() for pack_id in reviewer_pack_ids],
    }
    return {pack_id for pack_id in pack_ids if pack_id}


def _resolution_path_from_pack_id(pack_id: str) -> str:
    return pack_id.split("#", 1)[0] if "#" in pack_id else ""


def _normalize_resolution_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _resolution_path_matches(
    path: str,
    literal_paths: set[str],
    glob_patterns: set[str],
) -> bool:
    normalized = _normalize_resolution_path(path)
    return normalized in literal_paths or any(fnmatch.fnmatchcase(normalized, pattern) for pattern in glob_patterns)


def _resolution_changed_file_paths(changed_file: ChangedFile) -> tuple[str, ...]:
    return tuple(
        sorted({_normalize_resolution_path(path) for path in (changed_file.old_path, changed_file.new_path) if path})
    )


def _resolution_diff_rank(
    changed_file: ChangedFile,
    primary_paths: set[str],
) -> tuple[int, tuple[str, ...]]:
    paths = _resolution_changed_file_paths(changed_file)
    return (0 if primary_paths.intersection(paths) else 1, paths)


def _resolution_fallback_diff_rank(
    changed_file: ChangedFile,
    *,
    graph_connected_file_paths: set[str],
) -> tuple[int, int, int, int, tuple[str, ...]]:
    status = str(changed_file.status)
    if changed_file.old_path is None and changed_file.new_path is not None:
        status = "added"
    status_rank = {
        "added": 0,
        "renamed": 1,
        "modified": 2,
        "copied": 3,
        "deleted": 4,
    }.get(status, 5)
    risk_signals = [
        *changed_file.risk_signals,
        *(signal for hunk in changed_file.hunks for signal in hunk.risk_signals),
    ]
    severity_rank = min(
        (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(
                str(signal.severity),
                4,
            )
            for signal in risk_signals
        ),
        default=4,
    )
    max_risk_score = max((signal.score for signal in risk_signals), default=0)
    return (
        (0 if graph_connected_file_paths.intersection(_resolution_changed_file_paths(changed_file)) else 1),
        status_rank,
        severity_rank,
        -max_risk_score,
        _resolution_changed_file_paths(changed_file),
    )


def _resolution_identity_tokens_by_file(
    provenance_ids: set[str],
    relevant_diff_files: list[ChangedFile],
) -> dict[str, set[str]]:
    tokens_by_file: dict[str, set[str]] = {}
    for pack_id in provenance_ids:
        path = _normalize_resolution_path(_resolution_path_from_pack_id(pack_id))
        tokens = _resolution_pack_identity_tokens(pack_id)
        if path and tokens:
            tokens_by_file.setdefault(path, set()).update(tokens)
    for changed_file in relevant_diff_files:
        if str(changed_file.status) != "renamed":
            continue
        paths = _resolution_changed_file_paths(changed_file)
        linked_tokens = {token for path in paths for token in tokens_by_file.get(path, set())}
        if linked_tokens:
            for path in paths:
                tokens_by_file.setdefault(path, set()).update(linked_tokens)
    return tokens_by_file


def _resolution_pack_rank(
    pack: ContextPack,
    *,
    finding: Finding,
    primary_paths: set[str],
    provenance_ids: set[str],
    identity_tokens_by_file: dict[str, set[str]],
) -> tuple[int, str, str]:
    normalized_file = _normalize_resolution_path(pack.file)
    overlaps_finding_line = finding.line is not None and any(
        start_line <= finding.line <= end_line for start_line, end_line in pack.changed_lines
    )
    shares_identity = bool(
        identity_tokens_by_file.get(normalized_file, set()).intersection(_resolution_pack_identity_tokens(pack.id))
    )
    if pack.id in provenance_ids:
        priority = 0
    elif shares_identity:
        priority = 1
    elif normalized_file in primary_paths and overlaps_finding_line:
        priority = 2
    elif normalized_file in primary_paths:
        priority = 3
    else:
        priority = 4
    return (priority, normalized_file, pack.id)


def _resolution_fallback_pack_rank(
    pack: ContextPack,
    *,
    graph_connected: bool,
) -> tuple[int, int, str, str]:
    return (
        0 if graph_connected else 1,
        0 if pack.changed_lines else 1,
        _normalize_resolution_path(pack.file),
        pack.id,
    )


def _select_resolution_fallback_packs(
    candidates: list[ContextPack],
    *,
    limit: int,
) -> list[ContextPack]:
    selected: list[ContextPack] = []
    selected_ids: set[int] = set()
    selected_files: set[str] = set()
    for pack in candidates:
        normalized_file = _normalize_resolution_path(pack.file)
        if normalized_file in selected_files:
            continue
        selected.append(pack)
        selected_ids.add(id(pack))
        selected_files.add(normalized_file)
        if len(selected) == limit:
            return selected
    for pack in candidates:
        if id(pack) in selected_ids:
            continue
        selected.append(pack)
        if len(selected) == limit:
            break
    return selected


def resolution_diff_warnings_incomplete(delta_report: ReviewReport) -> bool:
    return _build_resolution_warning_evidence(delta_report).incomplete


def _build_resolution_warning_evidence(
    delta_report: ReviewReport,
) -> _ResolutionWarningEvidence:
    total = len(delta_report.diff.warnings)
    sanitized_warnings: list[str] = []
    raw_truncated: list[bool] = []
    for warning in delta_report.diff.warnings[:_RESOLUTION_DIFF_WARNING_LIMIT]:
        was_truncated = len(warning) > _RESOLUTION_DIFF_WARNING_MAX_CHARS
        bounded_warning = f"{warning[: _RESOLUTION_DIFF_WARNING_MAX_CHARS - 3]}..." if was_truncated else warning
        sanitized_warnings.append(
            _sanitize_resolution_warning(
                bounded_warning,
                repo_root=delta_report.project.root,
            )
        )
        raw_truncated.append(was_truncated)
    selected_warnings = [
        (
            warning
            if len(warning) <= _RESOLUTION_DIFF_WARNING_MAX_CHARS
            else f"{warning[: _RESOLUTION_DIFF_WARNING_MAX_CHARS - 3]}..."
        )
        for warning in sanitized_warnings
    ]
    truncated = sum(
        was_raw_truncated or len(warning) > _RESOLUTION_DIFF_WARNING_MAX_CHARS
        for warning, was_raw_truncated in zip(
            sanitized_warnings,
            raw_truncated,
            strict=True,
        )
    )
    omitted = total - len(selected_warnings)
    payload = {
        "total": total,
        "included": len(selected_warnings),
        "omitted": omitted,
        "truncated": truncated,
        "warnings": selected_warnings,
    }
    text, char_truncated = _bounded_resolution_json(
        payload,
        max_chars=_RESOLUTION_DIFF_WARNINGS_MAX_CHARS,
        identity={
            "total": total,
            "included": len(selected_warnings),
            "omitted": omitted,
            "truncated": truncated,
        },
    )
    return _ResolutionWarningEvidence(
        text=text,
        total=total,
        omitted=omitted,
        truncated=truncated,
        char_truncated=char_truncated,
        incomplete=bool(omitted or truncated or char_truncated),
    )


def _sanitize_resolution_warning(warning: str, *, repo_root: str) -> str:
    # Import lazily to keep the LLM package independent from report package
    # initialization while reusing the externally-facing redaction policy.
    from apex_ray.report.sarif import sanitize_external_text

    return sanitize_external_text(warning, repo_root)


def _resolution_pack_identity_tokens(pack_id: str) -> set[str]:
    if "#" not in pack_id:
        return set()
    identity = pack_id.split("#", 1)[1]
    if identity.startswith("cluster:"):
        identity = identity.removeprefix("cluster:")
    tokens: set[str] = set()
    for token in identity.split("+"):
        candidate, separator, suffix = token.rpartition(":")
        normalized = candidate if separator and suffix.isdigit() else token
        if normalized:
            tokens.add(normalized)
    return tokens


def _bounded_resolution_labels(
    values: Iterable[object],
    *,
    max_items: int = 64,
    max_label_chars: int = 512,
) -> list[str]:
    labels = sorted({str(value) for value in values})
    bounded = [
        label if len(label) <= max_label_chars else f"{label[: max_label_chars - 3]}..." for label in labels[:max_items]
    ]
    if len(labels) > max_items:
        bounded.append(f"... {len(labels) - max_items} additional values omitted")
    return bounded


def _bounded_resolution_json(
    payload: object,
    *,
    max_chars: int,
    identity: dict[str, object],
) -> tuple[str, bool]:
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if len(rendered) <= max_chars:
        return rendered, False
    compact = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    bounded_identity = {key: _bounded_resolution_identity_value(value) for key, value in identity.items()}
    truncation = {
        "applied": True,
        "max_chars": max_chars,
        "original_chars": len(rendered),
        "strategy": "deterministic_prefix_suffix_excerpt",
        "truncated": True,
    }

    def render_excerpt(excerpt_chars: int) -> str:
        prefix_chars = excerpt_chars * 3 // 4
        suffix_chars = excerpt_chars - prefix_chars
        return json.dumps(
            {
                "_truncation": truncation,
                "identity": bounded_identity,
                "serialized_prefix": compact[:prefix_chars],
                "serialized_suffix": compact[-suffix_chars:] if suffix_chars else "",
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )

    low = 0
    high = len(compact)
    best = render_excerpt(0)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = render_excerpt(midpoint)
        if len(candidate) <= max_chars:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best, True


def _bounded_resolution_identity_value(value: object) -> object:
    if isinstance(value, str):
        return value if len(value) <= 512 else f"{value[:509]}..."
    if isinstance(value, list):
        return [_bounded_resolution_identity_value(item) for item in value[:16]]
    return value


def _language_review_guidance(pack: ContextPack) -> str:
    language = _pack_language_hint(pack)
    if language == "Python":
        return (
            "Language hint: Python.\n"
            "For Python packs, pay extra attention to FastAPI route/dependency/auth boundaries, "
            "Pydantic model/settings/validator/schema changes, "
            "SQLAlchemy session/transaction/commit/rollback boundaries, Alembic migrations, "
            "async worker/event idempotency and enqueue-after-commit behavior, "
            "external HTTP/cloud/Redis client timeout/retry/lifecycle behavior, "
            "pytest/unittest fixture and override coverage, and dataclass/TypedDict/Protocol/ABC contracts."
        )
    if language == "TypeScript/JavaScript":
        return (
            "Language hint: TypeScript/JavaScript.\n"
            "For TypeScript/JavaScript packs, pay extra attention to NestJS decorators/modules/providers/guards, "
            "DTO/schema validators, route parameter/body contracts, DI/provider registration, "
            "enum/const collection fanout, object/array guard changes, and workspace import/export/member references."
        )
    return (
        f"Language hint: {language}.\n"
        "For fallback or unknown-language packs, prioritize generic boundary, auth, validation, persistence, "
        "serialization, path, shell, cache, and concurrency risks that are directly visible in the supplied context."
    )


def _focused_reviewer_guidance(pack: ContextPack) -> str:
    reviewer = pack.reviewer
    if reviewer is None:
        return ""
    lines = [
        f"Focused reviewer: {reviewer.name or reviewer.id} (`{reviewer.id}`).",
        f"Primary focus: {reviewer.focus or 'Concrete correctness issues within the configured scope.'}",
    ]
    if reviewer.instructions:
        lines.append("Reviewer-specific instructions:")
        lines.extend(f"- {instruction}" for instruction in reviewer.instructions)
    lines.extend(
        [
            "Stay within this focus unless another directly evidenced issue is blocking or critical.",
            "Do not turn the focused pass into a duplicate general review.",
        ]
    )
    return "\n".join(lines) + "\n"


def _language_shallow_review_guidance(pack: ContextPack) -> str:
    language = _pack_language_hint(pack)
    if language == "Python":
        return (
            "Language hint: Python.\n"
            "For Python boundary risks, look for direct evidence of "
            "FastAPI/Pydantic/SQLAlchemy/Alembic, async worker/event, external I/O, "
            "and pytest/unittest fixture changes in the supplied snippets."
        )
    if language == "TypeScript/JavaScript":
        return (
            "Language hint: TypeScript/JavaScript.\n"
            "For TypeScript/JavaScript boundary risks, look for direct evidence of "
            "NestJS/DTO/schema/DI/provider/route/cache changes in the supplied snippets."
        )
    return (
        f"Language hint: {language}.\n"
        "For fallback or unknown-language boundary risks, use only directly visible auth, validation, persistence, "
        "serialization, path, shell, cache, or concurrency evidence."
    )


def _language_verifier_guidance(pack: ContextPack) -> str:
    language = _pack_language_hint(pack)
    if language == "Python":
        return (
            "Language hint: Python.\n"
            "For Python-specific findings, approve only when the failure mode is grounded in supplied Python context "
            "such as FastAPI/Pydantic/SQLAlchemy/Alembic/pytest boundaries, async worker/event behavior, "
            "external I/O, or dataclass/TypedDict/Protocol contracts."
        )
    if language == "TypeScript/JavaScript":
        return (
            "Language hint: TypeScript/JavaScript.\n"
            "For TypeScript/JavaScript-specific findings, approve only when the failure mode is grounded in supplied "
            "TS/JS context such as NestJS/DTO/schema/DI/provider/route/cache boundaries or workspace references."
        )
    return (
        f"Language hint: {language}.\n"
        "For fallback or unknown-language findings, approve only when the generic boundary risk is directly supported "
        "by changed snippets or supplied context layers."
    )


def _pack_language_hint(pack: ContextPack) -> str:
    language = LANGUAGE_EXTENSIONS.get(Path(pack.file).suffix.lower(), "unknown")
    return LANGUAGE_HINTS.get(language, "unknown")
