import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from apex_ray.memory import pack_prompt_payload, select_memory_for_pack
from apex_ray.models import (
    AnalyzerFile,
    AnalyzerReference,
    AnalyzerResult,
    AnalyzerSymbol,
    ChangedFile,
    CodeSnippet,
    ContextConfig,
    ContextPack,
    ContextPackStats,
    FileKind,
    MemoryOmission,
    ReviewConfig,
    RiskSignal,
)
from apex_ray.rules import match_rules_for_pack, render_rule_for_prompt, rule_match_for_prompt

FILE_CLUSTER_MAX_SYMBOLS = 24
LARGE_FILE_CLUSTER_CHUNK_SIZE = 12
SMALL_FILE_CLUSTER_MAX_DIFF_LINES = 160
TEST_ANCHOR_MIN_LENGTH = 4
TEST_ANCHOR_LIMIT = 32


def build_context_packs(
    analyzer_results: list[AnalyzerResult],
    changed_files: list[ChangedFile],
    config: ReviewConfig,
    repo_root: Path | None = None,
    fallback_reasons_by_path: dict[str, str] | None = None,
) -> list[ContextPack]:
    fallback_reasons_by_path = fallback_reasons_by_path or {}
    changed_by_path = {file.path: file for file in changed_files}
    packed_paths: set[str] = set()
    packs: list[ContextPack] = []

    for result in analyzer_results:
        for analyzed_file in result.files:
            changed_file = changed_by_path.get(analyzed_file.path)
            if not changed_file:
                continue
            packs.extend(_packs_for_file(result, analyzed_file, changed_file, config, repo_root))
            packed_paths.add(changed_file.path)

    for changed_file in changed_files:
        if changed_file.path in packed_paths or not _should_build_fallback_pack(changed_file):
            continue
        packs.append(
            _fallback_pack_for_file(changed_file, config, repo_root, fallback_reasons_by_path.get(changed_file.path))
        )

    return packs


def _should_build_fallback_pack(changed_file: ChangedFile) -> bool:
    if changed_file.is_ignored:
        return False
    return changed_file.file_kind in {
        FileKind.SOURCE,
        FileKind.TEST,
        FileKind.CONFIG,
        FileKind.MIGRATION,
        FileKind.SCHEMA,
        FileKind.DEPENDENCY,
        FileKind.UNKNOWN,
    } or bool(changed_file.risk_signals)


def _fallback_pack_for_file(
    changed_file: ChangedFile,
    config: ReviewConfig,
    repo_root: Path | None,
    fallback_reason: str | None = None,
) -> ContextPack:
    related_test_snippets: list[CodeSnippet] = []
    changed_snippets = _changed_hunk_snippets(repo_root, changed_file, config.context)
    risk_signals = _risk_signals_for_ranges(changed_file, [], include_file_level=True)
    return _finalize_review_pack(
        ContextPack(
            id=f"{changed_file.path}#diff",
            file=changed_file.path,
            file_kind=changed_file.file_kind,
            changed_lines=[
                (hunk.new_start, hunk.new_start + max(hunk.new_lines - 1, 0)) for hunk in changed_file.hunks
            ],
            impact_notes=_impact_notes(
                [],
                [],
                [],
                [],
                [],
                [],
                risk_signals,
                [],
                [],
                [],
                [],
                related_test_snippets,
            ),
            diff_snippet=_diff_snippet(changed_file),
            changed_snippets=changed_snippets,
            symbol=None,
            symbols=[],
            imports=[],
            exports=[],
            related_tests=[],
            references=[],
            callees=[],
            contracts=[],
            metadata=[],
            reference_snippets=[],
            callee_snippets=[],
            contract_snippets=[],
            metadata_snippets=[],
            related_test_snippets=related_test_snippets,
            risk_signals=risk_signals,
            rules=config.rules,
            warnings=([fallback_reason] if fallback_reason else []),
        ),
        config,
    )


