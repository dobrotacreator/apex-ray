import ast

from apex_ray.models import AnalyzerReference

from .bindings import _python_attribute_name, _python_normalized_attribute_name
from .calls import _python_call_sites, _python_resolved_call_identities
from .state import _PythonCallSite, _PythonWorkspaceFile
from .symbols import _python_target_names
from .utils import _python_node_end_line, _python_node_start_line, _python_node_text, _ranges_overlap

_MIGRATION_OPERATIONS = frozenset(
    {
        "add_column",
        "alter_column",
        "batch_alter_table",
        "create_check_constraint",
        "create_exclude_constraint",
        "create_foreign_key",
        "create_index",
        "create_primary_key",
        "create_table",
        "create_unique_constraint",
        "drop_column",
        "drop_constraint",
        "drop_index",
        "drop_table",
        "execute",
        "rename_table",
    }
)
_TRANSACTION_OPERATIONS = frozenset({"begin", "commit", "flush", "rollback"})
_HTTP_OPERATIONS = frozenset({"delete", "get", "patch", "post", "put", "request", "send", "stream"})
_EVENT_OPERATIONS = frozenset({"ack", "commit", "emit", "enqueue", "nack", "publish", "send"})
_FIXTURE_OVERRIDE_OPERATIONS = frozenset(
    {"clear", "delattr", "delenv", "pop", "setattr", "setdefault", "setenv", "update"}
)
_EXTERNAL_CLIENT_MARKERS = frozenset({"client", "http", "requests", "transport", "webhook", "redis", "s3", "cloud"})
_EVENT_RECEIVER_MARKERS = frozenset({"broker", "consumer", "event", "outbox", "producer", "publisher", "queue"})


def _python_boundary_metadata_for_node(
    file: _PythonWorkspaceFile,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    workspace_module_names: set[str],
) -> list[AnalyzerReference]:
    metadata: list[AnalyzerReference] = []
    seen: set[tuple[str, int, str]] = set()
    parent_by_child = _python_parent_map(node)
    call_sites = _python_call_sites(file, workspace_module_names)
    batch_aliases = _python_migration_batch_aliases(node, file, call_sites)
    for call_site in call_sites:
        call = call_site.call
        if not _ranges_overlap(
            _python_node_start_line(node),
            _python_node_end_line(node),
            _python_node_start_line(call),
            _python_node_end_line(call),
        ):
            continue
        label = _python_boundary_call_label(file, call_site, batch_aliases)
        if label is None:
            continue
        text = _python_boundary_call_text(file, call, parent_by_child)
        _append_python_metadata(
            metadata,
            seen,
            AnalyzerReference(
                file=file.path,
                line=_python_node_start_line(call),
                endLine=_python_node_end_line(call),
                text=f"{label}: {text}",
                kind="metadata",
            ),
        )

    for assignment in _python_boundary_assignments(node):
        text = _python_node_text(file.source, assignment)
        if not text:
            continue
        _append_python_metadata(
            metadata,
            seen,
            AnalyzerReference(
                file=file.path,
                line=_python_node_start_line(assignment),
                endLine=_python_node_end_line(assignment),
                text=f"test fixture override: {text}",
                kind="metadata",
            ),
        )

    for consumer in _python_pytest_fixture_consumers(file, node):
        _append_python_metadata(metadata, seen, consumer)
    return metadata


def _python_parent_map(node: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}


def _python_boundary_call_label(
    file: _PythonWorkspaceFile,
    call_site: _PythonCallSite,
    batch_aliases: set[str],
) -> str | None:
    call = call_site.call
    names = _python_call_names(file, call_site)
    operation_names = _python_call_operation_names(call, names)
    receiver = _python_call_receiver_name(call)
    normalized_receiver = _python_normalized_call_receiver_name(call, call_site)
    lowered_names = [name.lower() for name in names if name]
    lowered_receiver = receiver.lower()
    lowered_normalized_receiver = normalized_receiver.lower()

    if operation_names & _MIGRATION_OPERATIONS and _is_migration_operation(
        lowered_names, lowered_receiver, lowered_normalized_receiver, batch_aliases
    ):
        return "migration operation"

    if operation_names & _TRANSACTION_OPERATIONS and _is_transaction_receiver(
        lowered_receiver, lowered_normalized_receiver
    ):
        return "transaction boundary"

    if operation_names & _EVENT_OPERATIONS and _is_event_receiver(lowered_receiver, lowered_normalized_receiver):
        return "worker/event boundary"

    if operation_names & _HTTP_OPERATIONS and (
        _is_external_io_receiver(lowered_receiver, lowered_normalized_receiver) or _is_external_io_name(lowered_names)
    ):
        return "external I/O call"

    if operation_names & _FIXTURE_OVERRIDE_OPERATIONS and _is_fixture_override_receiver(
        lowered_receiver, lowered_normalized_receiver
    ):
        return "test fixture override"

    return None


def _python_call_names(file: _PythonWorkspaceFile, call_site: _PythonCallSite) -> list[str]:
    call = call_site.call
    names = [
        _python_attribute_name(call.func),
        _python_normalized_attribute_name(call.func, call_site.context.bindings),
        *_python_resolved_call_identities(
            call,
            file,
            call_site.context.bindings,
            call_site.context.instance_types,
        ),
    ]
    return [name for name in names if name]


