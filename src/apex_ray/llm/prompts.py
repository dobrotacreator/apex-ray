import json

from apex_ray.memory import pack_prompt_payload
from apex_ray.models import ContextPack, Finding, ReviewReport


def build_review_prompt(pack: ContextPack) -> str:
    payload = pack_prompt_payload(pack, "review", depth="deep")
    return (
        "You are Apex Ray, a strict senior code reviewer.\n"
        "Review exactly one context pack from a TypeScript/JavaScript diff.\n"
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
    payload = pack_prompt_payload(pack, "review", depth="shallow")
    return (
        "You are Apex Ray's fast shallow code-review pass.\n"
        "Review exactly one compact TypeScript/JavaScript context pack from a diff.\n"
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
    delta_pack_payloads = [pack_prompt_payload(pack, "verify") for pack in delta_report.context_packs]
    delta_payload = {
        "diff": delta_report.diff.model_dump(mode="json"),
        "context_packs": delta_pack_payloads,
    }
    return (
        "You are Apex Ray's strict pre-push retry resolution pass.\n"
        "Decide whether a previously verified blocking code-review finding is resolved in the current snapshot.\n"
        "Return status `resolved` only when the supplied delta and current context prove that the failure mode no longer applies.\n"
        "Return `still_present` when the same failure mode remains visible or the delta leaves the relevant code unchanged.\n"
        "Return `uncertain` when the supplied context is insufficient, ambiguous, or the fix may be elsewhere.\n"
        "Do not mark resolved merely because the new delta review produced no findings.\n"
        "Treat previous_context_pack as historical evidence for what was blocked, and delta_report as the only new evidence.\n"
        "Prefer `uncertain` over `resolved` when proof is incomplete. `still_present` and `uncertain` both continue to block the gate.\n"
        "Return only JSON that matches the provided schema.\n\n"
        "Previous blocking finding JSON:\n"
        f"{json.dumps(finding.model_dump(mode='json'), indent=2)}\n\n"
        "Previous context pack JSON:\n"
        f"{json.dumps(previous_payload, indent=2)}\n\n"
        "Delta report JSON:\n"
        f"{json.dumps(delta_payload, indent=2)}\n"
    )