def _packs_for_file(
    result: AnalyzerResult,
    analyzed_file: AnalyzerFile,
    changed_file: ChangedFile,
    config: ReviewConfig,
    repo_root: Path | None,
) -> list[ContextPack]:
    changed_lines = [(hunk.new_start, hunk.new_start + max(hunk.new_lines - 1, 0)) for hunk in changed_file.hunks]
    diff_snippet = _diff_snippet(changed_file)
    packs: list[ContextPack] = []

    symbols = _non_redundant_changed_symbols(analyzed_file.changed_symbols or [])
    test_anchors = _test_anchor_terms(changed_file, symbols)
    if symbols:
        if _should_cluster_file(symbols, diff_snippet):
            primary = _primary_symbol(symbols)
            pack_changed_lines = _changed_lines_for_symbols(changed_file, symbols)
            risk_signals = _risk_signals_for_symbols(changed_file, symbols, include_file_level=True)
            references = _references_for_symbols(symbols)
            callees = _callees_for_symbols(symbols)
            contracts = _contracts_for_symbols(symbols)
            metadata = _metadata_for_symbols(symbols)
            reference_snippets = _reference_snippets(
                repo_root,
                references,
                config.context,
                excluded_files=set(analyzed_file.related_tests),
            )
            callee_snippets = _callee_snippets(repo_root, callees, config.context)
            contract_snippets = _contract_snippets(repo_root, contracts, config.context)
            metadata_snippets = _metadata_snippets(repo_root, metadata, config.context)
            related_test_snippets = _test_snippets(repo_root, analyzed_file.related_tests, config.context, test_anchors)
            packs.append(
                _symbol_pack(
                    analyzed_file=analyzed_file,
                    changed_file=changed_file,
                    symbols=symbols,
                    changed_lines=pack_changed_lines,
                    risk_signals=risk_signals,
                    diff_snippet=_diff_snippet_for_symbols(changed_file, symbols) or diff_snippet,
                    references=references,
                    callees=callees,
                    contracts=contracts,
                    metadata=metadata,
                    reference_snippets=reference_snippets,
                    callee_snippets=callee_snippets,
                    contract_snippets=contract_snippets,
                    metadata_snippets=metadata_snippets,
                    related_test_snippets=related_test_snippets,
                    primary=primary,
                    config=config,
                    result=result,
                    repo_root=repo_root,
                )
            )
            return packs

        if len(symbols) > FILE_CLUSTER_MAX_SYMBOLS:
            for chunk_index, chunk in enumerate(_symbol_chunks(symbols, LARGE_FILE_CLUSTER_CHUNK_SIZE)):
                primary = _primary_symbol(chunk)
                pack_changed_lines = _changed_lines_for_symbols(changed_file, chunk)
                risk_signals = _risk_signals_for_symbols(
                    changed_file,
                    chunk,
                    include_file_level=chunk_index == 0,
                )
                references = _references_for_symbols(chunk)
                callees = _callees_for_symbols(chunk)
                contracts = _contracts_for_symbols(chunk)
                metadata = _metadata_for_symbols(chunk)
                reference_snippets = _reference_snippets(
                    repo_root,
                    references,
                    config.context,
                    excluded_files=set(analyzed_file.related_tests),
                )
                callee_snippets = _callee_snippets(repo_root, callees, config.context)
                contract_snippets = _contract_snippets(repo_root, contracts, config.context)
                metadata_snippets = _metadata_snippets(repo_root, metadata, config.context)
                related_test_snippets = _test_snippets(
                    repo_root, analyzed_file.related_tests, config.context, test_anchors
                )
                packs.append(
                    _symbol_pack(
                        analyzed_file=analyzed_file,
                        changed_file=changed_file,
                        symbols=chunk,
                        changed_lines=pack_changed_lines,
                        risk_signals=risk_signals,
                        diff_snippet=_diff_snippet_for_symbols(changed_file, chunk) or diff_snippet,
                        references=references,
                        callees=callees,
                        contracts=contracts,
                        metadata=metadata,
                        reference_snippets=reference_snippets,
                        callee_snippets=callee_snippets,
                        contract_snippets=contract_snippets,
                        metadata_snippets=metadata_snippets,
                        related_test_snippets=related_test_snippets,
                        primary=primary,
                        config=config,
                        result=result,
                        repo_root=repo_root,
                    )
                )
            return packs

        for index, symbol in enumerate(symbols, start=1):
            pack_changed_lines = _changed_lines_for_symbols(changed_file, [symbol])
            risk_signals = _risk_signals_for_symbols(
                changed_file,
                [symbol],
                include_file_level=index == 1,
            )
            references = symbol.references
            callees = symbol.callees
            contracts = symbol.contracts
            metadata = symbol.metadata
            reference_snippets = _reference_snippets(
                repo_root,
                references,
                config.context,
                excluded_files=set(analyzed_file.related_tests),
            )
            callee_snippets = _callee_snippets(repo_root, callees, config.context)
            contract_snippets = _contract_snippets(repo_root, contracts, config.context)
            metadata_snippets = _metadata_snippets(repo_root, metadata, config.context)
            related_test_snippets = _test_snippets(repo_root, analyzed_file.related_tests, config.context, test_anchors)
            packs.append(
                _finalize_review_pack(
                    ContextPack(
                        id=f"{analyzed_file.path}#{symbol.name}:{index}",
                        file=analyzed_file.path,
                        file_kind=changed_file.file_kind,
                        changed_lines=pack_changed_lines,
                        impact_notes=_impact_notes(
                            [symbol],
                            references,
                            callees,
                            contracts,
                            metadata,
                            analyzed_file.related_tests,
                            risk_signals,
                            reference_snippets,
                            callee_snippets,
                            contract_snippets,
                            metadata_snippets,
                            related_test_snippets,
                        ),
                        diff_snippet=_diff_snippet_for_symbols(changed_file, [symbol]) or diff_snippet,
                        changed_snippets=_changed_symbol_snippets(
                            repo_root, analyzed_file.path, [symbol], config.context
                        ),
                        symbol=symbol,
                        symbols=[symbol],
                        imports=analyzed_file.imports,
                        exports=analyzed_file.exports,
                        related_tests=analyzed_file.related_tests,
                        references=references,
                        callees=callees,
                        contracts=contracts,
                        metadata=metadata,
                        reference_snippets=reference_snippets,
                        callee_snippets=callee_snippets,
                        contract_snippets=contract_snippets,
                        metadata_snippets=metadata_snippets,
                        related_test_snippets=related_test_snippets,
                        risk_signals=risk_signals,
                        rules=config.rules,
                        warnings=result.warnings,
                    ),
                    config,
                )
            )
        return packs

    related_test_snippets = _test_snippets(repo_root, analyzed_file.related_tests, config.context, test_anchors)
    risk_signals = _risk_signals_for_ranges(changed_file, changed_lines, include_file_level=True)
    packs.append(
        _finalize_review_pack(
            ContextPack(
                id=f"{analyzed_file.path}#file",
                file=analyzed_file.path,
                file_kind=changed_file.file_kind,
                changed_lines=changed_lines,
                impact_notes=_impact_notes(
                    [],
                    [],
                    [],
                    [],
                    [],
                    analyzed_file.related_tests,
                    risk_signals,
                    [],
                    [],
                    [],
                    [],
                    related_test_snippets,
                ),
                diff_snippet=diff_snippet,
                changed_snippets=_changed_hunk_snippets(repo_root, changed_file, config.context),
                symbol=None,
                imports=analyzed_file.imports,
                exports=analyzed_file.exports,
                related_tests=analyzed_file.related_tests,
                references=[],
                callees=[],
                contracts=[],
                metadata=[],
                reference_snippets=[],
                callee_snippets=[],
                contract_snippets=[],
                metadata_snippets=[],
                related_test_snippets=related_test_snippets,
                risk_signals=risk_signals,
                rules=config.rules,
                warnings=result.warnings,
            ),
            config,
        )
    )
    return packs


