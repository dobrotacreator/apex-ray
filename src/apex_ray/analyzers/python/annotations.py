import ast
from typing import Literal

from apex_ray.models import AnalyzerReference, AnalyzerSymbol

from .bindings import (
    _apply_python_import_statement,
    _empty_python_call_context,
    _python_attribute_name,
    _python_normalized_attribute_name,
    _python_shadow_names,
)
from .constants import (
    PYTHON_ANNOTATED_ANNOTATION_HEADS,
    PYTHON_LITERAL_ANNOTATION_HEADS,
    PYTHON_TYPE_CHECKING_IDENTITIES,
)
from .state import (
    _PythonAnnotationContext,
    _PythonCallContext,
    _PythonImportBindings,
    _PythonIndexedSymbol,
    _PythonWorkspaceFile,
)
from .symbols import _python_assignment_target_names
from .utils import _append_unique, _python_symbol_identity, _qualified_python_name


def _python_annotation_contracts_for_function(
    file: _PythonWorkspaceFile,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    symbol: AnalyzerSymbol,
    workspace_symbols: list[_PythonIndexedSymbol],
    workspace_module_names: set[str],
) -> list[AnalyzerReference]:
    annotation_nodes = _python_function_annotation_nodes(node)
    if not annotation_nodes:
        return []

    context = _python_annotation_context_for_function(file, node, symbol.name, workspace_module_names)
    symbols_by_identity = {
        indexed.identity: indexed
        for indexed in workspace_symbols
        if indexed.symbol.kind in {"class", "function", "variable"}
    }
    contracts: list[AnalyzerReference] = []
    seen: set[tuple[str, int, str]] = set()
    for annotation in annotation_nodes:
        for identity in _python_annotation_identities(annotation, file, context):
            selected = symbols_by_identity.get(identity)
            if selected is None:
                continue
            selected_symbol = selected.symbol
            key = (selected.path, selected_symbol.start_line, selected_symbol.signature)
            if key in seen:
                continue
            seen.add(key)
            contracts.append(
                AnalyzerReference(
                    file=selected.path,
                    line=selected_symbol.start_line,
                    endLine=selected_symbol.end_line,
                    text=selected_symbol.signature,
                    kind="contract",
                )
            )
    return contracts


