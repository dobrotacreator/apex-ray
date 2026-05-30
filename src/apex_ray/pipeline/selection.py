import json
from typing import Literal

from apex_ray.llm import estimate_review_input_tokens
from apex_ray.memory import pack_prompt_payload
from apex_ray.models import (
    ChangedFile,
    ContextPack,
    FileKind,
    LLMContextSelection,
    LLMCoverageMode,
    LLMSelectionStageSummary,
    ReviewReport,
)


def select_continuation_context_packs(
    report: ReviewReport,
    *,
    residual_priorities: set[str] | None = None,
    slices: set[str] | None = None,
    pack_ids: set[str] | None = None,
    only_unreviewed: bool = True,
) -> list[ContextPack]:
    status_by_id = {status.context_pack_id: status for status in report.llm_coverage.pack_statuses}
    unreviewed_ids = set(report.llm_coverage.unreviewed_context_pack_ids)
    selected: list[ContextPack] = []
    for pack in report.context_packs:
        status = status_by_id.get(pack.id)
        if only_unreviewed and pack.id not in unreviewed_ids:
            continue
        if pack_ids is not None and pack.id not in pack_ids:
            continue
        if residual_priorities is not None:
            priority = status.priority if status is not None else None
            if priority not in residual_priorities:
                continue
        if slices is not None:
            slice_name = status.slice if status is not None else pack_review_slice_for_continuation(pack)
            if slice_name not in slices:
                continue
        selected.append(pack)
    return selected


def merge_continuation_selection(
    existing: LLMContextSelection | None,
    context_packs: list[ContextPack],
    selected_packs: list[ContextPack],
    *,
    review_depth: Literal["deep", "shallow"],
) -> LLMContextSelection:
    selected_ids = [pack.id for pack in selected_packs]
    total_ids = [pack.id for pack in context_packs]
    if existing is None:
        existing = LLMContextSelection(total_context_pack_ids=total_ids)
    selected_set = set(existing.selected_context_pack_ids) | set(selected_ids)
    deep_set = set(existing.deep_selected_context_pack_ids)
    shallow_set = set(existing.shallow_selected_context_pack_ids)
    if review_depth == "deep":
        deep_set.update(selected_ids)
        shallow_set.difference_update(selected_ids)
    else:
        shallow_set.update(selected_ids)
    unselected = [pack_id for pack_id in total_ids if pack_id not in selected_set]
    skipped_reasons = {
        pack_id: reason for pack_id, reason in existing.skipped_context_pack_reasons.items() if pack_id in unselected
    }
    stages = [
        *existing.stages,
        LLMSelectionStageSummary(
            stage=f"continue_{review_depth}",
            selected_context_pack_ids=selected_ids,
            reason="user-requested continuation pass over previously unreviewed context packs",
        ),
    ]
    return LLMContextSelection(
        total_context_pack_ids=total_ids,
        selected_context_pack_ids=[pack_id for pack_id in total_ids if pack_id in selected_set],
        deep_selected_context_pack_ids=[pack_id for pack_id in total_ids if pack_id in deep_set],
        shallow_selected_context_pack_ids=[pack_id for pack_id in total_ids if pack_id in shallow_set],
        unselected_context_pack_ids=unselected,
        over_budget_context_pack_ids=[
            pack_id for pack_id in existing.over_budget_context_pack_ids if pack_id in unselected
        ],
        over_token_budget_context_pack_ids=[
            pack_id for pack_id in existing.over_token_budget_context_pack_ids if pack_id in unselected
        ],
        skipped_context_pack_reasons=skipped_reasons,
        stages=stages,
    )


def pack_review_slice_for_continuation(pack: ContextPack) -> str:
    if any(str(signal.severity) == "high" for signal in pack.risk_signals):
        return "high_risk"
    if any(str(rule.mode) == "strict" for rule in pack.rule_matches):
        return "high_risk"
    if any(str(rule.severity) in {"critical", "high"} for rule in pack.rule_matches):
        return "high_risk"
    if pack.file_kind in {FileKind.SCHEMA, FileKind.CONFIG, FileKind.MIGRATION, FileKind.DEPENDENCY}:
        return "contracts_config"
    if pack.file_kind == FileKind.SOURCE:
        return "source"
    if pack.file_kind == FileKind.TEST:
        return "tests"
    if pack.file_kind == FileKind.DOCS:
        return "docs"
    return "other"