def _symbol_pack(
    *,
    analyzed_file: AnalyzerFile,
    changed_file: ChangedFile,
    symbols: list[AnalyzerSymbol],
    changed_lines: list[tuple[int, int]],
    risk_signals: list[RiskSignal],
    diff_snippet: list[str],
    references: list[AnalyzerReference],
    callees: list[AnalyzerReference],
    contracts: list[AnalyzerReference],
    metadata: list[AnalyzerReference],
    reference_snippets: list[CodeSnippet],
    callee_snippets: list[CodeSnippet],
    contract_snippets: list[CodeSnippet],
    metadata_snippets: list[CodeSnippet],
    related_test_snippets: list[CodeSnippet],
    primary: AnalyzerSymbol,
    config: ReviewConfig,
    result: AnalyzerResult,
    repo_root: Path | None,
) -> ContextPack:
    return _finalize_review_pack(
        ContextPack(
            id=_cluster_pack_id(analyzed_file.path, symbols),
            file=analyzed_file.path,
            file_kind=changed_file.file_kind,
            changed_lines=changed_lines,
            impact_notes=_impact_notes(
                symbols,
                references,
                callees,
                contracts,
                metadata,
                analyzed_file.related_tests,
                risk_signals,
                reference_snippets,
                callee_snippets,
                contract_snippets,
                metadata_snippets,
                related_test_snippets,
            ),
            diff_snippet=diff_snippet,
            changed_snippets=_changed_symbol_snippets(repo_root, analyzed_file.path, symbols, config.context),
            symbol=primary,
            symbols=symbols,
            imports=analyzed_file.imports,
            exports=analyzed_file.exports,
            related_tests=analyzed_file.related_tests,
            references=references,
            callees=callees,
            contracts=contracts,
            metadata=metadata,
            reference_snippets=reference_snippets,
            callee_snippets=callee_snippets,
            contract_snippets=contract_snippets,
            metadata_snippets=metadata_snippets,
            related_test_snippets=related_test_snippets,
            risk_signals=risk_signals,
            rules=config.rules,
            warnings=result.warnings,
        ),
        config,
    )


