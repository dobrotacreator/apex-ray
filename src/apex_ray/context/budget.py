import hashlib
import json

from apex_ray.memory import pack_prompt_payload
from apex_ray.models import AnalyzerSymbol, CodeSnippet, ContextConfig, ContextPack, ContextPackStats, MemoryOmission


def finalize_pack(pack: ContextPack, config: ContextConfig) -> ContextPack:
    finalized = pack.model_copy(deep=True)
    notes: list[str] = []

    finalized.changed_snippets = _limit_snippet_count(
        finalized.changed_snippets,
        config.max_changed_snippets,
        "changed snippets",
        notes,
    )
    finalized.reference_snippets = _limit_snippet_count(
        finalized.reference_snippets,
        config.max_reference_snippets,
        "reference snippets",
        notes,
    )
    finalized.callee_snippets = _limit_snippet_count(
        finalized.callee_snippets,
        config.max_reference_snippets,
        "callee snippets",
        notes,
    )
    finalized.contract_snippets = _limit_snippet_count(
        finalized.contract_snippets,
        config.max_reference_snippets,
        "contract snippets",
        notes,
    )
    finalized.metadata_snippets = _limit_snippet_count(
        finalized.metadata_snippets,
        config.max_reference_snippets,
        "metadata snippets",
        notes,
    )
    finalized.related_test_snippets = _limit_snippet_count(
        finalized.related_test_snippets,
        config.max_related_test_snippets,
        "related test snippets",
        notes,
    )

    _enforce_char_budget(finalized, config, notes)
    finalized.stats = _pack_stats(finalized, config, notes)
    return finalized


def estimated_pack_chars(pack: ContextPack) -> int:
    payload = pack_prompt_payload(pack, "review")
    payload.pop("stats", None)
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _limit_snippet_count(
    snippets: list[CodeSnippet],
    max_count: int,
    label: str,
    notes: list[str],
) -> list[CodeSnippet]:
    if len(snippets) <= max_count:
        return snippets
    notes.append(f"trimmed {len(snippets) - max_count} {label} by count")
    return snippets[:max_count]


def _enforce_char_budget(pack: ContextPack, config: ContextConfig, notes: list[str]) -> None:
    estimated_chars = estimated_pack_chars(pack)
    if estimated_chars <= config.max_pack_chars:
        return

    def over_budget() -> bool:
        return estimated_chars > config.max_pack_chars

    def refresh_estimate() -> None:
        nonlocal estimated_chars
        estimated_chars = estimated_pack_chars(pack)

    while pack.related_test_snippets and over_budget():
        if not _truncate_longest_snippet(pack, notes, {"related test snippet"}):
            pack.related_test_snippets.pop()
            notes.append("dropped related test snippet to fit context budget")
        refresh_estimate()

    while pack.reference_snippets and over_budget():
        if not _truncate_longest_snippet(pack, notes, {"reference snippet"}):
            pack.reference_snippets.pop()
            notes.append("dropped reference snippet to fit context budget")
        refresh_estimate()

    while pack.callee_snippets and over_budget():
        if not _truncate_longest_snippet(pack, notes, {"callee snippet"}):
            pack.callee_snippets.pop()
            notes.append("dropped callee snippet to fit context budget")
        refresh_estimate()

    while pack.memory_matches and over_budget():
        dropped = pack.memory_matches.pop()
        pack.memory_omissions.append(
            MemoryOmission(
                id=dropped.id,
                title=dropped.title,
                kind=dropped.kind,
                reason="dropped memory card to fit context budget",
                score=dropped.score,
                source_path=dropped.source_path,
            )
        )
        notes.append("dropped memory card to fit context budget")
        refresh_estimate()

    while over_budget() and _truncate_longest_snippet(
        pack,
        notes,
        {"contract snippet", "metadata snippet"},
    ):
        refresh_estimate()

    while pack.contract_snippets and over_budget():
        pack.contract_snippets.pop()
        notes.append("dropped contract snippet to fit context budget")
        refresh_estimate()

    while pack.metadata_snippets and over_budget():
        pack.metadata_snippets.pop()
        notes.append("dropped metadata snippet to fit context budget")
        refresh_estimate()

    if over_budget() and _compact_analyzer_graph(pack, notes):
        refresh_estimate()

    iterations = 0
    while over_budget() and _truncate_longest_snippet(pack, notes):
        iterations += 1
        refresh_estimate()
        if iterations > 64:
            break

    if over_budget():
        notes.append("pack remains over context budget after snippet truncation")


