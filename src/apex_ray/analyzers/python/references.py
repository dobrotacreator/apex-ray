import ast

from apex_ray.models import AnalyzerReference, AnalyzerSymbol

from . import constants as _constants
from .annotations import _python_annotation_contracts_for_function
from .calls import _python_call_sites, _python_resolved_call_identities
from .state import _PythonImportBindings, _PythonIndexedSymbol, _PythonWorkspaceFile
from .symbols import _python_symbol_nodes
from .utils import (
    _python_node_end_line,
    _python_node_start_line,
    _python_node_text,
    _python_symbol_identity,
    _ranges_overlap,
)


def _populate_python_symbol_graph(
    changed_file: _PythonWorkspaceFile,
    symbols: list[AnalyzerSymbol],
    workspace: dict[str, _PythonWorkspaceFile],
) -> None:
    if not symbols:
        return
    symbol_nodes = _python_symbol_nodes(changed_file.module)
    indexed_symbols = _python_indexed_symbols(workspace)
    workspace_module_names = {file.module_name for file in workspace.values()}
    for symbol in symbols:
        symbol.references = _python_workspace_references(symbol, changed_file, workspace, workspace_module_names)
        node = symbol_nodes.get(symbol.name)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            symbol.callees = _python_callees_for_function(
                changed_file,
                node,
                symbol,
                indexed_symbols,
                workspace_module_names,
            )
            symbol.contracts = _merge_python_references(
                symbol.contracts,
                _python_annotation_contracts_for_function(
                    changed_file,
                    node,
                    symbol,
                    indexed_symbols,
                    workspace_module_names,
                ),
            )


def _python_indexed_symbols(workspace: dict[str, _PythonWorkspaceFile]) -> list[_PythonIndexedSymbol]:
    return [
        _PythonIndexedSymbol(
            path=file.path,
            module_name=file.module_name,
            identity=_python_symbol_identity(file.module_name, symbol.name),
            symbol=symbol,
        )
        for file in sorted(workspace.values(), key=lambda item: item.path)
        for symbol in file.symbols
    ]


def _python_workspace_references(
    target: AnalyzerSymbol,
    target_file: _PythonWorkspaceFile,
    workspace: dict[str, _PythonWorkspaceFile],
    workspace_module_names: set[str],
) -> list[AnalyzerReference]:
    references: list[AnalyzerReference] = []
    seen: set[tuple[str, int, str]] = set()
    for file in sorted(workspace.values(), key=lambda item: item.path):
        for call_site in _python_call_sites(file, workspace_module_names):
            call = call_site.call
            if file.path == target_file.path and _ranges_overlap(
                target.start_line,
                target.end_line,
                _python_node_start_line(call),
                _python_node_end_line(call),
            ):
                continue
            if not _python_call_references_symbol(
                call,
                target,
                target_file,
                file,
                call_site.context.bindings,
                call_site.context.instance_types,
            ):
                continue
            text = _python_node_text(file.source, call)
            if not text:
                continue
            key = (file.path, _python_node_start_line(call), text)
            if key in seen:
                continue
            seen.add(key)
            references.append(
                AnalyzerReference(
                    file=file.path,
                    line=_python_node_start_line(call),
                    endLine=_python_node_end_line(call),
                    text=text,
                    kind="call",
                )
            )
            if len(references) >= _constants.PYTHON_REFERENCE_LIMIT:
                return references
    return references


def _python_callees_for_function(
    file: _PythonWorkspaceFile,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    target: AnalyzerSymbol,
    workspace_symbols: list[_PythonIndexedSymbol],
    workspace_module_names: set[str],
) -> list[AnalyzerReference]:
    callees: list[AnalyzerReference] = []
    seen: set[tuple[str, int, str]] = set()
    symbols_by_identity = {
        indexed.identity: indexed
        for indexed in workspace_symbols
        if indexed.symbol.kind in {"function", "method", "class"}
    }
    target_identity = _python_symbol_identity(file.module_name, target.name)
    for call_site in _python_call_sites(file, workspace_module_names):
        call = call_site.call
        if not _ranges_overlap(
            _python_node_start_line(node),
            _python_node_end_line(node),
            _python_node_start_line(call),
            _python_node_end_line(call),
        ):
            continue
        selected = next(
            (
                symbols_by_identity[identity]
                for identity in _python_resolved_call_identities(
                    call,
                    file,
                    call_site.context.bindings,
                    call_site.context.instance_types,
                )
                if identity != target_identity and identity in symbols_by_identity
            ),
            None,
        )
        if selected is None:
            continue
        selected_symbol = selected.symbol
        key = (selected.path, selected_symbol.start_line, selected_symbol.signature)
        if key in seen:
            continue
        seen.add(key)
        callees.append(
            AnalyzerReference(
                file=selected.path,
                line=selected_symbol.start_line,
                endLine=selected_symbol.end_line,
                text=selected_symbol.signature,
                kind="callee",
            )
        )
        if len(callees) >= _constants.PYTHON_CALLEE_LIMIT:
            break
    return callees


def _merge_python_references(
    existing: list[AnalyzerReference],
    additional: list[AnalyzerReference],
) -> list[AnalyzerReference]:
    merged: list[AnalyzerReference] = []
    seen: set[tuple[str, int | None, str, str]] = set()
    for reference in [*existing, *additional]:
        key = (reference.file, reference.line, reference.kind, reference.text)
        if key in seen:
            continue
        seen.add(key)
        merged.append(reference)
    return merged


def _python_call_references_symbol(
    call: ast.Call,
    target: AnalyzerSymbol,
    target_file: _PythonWorkspaceFile,
    file: _PythonWorkspaceFile,
    bindings: _PythonImportBindings,
    instance_types: dict[str, str],
) -> bool:
    target_identity = _python_symbol_identity(target_file.module_name, target.name)
    return target_identity in _python_resolved_call_identities(call, file, bindings, instance_types)
