from collections import Counter
from collections.abc import Iterable

from apex_ray.models import (
    ContextPack,
    FileKind,
    LLMFileCoverageSummary,
    LLMResidualRiskSummary,
    LLMSliceCoverageSummary,
)


def _coverage_ratio(reviewed_context_packs: int, total_context_packs: int) -> float:
    if total_context_packs == 0:
        return 0.0
    return round(reviewed_context_packs / total_context_packs, 4)


def _build_file_coverage(
    context_packs: list[ContextPack],
    reviewed_ids: set[str],
    over_budget_ids: set[str],
    residual_risks: list[LLMResidualRiskSummary],
) -> list[LLMFileCoverageSummary]:
    residual_by_pack_id = {risk.context_pack_id: risk for risk in residual_risks}
    file_order: list[str] = []
    packs_by_file: dict[str, list[ContextPack]] = {}
    for pack in context_packs:
        if pack.file not in packs_by_file:
            packs_by_file[pack.file] = []
            file_order.append(pack.file)
        packs_by_file[pack.file].append(pack)

    summaries = []
    for file in file_order:
        packs = packs_by_file[file]
        reviewed_pack_ids = [pack.id for pack in packs if pack.id in reviewed_ids]
        unreviewed_pack_ids = [pack.id for pack in packs if pack.id not in reviewed_ids]
        residual_priority = _highest_residual_priority(
            residual_by_pack_id[pack_id].priority for pack_id in unreviewed_pack_ids if pack_id in residual_by_pack_id
        )
        risk_by_severity: Counter[str] = Counter()
        for pack in packs:
            risk_by_severity.update(_pack_risk_by_severity(pack))
        reviewed_packs = [pack for pack in packs if pack.id in reviewed_ids]
        unreviewed_packs = [pack for pack in packs if pack.id not in reviewed_ids]
        reviewed_changed_lines = _merge_line_ranges(range_ for pack in reviewed_packs for range_ in pack.changed_lines)
        unreviewed_changed_lines = _subtract_line_ranges(
            _merge_line_ranges(range_ for pack in unreviewed_packs for range_ in pack.changed_lines),
            reviewed_changed_lines,
        )
        reviewed_changed_symbols = _pack_symbol_names(reviewed_packs)
        reviewed_symbol_names = set(reviewed_changed_symbols)
        unreviewed_changed_symbols = [
            name for name in _pack_symbol_names(unreviewed_packs) if name not in reviewed_symbol_names
        ]
        summaries.append(
            LLMFileCoverageSummary(
                file=file,
                file_kind=packs[0].file_kind,
                total_context_packs=len(packs),
                reviewed_context_packs=len(reviewed_pack_ids),
                unreviewed_context_packs=len(unreviewed_pack_ids),
                cluster_context_packs=sum(1 for pack in packs if _pack_scope(pack) == "cluster"),
                file_context_packs=sum(1 for pack in packs if _pack_scope(pack) == "file"),
                symbol_context_packs=sum(1 for pack in packs if _pack_scope(pack) == "symbol"),
                over_budget_context_packs=sum(1 for pack in packs if pack.id in over_budget_ids),
                truncated_context_packs=sum(1 for pack in packs if pack.stats.truncated),
                risk_by_severity=dict(sorted(risk_by_severity.items())),
                residual_priority=residual_priority,
                reviewed_changed_lines=reviewed_changed_lines,
                unreviewed_changed_lines=unreviewed_changed_lines,
                reviewed_changed_symbols=reviewed_changed_symbols,
                unreviewed_changed_symbols=unreviewed_changed_symbols,
                reviewed_context_pack_ids=reviewed_pack_ids,
                unreviewed_context_pack_ids=unreviewed_pack_ids,
            )
        )
    return summaries


