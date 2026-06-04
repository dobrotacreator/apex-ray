import ast
import os
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apex_ray.models import (
    AnalyzerConfig,
    AnalyzerFile,
    AnalyzerReference,
    AnalyzerResult,
    AnalyzerSymbol,
    ChangedFile,
    ChangedHunk,
    FileKind,
)

from .common import _collapse_ranges

PYTHON_LANGUAGES = {"python"}
PYTHON_READ_ERRORS = (OSError, UnicodeDecodeError, SyntaxError)
PYTHON_RELATED_TEST_LIMIT = 10
PYTHON_REFERENCE_LIMIT = 24
PYTHON_CALLEE_LIMIT = 16
PYTHON_WORKSPACE_FILE_LIMIT = 4000
PYTHON_WORKSPACE_FILE_SIZE_LIMIT = 1_000_000
PYTHON_LITERAL_ANNOTATION_HEADS = {"Literal", "typing.Literal", "typing_extensions.Literal"}
PYTHON_ANNOTATED_ANNOTATION_HEADS = {"Annotated", "typing.Annotated", "typing_extensions.Annotated"}
PYTHON_TYPE_CHECKING_IDENTITIES = {"TYPE_CHECKING", "typing.TYPE_CHECKING"}
PYTHON_SCAN_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "site-packages",
}
PYTHON_DELETED_SYMBOL_RE = re.compile(
    r"^(?P<indent>\s*)(?P<definition_kind>async\s+def|def|class)\s+(?P<name>[A-Za-z_]\w*)"
)


@dataclass(frozen=True, slots=True)
class _PythonWorkspaceFile:
    path: str
    module_name: str
    source: str
    module: ast.Module
    symbols: list[AnalyzerSymbol]


@dataclass(frozen=True, slots=True)
class _PythonWorkspaceScan:
    files: dict[str, _PythonWorkspaceFile]
    warnings: list[str]
    partial: bool


@dataclass(frozen=True, slots=True)
class _PythonIndexedSymbol:
    path: str
    module_name: str
    identity: str
    symbol: AnalyzerSymbol


def has_python_changes(files: list[ChangedFile]) -> bool:
    return bool(python_changed_files(files))


def python_changed_files(files: list[ChangedFile]) -> list[ChangedFile]:
    return [
        file
        for file in files
        if file.language in PYTHON_LANGUAGES
        and file.file_kind in {FileKind.SOURCE, FileKind.TEST}
        and not file.is_ignored
        and file.new_path is not None
    ]


def run_python_analyzer(
    repo_root: Path,
    files: list[ChangedFile],
    config: AnalyzerConfig | None = None,
) -> AnalyzerResult | None:
    changed_files = python_changed_files(files)
    if not changed_files:
        return None

    analyzed_files: list[AnalyzerFile] = []
    warnings: list[str] = []
    failed_files: list[str] = []
    workspace_scan = _build_python_workspace(repo_root)
    workspace = workspace_scan.files
    warnings.extend(workspace_scan.warnings)

    for changed_file in changed_files:
        path = changed_file.path
        workspace_file = workspace.get(path)
        if workspace_file is None:
            source_path = _resolve_python_repo_path(repo_root, path)
            if source_path is None:
                warnings.append(f"Unsafe Python file path {path}; using diff-only fallback context.")
                failed_files.append(path)
                continue
            try:
                source = _read_python_source(source_path)
            except PYTHON_READ_ERRORS as exc:
                warnings.append(f"Unable to read Python file {path}: {exc}")
                failed_files.append(path)
                continue

            try:
                module = ast.parse(source, filename=path)
            except SyntaxError as exc:
                location = f" at line {exc.lineno}" if exc.lineno else ""
                warnings.append(f"Unable to parse Python file {path}{location}: {exc.msg}")
                failed_files.append(path)
                continue

            symbols = _collect_python_symbols(path, source, module)
            workspace_file = _PythonWorkspaceFile(
                path=path,
                module_name=_python_module_name(path),
                source=source,
                module=module,
                symbols=symbols,
            )
            workspace[path] = workspace_file
        elif _resolve_python_repo_path(repo_root, path) is None:
            warnings.append(f"Unsafe Python file path {path}; using diff-only fallback context.")
            failed_files.append(path)
            continue

        source = workspace_file.source
        module = workspace_file.module
        symbols = workspace_file.symbols
        changed_symbols = _changed_python_symbols(changed_file, symbols)
        _populate_python_symbol_graph(workspace_file, changed_symbols, workspace)
        related_tests = _python_related_tests(repo_root, changed_file, symbols)
        analyzed_files.append(
            AnalyzerFile(
                path=path,
                tsconfigPath=None,
                symbols=symbols,
                imports=_python_imports(source, module),
                exports=_python_exports(module, symbols),
                relatedTests=related_tests,
                changedSymbols=changed_symbols,
            )
        )

    return AnalyzerResult(
        language="python",
        projectRoot=str(repo_root),
        tsconfigPath=None,
        files=analyzed_files,
        warnings=warnings,
        indexCache=None,
        partial=bool(failed_files) or workspace_scan.partial,
        failedFiles=failed_files,
        shardFailures=[],
    )