def select_llm_context_packs(
    context_packs: list[ContextPack],
    changed_files: list[ChangedFile],
    max_packs: int | None = None,
) -> list[ContextPack]:
    selected_indexes = _select_llm_context_pack_indexes(
        list(enumerate(context_packs)),
        changed_files,
        max_packs,
    )
    return [context_packs[index] for index in selected_indexes]


def plan_llm_context_selection(
    context_packs: list[ContextPack],
    changed_files: list[ChangedFile],
    *,
    max_packs: int | None = None,
    max_deep_packs: int | None = None,
    max_input_tokens: int | None = None,
    max_pack_chars: int | None = None,
    coverage_mode: LLMCoverageMode | str = LLMCoverageMode.BALANCED,
) -> LLMContextSelection:
    mode = LLMCoverageMode(coverage_mode)
    deep_over_budget_ids = {
        pack.id for pack in context_packs if max_pack_chars is not None and pack.stats.estimated_chars > max_pack_chars
    }
    shallow_over_budget_ids = {
        pack.id
        for pack in context_packs
        if (max_pack_chars is not None and _review_payload_chars(pack, review_depth="shallow") > max_pack_chars)
    }
    effective_over_budget_ids = (
        deep_over_budget_ids if mode == LLMCoverageMode.FAST else deep_over_budget_ids & shallow_over_budget_ids
    )
    deep_reviewable_indexed = [
        (index, pack) for index, pack in enumerate(context_packs) if pack.id not in deep_over_budget_ids
    ]
    shallow_reviewable_indexed = [
        (index, pack) for index, pack in enumerate(context_packs) if pack.id not in shallow_over_budget_ids
    ]
    stages: list[LLMSelectionStageSummary] = []
    token_budget_remaining = max_input_tokens
    deep_cap = max_deep_packs if max_deep_packs is not None else max_packs
    deep_candidate_indexes = _select_llm_context_pack_indexes(
        deep_reviewable_indexed,
        changed_files,
        deep_cap,
    )
    if mode == LLMCoverageMode.EXHAUSTIVE and max_input_tokens is None:
        deep_candidate_indexes = [index for index, _pack in deep_reviewable_indexed]

    deep_selected_indexes, deep_tokens = _select_indexes_with_token_budget(
        deep_candidate_indexes,
        context_packs,
        changed_files,
        review_depth="deep",
        token_budget=token_budget_remaining,
    )
    if token_budget_remaining is not None:
        token_budget_remaining = max(0, token_budget_remaining - deep_tokens)
    deep_selected_ids = [context_packs[index].id for index in sorted(deep_selected_indexes)]
    stages.append(
        LLMSelectionStageSummary(
            stage="deep",
            budget_packs=deep_cap,
            budget_tokens=max_input_tokens,
            selected_estimated_tokens=deep_tokens,
            selected_context_pack_ids=deep_selected_ids,
            unselected_context_pack_ids=[
                context_packs[index].id for index in deep_candidate_indexes if index not in set(deep_selected_indexes)
            ],
            reason="coverage-first deep context selection",
        )
    )

    shallow_selected_indexes: list[int] = []
    shallow_tokens = 0
    if mode != LLMCoverageMode.FAST:
        deep_selected_index_set = set(deep_selected_indexes)
        shallow_candidate_indexes = [
            index for index, _pack in shallow_reviewable_indexed if index not in deep_selected_index_set
        ]
        shallow_selected_indexes, shallow_tokens = _select_indexes_with_token_budget(
            shallow_candidate_indexes,
            context_packs,
            changed_files,
            review_depth="shallow",
            token_budget=token_budget_remaining,
        )
        shallow_selected_ids = [context_packs[index].id for index in sorted(shallow_selected_indexes)]
        stages.append(
            LLMSelectionStageSummary(
                stage="shallow",
                budget_packs=None,
                budget_tokens=token_budget_remaining,
                selected_estimated_tokens=shallow_tokens,
                selected_context_pack_ids=shallow_selected_ids,
                unselected_context_pack_ids=[
                    context_packs[index].id
                    for index in shallow_candidate_indexes
                    if index not in set(shallow_selected_indexes)
                ],
                reason="compact breadth pass for reviewable packs not selected for deep review",
            )
        )

    selected_indexes = sorted(set(deep_selected_indexes) | set(shallow_selected_indexes))
    selected_ids = [context_packs[index].id for index in selected_indexes]
    deep_selected_ids = [context_packs[index].id for index in sorted(deep_selected_indexes)]
    shallow_selected_ids = [context_packs[index].id for index in sorted(shallow_selected_indexes)]
    selected_id_set = set(selected_ids)
    total_ids = [pack.id for pack in context_packs]
    unselected_ids = [pack.id for pack in context_packs if pack.id not in selected_id_set]
    deep_selected_index_set = set(deep_selected_indexes)
    token_budget_index_set = {index for index in deep_candidate_indexes if index not in deep_selected_index_set}
    if mode != LLMCoverageMode.FAST:
        shallow_selected_index_set = set(shallow_selected_indexes)
        token_budget_index_set.update(
            index
            for index, _pack in shallow_reviewable_indexed
            if index not in deep_selected_index_set and index not in shallow_selected_index_set
        )
    token_budget_ids = {
        context_packs[index].id for index in token_budget_index_set if context_packs[index].id not in selected_id_set
    }
    skipped_reasons = {
        pack_id: (
            "over context budget"
            if pack_id in effective_over_budget_ids
            else "not selected by LLM token budget"
            if pack_id in token_budget_ids
            else "not selected by LLM pack cap"
        )
        for pack_id in unselected_ids
    }
    return LLMContextSelection(
        total_context_pack_ids=total_ids,
        selected_context_pack_ids=selected_ids,
        deep_selected_context_pack_ids=deep_selected_ids,
        shallow_selected_context_pack_ids=shallow_selected_ids,
        unselected_context_pack_ids=unselected_ids,
        over_budget_context_pack_ids=[
            pack.id for pack in context_packs if pack.id in effective_over_budget_ids and pack.id not in selected_id_set
        ],
        over_token_budget_context_pack_ids=[pack.id for pack in context_packs if pack.id in token_budget_ids],
        skipped_context_pack_reasons=skipped_reasons,
        stages=stages,
    )