def _truncate_longest_snippet(
    pack: ContextPack,
    notes: list[str],
    allowed_labels: set[str] | None = None,
) -> bool:
    candidates: list[tuple[int, list[CodeSnippet], int, str]] = []
    snippet_groups = (
        (pack.changed_snippets, "changed snippet"),
        (pack.reference_snippets, "reference snippet"),
        (pack.callee_snippets, "callee snippet"),
        (pack.contract_snippets, "contract snippet"),
        (pack.metadata_snippets, "metadata snippet"),
        (pack.related_test_snippets, "related test snippet"),
    )
    for snippets, label in snippet_groups:
        if allowed_labels is not None and label not in allowed_labels:
            continue
        for index, snippet in enumerate(snippets):
            if len(snippet.code) > 128:
                candidates.append((len(snippet.code), snippets, index, label))

    if not candidates:
        return False

    _, snippets, index, label = max(candidates, key=lambda candidate: (candidate[0], candidate[3], candidate[2]))
    snippet = snippets[index]
    lines = snippet.code.splitlines(keepends=True)
    if len(lines) > 1:
        new_line_count = max(1, len(lines) // 2)
        if label in {"reference snippet", "callee snippet", "contract snippet", "metadata snippet"}:
            start_offset = max(0, (len(lines) - new_line_count) // 2)
            new_start_line = snippet.start_line + start_offset
            kept_lines = lines[start_offset : start_offset + new_line_count]
        else:
            new_start_line = snippet.start_line
            kept_lines = lines[:new_line_count]
        snippets[index] = snippet.model_copy(
            update={
                "start_line": new_start_line,
                "end_line": new_start_line + new_line_count - 1,
                "code": "".join(kept_lines),
            }
        )
    else:
        snippets[index] = snippet.model_copy(update={"code": snippet.code[: max(128, len(snippet.code) // 2)]})
    notes.append(f"truncated longest {label} to fit context budget")
    return True


def _pack_stats(pack: ContextPack, config: ContextConfig, notes: list[str]) -> ContextPackStats:
    return ContextPackStats(
        diff_lines=len(pack.diff_snippet),
        changed_snippet_lines=_snippet_lines(pack.changed_snippets),
        reference_snippet_lines=_snippet_lines(pack.reference_snippets),
        callee_snippet_lines=_snippet_lines(pack.callee_snippets),
        contract_snippet_lines=_snippet_lines(pack.contract_snippets),
        metadata_snippet_lines=_snippet_lines(pack.metadata_snippets),
        related_test_snippet_lines=_snippet_lines(pack.related_test_snippets),
        memory_cards=len(pack.memory_matches),
        memory_chars=sum(match.prompt_chars for match in pack.memory_matches),
        estimated_chars=estimated_pack_chars(pack),
        truncated=bool(notes),
        truncation_notes=notes,
        policy_key=_context_policy_key(config),
    )


def _compact_analyzer_graph(pack: ContextPack, notes: list[str]) -> bool:
    changed = False
    if pack.references:
        pack.references = []
        changed = True
    if pack.callees:
        pack.callees = []
        changed = True
    if pack.contracts:
        pack.contracts = []
        changed = True
    if pack.metadata:
        pack.metadata = []
        changed = True
    if pack.symbol is not None and _symbol_has_graph_context(pack.symbol):
        pack.symbol = _compact_symbol_graph(pack.symbol)
        changed = True
    compacted_symbols = []
    for symbol in pack.symbols:
        if _symbol_has_graph_context(symbol):
            compacted_symbols.append(_compact_symbol_graph(symbol))
            changed = True
        else:
            compacted_symbols.append(symbol)
    if changed:
        pack.symbols = compacted_symbols
        notes.append("compacted over-budget analyzer graph to fit context budget")
    return changed


def _symbol_has_graph_context(symbol: AnalyzerSymbol) -> bool:
    return bool(symbol.references or symbol.callees or symbol.contracts or symbol.metadata)


def _compact_symbol_graph(symbol: AnalyzerSymbol) -> AnalyzerSymbol:
    return symbol.model_copy(
        update={
            "references": [],
            "callees": [],
            "contracts": [],
            "metadata": [],
        }
    )


def _snippet_lines(snippets: list[CodeSnippet]) -> int:
    return sum(len(snippet.code.splitlines()) for snippet in snippets)


def _context_policy_key(config: ContextConfig) -> str:
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]