def _symbol_chunks(symbols: list[AnalyzerSymbol], size: int) -> list[list[AnalyzerSymbol]]:
    ordered = sorted(symbols, key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name))
    return [ordered[index : index + size] for index in range(0, len(ordered), size)]


def _should_cluster_file(symbols: list[AnalyzerSymbol], diff_snippet: list[str]) -> bool:
    return (
        len(symbols) > 1
        and len(symbols) <= FILE_CLUSTER_MAX_SYMBOLS
        and len(diff_snippet) <= SMALL_FILE_CLUSTER_MAX_DIFF_LINES
    )


def _non_redundant_changed_symbols(symbols: list[AnalyzerSymbol]) -> list[AnalyzerSymbol]:
    return [
        symbol
        for symbol in symbols
        if not (
            symbol.kind in {"class", "interface"}
            and not symbol.metadata
            and not symbol.contracts
            and not symbol.callees
            and any(_symbol_strictly_contains(symbol, other) for other in symbols if other is not symbol)
        )
    ]


def _symbol_strictly_contains(parent: AnalyzerSymbol, child: AnalyzerSymbol) -> bool:
    return (
        parent.start_line <= child.start_line
        and parent.end_line >= child.end_line
        and (parent.start_line, parent.end_line) != (child.start_line, child.end_line)
    )


def _primary_symbol(symbols: list[AnalyzerSymbol]) -> AnalyzerSymbol:
    return sorted(symbols, key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name))[0]


def _risk_signals_for_symbols(
    changed_file: ChangedFile,
    symbols: list[AnalyzerSymbol],
    *,
    include_file_level: bool = False,
) -> list[RiskSignal]:
    ranges = [(symbol.start_line, symbol.end_line) for symbol in symbols]
    include_file_level = include_file_level or changed_file.file_kind in {
        FileKind.CONFIG,
        FileKind.DEPENDENCY,
        FileKind.MIGRATION,
        FileKind.SCHEMA,
    }
    return _risk_signals_for_ranges(changed_file, ranges, include_file_level=include_file_level)


def _changed_lines_for_symbols(changed_file: ChangedFile, symbols: list[AnalyzerSymbol]) -> list[tuple[int, int]]:
    symbol_ranges = [(symbol.start_line, symbol.end_line) for symbol in symbols]
    hunk_ranges = [(hunk.new_start, hunk.new_start + max(hunk.new_lines - 1, 0)) for hunk in changed_file.hunks]
    changed_ranges: list[tuple[int, int]] = []
    for hunk_start, hunk_end in hunk_ranges:
        for symbol_start, symbol_end in symbol_ranges:
            if not _ranges_overlap(hunk_start, hunk_end, symbol_start, symbol_end):
                continue
            changed_ranges.append((max(hunk_start, symbol_start), min(hunk_end, symbol_end)))
    return _merge_line_ranges(changed_ranges) or hunk_ranges


def _merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
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


def _risk_signals_for_ranges(
    changed_file: ChangedFile,
    ranges: list[tuple[int, int]],
    *,
    include_file_level: bool,
) -> list[RiskSignal]:
    if not ranges:
        return list(changed_file.risk_signals)

    localized: list[RiskSignal] = []
    for signal in changed_file.risk_signals:
        if signal.line is None:
            if include_file_level:
                localized.append(signal)
            continue
        if any(start <= signal.line <= end for start, end in ranges):
            localized.append(signal)
    return localized


def _cluster_pack_id(path: str, symbols: list[AnalyzerSymbol]) -> str:
    names = "+".join(symbol.name for symbol in sorted(symbols, key=lambda symbol: symbol.start_line))
    return f"{path}#cluster:{names}"


def _references_for_symbols(symbols: list[AnalyzerSymbol]) -> list[AnalyzerReference]:
    seen: set[tuple[str, int, str]] = set()
    references: list[AnalyzerReference] = []
    for symbol in symbols:
        for reference in symbol.references:
            key = (reference.file, reference.line, reference.text)
            if key in seen:
                continue
            seen.add(key)
            references.append(reference)
    return references


