import ast
from pathlib import Path

from .state import _PythonCallContext, _PythonImportBindings, _PythonWorkspaceFile
from .utils import _python_symbol_identity


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


def _python_attribute_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _python_attribute_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""
