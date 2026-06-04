import ast

from .bindings import (
    _apply_python_import_statement,
    _copy_python_call_context,
    _empty_python_call_context,
    _python_attribute_name,
    _python_normalized_attribute_name,
    _python_shadow_names,
)
from .state import _PythonCallContext, _PythonCallSite, _PythonImportBindings, _PythonWorkspaceFile
from .symbols import _python_target_names
from .utils import _append_unique, _python_symbol_identity


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