def _review_payload_chars(
    pack: ContextPack,
    *,
    review_depth: Literal["deep", "shallow"],
) -> int:
    payload = pack_prompt_payload(pack, "review", depth=review_depth)
    payload.pop("stats", None)
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _select_llm_context_pack_indexes(
    indexed_context_packs: list[tuple[int, ContextPack]],
    changed_files: list[ChangedFile],
    max_packs: int | None,
) -> list[int]:
    if max_packs is None or len(indexed_context_packs) <= max_packs:
        return [index for index, _pack in indexed_context_packs]
    kind_by_path = {file.path: file.file_kind for file in changed_files}
    indexed = list(indexed_context_packs)
    indexed.sort(
        key=lambda item: _llm_pack_priority(item[1], kind_by_path, item[0]),
        reverse=True,
    )
    groups: dict[str, list[tuple[int, ContextPack]]] = {}
    group_order: list[str] = []
    for index, pack in indexed:
        if pack.file not in groups:
            groups[pack.file] = []
            group_order.append(pack.file)
        groups[pack.file].append((index, pack))

    selected_indexes: list[int] = []
    selected_index_set: set[int] = set()
    selected_files: set[str] = set()

    def select(index: int, pack: ContextPack) -> None:
        if len(selected_indexes) >= max_packs or index in selected_index_set:
            return
        selected_indexes.append(index)
        selected_index_set.add(index)
        selected_files.add(pack.file)

    breadth_budget = _initial_llm_breadth_budget(max_packs, len(group_order))
    for file in group_order:
        if len(selected_indexes) >= breadth_budget:
            break
        group = groups[file]
        if group:
            index, pack = group[0]
            select(index, pack)

    for file in group_order:
        if len(selected_indexes) >= max_packs:
            break
        if file in selected_files:
            continue
        for index, pack in groups[file]:
            if _is_high_value_extra_pack(pack):
                select(index, pack)
                break

    for index, pack in indexed:
        if len(selected_indexes) >= max_packs:
            break
        if index in selected_index_set or not _is_high_value_extra_pack(pack):
            continue
        select(index, pack)

    for file in group_order:
        if len(selected_indexes) >= max_packs:
            break
        group = groups[file]
        if not group or any(index in selected_index_set for index, _pack in group):
            continue
        index, pack = group[0]
        select(index, pack)

    for index, pack in indexed:
        if len(selected_indexes) >= max_packs:
            break
        select(index, pack)

    selected_indexes = sorted(selected_indexes)
    return selected_indexes