def _build_slice_coverage(
    context_packs: list[ContextPack],
    reviewed_ids: set[str],
    deep_reviewed_ids: set[str],
    shallow_reviewed_ids: set[str],
    residual_risks: list[LLMResidualRiskSummary],
) -> list[LLMSliceCoverageSummary]:
    residual_by_pack_id = {risk.context_pack_id: risk for risk in residual_risks}
    slice_order: list[str] = []
    packs_by_slice: dict[str, list[ContextPack]] = {}
    for pack in context_packs:
        slice_name = _pack_review_slice(pack)
        if slice_name not in packs_by_slice:
            packs_by_slice[slice_name] = []
            slice_order.append(slice_name)
        packs_by_slice[slice_name].append(pack)

    summaries: list[LLMSliceCoverageSummary] = []
    for slice_name in sorted(slice_order, key=_slice_sort_key):
        packs = packs_by_slice[slice_name]
        reviewed_pack_ids = [pack.id for pack in packs if pack.id in reviewed_ids]
        unreviewed_pack_ids = [pack.id for pack in packs if pack.id not in reviewed_ids]
        high_risk_pack_ids = [pack.id for pack in packs if _is_high_risk_pack(pack)]
        residual_priority = _highest_residual_priority(
            residual_by_pack_id[pack_id].priority for pack_id in unreviewed_pack_ids if pack_id in residual_by_pack_id
        )
        summaries.append(
            LLMSliceCoverageSummary(
                slice=slice_name,
                total_context_packs=len(packs),
                reviewed_context_packs=len(reviewed_pack_ids),
                unreviewed_context_packs=len(unreviewed_pack_ids),
                deep_reviewed_context_packs=sum(1 for pack in packs if pack.id in deep_reviewed_ids),
                shallow_reviewed_context_packs=sum(1 for pack in packs if pack.id in shallow_reviewed_ids),
                high_risk_context_packs=len(high_risk_pack_ids),
                reviewed_high_risk_context_packs=sum(1 for pack_id in high_risk_pack_ids if pack_id in reviewed_ids),
                residual_priority=residual_priority,
                reviewed_context_pack_ids=reviewed_pack_ids,
                unreviewed_context_pack_ids=unreviewed_pack_ids,
            )
        )
    return summaries


def _pack_review_slice(pack: ContextPack) -> str:
    if _is_high_risk_pack(pack):
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


def _slice_sort_key(slice_name: str) -> tuple[int, str]:
    order = {
        "high_risk": 0,
        "contracts_config": 1,
        "source": 2,
        "tests": 3,
        "docs": 4,
        "other": 5,
    }
    return (order.get(slice_name, 99), slice_name)


def _source_line_coverage_ratio(file_coverage: list[LLMFileCoverageSummary]) -> float:
    reviewed = 0
    total = 0
    for summary in file_coverage:
        if summary.file_kind != FileKind.SOURCE:
            continue
        reviewed_lines = _line_range_count(summary.reviewed_changed_lines)
        unreviewed_lines = _line_range_count(summary.unreviewed_changed_lines)
        reviewed += reviewed_lines
        total += reviewed_lines + unreviewed_lines
    if total == 0:
        return 1.0
    return _coverage_ratio(reviewed, total)


def _is_high_risk_pack(pack: ContextPack) -> bool:
    if any(str(signal.severity) == "high" for signal in pack.risk_signals):
        return True
    if any(str(rule.mode) == "strict" for rule in pack.rule_matches):
        return True
    return any(str(rule.severity) in {"critical", "high"} for rule in pack.rule_matches)


def _pack_risk_by_severity(pack: ContextPack) -> Counter[str]:
    return Counter(str(signal.severity) for signal in pack.risk_signals)


def _merge_line_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((min(start, end), max(start, end)) for start, end in ranges if start > 0 and end > 0)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end + 1:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _line_range_count(ranges: list[tuple[int, int]]) -> int:
    return sum(max(0, end - start + 1) for start, end in ranges)


def _subtract_line_ranges(
    ranges: list[tuple[int, int]],
    covered_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    remaining: list[tuple[int, int]] = []
    for start, end in ranges:
        fragments = [(start, end)]
        for covered_start, covered_end in covered_ranges:
            next_fragments: list[tuple[int, int]] = []
            for fragment_start, fragment_end in fragments:
                if covered_end < fragment_start or covered_start > fragment_end:
                    next_fragments.append((fragment_start, fragment_end))
                    continue
                if fragment_start < covered_start:
                    next_fragments.append((fragment_start, covered_start - 1))
                if covered_end < fragment_end:
                    next_fragments.append((covered_end + 1, fragment_end))
            fragments = next_fragments
            if not fragments:
                break
        remaining.extend(fragments)
    return _merge_line_ranges(remaining)


def _pack_symbol_names(packs: list[ContextPack]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for pack in packs:
        symbols = pack.symbols or ([pack.symbol] if pack.symbol is not None else [])
        for symbol in symbols:
            if symbol is None or symbol.name in seen:
                continue
            seen.add(symbol.name)
            names.append(symbol.name)
    return names


def _highest_residual_priority(priorities: Iterable[str]) -> str | None:
    priority_order = {"p0": 3, "p1": 2, "p2": 1}
    highest = None
    for priority in priorities:
        if highest is None or priority_order.get(str(priority), 0) > priority_order.get(highest, 0):
            highest = str(priority)
    return highest


def _pack_scope(pack: ContextPack) -> str:
    if "#cluster:" in pack.id or len(pack.symbols) > 1:
        return "cluster"
    if pack.symbol is not None or pack.symbols:
        return "symbol"
    return "file"


def _format_pack_symbols(pack: ContextPack) -> str:
    if pack.symbols:
        names = ", ".join(f"{symbol.kind} `{symbol.name}`" for symbol in pack.symbols)
        return names
    if pack.symbol:
        return f"{pack.symbol.kind} `{pack.symbol.name}`"
    return "file-level context"
