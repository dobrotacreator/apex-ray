from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from apex_ray import __version__
from apex_ray.line_ranges import merge_line_ranges, subtract_line_ranges
from apex_ray.models import (
    AnalyzerConfig,
    AnalyzerFile,
    AnalyzerReference,
    AnalyzerResult,
    AnalyzerSymbol,
    ChangedFile,
    DiffLineKind,
    FileKind,
)

from ..common import AnalyzerError, _collapse_ranges
from .cache import build_dart_analysis_cache_key, load_dart_analysis_cache, write_dart_analysis_cache
from .directives import DartDirective, parse_dart_directives
from .generated import is_generated_dart_path
from .lsp import DartLspClient, DartLspError, DartLspResponseError, DartLspTimeout
from .mapping import (
    DartDocumentSymbol,
    DartReferenceSourceReader,
    analyzer_references_from_lsp_locations,
    changed_document_symbols,
    collect_analyzer_references_from_lsp_locations,
    collect_document_symbols,
)
from .metadata import DartFrameworkMetadataIndex, build_dart_framework_metadata_index
from .platform_channels import (
    PlatformChannelIndex,
    build_platform_channel_index,
    platform_channel_contracts,
)
from .protocol import path_to_file_uri
from .related_tests import DartRelatedTestIndex, build_dart_related_test_index, rank_related_dart_tests
from .toolchain import resolve_dart_toolchain
from .workspace import reverse_dependency_anchors

DART_LANGUAGES = {"dart"}
DART_SOURCE_BYTE_LIMIT = 4 * 1024 * 1024
DART_CHANGED_SOURCE_FILE_LIMIT = 256
DART_CHANGED_SOURCE_TOTAL_BYTE_LIMIT = 32 * 1024 * 1024
DART_ANCHOR_SOURCE_BYTE_LIMIT = 512 * 1024
DART_ANCHOR_TOTAL_SOURCE_BYTE_LIMIT = 8 * 1024 * 1024
DART_DOCUMENT_SYMBOL_LIMIT_PER_FILE = 5_000
DART_DOCUMENT_SYMBOL_LIMIT_TOTAL = 20_000
DART_FLUTTER_OUTLINE_NODE_LIMIT = 5_000
DART_FLUTTER_MANIFEST_FILE_LIMIT = 512
DART_FLUTTER_MANIFEST_BYTE_LIMIT = 1024 * 1024
DART_FLUTTER_MANIFEST_TOTAL_BYTE_LIMIT = 8 * 1024 * 1024
DART_DIAGNOSTIC_NODE_LIMIT_PER_FILE = 5_000
DART_DIAGNOSTIC_LIMIT_PER_SYMBOL = 6
DART_FLUTTER_OUTLINE_LIMIT_PER_SYMBOL = 12
DART_FILE_ACTIVATION_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4)
_DART_NOTIFICATION_METADATA_PREFIXES = (
    "Dart analyzer diagnostic (",
    "Flutter widget outline:",
)
_DART_DIAGNOSTICS_NOTIFICATION_METHOD = "textDocument/publishDiagnostics"
_DART_FLUTTER_OUTLINE_NOTIFICATION_METHOD = "dart/textDocument/publishFlutterOutline"


def has_dart_changes(files: list[ChangedFile]) -> bool:
    return bool(dart_changed_files(files))


def dart_changed_files(files: list[ChangedFile]) -> list[ChangedFile]:
    return [
        file
        for file in files
        if file.language in DART_LANGUAGES
        and file.file_kind in {FileKind.SOURCE, FileKind.TEST}
        and not file.is_ignored
        and file.new_path is not None
        and not is_generated_dart_path(file.new_path)
    ]