def _callees_for_symbols(symbols: list[AnalyzerSymbol]) -> list[AnalyzerReference]:
    seen: set[tuple[str, int, str]] = set()
    callees: list[AnalyzerReference] = []
    for symbol in symbols:
        for callee in symbol.callees:
            key = (callee.file, callee.line, callee.text)
            if key in seen:
                continue
            seen.add(key)
            callees.append(callee)
    return callees


def _contracts_for_symbols(symbols: list[AnalyzerSymbol]) -> list[AnalyzerReference]:
    seen: set[tuple[str, int, str]] = set()
    contracts: list[AnalyzerReference] = []
    for symbol in symbols:
        for reference in symbol.contracts:
            key = (reference.file, reference.line, reference.text)
            if key in seen:
                continue
            seen.add(key)
            contracts.append(reference)
    return contracts


def _metadata_for_symbols(symbols: list[AnalyzerSymbol]) -> list[AnalyzerReference]:
    seen: set[tuple[str, int, str]] = set()
    metadata: list[AnalyzerReference] = []
    for symbol in symbols:
        for reference in symbol.metadata:
            key = (reference.file, reference.line, reference.text)
            if key in seen:
                continue
            seen.add(key)
            metadata.append(reference)
    return metadata


def _impact_notes(
    symbols: list[AnalyzerSymbol],
    references: list[AnalyzerReference],
    callees: list[AnalyzerReference],
    contracts: list[AnalyzerReference],
    metadata: list[AnalyzerReference],
    related_tests: list[str],
    risk_signals: list[RiskSignal],
    reference_snippets: list[CodeSnippet],
    callee_snippets: list[CodeSnippet],
    contract_snippets: list[CodeSnippet],
    metadata_snippets: list[CodeSnippet],
    related_test_snippets: list[CodeSnippet],
) -> list[str]:
    notes: list[str] = []
    if symbols:
        notes.append(f"Changed symbols: {_format_symbol_list(symbols)}.")
    else:
        notes.append("Changed scope: file-level change; no changed symbol was identified by the analyzer.")

    non_import_references = [reference for reference in references if not _is_import_reference(reference)]
    if references:
        kind_counts = _reference_kind_counts(references)
        usage_files = sorted({reference.file for reference in non_import_references})
        usage_summary = (
            f"{len(non_import_references)} non-import usage references across {len(usage_files)} files"
            if non_import_references
            else "no non-import usage references"
        )
        notes.append(
            "Reference impact: "
            f"{len(references)} total references ({_format_counts(kind_counts)}); "
            f"{usage_summary}; {len(reference_snippets)} usage snippets included."
        )
    else:
        notes.append("Reference impact: no references were found for the changed scope.")

    if callees:
        callee_files = sorted({callee.file for callee in callees})
        notes.append(
            "Callee contracts: "
            f"{len(callees)} called definitions across {len(callee_files)} files; "
            f"{len(callee_snippets)} snippets included."
        )

    if contracts:
        contract_files = sorted({contract.file for contract in contracts})
        notes.append(
            "Contract context: "
            f"{len(contracts)} schema/type contracts across {len(contract_files)} files; "
            f"{len(contract_snippets)} snippets included."
        )

    if metadata:
        notes.append(
            f"Framework metadata: {len(metadata)} decorator references; {len(metadata_snippets)} snippets included."
        )

    if related_tests:
        preview = ", ".join(related_tests[:3])
        suffix = f" and {len(related_tests) - 3} more" if len(related_tests) > 3 else ""
        notes.append(
            f"Related tests: {len(related_tests)} candidate files; "
            f"{len(related_test_snippets)} snippets included: {preview}{suffix}."
        )
    else:
        notes.append("Related tests: none found by import or naming heuristics.")

    if risk_signals:
        counts = _risk_signal_counts(risk_signals)
        notes.append(f"Static risk signals: {_format_counts(counts)}.")

    return notes


def _format_symbol_list(symbols: list[AnalyzerSymbol]) -> str:
    descriptors = []
    for symbol in symbols:
        export_marker = "exported " if symbol.exported else ""
        descriptors.append(f"{export_marker}{symbol.kind} {symbol.name} lines {symbol.start_line}-{symbol.end_line}")
    return "; ".join(descriptors)