def _resolve_python_repo_path(repo_root: Path, rel_path: str) -> Path | None:
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


def _read_python_source(path: Path) -> str:
    with tokenize.open(path) as source_file:
        return source_file.read()


def _build_python_workspace(repo_root: Path) -> _PythonWorkspaceScan:
    workspace: dict[str, _PythonWorkspaceFile] = {}
    warnings: list[str] = []
    partial = False
    for index, path in enumerate(_iter_python_files(repo_root)):
        if index >= PYTHON_WORKSPACE_FILE_LIMIT:
            warnings.append(
                f"Python workspace scan reached file limit ({PYTHON_WORKSPACE_FILE_LIMIT}); "
                "reference context may be incomplete."
            )
            partial = True
            break
        source_path = _resolve_python_repo_path(repo_root, path)
        if source_path is None:
            continue
        try:
            source_size = source_path.stat().st_size
        except OSError:
            continue
        if source_size > PYTHON_WORKSPACE_FILE_SIZE_LIMIT:
            warnings.append(
                f"Skipping Python workspace file {path}: size {source_size} bytes exceeds "
                f"limit {PYTHON_WORKSPACE_FILE_SIZE_LIMIT} bytes; reference context may be incomplete."
            )
            partial = True
            continue
        try:
            source = _read_python_source(source_path)
            module = ast.parse(source, filename=path)
        except PYTHON_READ_ERRORS:
            continue
        workspace[path] = _PythonWorkspaceFile(
            path=path,
            module_name=_python_module_name(path),
            source=source,
            module=module,
            symbols=_collect_python_symbols(path, source, module),
        )
    return _PythonWorkspaceScan(files=workspace, warnings=warnings, partial=partial)


def _collect_python_symbols(path: str, source: str, module: ast.Module) -> list[AnalyzerSymbol]:
    lines = source.splitlines()
    return sorted(
        _collect_python_body_symbols(
            path,
            source,
            lines,
            module.body,
            parent_name=None,
            parent_exported=True,
            scope_kind="module",
        ),
        key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name),
    )