def _python_call_operation_names(call: ast.Call, names: list[str]) -> set[str]:
    operations = {name.rsplit(".", maxsplit=1)[-1] for name in names if name}
    if isinstance(call.func, ast.Attribute):
        operations.add(call.func.attr)
    elif isinstance(call.func, ast.Name):
        operations.add(call.func.id)
    return operations


def _python_call_receiver_name(call: ast.Call) -> str:
    if not isinstance(call.func, ast.Attribute):
        return ""
    return _python_attribute_name(call.func.value)


def _python_normalized_call_receiver_name(call: ast.Call, call_site: _PythonCallSite) -> str:
    if not isinstance(call.func, ast.Attribute):
        return ""
    return _python_normalized_attribute_name(call.func.value, call_site.context.bindings)


def _is_transaction_receiver(receiver: str, normalized_receiver: str) -> bool:
    candidates = (receiver, normalized_receiver)
    return any("session" in candidate or candidate.endswith(".db") or candidate == "db" for candidate in candidates)


def _is_migration_operation(
    names: list[str],
    receiver: str,
    normalized_receiver: str,
    batch_aliases: set[str],
) -> bool:
    return (
        any(name.startswith("alembic.op.") for name in names)
        or receiver in batch_aliases
        or normalized_receiver in batch_aliases
    )


def _is_external_io_receiver(receiver: str, normalized_receiver: str) -> bool:
    candidates = (receiver, normalized_receiver)
    return any(marker in candidate for marker in _EXTERNAL_CLIENT_MARKERS for candidate in candidates)


def _is_external_io_name(names: list[str]) -> bool:
    return any(name.startswith(("aiohttp.", "httpx.", "redis.", "requests.", "urllib3.")) for name in names)


def _is_event_receiver(receiver: str, normalized_receiver: str) -> bool:
    candidates = (receiver, normalized_receiver)
    return any(marker in candidate for marker in _EVENT_RECEIVER_MARKERS for candidate in candidates)


def _is_fixture_override_receiver(receiver: str, normalized_receiver: str) -> bool:
    candidates = (receiver, normalized_receiver)
    return any("dependency_overrides" in candidate or "monkeypatch" in candidate for candidate in candidates)


def _python_boundary_call_text(
    file: _PythonWorkspaceFile,
    call: ast.Call,
    parent_by_child: dict[ast.AST, ast.AST],
) -> str:
    call_text = _python_node_text(file.source, call)
    if not call_text:
        return ""
    parent = parent_by_child.get(call)
    if isinstance(parent, ast.Await):
        return f"await {call_text}"
    return call_text


def _python_boundary_assignments(node: ast.AST) -> list[ast.Assign | ast.AnnAssign | ast.AugAssign]:
    assignments: list[ast.Assign | ast.AnnAssign | ast.AugAssign] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign | ast.AnnAssign | ast.AugAssign):
            continue
        targets = child.targets if isinstance(child, ast.Assign) else [child.target]
        if any(_is_fixture_override_target(target) for target in targets):
            assignments.append(child)
    return assignments


def _is_fixture_override_target(target: ast.expr) -> bool:
    text = _python_attribute_name(target)
    if isinstance(target, ast.Subscript):
        text = _python_attribute_name(target.value)
    return "dependency_overrides" in text


def _python_migration_batch_aliases(
    node: ast.AST,
    file: _PythonWorkspaceFile,
    call_sites: list[_PythonCallSite],
) -> set[str]:
    call_site_by_call = {call_site.call: call_site for call_site in call_sites}
    aliases: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.With | ast.AsyncWith):
            continue
        for item in child.items:
            if item.optional_vars is None or not isinstance(item.context_expr, ast.Call):
                continue
            call_site = call_site_by_call.get(item.context_expr)
            if call_site is None:
                continue
            names = [name.lower() for name in _python_call_names(file, call_site)]
            if any(name.startswith("alembic.op.batch_alter_table") for name in names):
                aliases.update(name.lower() for name in _python_target_names(item.optional_vars))
    return aliases


def _python_pytest_fixture_consumers(
    file: _PythonWorkspaceFile,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[AnalyzerReference]:
    if not _is_pytest_fixture_function(file, node):
        return []
    consumers: list[AnalyzerReference] = []
    for candidate in ast.walk(file.module):
        if not isinstance(candidate, ast.FunctionDef | ast.AsyncFunctionDef) or candidate is node:
            continue
        if node.name not in _python_argument_names(candidate.args):
            continue
        signature = _python_node_text(file.source, candidate).splitlines()[0].strip().removesuffix(":")
        consumers.append(
            AnalyzerReference(
                file=file.path,
                line=_python_node_start_line(candidate),
                endLine=_python_node_start_line(candidate),
                text=f"test fixture consumer: {signature}",
                kind="metadata",
            )
        )
    return consumers


def _is_pytest_fixture_function(file: _PythonWorkspaceFile, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        text = _python_node_text(file.source, decorator)
        if text in {"fixture", "pytest.fixture"} or text.startswith(("fixture(", "pytest.fixture(")):
            return True
    return False


def _python_argument_names(arguments: ast.arguments) -> set[str]:
    names = {
        argument.arg
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
    }
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _append_python_metadata(
    metadata: list[AnalyzerReference],
    seen: set[tuple[str, int, str]],
    reference: AnalyzerReference,
) -> None:
    if not reference.text:
        return
    key = (reference.file, reference.line, reference.text)
    if key in seen:
        return
    seen.add(key)
    metadata.append(reference)
