import ast


def _qualified_python_name(parent_name: str | None, name: str) -> str:
    return f"{parent_name}.{name}" if parent_name else name


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _python_symbol_identity(module_name: str, symbol_name: str) -> str:
    return f"{module_name}.{symbol_name}" if module_name else symbol_name


def _python_module_name(path: str) -> str:
    without_suffix = path.removesuffix(".py").replace("/", ".")
    if without_suffix.endswith(".__init__"):
        without_suffix = without_suffix.removesuffix(".__init__")
    if without_suffix.startswith("src."):
        return without_suffix.removeprefix("src.")
    return without_suffix


def _python_node_text(source: str, node: ast.AST) -> str:
    return (ast.get_source_segment(source, node) or "").strip()


def _python_node_start_line(node: ast.AST) -> int:
    lines = [getattr(node, "lineno", 1)]
    if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        lines.extend(getattr(decorator, "lineno", getattr(node, "lineno", 1)) for decorator in node.decorator_list)
    return min(line for line in lines if line is not None)


def _python_node_end_line(node: ast.AST) -> int:
    return getattr(node, "end_lineno", getattr(node, "lineno", 1)) or getattr(node, "lineno", 1)
