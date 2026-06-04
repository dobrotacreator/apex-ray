import ast
from typing import Literal

from apex_ray.models import AnalyzerReference, AnalyzerSymbol, ChangedFile, ChangedHunk

from ..common import _collapse_ranges
from .constants import PYTHON_DELETED_SYMBOL_RE
from .utils import (
    _python_node_end_line,
    _python_node_start_line,
    _python_node_text,
    _qualified_python_name,
    _ranges_overlap,
)


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