def _reference_kind_counts(references: list[AnalyzerReference]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reference in references:
        counts[reference.kind] = counts.get(reference.kind, 0) + 1
    return counts


def _risk_signal_counts(risk_signals: list[RiskSignal]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for signal in risk_signals:
        severity = str(signal.severity)
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}={counts[key]}" for key in sorted(counts)) or "none"


def _diff_snippet(file: ChangedFile, ranges: list[tuple[int, int]] | None = None) -> list[str]:
    lines: list[str] = []
    for hunk in file.hunks:
        if ranges and not any(
            _ranges_overlap(hunk.new_start, hunk.new_start + max(hunk.new_lines - 1, 0), start, end)
            for start, end in ranges
        ):
            continue
        header = f"@@ -{hunk.old_start},{hunk.old_lines} +{hunk.new_start},{hunk.new_lines} @@"
        if hunk.section_header:
            header = f"{header} {hunk.section_header}"
        lines.append(header)
        for line in hunk.lines:
            prefix = {"context": " ", "add": "+", "delete": "-"}[line.kind]
            lines.append(f"{prefix}{line.content}")
    return lines


def _diff_snippet_for_symbols(file: ChangedFile, symbols: list[AnalyzerSymbol]) -> list[str]:
    return _diff_snippet(file, [(symbol.start_line, symbol.end_line) for symbol in symbols])


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def _test_anchor_terms(changed_file: ChangedFile, symbols: list[AnalyzerSymbol]) -> list[str]:
    terms: list[str] = []
    for hunk in changed_file.hunks:
        for line in hunk.lines:
            if line.kind not in {"add", "delete"}:
                continue
            terms.extend(_quoted_terms(line.content))
    for symbol in symbols:
        terms.append(symbol.name)
        if ":" in symbol.name:
            terms.append(symbol.name.rsplit(":", 1)[-1])

    seen: set[str] = set()
    unique_terms: list[str] = []
    for term in terms:
        normalized = term.strip()
        if len(normalized) < TEST_ANCHOR_MIN_LENGTH or normalized in seen:
            continue
        seen.add(normalized)
        unique_terms.append(normalized)
        if len(unique_terms) >= TEST_ANCHOR_LIMIT:
            break
    return unique_terms


def _quoted_terms(text: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r"""["'`]([^"'`]{4,160})["'`]""", text)]


def _changed_symbol_snippets(
    repo_root: Path | None,
    file_path: str,
    symbols: list[AnalyzerSymbol],
    config: ContextConfig,
) -> list[CodeSnippet]:
    if repo_root is None:
        return []

    snippets: list[CodeSnippet] = []
    seen_ranges: set[tuple[str, int, int]] = set()
    sorted_symbols = sorted(symbols, key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name))
    for symbol in sorted_symbols:
        snippet = _snippet_for_line_range(
            repo_root,
            file_path,
            symbol.start_line,
            symbol.end_line,
            max_lines=config.max_changed_snippet_lines,
        )
        if not snippet:
            continue
        range_key = (snippet.file, snippet.start_line, snippet.end_line)
        if range_key in seen_ranges:
            continue
        seen_ranges.add(range_key)
        snippets.append(snippet)
        if len(snippets) >= config.max_changed_snippets:
            break
    return snippets


def _changed_hunk_snippets(
    repo_root: Path | None,
    changed_file: ChangedFile,
    config: ContextConfig,
) -> list[CodeSnippet]:
    if repo_root is None:
        return []

    snippets: list[CodeSnippet] = []
    seen_ranges: set[tuple[str, int, int]] = set()
    for hunk in changed_file.hunks:
        start_line = max(1, hunk.new_start - config.hunk_context_lines)
        hunk_end = hunk.new_start + max(hunk.new_lines - 1, 0)
        end_line = hunk_end + config.hunk_context_lines
        snippet = _snippet_for_line_range(
            repo_root,
            changed_file.path,
            start_line,
            end_line,
            max_lines=config.max_changed_snippet_lines,
        )
        if not snippet:
            continue
        range_key = (snippet.file, snippet.start_line, snippet.end_line)
        if range_key in seen_ranges:
            continue
        seen_ranges.add(range_key)
        snippets.append(snippet)
        if len(snippets) >= config.max_hunk_snippets:
            break
    return snippets