def _select_indexes_with_token_budget(
    candidate_indexes: list[int],
    context_packs: list[ContextPack],
    changed_files: list[ChangedFile],
    *,
    review_depth: Literal["deep", "shallow"],
    token_budget: int | None,
) -> tuple[list[int], int]:
    if not candidate_indexes:
        return [], 0
    candidate_set = set(candidate_indexes)
    if token_budget is None:
        tokens = sum(
            estimate_review_input_tokens(context_packs[index], review_depth=review_depth) for index in candidate_indexes
        )
        return list(candidate_indexes), tokens
    if token_budget <= 0:
        return [], 0

    kind_by_path = {file.path: file.file_kind for file in changed_files}
    prioritized = [(index, context_packs[index]) for index in range(len(context_packs)) if index in candidate_set]
    prioritized.sort(
        key=lambda item: _llm_pack_priority(item[1], kind_by_path, item[0]),
        reverse=True,
    )

    selected: list[int] = []
    used_tokens = 0
    for index, pack in prioritized:
        pack_tokens = estimate_review_input_tokens(pack, review_depth=review_depth)
        if used_tokens + pack_tokens > token_budget:
            continue
        selected.append(index)
        used_tokens += pack_tokens
    return selected, used_tokens


def _initial_llm_breadth_budget(max_packs: int, file_count: int) -> int:
    if max_packs <= 2:
        return min(max_packs, file_count)
    return min(file_count, max(2, max_packs // 2))


def _is_high_value_extra_pack(pack: ContextPack) -> bool:
    if any(str(signal.severity) == "high" for signal in pack.risk_signals):
        return True
    if any(str(rule.mode) == "strict" for rule in pack.rule_matches):
        return True
    return any(str(rule.severity) in {"critical", "high"} for rule in pack.rule_matches)


def _llm_pack_priority(
    pack: ContextPack,
    kind_by_path: dict[str, FileKind],
    original_index: int,
) -> tuple[int, int, int, int, int, int]:
    file_kind = kind_by_path.get(pack.file, pack.file_kind)
    high_risk = sum(1 for signal in pack.risk_signals if str(signal.severity) == "high")
    medium_risk = sum(1 for signal in pack.risk_signals if str(signal.severity) == "medium")
    strict_rules = sum(1 for rule in pack.rule_matches if str(rule.mode) == "strict")
    severe_rules = sum(1 for rule in pack.rule_matches if str(rule.severity) in {"critical", "high"})
    risk_score = high_risk * 1000 + strict_rules * 800 + severe_rules * 500 + medium_risk * 100
    review_tier = 1 if file_kind == FileKind.TEST else 2
    kind_score = {
        FileKind.SOURCE: 5,
        FileKind.SCHEMA: 4,
        FileKind.MIGRATION: 4,
        FileKind.CONFIG: 3,
        FileKind.UNKNOWN: 2,
        FileKind.TEST: 1,
    }.get(file_kind, 2)
    return (
        review_tier,
        risk_score,
        kind_score,
        min(pack.stats.estimated_chars, 100_000),
        len(pack.references) + len(pack.contracts) + len(pack.metadata),
        -original_index,
    )