def _collect_python_body_symbols(
    path: str,
    source: str,
    lines: list[str],
    body: list[ast.stmt],
    *,
    parent_name: str | None,
    parent_exported: bool,
    scope_kind: Literal["module", "class", "function"],
) -> list[AnalyzerSymbol]:
    symbols: list[AnalyzerSymbol] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            name = _qualified_python_name(parent_name, node.name)
            exported = scope_kind != "function" and parent_exported and not node.name.startswith("_")
            class_symbol = _python_class_symbol(path, source, lines, node, name=name, exported=exported)
            symbols.append(class_symbol)
            symbols.extend(
                _collect_python_body_symbols(
                    path,
                    source,
                    lines,
                    node.body,
                    parent_name=name,
                    parent_exported=exported,
                    scope_kind="class",
                )
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            name = _qualified_python_name(parent_name, node.name)
            exported = (
                not node.name.startswith("_")
                if scope_kind == "module"
                else scope_kind == "class" and parent_exported and not node.name.startswith("_")
            )
            symbols.append(
                _python_function_symbol(
                    path,
                    source,
                    lines,
                    node,
                    name=name,
                    exported=exported,
                    kind="method" if scope_kind == "class" else "function",
                )
            )
            symbols.extend(
                _collect_python_body_symbols(
                    path,
                    source,
                    lines,
                    node.body,
                    parent_name=name,
                    parent_exported=False,
                    scope_kind="function",
                )
            )
        elif scope_kind == "module" and isinstance(node, ast.Assign | ast.AnnAssign):
            symbols.extend(_python_assignment_symbols(path, source, lines, node))
    return symbols


def _qualified_python_name(parent_name: str | None, name: str) -> str:
    return f"{parent_name}.{name}" if parent_name else name


def _python_function_symbol(
    path: str,
    source: str,
    lines: list[str],
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    name: str,
    exported: bool,
    kind: str,
) -> AnalyzerSymbol:
    return AnalyzerSymbol(
        name=name,
        kind=kind,
        startLine=_python_node_start_line(node),
        endLine=_python_node_end_line(node),
        exported=exported,
        signature=_python_signature(source, lines, node),
        references=[],
        callees=[],
        contracts=[],
        metadata=_python_decorator_references(path, source, node.decorator_list),
    )


def _python_class_symbol(
    path: str,
    source: str,
    lines: list[str],
    node: ast.ClassDef,
    *,
    name: str,
    exported: bool,
) -> AnalyzerSymbol:
    return AnalyzerSymbol(
        name=name,
        kind="class",
        startLine=_python_node_start_line(node),
        endLine=_python_node_end_line(node),
        exported=exported,
        signature=_python_signature(source, lines, node),
        references=[],
        callees=[],
        contracts=_python_base_references(path, source, node.bases),
        metadata=_python_decorator_references(path, source, node.decorator_list),
    )


def _python_assignment_symbols(
    path: str,
    source: str,
    lines: list[str],
    node: ast.Assign | ast.AnnAssign,
) -> list[AnalyzerSymbol]:
    names = _python_assignment_target_names(node)
    if not names:
        return []
    return [
        AnalyzerSymbol(
            name=name,
            kind="variable",
            startLine=_python_node_start_line(node),
            endLine=_python_node_end_line(node),
            exported=not name.startswith("_"),
            signature=_python_signature(source, lines, node),
            references=[],
            callees=[],
            contracts=_python_annotation_references(path, source, node),
            metadata=[],
        )
        for name in names
    ]


def _python_assignment_target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        names.extend(_python_target_names(target))
    return names


def _python_target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        names: list[str] = []
        for element in target.elts:
            names.extend(_python_target_names(element))
        return names
    return []


def _changed_python_symbols(changed_file: ChangedFile, symbols: list[AnalyzerSymbol]) -> list[AnalyzerSymbol]:
    changed_ranges = _changed_python_line_ranges(changed_file)
    changed_symbols = [
        symbol
        for symbol in symbols
        if any(_ranges_overlap(symbol.start_line, symbol.end_line, start, end) for start, end in changed_ranges)
    ]
    return _dedupe_python_symbols([*_deleted_python_symbols(changed_file), *changed_symbols])


def _dedupe_python_symbols(symbols: list[AnalyzerSymbol]) -> list[AnalyzerSymbol]:
    seen: set[tuple[str, int, str]] = set()
    deduped: list[AnalyzerSymbol] = []
    for symbol in symbols:
        key = (symbol.name, symbol.start_line, symbol.signature)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(symbol)
    return deduped


def _changed_python_line_ranges(file: ChangedFile) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for hunk in file.hunks:
        added_lines = sorted(line.new_line for line in hunk.lines if line.new_line is not None and line.kind == "add")
        if added_lines:
            ranges.extend(_collapse_ranges(added_lines))
        elif not _deletes_python_symbol_definition(hunk):
            ranges.append((hunk.new_start, hunk.new_start))
    return ranges


def _deletes_python_symbol_definition(hunk: ChangedHunk) -> bool:
    return any(line.kind == "delete" and PYTHON_DELETED_SYMBOL_RE.match(line.content) for line in hunk.lines)


def _deleted_python_symbols(changed_file: ChangedFile) -> list[AnalyzerSymbol]:
    symbols: list[AnalyzerSymbol] = []
    for hunk in changed_file.hunks:
        class_context: list[tuple[int, str]] = []
        line_number = max(1, hunk.new_start)
        for line in hunk.lines:
            match = PYTHON_DELETED_SYMBOL_RE.match(line.content)
            if line.kind != "delete":
                _update_deleted_python_class_context(line.content, class_context)
                if line.new_line is not None:
                    line_number = line.new_line + 1
                continue
            if not match:
                continue
            stripped = line.content.strip()
            raw_name = match.group("name")
            definition_kind = match.group("definition_kind")
            indent = _python_line_indent(line.content)
            parent_class = _deleted_python_parent_class(class_context, indent)
            if definition_kind == "class":
                name = raw_name
                kind = "class"
            elif parent_class:
                name = f"{parent_class}.{raw_name}"
                kind = "method"
            else:
                name = raw_name
                kind = "function"
            symbols.append(
                AnalyzerSymbol(
                    name=name,
                    kind=kind,
                    startLine=line_number,
                    endLine=line_number,
                    exported=not raw_name.startswith("_"),
                    signature=f"removed Python {kind}: {stripped}",
                    references=[],
                    callees=[],
                    contracts=[],
                    metadata=[],
                )
            )
            _update_deleted_python_class_context(line.content, class_context)
    return symbols


def _update_deleted_python_class_context(content: str, class_context: list[tuple[int, str]]) -> None:
    if not content.strip():
        return
    indent = _python_line_indent(content)
    while class_context and indent <= class_context[-1][0]:
        class_context.pop()
    match = PYTHON_DELETED_SYMBOL_RE.match(content)
    if match is None or match.group("definition_kind") != "class":
        return
    raw_name = match.group("name")
    parent_name = class_context[-1][1] if class_context else None
    class_context.append((indent, _qualified_python_name(parent_name, raw_name)))


def _deleted_python_parent_class(class_context: list[tuple[int, str]], indent: int) -> str | None:
    for class_indent, class_name in reversed(class_context):
        if class_indent < indent:
            return class_name
    return None


def _python_line_indent(content: str) -> int:
    return len(content) - len(content.lstrip())


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


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
            if len(references) >= PYTHON_REFERENCE_LIMIT:
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
        if len(callees) >= PYTHON_CALLEE_LIMIT:
            break
    return callees


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


@dataclass(frozen=True, slots=True)
class _PythonImportBindings:
    direct_imports: dict[str, set[str]]
    module_imports: dict[str, str]


@dataclass(frozen=True, slots=True)
class _PythonCallContext:
    bindings: _PythonImportBindings
    instance_types: dict[str, str]


@dataclass(frozen=True, slots=True)
class _PythonCallSite:
    call: ast.Call
    context: _PythonCallContext


@dataclass(frozen=True, slots=True)
class _PythonAnnotationContext:
    bindings: _PythonImportBindings
    scope_prefixes: list[str]


def _python_call_sites(file: _PythonWorkspaceFile, workspace_module_names: set[str]) -> list[_PythonCallSite]:
    call_sites: list[_PythonCallSite] = []
    _visit_python_body(
        file.module.body,
        file,
        workspace_module_names,
        _empty_python_call_context(),
        call_sites,
    )
    return call_sites


def _visit_python_body(
    body: list[ast.stmt],
    file: _PythonWorkspaceFile,
    workspace_module_names: set[str],
    context: _PythonCallContext,
    call_sites: list[_PythonCallSite],
) -> None:
    for statement in body:
        _visit_python_statement(statement, file, workspace_module_names, context, call_sites)


def _visit_python_statement(
    statement: ast.stmt,
    file: _PythonWorkspaceFile,
    workspace_module_names: set[str],
    context: _PythonCallContext,
    call_sites: list[_PythonCallSite],
) -> None:
    if isinstance(statement, ast.Import | ast.ImportFrom):
        _apply_python_import_statement(context, file, workspace_module_names, statement)
    elif isinstance(statement, ast.Assign):
        _visit_python_expression(statement.value, context, call_sites)
        _assign_python_instance_types(statement.targets, statement.value, file, context)
    elif isinstance(statement, ast.AnnAssign):
        _visit_python_expression(statement.annotation, context, call_sites)
        if statement.value is not None:
            _visit_python_expression(statement.value, context, call_sites)
        _assign_python_instance_types([statement.target], statement.value, file, context)
    elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
        _visit_python_function_definition(statement, file, workspace_module_names, context, call_sites)
    elif isinstance(statement, ast.ClassDef):
        _visit_python_class_definition(statement, file, workspace_module_names, context, call_sites)
    elif isinstance(statement, ast.Return):
        if statement.value is not None:
            _visit_python_expression(statement.value, context, call_sites)
    elif isinstance(statement, ast.Expr):
        _visit_python_expression(statement.value, context, call_sites)
    elif isinstance(statement, ast.If):
        _visit_python_expression(statement.test, context, call_sites)
        _visit_python_body(
            statement.body,
            file,
            workspace_module_names,
            _copy_python_call_context(context),
            call_sites,
        )
        _visit_python_body(
            statement.orelse,
            file,
            workspace_module_names,
            _copy_python_call_context(context),
            call_sites,
        )
    elif isinstance(statement, ast.For | ast.AsyncFor):
        _visit_python_expression(statement.iter, context, call_sites)
        body_context = _copy_python_call_context(context)
        _python_shadow_names(body_context, _python_target_names(statement.target))
        _visit_python_body(statement.body, file, workspace_module_names, body_context, call_sites)
        _visit_python_body(
            statement.orelse,
            file,
            workspace_module_names,
            _copy_python_call_context(context),
            call_sites,
        )
    elif isinstance(statement, ast.With | ast.AsyncWith):
        body_context = _copy_python_call_context(context)
        for item in statement.items:
            _visit_python_expression(item.context_expr, context, call_sites)
            if item.optional_vars is not None:
                _python_shadow_names(body_context, _python_target_names(item.optional_vars))
        _visit_python_body(statement.body, file, workspace_module_names, body_context, call_sites)
    elif isinstance(statement, ast.Try):
        _visit_python_body(
            statement.body,
            file,
            workspace_module_names,
            _copy_python_call_context(context),
            call_sites,
        )
        for handler in statement.handlers:
            handler_context = _copy_python_call_context(context)
            if handler.type is not None:
                _visit_python_expression(handler.type, context, call_sites)
            if handler.name:
                _python_shadow_names(handler_context, [handler.name])
            _visit_python_body(handler.body, file, workspace_module_names, handler_context, call_sites)
        _visit_python_body(
            statement.orelse,
            file,
            workspace_module_names,
            _copy_python_call_context(context),
            call_sites,
        )
        _visit_python_body(
            statement.finalbody,
            file,
            workspace_module_names,
            _copy_python_call_context(context),
            call_sites,
        )
    elif isinstance(statement, ast.Match):
        _visit_python_expression(statement.subject, context, call_sites)
        for case in statement.cases:
            _visit_python_body(
                case.body,
                file,
                workspace_module_names,
                _copy_python_call_context(context),
                call_sites,
            )
    elif isinstance(statement, ast.AugAssign):
        _visit_python_expression(statement.value, context, call_sites)
        _python_shadow_names(context, _python_target_names(statement.target))
    else:
        _visit_unknown_python_statement(statement, file, workspace_module_names, context, call_sites)


def _visit_python_expression(
    expression: ast.expr,
    context: _PythonCallContext,
    call_sites: list[_PythonCallSite],
) -> None:
    for node in ast.walk(expression):
        if isinstance(node, ast.Call):
            call_sites.append(_PythonCallSite(call=node, context=_copy_python_call_context(context)))


def _visit_python_function_definition(
    statement: ast.FunctionDef | ast.AsyncFunctionDef,
    file: _PythonWorkspaceFile,
    workspace_module_names: set[str],
    context: _PythonCallContext,
    call_sites: list[_PythonCallSite],
) -> None:
    for decorator in statement.decorator_list:
        _visit_python_expression(decorator, context, call_sites)
    for default in [*statement.args.defaults, *[default for default in statement.args.kw_defaults if default]]:
        _visit_python_expression(default, context, call_sites)
    if statement.returns is not None:
        _visit_python_expression(statement.returns, context, call_sites)

    function_context = _copy_python_call_context(context)
    _python_shadow_names(function_context, _python_argument_names(statement.args))
    _visit_python_body(statement.body, file, workspace_module_names, function_context, call_sites)
    _python_shadow_names(context, [statement.name])


def _visit_python_class_definition(
    statement: ast.ClassDef,
    file: _PythonWorkspaceFile,
    workspace_module_names: set[str],
    context: _PythonCallContext,
    call_sites: list[_PythonCallSite],
) -> None:
    for decorator in statement.decorator_list:
        _visit_python_expression(decorator, context, call_sites)
    for base in statement.bases:
        _visit_python_expression(base, context, call_sites)
    for keyword in statement.keywords:
        _visit_python_expression(keyword.value, context, call_sites)

    class_context = _copy_python_call_context(context)
    _visit_python_body(statement.body, file, workspace_module_names, class_context, call_sites)
    _python_shadow_names(context, [statement.name])


def _visit_unknown_python_statement(
    statement: ast.stmt,
    file: _PythonWorkspaceFile,
    workspace_module_names: set[str],
    context: _PythonCallContext,
    call_sites: list[_PythonCallSite],
) -> None:
    for child in ast.iter_child_nodes(statement):
        if isinstance(child, ast.expr):
            _visit_python_expression(child, context, call_sites)
        elif isinstance(child, ast.stmt):
            _visit_python_statement(child, file, workspace_module_names, context, call_sites)


def _assign_python_instance_types(
    targets: list[ast.expr],
    value: ast.expr | None,
    file: _PythonWorkspaceFile,
    context: _PythonCallContext,
) -> None:
    target_names = [name for target in targets for name in _python_target_names(target)]
    if not target_names:
        return
    constructor_identities = (
        _python_resolved_call_identities(value, file, context.bindings, context.instance_types)
        if isinstance(value, ast.Call)
        else []
    )
    _python_shadow_names(context, target_names)
    if not constructor_identities:
        return
    resolved_class = constructor_identities[0]
    for target_name in target_names:
        context.instance_types[target_name] = resolved_class


def _apply_python_import_statement(
    context: _PythonCallContext,
    file: _PythonWorkspaceFile,
    workspace_module_names: set[str],
    statement: ast.Import | ast.ImportFrom,
) -> None:
    if isinstance(statement, ast.ImportFrom):
        module_name = _python_import_from_module_name(file, statement)
        if module_name is None:
            return
        for alias in statement.names:
            if alias.name == "*":
                continue
            local_name = alias.asname or alias.name
            imported_module_name = _python_imported_module_name(module_name, alias.name)
            if imported_module_name in workspace_module_names:
                _python_bind_module_import(context, local_name, imported_module_name)
            else:
                _python_bind_direct_import(context, local_name, _python_symbol_identity(module_name, alias.name))
    else:
        for alias in statement.names:
            local_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
            _python_bind_module_import(context, local_name, alias.name)


def _python_bind_direct_import(context: _PythonCallContext, local_name: str, identity: str) -> None:
    _python_shadow_names(context, [local_name])
    context.bindings.direct_imports[local_name] = {identity}


def _python_bind_module_import(context: _PythonCallContext, local_name: str, module_name: str) -> None:
    _python_shadow_names(context, [local_name])
    context.bindings.module_imports[local_name] = module_name


def _python_shadow_names(context: _PythonCallContext, names: list[str]) -> None:
    for name in names:
        context.bindings.direct_imports.pop(name, None)
        context.bindings.module_imports.pop(name, None)
        context.instance_types.pop(name, None)


def _python_argument_names(arguments: ast.arguments) -> list[str]:
    names = [
        argument.arg
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
    ]
    if arguments.vararg is not None:
        names.append(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.append(arguments.kwarg.arg)
    return names


def _empty_python_call_context() -> _PythonCallContext:
    return _PythonCallContext(
        bindings=_PythonImportBindings(direct_imports={}, module_imports={}),
        instance_types={},
    )


def _copy_python_call_context(context: _PythonCallContext) -> _PythonCallContext:
    return _PythonCallContext(
        bindings=_PythonImportBindings(
            direct_imports={name: set(identities) for name, identities in context.bindings.direct_imports.items()},
            module_imports=dict(context.bindings.module_imports),
        ),
        instance_types=dict(context.instance_types),
    )


def _python_resolved_call_identities(
    call: ast.Call,
    file: _PythonWorkspaceFile,
    bindings: _PythonImportBindings,
    instance_types: dict[str, str],
) -> list[str]:
    identities: list[str] = []
    if isinstance(call.func, ast.Name):
        local_name = call.func.id
        for imported_identity in sorted(bindings.direct_imports.get(local_name, set())):
            _append_unique(identities, imported_identity)
        _append_unique(identities, _python_symbol_identity(file.module_name, local_name))
    elif isinstance(call.func, ast.Attribute):
        value = call.func.value
        value_name = _python_attribute_name(value)
        normalized_value_name = _python_normalized_attribute_name(value, bindings)
        if normalized_value_name:
            _append_unique(identities, f"{normalized_value_name}.{call.func.attr}")
        if value_name:
            _append_unique(identities, f"{value_name}.{call.func.attr}")
            _append_unique(identities, _python_symbol_identity(file.module_name, f"{value_name}.{call.func.attr}"))
        if isinstance(value, ast.Name):
            for imported_identity in sorted(bindings.direct_imports.get(value.id, set())):
                _append_unique(identities, f"{imported_identity}.{call.func.attr}")
            instance_identity = instance_types.get(value.id)
            if instance_identity:
                _append_unique(identities, f"{instance_identity}.{call.func.attr}")
    return identities


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _python_normalized_attribute_name(node: ast.AST, bindings: _PythonImportBindings) -> str:
    if isinstance(node, ast.Name):
        return bindings.module_imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _python_normalized_attribute_name(node.value, bindings)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _python_import_from_module_name(file: _PythonWorkspaceFile, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module or ""

    package_name = _python_package_name(file.path, file.module_name)
    package_parts = package_name.split(".") if package_name else []
    ancestor_count = node.level - 1
    if ancestor_count > len(package_parts):
        return None

    base_parts = package_parts[: len(package_parts) - ancestor_count]
    if node.module:
        base_parts.extend(part for part in node.module.split(".") if part)
    return ".".join(base_parts)


def _python_package_name(path: str, module_name: str) -> str:
    if Path(path).name == "__init__.py":
        return module_name
    if "." not in module_name:
        return ""
    return module_name.rsplit(".", maxsplit=1)[0]


def _python_imported_module_name(module_name: str, imported_name: str) -> str:
    return f"{module_name}.{imported_name}" if module_name else imported_name


def _python_symbol_identity(module_name: str, symbol_name: str) -> str:
    return f"{module_name}.{symbol_name}" if module_name else symbol_name


def _python_attribute_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _python_attribute_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _python_symbol_nodes(module: ast.Module) -> dict[str, ast.AST]:
    nodes: dict[str, ast.AST] = {}

    def visit_body(
        body: list[ast.stmt], parent_name: str | None, scope_kind: Literal["module", "class", "function"]
    ) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                name = _qualified_python_name(parent_name, node.name)
                nodes[name] = node
                visit_body(node.body, name, "class")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                name = _qualified_python_name(parent_name, node.name)
                nodes[name] = node
                visit_body(node.body, name, "function")
            elif scope_kind == "module" and isinstance(node, ast.Assign | ast.AnnAssign):
                for name in _python_assignment_target_names(node):
                    nodes[name] = node

    visit_body(module.body, None, "module")
    return nodes


def _python_imports(source: str, module: ast.Module) -> list[str]:
    imports: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        text = _python_node_text(source, node)
        if text and text not in seen:
            seen.add(text)
            imports.append(text)
    return imports


def _python_exports(module: ast.Module, symbols: list[AnalyzerSymbol]) -> list[str]:
    explicit_exports = _python_dunder_all(module)
    if explicit_exports is not None:
        return explicit_exports
    return sorted(symbol.name for symbol in symbols if symbol.exported and "." not in symbol.name)


def _python_dunder_all(module: ast.Module) -> list[str] | None:
    for node in module.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__":
            value = node.value
        if value is None:
            continue
        try:
            parsed = ast.literal_eval(value)
        except ValueError:
            continue
        except SyntaxError:
            continue
        if isinstance(parsed, list | tuple) and all(isinstance(item, str) for item in parsed):
            return sorted(parsed)
    return None


def _python_related_tests(repo_root: Path, changed_file: ChangedFile, symbols: list[AnalyzerSymbol]) -> list[str]:
    if changed_file.file_kind == FileKind.TEST:
        return []

    changed_path = changed_file.path
    module_name = _python_module_name(changed_path)
    short_module_name = module_name.removeprefix("src.") if module_name.startswith("src.") else module_name
    stem = Path(changed_path).stem
    symbol_names = {symbol.name.split(".")[-1] for symbol in symbols}
    candidates: list[tuple[int, str]] = []

    for test_path in _iter_python_test_files(repo_root):
        if test_path == changed_path:
            continue
        score = _python_related_test_score(repo_root, test_path, stem, module_name, short_module_name, symbol_names)
        if score:
            candidates.append((score, test_path))

    return [path for _, path in sorted(candidates, key=lambda item: (-item[0], item[1]))[:PYTHON_RELATED_TEST_LIMIT]]


def _python_related_test_score(
    repo_root: Path,
    test_path: str,
    stem: str,
    module_name: str,
    short_module_name: str,
    symbol_names: set[str],
) -> int:
    score = 0
    test_name = Path(test_path).name
    if test_name in {f"test_{stem}.py", f"{stem}_test.py"} or f"/{stem}/" in f"/{test_path}/":
        score += 40

    source_path = _resolve_python_repo_path(repo_root, test_path)
    if source_path is None:
        return 0

    try:
        text = _read_python_source(source_path)
    except PYTHON_READ_ERRORS:
        return score

    import_needles = {
        f"from {module_name} import",
        f"import {module_name}",
        f"from {short_module_name} import",
        f"import {short_module_name}",
    }
    if any(needle in text for needle in import_needles):
        score += 60
    if stem in text:
        score += 10
    if any(name and name in text for name in symbol_names):
        score += 20
    return score


def _iter_python_test_files(repo_root: Path) -> list[str]:
    return [path for path in _iter_python_files(repo_root) if _is_python_test_path(path)]


def _iter_python_files(repo_root: Path) -> list[str]:
    paths: list[str] = []
    for current_root, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in PYTHON_SCAN_IGNORED_DIRS]
        current_path = Path(current_root)
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = current_path / filename
            rel_path = path.relative_to(repo_root).as_posix()
            paths.append(rel_path)
    return sorted(paths)


def _is_python_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name
    return "tests" in parts or "test" in parts or name.startswith("test_") or name.endswith("_test.py")


def _python_module_name(path: str) -> str:
    without_suffix = path.removesuffix(".py").replace("/", ".")
    if without_suffix.endswith(".__init__"):
        without_suffix = without_suffix.removesuffix(".__init__")
    if without_suffix.startswith("src."):
        return without_suffix.removeprefix("src.")
    return without_suffix


def _python_decorator_references(path: str, source: str, decorators: list[ast.expr]) -> list[AnalyzerReference]:
    references: list[AnalyzerReference] = []
    for decorator in decorators:
        text = _python_node_text(source, decorator)
        if not text:
            continue
        references.append(
            AnalyzerReference(
                file=path,
                line=_python_node_start_line(decorator),
                endLine=_python_node_end_line(decorator),
                text=f"@{text}",
                kind="metadata",
            )
        )
    return references


def _python_base_references(path: str, source: str, bases: list[ast.expr]) -> list[AnalyzerReference]:
    references: list[AnalyzerReference] = []
    for base in bases:
        text = _python_node_text(source, base)
        if not text:
            continue
        references.append(
            AnalyzerReference(
                file=path,
                line=_python_node_start_line(base),
                endLine=_python_node_end_line(base),
                text=text,
                kind="contract",
            )
        )
    return references


def _python_annotation_references(path: str, source: str, node: ast.Assign | ast.AnnAssign) -> list[AnalyzerReference]:
    if not isinstance(node, ast.AnnAssign):
        return []
    text = _python_node_text(source, node.annotation)
    if not text:
        return []
    return [
        AnalyzerReference(
            file=path,
            line=_python_node_start_line(node.annotation),
            endLine=_python_node_end_line(node.annotation),
            text=text,
            kind="contract",
        )
    ]


def _python_signature(
    source: str,
    lines: list[str],
    node: ast.AST,
) -> str:
    segment = _python_node_text(source, node)
    if segment:
        first_line = segment.splitlines()[0].strip()
        return first_line.removesuffix(":")
    line_number = _python_node_start_line(node)
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1].strip().removesuffix(":")
    return ""


def _python_node_text(source: str, node: ast.AST) -> str:
    return (ast.get_source_segment(source, node) or "").strip()


def _python_node_start_line(node: ast.AST) -> int:
    lines = [getattr(node, "lineno", 1)]
    if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        lines.extend(getattr(decorator, "lineno", getattr(node, "lineno", 1)) for decorator in node.decorator_list)
    return min(line for line in lines if line is not None)


def _python_node_end_line(node: ast.AST) -> int:
    return getattr(node, "end_lineno", getattr(node, "lineno", 1)) or getattr(node, "lineno", 1)