def _reference_snippets(
    repo_root: Path | None,
    references: list[AnalyzerReference],
    config: ContextConfig,
    excluded_files: set[str] | None = None,
) -> list[CodeSnippet]:
    if repo_root is None:
        return []
    excluded_files = excluded_files or set()
    snippets: list[CodeSnippet] = []
    seen: set[tuple[str, int]] = set()
    seen_ranges: set[tuple[str, int, int]] = set()
    for reference in references:
        if reference.file in excluded_files or _is_import_reference(reference):
            continue
        key = (reference.file, reference.line)
        if key in seen:
            continue
        seen.add(key)
        start_line = _leading_comment_start(repo_root, reference.file, reference.line)
        if reference.end_line and reference.end_line > reference.line:
            snippet = _snippet_for_line_range(
                repo_root,
                reference.file,
                start_line,
                reference.end_line,
                max_lines=config.max_changed_snippet_lines,
            )
        elif start_line < reference.line:
            snippet = _snippet_for_line_range(
                repo_root,
                reference.file,
                start_line,
                reference.line,
                max_lines=config.max_changed_snippet_lines,
            )
        else:
            snippet = _snippet_around_line(
                repo_root, reference.file, reference.line, config.reference_snippet_context_lines
            )
        if snippet:
            range_key = (snippet.file, snippet.start_line, snippet.end_line)
            if range_key in seen_ranges:
                continue
            seen_ranges.add(range_key)
            snippets.append(snippet)
        if len(snippets) >= config.max_reference_snippets:
            break
    return snippets


def _callee_snippets(
    repo_root: Path | None,
    callees: list[AnalyzerReference],
    config: ContextConfig,
) -> list[CodeSnippet]:
    return _reference_snippets(repo_root, callees, config)


def _contract_snippets(
    repo_root: Path | None,
    contracts: list[AnalyzerReference],
    config: ContextConfig,
) -> list[CodeSnippet]:
    if repo_root is None:
        return []
    snippets: list[CodeSnippet] = []
    seen: set[tuple[str, int]] = set()
    seen_ranges: set[tuple[str, int, int]] = set()
    for contract in contracts:
        key = (contract.file, contract.line)
        if key in seen:
            continue
        seen.add(key)
        if contract.end_line and contract.end_line > contract.line:
            snippet = _snippet_for_line_range(
                repo_root,
                contract.file,
                contract.line,
                contract.end_line,
                max_lines=config.max_changed_snippet_lines,
            )
        else:
            snippet = _snippet_around_line(
                repo_root, contract.file, contract.line, config.reference_snippet_context_lines
            )
        if snippet:
            range_key = (snippet.file, snippet.start_line, snippet.end_line)
            if range_key in seen_ranges:
                continue
            seen_ranges.add(range_key)
            snippets.append(snippet)
        if len(snippets) >= config.max_reference_snippets:
            break
    return snippets


def _metadata_snippets(
    repo_root: Path | None,
    metadata: list[AnalyzerReference],
    config: ContextConfig,
) -> list[CodeSnippet]:
    return _reference_snippets(repo_root, metadata, config)


def _is_import_reference(reference: AnalyzerReference) -> bool:
    return reference.kind == "import" or reference.text.lstrip().startswith("import ")


def _leading_comment_start(repo_root: Path, rel_path: str, line: int) -> int:
    lines = _read_lines(repo_root, rel_path)
    if not lines or line <= 1:
        return line
    index = line - 2
    stripped = lines[index].strip()
    if stripped.endswith("*/"):
        while index >= 0:
            current = lines[index].strip()
            if current.startswith("/*"):
                return index + 1
            index -= 1
    if stripped.startswith("//"):
        while index >= 0 and lines[index].strip().startswith("//"):
            index -= 1
        return index + 2
    return line


def _test_snippets(
    repo_root: Path | None,
    test_paths: list[str],
    config: ContextConfig,
    anchors: list[str] | None = None,
) -> list[CodeSnippet]:
    if repo_root is None:
        return []
    snippets: list[CodeSnippet] = []
    for test_path in test_paths[: config.max_related_test_snippets]:
        snippet = _anchored_test_snippet(repo_root, test_path, anchors or [], config.max_related_test_snippet_lines)
        if snippet is None:
            snippet = _snippet_for_file_start(repo_root, test_path, config.max_related_test_snippet_lines)
        if snippet:
            snippets.append(snippet)
    return snippets


def _anchored_test_snippet(
    repo_root: Path,
    rel_path: str,
    anchors: list[str],
    max_lines: int,
) -> CodeSnippet | None:
    if not anchors:
        return None
    lines = _read_lines(repo_root, rel_path)
    if not lines:
        return None
    for index, line in enumerate(lines, start=1):
        for anchor in anchors:
            if anchor in line:
                return _snippet_for_line_window(repo_root, rel_path, index, max_lines)
    return None