def run_dart_analyzer(
    repo_root: Path,
    files: list[ChangedFile],
    config: AnalyzerConfig | None = None,
    *,
    project_files: list[Path] | None = None,
) -> AnalyzerResult | None:
    changed_files = dart_changed_files(files)
    if not changed_files:
        return None
    config = config or AnalyzerConfig()
    dart_config = config.dart
    if not dart_config.enabled:
        return None

    root = repo_root.resolve()
    deadline = time.monotonic() + config.timeout_seconds
    probe_toolchain = config.index_cache_enabled or not dart_config.plugins
    resolution = resolve_dart_toolchain(
        root,
        dart_config,
        probe_version=probe_toolchain,
        timeout_seconds=min(2.0, max(0.1, deadline - time.monotonic())),
    )
    if not resolution.command:
        message = resolution.error or "No Dart SDK command could be resolved for this project."
        if resolution.remediation:
            message = f"{message} {resolution.remediation}"
        raise AnalyzerError(message)
    if resolution.error:
        message = resolution.error
        if resolution.remediation:
            message = f"{message} {resolution.remediation}"
        raise AnalyzerError(message)
    command = [
        *resolution.command,
        "language-server",
        "--client-id",
        "apex-ray",
        "--client-version",
        __version__,
    ]
    if not dart_config.plugins:
        command.append("--no-plugins")

    cache_key = build_dart_analysis_cache_key(
        root,
        changed_files,
        project_files,
        command,
        config,
        toolchain_version=resolution.version,
        deadline=deadline,
    )
    cached = (
        load_dart_analysis_cache(
            root,
            changed_files,
            project_files,
            command,
            config,
            cache_key=cache_key,
            deadline=deadline,
        )
        if cache_key is not None
        else None
    )
    if cached is not None:
        return cached

    warnings: list[str] = []
    failed_files: list[str] = []
    sources: dict[str, str] = {}
    retained_source_bytes = 0
    source_limit_failures: list[str] = []
    for changed_file in changed_files:
        if time.monotonic() >= deadline:
            warnings.append("Dart analyzer time budget exhausted while reading changed files.")
            failed_files.extend(file.path for file in changed_files if file.path not in sources)
            break
        if len(sources) >= DART_CHANGED_SOURCE_FILE_LIMIT:
            source_limit_failures.append(changed_file.path)
            failed_files.append(changed_file.path)
            continue
        remaining_source_bytes = DART_CHANGED_SOURCE_TOTAL_BYTE_LIMIT - retained_source_bytes
        try:
            source_size = _dart_source_byte_size(root, changed_file.path)
        except (OSError, UnicodeError, ValueError) as exc:
            warnings.append(f"Unable to read Dart file {changed_file.path}: {exc}")
            failed_files.append(changed_file.path)
            continue
        if source_size > DART_SOURCE_BYTE_LIMIT:
            warnings.append(
                f"Unable to read Dart file {changed_file.path}: "
                f"file exceeds the {DART_SOURCE_BYTE_LIMIT}-byte analyzer limit"
            )
            failed_files.append(changed_file.path)
            continue
        if source_size > remaining_source_bytes:
            source_limit_failures.append(changed_file.path)
            failed_files.append(changed_file.path)
            continue
        try:
            source = _read_changed_dart_source(
                root,
                changed_file.path,
                max_bytes=min(DART_SOURCE_BYTE_LIMIT, remaining_source_bytes),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            warnings.append(f"Unable to read Dart file {changed_file.path}: {exc}")
            failed_files.append(changed_file.path)
            continue
        source_bytes = len(source.encode("utf-8"))
        if retained_source_bytes + source_bytes > DART_CHANGED_SOURCE_TOTAL_BYTE_LIMIT:
            source_limit_failures.append(changed_file.path)
            failed_files.append(changed_file.path)
            continue
        sources[changed_file.path] = source
        retained_source_bytes += source_bytes

    if source_limit_failures:
        warnings.append(
            "Dart changed-source safety limit reached; "
            f"using diff-only fallback for {len(source_limit_failures)} file(s)."
        )

    if not sources:
        return AnalyzerResult(
            language="dart",
            projectRoot=str(root),
            files=[],
            warnings=warnings,
            partial=True,
            failedFiles=failed_files,
        )

    anchor_sources: dict[str, str] = {}
    retained_anchor_bytes = 0
    skipped_anchor_sources = 0
    for anchor in reverse_dependency_anchors(
        root,
        list(sources),
        project_files,
        limit=dart_config.max_dependency_package_anchors,
        deadline=deadline,
    ):
        anchor_path = anchor.as_posix()
        remaining_anchor_bytes = DART_ANCHOR_TOTAL_SOURCE_BYTE_LIMIT - retained_anchor_bytes
        try:
            anchor_size = _dart_source_byte_size(root, anchor_path)
        except (OSError, UnicodeError, ValueError) as exc:
            warnings.append(
                f"Unable to open Dart reverse-dependency anchor {anchor_path}: {exc}; "
                "cross-package references may be incomplete."
            )
            continue
        if anchor_size > DART_ANCHOR_SOURCE_BYTE_LIMIT:
            warnings.append(
                f"Unable to open Dart reverse-dependency anchor {anchor_path}: "
                f"file exceeds the {DART_ANCHOR_SOURCE_BYTE_LIMIT}-byte analyzer limit; "
                "cross-package references may be incomplete."
            )
            continue
        if anchor_size > remaining_anchor_bytes:
            skipped_anchor_sources += 1
            continue
        try:
            anchor_source = _read_changed_dart_source(
                root,
                anchor_path,
                max_bytes=min(DART_ANCHOR_SOURCE_BYTE_LIMIT, remaining_anchor_bytes),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            warnings.append(
                f"Unable to open Dart reverse-dependency anchor {anchor_path}: {exc}; "
                "cross-package references may be incomplete."
            )
            continue
        anchor_bytes = len(anchor_source.encode("utf-8"))
        if retained_anchor_bytes + anchor_bytes > DART_ANCHOR_TOTAL_SOURCE_BYTE_LIMIT:
            skipped_anchor_sources += 1
            continue
        anchor_sources[anchor_path] = anchor_source
        retained_anchor_bytes += anchor_bytes
    if skipped_anchor_sources:
        warnings.append(
            f"Dart anchor-source safety limit reached; skipped {skipped_anchor_sources} reverse-dependency anchor(s)."
        )

    flutter_detection_partial = [False]
    flutter_outline = _flutter_outline_enabled(
        root,
        sources,
        project_files,
        dart_config.flutter,
        deadline,
        flutter_detection_partial,
    )
    if flutter_detection_partial[0]:
        warnings.append(
            "Dart Flutter auto-detection reached its manifest safety budget; "
            "Flutter outline metadata may be incomplete."
        )
    allowed_project_paths = _allowed_project_paths(root, project_files, fallback_paths=sources)
    platform_channel_index = _platform_channel_index(root, sources, allowed_project_paths, warnings, deadline)
    candidate_paths = sorted(allowed_project_paths)
    related_test_index = build_dart_related_test_index(root, candidate_paths=candidate_paths, deadline=deadline)
    if related_test_index.truncated:
        warnings.append(
            "Dart related-test index reached its safety limit; some test evidence may use path-only ranking."
        )
    if time.monotonic() >= deadline:
        return AnalyzerResult(
            language="dart",
            projectRoot=str(root),
            files=[],
            warnings=_dedupe_strings(
                [*warnings, "Dart analyzer time budget exhausted while preparing project context."]
            ),
            partial=True,
            failedFiles=sorted({*failed_files, *(file.path for file in changed_files)}),
        )
    analyzed_files: list[AnalyzerFile] = []
    finalization_partial = flutter_detection_partial[0]
    semantic_partial_files: set[str] = set()
    document_symbol_budget = [DART_DOCUMENT_SYMBOL_LIMIT_TOTAL]
    reference_source_reader = DartReferenceSourceReader(root, deadline=deadline)
    uris = {path: path_to_file_uri((root / path).resolve()) for path in sources}
    snapshot_methods = {_DART_DIAGNOSTICS_NOTIFICATION_METHOD}
    if flutter_outline:
        snapshot_methods.add(_DART_FLUTTER_OUTLINE_NOTIFICATION_METHOD)
    notification_snapshot_keys = frozenset((method, uri) for method in snapshot_methods for uri in uris.values())
    try:
        with DartLspClient(
            command,
            root,
            timeout=min(30.0, float(config.timeout_seconds)),
            deadline=deadline,
            notification_snapshot_keys=notification_snapshot_keys,
        ) as client:
            client.initialize(path_to_file_uri(root), flutter_outline=flutter_outline)
            for path, source in sources.items():
                client.did_open(uris[path], source)
            for path, source in anchor_sources.items():
                if path not in uris:
                    client.did_open(path_to_file_uri((root / path).resolve()), source)
            if not _wait_for_workspace_analysis(client, warnings, deadline):
                finalization_partial = True
                warnings.append(
                    "Dart initial workspace synchronization did not complete; "
                    "semantic results are retained but will not be cached."
                )

            for changed_file in changed_files:
                source = sources.get(changed_file.path)
                if source is None:
                    continue
                if time.monotonic() >= deadline:
                    warnings.append(
                        f"Dart analyzer time budget exhausted before {changed_file.path}; "
                        "using diff-only fallback context."
                    )
                    failed_files.extend(
                        pending.path
                        for pending in changed_files
                        if pending.path not in {item.path for item in analyzed_files}
                        and pending.path not in failed_files
                    )
                    break
                try:
                    analyzed_files.append(
                        _analyze_dart_file(
                            client,
                            root,
                            changed_file,
                            source,
                            uris[changed_file.path],
                            config,
                            related_test_index,
                            reference_source_reader,
                            allowed_project_paths,
                            platform_channel_index,
                            deadline=deadline,
                            warnings=warnings,
                            semantic_partial_files=semantic_partial_files,
                            document_symbol_budget=document_symbol_budget,
                        )
                    )
                except DartLspError as exc:
                    warnings.append(f"Unable to analyze Dart file {changed_file.path}: {exc}")
                    failed_files.append(changed_file.path)
            try:
                if not _wait_for_workspace_analysis(client, warnings, deadline):
                    finalization_partial = True
                    warnings.append(
                        "Dart final metadata synchronization did not complete; "
                        f"retained {len(analyzed_files)} analyzed file(s)."
                    )
            except DartLspError as exc:
                finalization_partial = True
                warnings.append(
                    "Dart final metadata synchronization did not complete; "
                    f"retained {len(analyzed_files)} analyzed file(s): {exc}"
                )
            analyzed_files = _reconcile_lsp_document_metadata(
                client,
                analyzed_files,
                uris,
                flutter_outline=flutter_outline,
                deadline=deadline,
                warnings=warnings,
                semantic_partial_files=semantic_partial_files,
            )
            dropped_notifications = int(getattr(client, "dropped_notifications", 0) or 0)
            if dropped_notifications:
                finalization_partial = True
                warnings.append(
                    f"Dart language server dropped {dropped_notifications} LSP notification(s) "
                    "after reaching the bounded notification buffer; diagnostics or Flutter "
                    "metadata may be incomplete."
                )
    except DartLspError as exc:
        raise AnalyzerError(str(exc)) from exc

    if reference_source_reader.partial:
        finalization_partial = True
        warnings.append(
            "Dart reference-source safety budget was exhausted or a referenced source was unavailable; "
            f"retained {reference_source_reader.files_read} bounded source file(s) and omitted "
            f"{reference_source_reader.skipped_files} file(s)."
        )

    failed_files = sorted(set(failed_files))
    result = AnalyzerResult(
        language="dart",
        projectRoot=str(root),
        files=analyzed_files,
        warnings=_dedupe_strings(warnings),
        partial=bool(failed_files) or finalization_partial or bool(semantic_partial_files),
        failedFiles=failed_files,
    )
    cache_stats = (
        write_dart_analysis_cache(
            root,
            changed_files,
            project_files,
            command,
            config,
            result,
            cache_key=cache_key,
            deadline=deadline,
        )
        if cache_key is not None
        else None
    )
    if cache_stats is not None:
        result = result.model_copy(update={"index_cache": cache_stats})
    return result


def _analyze_dart_file(
    client: DartLspClient,
    repo_root: Path,
    changed_file: ChangedFile,
    source: str,
    uri: str,
    config: AnalyzerConfig,
    related_test_index: DartRelatedTestIndex,
    reference_source_reader: DartReferenceSourceReader,
    allowed_reference_paths: frozenset[str],
    platform_channel_index: PlatformChannelIndex | None,
    *,
    deadline: float,
    warnings: list[str],
    semantic_partial_files: set[str],
    document_symbol_budget: list[int],
) -> AnalyzerFile:
    payload = _request_document_symbols(client, uri, deadline)
    symbol_limit = min(DART_DOCUMENT_SYMBOL_LIMIT_PER_FILE, max(0, document_symbol_budget[0]))
    collected_symbols = collect_document_symbols(payload, uri=uri, limit=symbol_limit)
    document_symbol_budget[0] -= len(collected_symbols.symbols)
    document_symbols = _with_source_signatures(collected_symbols.symbols, source)
    if collected_symbols.truncated:
        semantic_partial_files.add(changed_file.path)
        warnings.append(
            f"Dart document-symbol safety limit reached for {changed_file.path}; "
            f"retained {len(document_symbols)} symbol(s) and preserved the changed diff as fallback context."
        )
    changed_ranges = _changed_new_line_ranges(changed_file)
    changed_symbols = changed_document_symbols(document_symbols, changed_ranges)
    selected = changed_symbols[: config.dart.max_changed_symbols]
    omitted = changed_symbols[len(selected) :]
    if omitted:
        warnings.append(
            f"Dart changed-symbol limit reached for {changed_file.path}; "
            f"semantically enriched {len(selected)} symbols and preserved {len(omitted)} "
            "as diff-only review ranges."
        )

    enriched: list[AnalyzerSymbol] = []
    framework_metadata_index = (
        build_dart_framework_metadata_index(changed_file.path, source, deadline=deadline) if selected else None
    )
    if framework_metadata_index is not None and framework_metadata_index.truncated:
        semantic_partial_files.add(changed_file.path)
        warnings.append(
            f"Dart framework-metadata safety budget was exhausted for {changed_file.path}; "
            "retained bounded metadata and preserved the changed diff as fallback context."
        )
    for item in selected:
        if time.monotonic() >= deadline:
            raise DartLspTimeout(f"Dart analyzer time budget exhausted while analyzing {changed_file.path}")
        enriched.append(
            _enrich_dart_symbol(
                client,
                repo_root,
                changed_file.path,
                item,
                config,
                reference_source_reader,
                allowed_reference_paths,
                platform_channel_index,
                framework_metadata_index,
                uri=uri,
                deadline=deadline,
                warnings=warnings,
                semantic_partial_files=semantic_partial_files,
            )
        )

    enriched_by_identity = {(symbol.name, symbol.start_line, symbol.end_line): symbol for symbol in enriched}
    all_symbols = [
        enriched_by_identity.get(
            (item.symbol.name, item.symbol.start_line, item.symbol.end_line),
            item.symbol,
        )
        for item in document_symbols
    ]
    semantic_test_references = {
        reference.file for symbol in enriched for reference in symbol.references if _is_dart_test_path(reference.file)
    }
    related_tests = rank_related_dart_tests(
        repo_root,
        changed_file.path,
        semantic_references=semantic_test_references,
        symbol_names={symbol.name.rsplit(".", 1)[-1] for symbol in enriched},
        limit=config.dart.max_related_tests_per_file,
        index=related_test_index,
    )
    if time.monotonic() >= deadline:
        raise DartLspTimeout(f"Dart analyzer time budget exhausted while ranking related tests for {changed_file.path}")
    directives = parse_dart_directives(source)
    imports = _dart_imports(directives)
    exports = _dart_exports(directives, all_symbols)
    uncovered_ranges = (
        changed_ranges
        if collected_symbols.truncated
        else (
            merge_line_ranges(
                [
                    *subtract_line_ranges(
                        changed_ranges,
                        [(item.symbol.start_line, item.symbol.end_line) for item in selected],
                    ),
                    *(
                        (max(changed_start, item.symbol.start_line), min(changed_end, item.symbol.end_line))
                        for item in omitted
                        for changed_start, changed_end in changed_ranges
                        if item.symbol.start_line <= changed_end and changed_start <= item.symbol.end_line
                    ),
                ]
            )
            if selected
            else []
        )
    )
    return AnalyzerFile(
        path=changed_file.path,
        symbols=all_symbols,
        imports=imports,
        exports=exports,
        relatedTests=related_tests,
        changedSymbols=enriched,
        uncoveredChangedRanges=uncovered_ranges,
    )


def _enrich_dart_symbol(
    client: DartLspClient,
    repo_root: Path,
    path: str,
    item: DartDocumentSymbol,
    config: AnalyzerConfig,
    reference_source_reader: DartReferenceSourceReader,
    allowed_reference_paths: frozenset[str],
    platform_channel_index: PlatformChannelIndex | None,
    framework_metadata_index: DartFrameworkMetadataIndex | None,
    *,
    uri: str,
    deadline: float,
    warnings: list[str],
    semantic_partial_files: set[str],
) -> AnalyzerSymbol:
    position_params = {
        "textDocument": {"uri": uri},
        "position": item.position,
    }

    def optional_request(method: str, params: object) -> object:
        return _optional_request(
            client,
            method,
            params,
            deadline,
            warnings,
            partial_files=semantic_partial_files,
            path=path,
        )

    raw_references = optional_request(
        "textDocument/references",
        {**position_params, "context": {"includeDeclaration": False}},
    )
    references, suppressed_generated = _repository_references(
        repo_root,
        raw_references,
        kind="read",
        limit=config.dart.max_references_per_symbol,
        source_reader=reference_source_reader,
        allowed_paths=allowed_reference_paths,
    )

    callees: list[AnalyzerReference] = []
    prepared_call = optional_request(
        "textDocument/prepareCallHierarchy",
        position_params,
    )
    call_item = _first_hierarchy_item(prepared_call)
    if call_item is not None:
        incoming = optional_request(
            "callHierarchy/incomingCalls",
            {"item": call_item},
        )
        references.extend(
            _hierarchy_references(
                repo_root,
                incoming,
                member="from",
                kind="call",
                limit=config.dart.max_references_per_symbol,
                source_reader=reference_source_reader,
                allowed_paths=allowed_reference_paths,
            )
        )
        outgoing = optional_request(
            "callHierarchy/outgoingCalls",
            {"item": call_item},
        )
        callees = _hierarchy_references(
            repo_root,
            outgoing,
            member="to",
            kind="callee",
            limit=config.dart.max_callees_per_symbol,
            source_reader=reference_source_reader,
            allowed_paths=allowed_reference_paths,
        )

    contracts: list[AnalyzerReference] = []
    prepared_type = optional_request(
        "textDocument/prepareTypeHierarchy",
        position_params,
    )
    type_item = _first_hierarchy_item(prepared_type)
    if type_item is not None:
        supertypes = optional_request(
            "typeHierarchy/supertypes",
            {"item": type_item},
        )
        contracts = _location_item_references(
            repo_root,
            supertypes,
            kind="contract",
            limit=config.dart.max_callees_per_symbol,
            source_reader=reference_source_reader,
            allowed_paths=allowed_reference_paths,
        )
        subtypes = optional_request(
            "typeHierarchy/subtypes",
            {"item": type_item},
        )
        references.extend(
            _location_item_references(
                repo_root,
                subtypes,
                kind="type",
                limit=config.dart.max_references_per_symbol,
                source_reader=reference_source_reader,
                allowed_paths=allowed_reference_paths,
            )
        )

    if platform_channel_index is not None:
        contracts.extend(
            platform_channel_contracts(
                platform_channel_index,
                path,
                start_line=item.symbol.start_line,
                end_line=item.symbol.end_line,
                limit=config.dart.max_callees_per_symbol,
            )
        )

    metadata = (
        framework_metadata_index.for_range(
            start_line=item.symbol.start_line,
            end_line=item.symbol.end_line,
            deadline=deadline,
        )
        if framework_metadata_index is not None
        else []
    )
    if suppressed_generated:
        metadata.append(
            AnalyzerReference(
                file=path,
                line=item.symbol.start_line,
                endLine=item.symbol.start_line,
                text=f"generated references suppressed from prompt context: {suppressed_generated}",
                kind="metadata",
            )
        )

    return item.symbol.model_copy(
        update={
            "references": _dedupe_references(references)[: config.dart.max_references_per_symbol],
            "callees": _dedupe_references(callees)[: config.dart.max_callees_per_symbol],
            "contracts": _dedupe_references(contracts)[: config.dart.max_callees_per_symbol],
            "metadata": _dedupe_references(metadata),
        }
    )


def _optional_request(
    client: DartLspClient,
    method: str,
    params: object,
    deadline: float,
    warnings: list[str],
    *,
    partial_files: set[str] | None = None,
    path: str | None = None,
) -> object:
    try:
        return client.request(method, params, deadline=deadline)
    except DartLspResponseError as exc:
        warnings.append(f"Dart LSP method {method} unavailable: {exc}")
        return None
    except DartLspTimeout as exc:
        if time.monotonic() >= deadline:
            raise
        warnings.append(f"Dart LSP method {method} timed out: {exc}")
        if partial_files is not None and path is not None:
            partial_files.add(path)
        return None


def _request_document_symbols(client: DartLspClient, uri: str, deadline: float) -> object:
    params = {"textDocument": {"uri": uri}}
    for attempt in range(len(DART_FILE_ACTIVATION_RETRY_DELAYS) + 1):
        try:
            return client.request("textDocument/documentSymbol", params, deadline=deadline)
        except DartLspResponseError as exc:
            if not _is_file_activation_race(exc) or attempt >= len(DART_FILE_ACTIVATION_RETRY_DELAYS):
                raise
            delay = DART_FILE_ACTIVATION_RETRY_DELAYS[attempt]
            if time.monotonic() + delay >= deadline:
                raise DartLspTimeout("Dart analyzer time budget exhausted while activating an open file") from exc
            time.sleep(delay)
    raise AssertionError("unreachable")


def _is_file_activation_race(error: DartLspResponseError) -> bool:
    return error.code == -32007 and "not being analyzed" in error.message.casefold()


def _repository_references(
    repo_root: Path,
    payload: object,
    *,
    kind: str,
    limit: int,
    source_reader: DartReferenceSourceReader | None = None,
    allowed_paths: frozenset[str] | None = None,
) -> tuple[list[AnalyzerReference], int]:
    collected = collect_analyzer_references_from_lsp_locations(
        repo_root,
        payload,
        kind=kind,
        limit=limit,
        exclude=is_generated_dart_path,
        allowed=allowed_paths.__contains__ if allowed_paths is not None else None,
        reader=source_reader,
    )
    return collected.references, collected.excluded_count


def _hierarchy_references(
    repo_root: Path,
    payload: object,
    *,
    member: str,
    kind: str,
    limit: int,
    source_reader: DartReferenceSourceReader | None = None,
    allowed_paths: frozenset[str] | None = None,
) -> list[AnalyzerReference]:
    if not isinstance(payload, list):
        return []
    items = [entry.get(member) for entry in payload if isinstance(entry, dict)]
    return _location_item_references(
        repo_root,
        items,
        kind=kind,
        limit=limit,
        source_reader=source_reader,
        allowed_paths=allowed_paths,
    )


def _location_item_references(
    repo_root: Path,
    payload: object,
    *,
    kind: str,
    limit: int,
    source_reader: DartReferenceSourceReader | None = None,
    allowed_paths: frozenset[str] | None = None,
) -> list[AnalyzerReference]:
    if not isinstance(payload, list):
        return []
    locations: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        range_payload = item.get("selectionRange") or item.get("range")
        if isinstance(uri, str) and isinstance(range_payload, dict):
            locations.append({"uri": uri, "range": range_payload})
    return analyzer_references_from_lsp_locations(
        repo_root,
        locations,
        kind=kind,
        limit=limit,
        exclude=is_generated_dart_path,
        allowed=allowed_paths.__contains__ if allowed_paths is not None else None,
        reader=source_reader,
    )


def _first_hierarchy_item(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, list):
        return None
    return next((item for item in payload if isinstance(item, dict)), None)


def _flutter_outline_metadata(
    client: DartLspClient,
    path: str,
    uri: str,
    symbol: AnalyzerSymbol,
) -> list[AnalyzerReference]:
    evidence, _truncated = _latest_flutter_outline_metadata(client, path, uri, deadline=None)
    return _notification_metadata_for_symbol(
        evidence,
        symbol,
        limit=DART_FLUTTER_OUTLINE_LIMIT_PER_SYMBOL,
        deadline=None,
    )[0]


def _latest_flutter_outline_metadata(
    client: DartLspClient,
    path: str,
    uri: str,
    *,
    deadline: float | None,
) -> tuple[list[AnalyzerReference], bool]:
    if _runner_deadline_expired(deadline):
        return [], True
    evidence: list[AnalyzerReference] = []
    truncated = False
    messages = client.notifications(_DART_FLUTTER_OUTLINE_NOTIFICATION_METHOD, uri=uri)
    for message in messages[-1:]:
        params = message.get("params")
        outline = params.get("outline") if isinstance(params, dict) else None
        nodes, nodes_truncated = _collect_flattened_outline(
            outline,
            limit=DART_FLUTTER_OUTLINE_NODE_LIMIT,
        )
        truncated = truncated or nodes_truncated
        for index, node in enumerate(nodes):
            if index % 64 == 0 and _runner_deadline_expired(deadline):
                truncated = True
                break
            range_payload = node.get("codeRange") or node.get("range")
            line_range = _one_based_range(range_payload)
            if line_range is None:
                continue
            raw_kind = node.get("kind")
            kind = raw_kind[:100] if isinstance(raw_kind, str) else "widget"
            label = ""
            for key in ("className", "label", "variableName"):
                raw_label = node.get(key)
                if isinstance(raw_label, str) and raw_label:
                    label = raw_label[:500]
                    break
            text = f"Flutter widget outline: {kind}{f' {label}' if label else ''}"
            evidence.append(
                AnalyzerReference(
                    file=path,
                    line=line_range[0],
                    endLine=line_range[1],
                    text=text,
                    kind="metadata",
                )
            )
    return _dedupe_references(evidence), truncated or _runner_deadline_expired(deadline)


def _diagnostic_metadata(
    client: DartLspClient,
    path: str,
    uri: str,
    symbol: AnalyzerSymbol,
) -> list[AnalyzerReference]:
    evidence, _truncated = _latest_diagnostic_metadata(client, path, uri, deadline=None)
    return _notification_metadata_for_symbol(
        evidence,
        symbol,
        limit=DART_DIAGNOSTIC_LIMIT_PER_SYMBOL,
        deadline=None,
    )[0]


def _latest_diagnostic_metadata(
    client: DartLspClient,
    path: str,
    uri: str,
    *,
    deadline: float | None,
) -> tuple[list[AnalyzerReference], bool]:
    if _runner_deadline_expired(deadline):
        return [], True
    evidence: list[AnalyzerReference] = []
    truncated = False
    messages = client.notifications(_DART_DIAGNOSTICS_NOTIFICATION_METHOD, uri=uri)
    for message in messages[-1:]:
        params = message.get("params")
        diagnostics = params.get("diagnostics") if isinstance(params, dict) else None
        if not isinstance(diagnostics, list):
            continue
        truncated = len(diagnostics) > DART_DIAGNOSTIC_NODE_LIMIT_PER_FILE
        for index, diagnostic in enumerate(diagnostics):
            if index >= DART_DIAGNOSTIC_NODE_LIMIT_PER_FILE:
                break
            if index % 64 == 0 and _runner_deadline_expired(deadline):
                truncated = True
                break
            if not isinstance(diagnostic, dict) or diagnostic.get("severity") not in {1, 2}:
                continue
            line_range = _one_based_range(diagnostic.get("range"))
            message_text = diagnostic.get("message")
            if line_range is None or not isinstance(message_text, str):
                continue
            level = "error" if diagnostic.get("severity") == 1 else "warning"
            evidence.append(
                AnalyzerReference(
                    file=path,
                    line=line_range[0],
                    endLine=line_range[1],
                    text=f"Dart analyzer diagnostic ({level}): {message_text[:500]}",
                    kind="metadata",
                )
            )
    return _dedupe_references(evidence), truncated or _runner_deadline_expired(deadline)


def _notification_metadata_for_symbol(
    evidence: list[AnalyzerReference],
    symbol: AnalyzerSymbol,
    *,
    limit: int,
    deadline: float | None,
) -> tuple[list[AnalyzerReference], bool]:
    selected: list[AnalyzerReference] = []
    for index, item in enumerate(evidence):
        if index % 64 == 0 and _runner_deadline_expired(deadline):
            return selected, True
        if _ranges_overlap(
            (symbol.start_line, symbol.end_line),
            (item.line, item.end_line or item.line),
        ):
            selected.append(item)
            if len(selected) >= limit:
                break
    return selected, _runner_deadline_expired(deadline)


def _reconcile_lsp_document_metadata(
    client: DartLspClient,
    analyzed_files: list[AnalyzerFile],
    uris: dict[str, str],
    *,
    flutter_outline: bool,
    deadline: float | None = None,
    warnings: list[str] | None = None,
    semantic_partial_files: set[str] | None = None,
) -> list[AnalyzerFile]:
    """Reconcile asynchronous notifications after semantic requests settle."""

    reconciled: list[AnalyzerFile] = []
    for file_index, analyzed_file in enumerate(analyzed_files):
        if _runner_deadline_expired(deadline):
            remaining_files = analyzed_files[file_index:]
            if semantic_partial_files is not None:
                semantic_partial_files.update(file.path for file in remaining_files)
            if warnings is not None:
                warnings.append(
                    "Dart notification-metadata reconciliation stopped at the analyzer deadline; "
                    "diagnostics or Flutter outline metadata may be incomplete."
                )
            reconciled.extend(remaining_files)
            break
        uri = uris.get(analyzed_file.path)
        if uri is None:
            reconciled.append(analyzed_file)
            continue
        outline_metadata: list[AnalyzerReference] = []
        outline_truncated = False
        if flutter_outline:
            outline_metadata, outline_truncated = _latest_flutter_outline_metadata(
                client,
                analyzed_file.path,
                uri,
                deadline=deadline,
            )
        diagnostic_metadata, diagnostic_truncated = _latest_diagnostic_metadata(
            client,
            analyzed_file.path,
            uri,
            deadline=deadline,
        )
        notification_truncated = outline_truncated or diagnostic_truncated
        symbols: list[AnalyzerSymbol] = []
        for symbol in analyzed_file.changed_symbols:
            metadata = [
                item for item in symbol.metadata if not item.text.startswith(_DART_NOTIFICATION_METADATA_PREFIXES)
            ]
            outline_for_symbol, outline_query_truncated = _notification_metadata_for_symbol(
                outline_metadata,
                symbol,
                limit=DART_FLUTTER_OUTLINE_LIMIT_PER_SYMBOL,
                deadline=deadline,
            )
            diagnostics_for_symbol, diagnostic_query_truncated = _notification_metadata_for_symbol(
                diagnostic_metadata,
                symbol,
                limit=DART_DIAGNOSTIC_LIMIT_PER_SYMBOL,
                deadline=deadline,
            )
            metadata.extend(outline_for_symbol)
            metadata.extend(diagnostics_for_symbol)
            notification_truncated = notification_truncated or outline_query_truncated or diagnostic_query_truncated
            symbols.append(symbol.model_copy(update={"metadata": _dedupe_references(metadata)}))
        if notification_truncated:
            if semantic_partial_files is not None:
                semantic_partial_files.add(analyzed_file.path)
            if warnings is not None:
                warnings.append(
                    f"Dart notification-metadata safety budget was exhausted for {analyzed_file.path}; "
                    "diagnostics or Flutter outline metadata may be incomplete."
                )
        reconciled_by_identity = {(symbol.name, symbol.start_line, symbol.end_line): symbol for symbol in symbols}
        all_symbols = [
            reconciled_by_identity.get(
                (symbol.name, symbol.start_line, symbol.end_line),
                symbol,
            )
            for symbol in analyzed_file.symbols
        ]
        reconciled.append(
            analyzed_file.model_copy(
                update={
                    "symbols": all_symbols,
                    "changed_symbols": symbols,
                }
            )
        )
    return reconciled


def _flatten_outline(
    payload: object,
    *,
    limit: int = DART_FLUTTER_OUTLINE_NODE_LIMIT,
) -> list[dict[str, Any]]:
    return _collect_flattened_outline(payload, limit=limit)[0]


def _collect_flattened_outline(
    payload: object,
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(payload, dict) or limit <= 0:
        return [], isinstance(payload, dict)
    output: list[dict[str, Any]] = []
    pending = [payload]
    truncated = False
    while pending and len(output) < limit:
        node = pending.pop()
        output.append(node)
        children = node.get("children")
        if isinstance(children, list):
            capacity = max(0, limit - len(output) - len(pending))
            retained_children = [child for child in children[:capacity] if isinstance(child, dict)]
            if len(children) > capacity:
                truncated = True
            pending.extend(reversed(retained_children))
    return output, truncated or bool(pending)


def _runner_deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _one_based_range(payload: object) -> tuple[int, int] | None:
    if not isinstance(payload, dict):
        return None
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    start_line = start.get("line")
    end_line = end.get("line")
    end_character = end.get("character")
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        return None
    inclusive_end = end_line if end_line > start_line and end_character == 0 else end_line + 1
    return start_line + 1, max(start_line + 1, inclusive_end)


def _changed_new_line_ranges(file: ChangedFile) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for hunk in file.hunks:
        added = sorted(
            line.new_line for line in hunk.lines if line.kind == DiffLineKind.ADD and line.new_line is not None
        )
        if added:
            ranges.extend(_collapse_ranges(added))
        elif hunk.new_start > 0:
            ranges.append((hunk.new_start, hunk.new_start))
    return ranges


def _with_source_signatures(
    symbols: list[DartDocumentSymbol],
    source: str,
) -> list[DartDocumentSymbol]:
    lines = source.splitlines()
    output: list[DartDocumentSymbol] = []
    for item in symbols:
        if item.symbol.signature:
            output.append(item)
            continue
        line = lines[item.symbol.start_line - 1].strip() if item.symbol.start_line <= len(lines) else ""
        output.append(replace(item, symbol=item.symbol.model_copy(update={"signature": line[:1_000]})))
    return output


def _dart_imports(directives: list[DartDirective]) -> list[str]:
    values: list[str] = []
    for directive in directives:
        if directive.kind == "import":
            values.extend((directive.target, *directive.conditional_targets))
        elif directive.kind == "part":
            values.append(directive.target)
        elif directive.kind == "part-of":
            values.append(f"part of {directive.target}")
    return _dedupe_strings(values)


def _dart_exports(directives: list[DartDirective], symbols: list[AnalyzerSymbol]) -> list[str]:
    values = [directive.target for directive in directives if directive.kind == "export"]
    values.extend(
        symbol.name
        for symbol in symbols
        if symbol.exported and "." not in symbol.name and symbol.kind not in {"field", "variable"}
    )
    return _dedupe_strings(values)


def _read_changed_dart_source(
    repo_root: Path,
    path: str,
    *,
    max_bytes: int = DART_SOURCE_BYTE_LIMIT,
) -> str:
    candidate = _validated_dart_source_path(repo_root, path)
    size = candidate.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file exceeds the {max_bytes}-byte analyzer limit")
    return candidate.read_text(encoding="utf-8")


def _dart_source_byte_size(repo_root: Path, path: str) -> int:
    return _validated_dart_source_path(repo_root, path).stat().st_size


def _validated_dart_source_path(repo_root: Path, path: str) -> Path:
    unresolved = repo_root / path
    if unresolved.is_symlink():
        raise ValueError("path is a repository symlink, not a regular repository file")
    candidate = unresolved.resolve(strict=True)
    candidate.relative_to(repo_root)
    if not candidate.is_file():
        raise ValueError("path is not a regular repository file")
    return candidate


def _flutter_outline_enabled(
    repo_root: Path,
    sources: dict[str, str],
    project_files: list[Path] | None,
    mode: str,
    deadline: float | None = None,
    partial: list[bool] | None = None,
) -> bool:
    if mode == "enabled":
        return True
    if mode == "disabled":
        return False
    if any("package:flutter/" in source for source in sources.values()):
        return True
    if project_files is None:
        manifests = [Path("pubspec.yaml"), Path("pubspec.yml")]
    else:
        manifests = sorted(
            {path for path in project_files if not path.is_absolute() and path.name in {"pubspec.yaml", "pubspec.yml"}},
            key=lambda path: path.as_posix(),
        )
    retained_bytes = 0
    truncated = len(manifests) > DART_FLUTTER_MANIFEST_FILE_LIMIT
    for manifest in manifests[:DART_FLUTTER_MANIFEST_FILE_LIMIT]:
        if deadline is not None and time.monotonic() >= deadline:
            _mark_flutter_detection_partial(partial)
            return False
        unresolved = repo_root / manifest
        if not unresolved.exists():
            continue
        try:
            manifest_path = manifest.as_posix()
            size = _dart_source_byte_size(repo_root, manifest_path)
            if size > DART_FLUTTER_MANIFEST_BYTE_LIMIT:
                truncated = True
                continue
            if retained_bytes + size > DART_FLUTTER_MANIFEST_TOTAL_BYTE_LIMIT:
                truncated = True
                break
            text = _read_changed_dart_source(
                repo_root,
                manifest_path,
                max_bytes=DART_FLUTTER_MANIFEST_BYTE_LIMIT,
            )
            retained_bytes += size
        except OSError, UnicodeError, ValueError:
            truncated = True
            continue
        if deadline is not None and time.monotonic() >= deadline:
            _mark_flutter_detection_partial(partial)
            return False
        if "sdk: flutter" in text or "sdk:\n    flutter" in text:
            return True
    if truncated:
        _mark_flutter_detection_partial(partial)
    return False


def _mark_flutter_detection_partial(partial: list[bool] | None) -> None:
    if partial is not None:
        partial[0] = True


def _platform_channel_index(
    repo_root: Path,
    sources: dict[str, str],
    allowed_project_paths: frozenset[str],
    warnings: list[str],
    deadline: float,
) -> PlatformChannelIndex | None:
    if not any(
        marker in source
        for source in sources.values()
        for marker in ("MethodChannel", "EventChannel", "BasicMessageChannel")
    ):
        return None
    index = build_platform_channel_index(
        repo_root,
        candidate_paths=sorted(allowed_project_paths),
        deadline=deadline,
    )
    if index.truncated:
        warnings.append("Dart platform-channel index reached its safety limit; some native contracts may be omitted.")
    return index


def _allowed_project_paths(
    repo_root: Path,
    project_files: list[Path] | None,
    *,
    fallback_paths: dict[str, str],
) -> frozenset[str]:
    """Build the only repository paths whose source may enter analyzer output.

    Project discovery has already applied Git and Apex Ray ignore rules. LSP is
    intentionally allowed to analyze the whole workspace locally, but its
    locations must not bypass that inventory when snippets are materialized.
    Direct API calls without an inventory remain conservative and expose only
    the changed sources supplied to this run.
    """

    root = repo_root.resolve(strict=False)
    allowed = set(fallback_paths)
    if project_files is None:
        return frozenset(allowed)
    for entry in project_files:
        try:
            if entry.is_absolute():
                relative = entry.resolve(strict=False).relative_to(root)
            else:
                relative = entry
                if ".." in relative.parts:
                    continue
        except OSError, ValueError:
            continue
        if relative.parts:
            allowed.add(relative.as_posix())
    return frozenset(allowed)


def _wait_for_workspace_analysis(
    client: DartLspClient,
    warnings: list[str],
    deadline: float,
) -> bool:
    experimental = client.server_capabilities.get("experimental")
    if not isinstance(experimental, dict) or not experimental.get("workspaceAnalysisComplete"):
        return True
    try:
        client.request("dart/workspace/analysis/complete", None, deadline=deadline)
    except DartLspResponseError as exc:
        warnings.append(f"Dart LSP method dart/workspace/analysis/complete unavailable: {exc}")
        return False
    except DartLspTimeout as exc:
        if time.monotonic() >= deadline:
            raise
        warnings.append(f"Dart LSP method dart/workspace/analysis/complete timed out: {exc}")
        return False
    return True


def _is_dart_test_path(path: str) -> bool:
    parts = Path(path).parts
    return path.endswith("_test.dart") and ("test" in parts or "integration_test" in parts)


def _ranges_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def _dedupe_references(references: list[AnalyzerReference]) -> list[AnalyzerReference]:
    unique: dict[tuple[str, int, int | None, str, str], AnalyzerReference] = {}
    for reference in references:
        key = (
            reference.file,
            reference.line,
            reference.end_line,
            reference.kind,
            reference.text,
        )
        unique.setdefault(key, reference)
    return sorted(
        unique.values(),
        key=lambda item: (item.file, item.line, item.end_line or item.line, item.kind, item.text),
    )


def _dedupe_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
