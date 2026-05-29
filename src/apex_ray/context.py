from pathlib import Path

from apex_ray.context_budget import estimated_pack_chars as _estimated_pack_chars
from apex_ray.context_budget import finalize_pack as _finalize_pack
from apex_ray.context_snippets import callee_snippets as _callee_snippets
from apex_ray.context_snippets import changed_hunk_snippets as _changed_hunk_snippets
from apex_ray.context_snippets import changed_symbol_snippets as _changed_symbol_snippets
from apex_ray.context_snippets import contract_snippets as _contract_snippets
from apex_ray.context_snippets import is_import_reference as _is_import_reference
from apex_ray.context_snippets import metadata_snippets as _metadata_snippets
from apex_ray.context_snippets import reference_snippets as _reference_snippets
from apex_ray.context_snippets import test_anchor_terms as _test_anchor_terms
from apex_ray.context_snippets import test_snippets as _test_snippets
from apex_ray.line_ranges import merge_line_ranges as _merge_line_ranges
from apex_ray.memory import select_memory_for_pack
from apex_ray.models import (
    AnalyzerFile,
    AnalyzerReference,
    AnalyzerResult,
    AnalyzerSymbol,
    ChangedFile,
    CodeSnippet,
    ContextPack,
    FileKind,
    ReviewConfig,
    RiskSignal,
)
from apex_ray.rules import match_rules_for_pack, render_rule_for_prompt, rule_match_for_prompt

FILE_CLUSTER_MAX_SYMBOLS = 24
LARGE_FILE_CLUSTER_CHUNK_SIZE = 12
SMALL_FILE_CLUSTER_MAX_DIFF_LINES = 160


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