def _finalize_review_pack(pack: ContextPack, config: ReviewConfig) -> ContextPack:
    matched_rules = match_rules_for_pack(pack, config.rule_definitions)
    if config.rules or matched_rules:
        pack.rules = [
            *config.rules,
            *(render_rule_for_prompt(rule) for rule in matched_rules),
        ]
        pack.rule_matches = [rule_match_for_prompt(rule) for rule in matched_rules]
    if config.memory.enabled and config.memory_definitions:
        pack.memory_matches, pack.memory_omissions = select_memory_for_pack(
            pack,
            config.memory_definitions,
            config.memory,
            base_chars=_estimated_pack_chars(pack),
        )
    return _finalize_pack(pack, config.context)


def _finalize_pack(pack: ContextPack, config: ContextConfig) -> ContextPack:
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
    estimated_chars = _estimated_pack_chars(pack)
    if estimated_chars <= config.max_pack_chars:
        return

    def over_budget() -> bool:
        return estimated_chars > config.max_pack_chars

    def refresh_estimate() -> None:
        nonlocal estimated_chars
        estimated_chars = _estimated_pack_chars(pack)

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
        estimated_chars=_estimated_pack_chars(pack),
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


def _estimated_pack_chars(pack: ContextPack) -> int:
    payload = pack_prompt_payload(pack, "review")
    payload.pop("stats", None)
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _context_policy_key(config: ContextConfig) -> str:
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _snippet_around_line(repo_root: Path, rel_path: str, line: int, context_lines: int) -> CodeSnippet | None:
    lines = _read_lines(repo_root, rel_path)
    if not lines or line < 1:
        return None
    start_line = max(1, line - context_lines)
    end_line = min(len(lines), line + context_lines)
    return CodeSnippet(
        file=rel_path,
        start_line=start_line,
        end_line=end_line,
        code="".join(lines[start_line - 1 : end_line]),
    )


def _snippet_for_line_range(
    repo_root: Path,
    rel_path: str,
    start_line: int,
    end_line: int,
    max_lines: int,
) -> CodeSnippet | None:
    lines = _read_lines(repo_root, rel_path)
    if not lines or start_line < 1 or end_line < start_line:
        return None

    bounded_start = min(start_line, len(lines))
    bounded_end = min(end_line, len(lines), bounded_start + max_lines - 1)
    return CodeSnippet(
        file=rel_path,
        start_line=bounded_start,
        end_line=bounded_end,
        code="".join(lines[bounded_start - 1 : bounded_end]),
    )


def _snippet_for_line_window(repo_root: Path, rel_path: str, line: int, max_lines: int) -> CodeSnippet | None:
    lines = _read_lines(repo_root, rel_path)
    if not lines or line < 1:
        return None
    before = max(0, (max_lines - 1) // 2)
    start_line = max(1, line - before)
    end_line = min(len(lines), start_line + max_lines - 1)
    return CodeSnippet(
        file=rel_path,
        start_line=start_line,
        end_line=end_line,
        code="".join(lines[start_line - 1 : end_line]),
    )


def _snippet_for_file_start(repo_root: Path, rel_path: str, max_lines: int) -> CodeSnippet | None:
    lines = _read_lines(repo_root, rel_path)
    if not lines:
        return None
    end_line = min(len(lines), max_lines)
    return CodeSnippet(
        file=rel_path,
        start_line=1,
        end_line=end_line,
        code="".join(lines[:end_line]),
    )


def _read_lines(repo_root: Path, rel_path: str) -> list[str]:
    path = _resolve_repo_path(repo_root, rel_path)
    if path is None:
        return []
    try:
        stat = path.stat()
    except OSError:
        return []
    if not path.is_file():
        return []
    return list(_read_lines_cached(str(path), stat.st_mtime_ns, stat.st_size))


@lru_cache(maxsize=4096)
def _read_lines_cached(path: str, mtime_ns: int, size: int) -> tuple[str, ...]:
    try:
        return tuple(Path(path).read_text(encoding="utf-8").splitlines(keepends=True))
    except OSError, UnicodeDecodeError:
        return ()


def _resolve_repo_path(repo_root: Path, rel_path: str) -> Path | None:
    candidate = Path(rel_path)
    if candidate.is_absolute():
        return None

    resolved_root = repo_root.resolve()
    resolved_path = (resolved_root / candidate).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved_path