def _python_function_annotation_nodes(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    annotations = [
        argument.annotation
        for argument in [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if argument.annotation is not None
    ]
    if node.args.vararg is not None and node.args.vararg.annotation is not None:
        annotations.append(node.args.vararg.annotation)
    if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
        annotations.append(node.args.kwarg.annotation)
    if node.returns is not None:
        annotations.append(node.returns)
    return annotations


def _python_annotation_identities(
    annotation: ast.expr,
    file: _PythonWorkspaceFile,
    context: _PythonAnnotationContext,
) -> list[str]:
    identities: list[str] = []
    for node in _python_annotation_identity_nodes(annotation, context.bindings):
        if isinstance(node, ast.Name):
            imported_identities = sorted(context.bindings.direct_imports.get(node.id, set()))
            if imported_identities:
                for imported_identity in imported_identities:
                    _append_unique(identities, imported_identity)
                continue
            for scoped_identity in _python_scoped_annotation_identities(file, node.id, context.scope_prefixes):
                _append_unique(identities, scoped_identity)
        elif isinstance(node, ast.Attribute):
            normalized = _python_normalized_attribute_name(node, context.bindings)
            if normalized:
                _append_unique(identities, normalized)
            raw_name = _python_attribute_name(node)
            if raw_name:
                _append_unique(identities, raw_name)
                for scoped_identity in _python_scoped_annotation_identities(file, raw_name, context.scope_prefixes):
                    _append_unique(identities, scoped_identity)
    return identities


def _python_annotation_identity_nodes(annotation: ast.expr, bindings: _PythonImportBindings) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    _collect_python_annotation_identity_nodes(annotation, nodes, bindings)
    return nodes


def _collect_python_annotation_identity_nodes(
    annotation: ast.expr,
    nodes: list[ast.AST],
    bindings: _PythonImportBindings,
) -> None:
    if isinstance(annotation, ast.Name | ast.Attribute):
        nodes.append(annotation)
    elif isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            parsed = ast.parse(annotation.value, mode="eval")
        except SyntaxError:
            return
        if isinstance(parsed, ast.Expression):
            _collect_python_annotation_identity_nodes(parsed.body, nodes, bindings)
    elif isinstance(annotation, ast.Subscript):
        head_kind = _python_special_annotation_head_kind(annotation.value, bindings)
        if head_kind == "literal":
            return
        slice_nodes = _python_subscript_slice_nodes(annotation.slice)
        if head_kind == "annotated":
            if slice_nodes:
                _collect_python_annotation_identity_nodes(slice_nodes[0], nodes, bindings)
            return
        _collect_python_annotation_identity_nodes(annotation.value, nodes, bindings)
        for slice_node in slice_nodes:
            _collect_python_annotation_identity_nodes(slice_node, nodes, bindings)
    elif isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        _collect_python_annotation_identity_nodes(annotation.left, nodes, bindings)
        _collect_python_annotation_identity_nodes(annotation.right, nodes, bindings)
    elif isinstance(annotation, ast.Tuple | ast.List):
        for element in annotation.elts:
            _collect_python_annotation_identity_nodes(element, nodes, bindings)
    else:
        for child in ast.iter_child_nodes(annotation):
            if isinstance(child, ast.expr):
                _collect_python_annotation_identity_nodes(child, nodes, bindings)


def _python_special_annotation_head_kind(
    annotation: ast.expr,
    bindings: _PythonImportBindings,
) -> Literal["literal", "annotated"] | None:
    identities = _python_annotation_head_identities(annotation, bindings)
    if any(identity in PYTHON_LITERAL_ANNOTATION_HEADS for identity in identities):
        return "literal"
    if any(identity in PYTHON_ANNOTATED_ANNOTATION_HEADS for identity in identities):
        return "annotated"
    return None


def _python_annotation_head_identities(
    annotation: ast.expr,
    bindings: _PythonImportBindings,
) -> list[str]:
    identities: list[str] = []
    if isinstance(annotation, ast.Name):
        _append_unique(identities, annotation.id)
        for imported_identity in sorted(bindings.direct_imports.get(annotation.id, set())):
            _append_unique(identities, imported_identity)
    elif isinstance(annotation, ast.Attribute):
        normalized = _python_normalized_attribute_name(annotation, bindings)
        if normalized:
            _append_unique(identities, normalized)
        raw_name = _python_attribute_name(annotation)
        if raw_name:
            _append_unique(identities, raw_name)
        if isinstance(annotation.value, ast.Name):
            for imported_identity in sorted(bindings.direct_imports.get(annotation.value.id, set())):
                _append_unique(identities, f"{imported_identity}.{annotation.attr}")
    return identities


def _python_subscript_slice_nodes(slice_node: ast.expr) -> list[ast.expr]:
    if isinstance(slice_node, ast.Tuple):
        return list(slice_node.elts)
    return [slice_node]


def _python_annotation_context_for_function(
    file: _PythonWorkspaceFile,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    symbol_name: str,
    workspace_module_names: set[str],
) -> _PythonAnnotationContext:
    call_context = _empty_python_call_context()
    ancestors = _python_enclosing_scope_nodes(file.module.body, node) or []
    body = file.module.body
    for stop_node in [*ancestors, node]:
        _apply_python_annotation_scope_statements(call_context, file, workspace_module_names, body, stop_node)
        body = stop_node.body if isinstance(stop_node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) else []
    return _PythonAnnotationContext(
        bindings=call_context.bindings,
        scope_prefixes=_python_annotation_scope_prefixes(symbol_name),
    )


def _python_enclosing_scope_nodes(
    body: list[ast.stmt],
    target: ast.AST,
) -> list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef] | None:
    for statement in body:
        if statement is target:
            return []
        if isinstance(statement, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            child_path = _python_enclosing_scope_nodes(statement.body, target)
            if child_path is not None:
                return [statement, *child_path]
    return None


def _apply_python_annotation_scope_statements(
    context: _PythonCallContext,
    file: _PythonWorkspaceFile,
    workspace_module_names: set[str],
    body: list[ast.stmt],
    stop_node: ast.AST,
) -> None:
    for statement in body:
        if statement is stop_node:
            return
        if isinstance(statement, ast.Import | ast.ImportFrom):
            _apply_python_import_statement(context, file, workspace_module_names, statement)
        elif isinstance(statement, ast.If) and _is_python_type_checking_guard(statement.test, context.bindings):
            for guarded_statement in statement.body:
                if isinstance(guarded_statement, ast.Import | ast.ImportFrom):
                    _apply_python_import_statement(context, file, workspace_module_names, guarded_statement)
        else:
            _python_shadow_names(context, _python_statement_bound_names(statement))


def _is_python_type_checking_guard(test: ast.expr, bindings: _PythonImportBindings) -> bool:
    return any(identity in PYTHON_TYPE_CHECKING_IDENTITIES for identity in _python_guard_identities(test, bindings))


def _python_guard_identities(test: ast.expr, bindings: _PythonImportBindings) -> list[str]:
    identities: list[str] = []
    if isinstance(test, ast.Name):
        _append_unique(identities, test.id)
        for imported_identity in sorted(bindings.direct_imports.get(test.id, set())):
            _append_unique(identities, imported_identity)
    elif isinstance(test, ast.Attribute):
        normalized = _python_normalized_attribute_name(test, bindings)
        if normalized:
            _append_unique(identities, normalized)
        raw_name = _python_attribute_name(test)
        if raw_name:
            _append_unique(identities, raw_name)
        if isinstance(test.value, ast.Name):
            for imported_identity in sorted(bindings.direct_imports.get(test.value.id, set())):
                _append_unique(identities, f"{imported_identity}.{test.attr}")
    return identities


def _python_statement_bound_names(statement: ast.stmt) -> list[str]:
    if isinstance(statement, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return [statement.name]
    if isinstance(statement, ast.Assign | ast.AnnAssign):
        return _python_assignment_target_names(statement)
    return []


def _python_annotation_scope_prefixes(symbol_name: str) -> list[str]:
    parts = symbol_name.split(".")
    return [".".join(parts[:end]) for end in range(len(parts) - 1, 0, -1)] + [""]


def _python_scoped_annotation_identities(
    file: _PythonWorkspaceFile,
    symbol_name: str,
    scope_prefixes: list[str],
) -> list[str]:
    identities: list[str] = []
    for scope_prefix in scope_prefixes:
        qualified_name = _qualified_python_name(scope_prefix or None, symbol_name)
        _append_unique(identities, _python_symbol_identity(file.module_name, qualified_name))
    return identities
