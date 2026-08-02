import re
import time
from bisect import bisect_left
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from apex_ray.models import AnalyzerReference

from .constants import DART_METADATA_LIMIT
from .directives import parse_dart_directives
from .generated import is_generated_dart_path

_LIFECYCLE_METHODS = (
    "initState",
    "didChangeDependencies",
    "didUpdateWidget",
    "deactivate",
    "dispose",
    "reassemble",
)
_RESOURCE_TYPES = (
    "AnimationController",
    "FocusNode",
    "PageController",
    "ScrollController",
    "StreamController",
    "StreamSubscription",
    "TabController",
    "TextEditingController",
    "Timer",
)
_ASYNC_CONTEXT_EVENT_RE = re.compile(
    r"(?P<async_open>\basync\s*\{)"
    r"|(?P<open>\{)"
    r"|(?P<close>\})"
    r"|(?P<await>\bawait\b)"
    r"|(?P<mounted>\b(?:context\s*\.\s*)?mounted\b)"
    r"|(?P<context>"
    r"\bNavigator\s*\.\s*of\s*\(\s*context\b"
    r"|\bshow(?:Dialog|ModalBottomSheet)\s*\([^)]*\bcontext\b"
    r"|\bcontext\s*\.\s*(?!mounted\b)"
    r")"
)

MetadataSink = Callable[[int, str], None]
_DART_METADATA_INDEX_LIMIT = 6_400


class _DartMetadataDeadlineExpired(RuntimeError):
    pass


class _DartMetadataLimitReached(RuntimeError):
    pass


@dataclass(slots=True)
class _AsyncContextState:
    saw_await: bool = False
    mounted_guard: bool = False


@dataclass(frozen=True, slots=True)
class DartFrameworkMetadataIndex:
    """Immutable per-file framework evidence prepared for cheap range queries."""

    path: str
    references: tuple[AnalyzerReference, ...] = ()
    truncated: bool = False
    deadline: float | None = None

    def for_range(
        self,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        max_items: int = DART_METADATA_LIMIT,
        deadline: float | None = None,
    ) -> list[AnalyzerReference]:
        """Return a deterministic bounded slice without rescanning source text.

        Deadlines are absolute ``time.monotonic()`` values. The build deadline
        remains the default for range queries so callers cannot accidentally
        keep spending an analyzer budget after it expires.
        """

        query_deadline = self.deadline if deadline is None else deadline
        _validate_deadline(query_deadline)
        if max_items <= 0 or _deadline_reached(query_deadline):
            return []
        start = max(1, start_line)
        end = end_line if end_line is not None else 2**63 - 1
        if start > end:
            return []

        offset = bisect_left(self.references, start, key=lambda item: item.line)
        selected: list[AnalyzerReference] = []
        for position in range(offset, len(self.references)):
            item = self.references[position]
            if item.line > end or len(selected) >= max_items or _deadline_reached(query_deadline):
                break
            selected.append(item)
        return selected


def build_dart_framework_metadata_index(
    path: str,
    source: str,
    *,
    deadline: float | None = None,
) -> DartFrameworkMetadataIndex:
    """Prepare framework evidence once for all symbols in one Dart file.

    ``deadline`` is an absolute ``time.monotonic()`` value. Expiry preserves
    deterministic evidence completed before the boundary and marks the result
    as truncated.
    """

    _validate_deadline(deadline)
    if is_generated_dart_path(path):
        return DartFrameworkMetadataIndex(path=path, deadline=deadline)
    if _deadline_reached(deadline):
        return DartFrameworkMetadataIndex(path=path, truncated=True, deadline=deadline)

    imports = {
        target
        for directive in parse_dart_directives(source)
        if directive.kind == "import"
        for target in (directive.target, *directive.conditional_targets)
    }
    if _deadline_reached(deadline):
        return DartFrameworkMetadataIndex(path=path, truncated=True, deadline=deadline)
    features = _framework_features(imports)
    if not features:
        return DartFrameworkMetadataIndex(path=path, deadline=deadline)

    try:
        masked = _mask_comments_and_strings(source, deadline=deadline)
    except _DartMetadataDeadlineExpired:
        return DartFrameworkMetadataIndex(path=path, truncated=True, deadline=deadline)

    evidence: list[AnalyzerReference] = []
    seen: set[tuple[int, str]] = set()

    def add(line: int, text: str) -> None:
        if (line, text) in seen:
            return
        if len(evidence) >= _DART_METADATA_INDEX_LIMIT:
            raise _DartMetadataLimitReached
        seen.add((line, text))
        evidence.append(AnalyzerReference(file=path, line=line, endLine=line, text=text, kind="metadata"))

    truncated = False
    try:
        for line_number, code in enumerate(masked.splitlines(), start=1):
            if _deadline_reached(deadline):
                truncated = True
                break
            if "flutter" in features:
                _collect_flutter_line(code, line_number, add)
            if "bloc" in features:
                _collect_bloc_line(code, line_number, add)
            if "mobx" in features:
                _collect_mobx_line(code, line_number, add)
            if "di" in features:
                _collect_di_line(code, line_number, add)
            if "routing" in features:
                _collect_routing_line(code, line_number, add)
            if "serialization" in features:
                _collect_serialization_line(code, line_number, add)
            _collect_boundary_line(code, line_number, features, add)

        if "flutter" in features and not truncated:
            truncated = _collect_async_context(masked, add, deadline=deadline)
    except _DartMetadataLimitReached:
        truncated = True

    return DartFrameworkMetadataIndex(
        path=path,
        references=tuple(sorted(evidence, key=lambda item: (item.line, item.text, item.file))),
        truncated=truncated,
        deadline=deadline,
    )


def collect_dart_framework_metadata(
    path: str,
    source: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_items: int = DART_METADATA_LIMIT,
    deadline: float | None = None,
) -> list[AnalyzerReference]:
    """Collect bounded framework evidence without treating it as findings.

    This compatibility wrapper prepares a one-file index. Call
    :func:`build_dart_framework_metadata_index` once and use ``for_range`` when
    collecting metadata for multiple symbols from the same source.
    """

    if max_items <= 0 or is_generated_dart_path(path):
        return []
    index = build_dart_framework_metadata_index(path, source, deadline=deadline)
    return index.for_range(
        start_line=start_line,
        end_line=end_line,
        max_items=max_items,
        deadline=deadline,
    )


def _framework_features(imports: set[str]) -> set[str]:
    features: set[str] = set()
    if any(target.startswith("package:flutter/") for target in imports):
        features.add("flutter")
    if any(target.startswith(("package:bloc/", "package:flutter_bloc/")) for target in imports):
        features.add("bloc")
    if any(target.startswith(("package:mobx/", "package:flutter_mobx/")) for target in imports):
        features.add("mobx")
    if any(
        target.startswith(
            (
                "package:get_it/",
                "package:injectable/",
                "package:provider/",
                "package:riverpod/",
                "package:flutter_riverpod/",
            )
        )
        for target in imports
    ):
        features.add("di")
    if any(target.startswith("package:go_router/") for target in imports):
        features.add("routing")
    if any(target.startswith(("package:json_annotation/", "package:freezed_annotation/")) for target in imports):
        features.add("serialization")
    boundary_packages = {
        "network": ("package:dio/", "package:http/", "package:chopper/", "package:retrofit/"),
        "secure-storage": ("package:flutter_secure_storage/",),
        "persistence": ("package:hive/", "package:shared_preferences/", "package:sqflite/", "package:drift/"),
        "permissions": ("package:permission_handler/",),
        "background": ("package:workmanager/", "package:flutter_background_service/"),
    }
    for feature, prefixes in boundary_packages.items():
        if any(target.startswith(prefixes) for target in imports):
            features.add(feature)
    if "dart:isolate" in imports:
        features.add("isolate")
    return features


def _collect_flutter_line(code: str, line: int, add: MetadataSink) -> None:
    widget = re.search(r"\bclass\s+[_$A-Za-z]\w*\s+extends\s+(StatefulWidget|StatelessWidget)\b", code)
    if widget:
        add(line, f"flutter widget declaration: {widget.group(1)}")
    state = re.search(r"\bclass\s+[_$A-Za-z]\w*\s+extends\s+State\s*<\s*([^>,]+)", code)
    if state:
        add(line, f"flutter state declaration: State<{state.group(1).strip()}>")
    for lifecycle in _LIFECYCLE_METHODS:
        if re.search(rf"\b{lifecycle}\s*\(", code):
            add(line, f"lifecycle method: {lifecycle}")
    if re.search(r"\bcreateState\s*\(", code):
        add(line, "widget-state relationship: createState")
    for resource in _RESOURCE_TYPES:
        if re.search(rf"\b{resource}(?:\s*<[^>]*>)?\s*\(", code):
            add(line, f"lifecycle resource allocation: {resource}")
    cleanup = re.search(r"\b([_$A-Za-z]\w*)\s*\.\s*(cancel|close|dispose|removeListener)\s*\(", code)
    if cleanup:
        add(line, f"lifecycle cleanup call: {cleanup.group(1)}.{cleanup.group(2)}")


def _collect_bloc_line(code: str, line: int, add: MetadataSink) -> None:
    declaration = re.search(r"\bclass\s+[_$A-Za-z]\w*\s+extends\s+(Bloc|Cubit)\b", code)
    if declaration:
        add(line, f"state management declaration: {declaration.group(1)}")
    for call in ("add", "emit"):
        if re.search(rf"(?:\.|\b){call}\s*\(", code):
            add(line, f"state transition call: {call}")
    consumer = re.search(r"\b(BlocBuilder|BlocConsumer|BlocListener|BlocSelector)\s*<", code)
    if consumer:
        add(line, f"state consumer: {consumer.group(1)}")


def _collect_mobx_line(code: str, line: int, add: MetadataSink) -> None:
    annotation = re.search(r"@(observable|computed|action|readonly)\b", code)
    if annotation:
        add(line, f"MobX annotation: {annotation.group(1)}")
    primitive = re.search(r"\b(Observer|reaction|autorun|when)\s*\(", code)
    if primitive:
        add(line, f"MobX primitive: {primitive.group(1)}")


def _collect_di_line(code: str, line: int, add: MetadataSink) -> None:
    annotation = re.search(r"@(injectable|singleton|lazySingleton|module|factoryMethod)\b", code)
    if annotation:
        add(line, f"dependency injection annotation: {annotation.group(1)}")
    if re.search(r"\b(?:GetIt\s*\.\s*(?:I|instance)|getIt)\s*(?:<|\()", code):
        add(line, "dependency injection: GetIt lookup")
    provider = re.search(r"\bcontext\s*\.\s*(read|watch|select)\s*(?:<|\()", code)
    if provider:
        add(line, f"provider dependency access: context.{provider.group(1)}")
    if re.search(r"\bref\s*\.\s*(read|watch|listen)\s*\(", code):
        add(line, "provider dependency access: ref")


def _collect_routing_line(code: str, line: int, add: MetadataSink) -> None:
    declaration = re.search(r"\b(GoRoute|ShellRoute|StatefulShellRoute)\s*\(", code)
    if declaration:
        add(line, f"routing declaration: {declaration.group(1)}")
    navigation = re.search(r"\bcontext\s*\.\s*(go|goNamed|push|pushNamed|replace|replaceNamed)\s*\(", code)
    if navigation:
        add(line, f"navigation call: context.{navigation.group(1)}")
    if re.search(r"\bredirect\s*:", code):
        add(line, "routing contract: redirect")
    navigator = re.search(r"\bNavigator\s*\.\s*(of|push|pop|pushNamed|pushReplacement)\s*\(", code)
    if navigator:
        add(line, f"navigation call: Navigator.{navigator.group(1)}")


def _collect_serialization_line(code: str, line: int, add: MetadataSink) -> None:
    annotation = re.search(r"@(JsonSerializable|JsonKey|freezed|Freezed)\b", code)
    if annotation:
        add(line, f"serialization annotation: {annotation.group(1)}")
    factory = re.search(r"\b(fromJson|toJson)\s*\(", code)
    if factory:
        add(line, f"serialization contract: {factory.group(1)}")


def _collect_boundary_line(code: str, line: int, features: set[str], add: MetadataSink) -> None:
    if "network" in features and re.search(r"\.\s*(get|post|put|patch|delete|request|send)\s*\(", code):
        add(line, "boundary: network I/O")
    if "secure-storage" in features and re.search(r"\bFlutterSecureStorage\s*\(|\.\s*(read|write|delete)\s*\(", code):
        add(line, "boundary: secure storage")
    if "persistence" in features and re.search(r"\b(Hive|SharedPreferences|Database|DriftDatabase)\b", code):
        add(line, "boundary: local persistence")
    if "permissions" in features and re.search(r"\bPermission\b|\.\s*request\s*\(", code):
        add(line, "boundary: platform permission")
    if "isolate" in features and re.search(r"\b(Isolate\s*\.\s*spawn|compute)\s*\(", code):
        add(line, "boundary: isolate work")
    if "background" in features and re.search(r"\b(registerTask|executeTask|startService)\s*\(", code):
        add(line, "boundary: background execution")


def _collect_async_context(
    masked: str,
    add: MetadataSink,
    *,
    deadline: float | None,
) -> bool:
    """Collect async BuildContext evidence with one lexical token pass.

    Only the innermost active async body owns an ``await``. This avoids both
    rescanning enclosing bodies and incorrectly inheriting an outer await in a
    nested async closure. The return value reports deadline truncation.
    """

    brace_stack: list[_AsyncContextState | None] = []
    active_async: list[_AsyncContextState] = []
    current_line = 1
    previous_position = 0
    for event in _iter_async_context_events(masked):
        if _deadline_reached(deadline):
            return True
        current_line += masked.count("\n", previous_position, event.start())
        previous_position = event.start()
        kind = event.lastgroup
        if kind == "async_open":
            state = _AsyncContextState()
            brace_stack.append(state)
            active_async.append(state)
        elif kind == "open":
            brace_stack.append(None)
        elif kind == "close":
            if not brace_stack:
                continue
            state = brace_stack.pop()
            if state is not None and active_async and active_async[-1] is state:
                active_async.pop()
        elif not active_async:
            continue
        elif kind == "await":
            active_async[-1].saw_await = True
            active_async[-1].mounted_guard = False
        elif kind == "mounted" and active_async[-1].saw_await:
            active_async[-1].mounted_guard = True
        elif kind == "context" and active_async[-1].saw_await:
            guarded = active_async[-1].mounted_guard
            add(
                current_line,
                f"async BuildContext use after await; mounted guard: {'present' if guarded else 'absent'}",
            )
    return _deadline_reached(deadline)


def _iter_async_context_events(masked: str) -> Iterator[re.Match[str]]:
    return _ASYNC_CONTEXT_EVENT_RE.finditer(masked)


def _mask_comments_and_strings(source: str, *, deadline: float | None = None) -> str:
    result = list(source)
    index = 0
    next_deadline_check = 0
    state = "code"
    quote = ""
    raw = False
    triple = False
    block_depth = 0
    while index < len(source):
        if index >= next_deadline_check:
            if _deadline_reached(deadline):
                raise _DartMetadataDeadlineExpired
            next_deadline_check = index + 4_096
        if state == "code":
            if source.startswith("//", index):
                result[index] = result[index + 1] = " "
                index += 2
                state = "line-comment"
                continue
            if source.startswith("/*", index):
                result[index] = result[index + 1] = " "
                index += 2
                state = "block-comment"
                block_depth = 1
                continue
            prefix = source[index] in "rR" and index + 1 < len(source) and source[index + 1] in "'\""
            quote_index = index + 1 if prefix else index
            if source[quote_index : quote_index + 1] in {"'", '"'}:
                raw = prefix
                quote = source[quote_index]
                triple = source.startswith(quote * 3, quote_index)
                delimiter_length = 3 if triple else 1
                for offset in range((1 if prefix else 0) + delimiter_length):
                    result[index + offset] = " "
                index += (1 if prefix else 0) + delimiter_length
                state = "string"
                continue
            index += 1
        elif state == "line-comment":
            if source[index] == "\n":
                state = "code"
            else:
                result[index] = " "
            index += 1
        elif state == "block-comment":
            if source.startswith("/*", index):
                result[index] = result[index + 1] = " "
                block_depth += 1
                index += 2
            elif source.startswith("*/", index):
                result[index] = result[index + 1] = " "
                block_depth -= 1
                index += 2
                if block_depth == 0:
                    state = "code"
            else:
                if source[index] != "\n":
                    result[index] = " "
                index += 1
        else:
            delimiter = quote * (3 if triple else 1)
            if source.startswith(delimiter, index):
                for offset in range(len(delimiter)):
                    result[index + offset] = " "
                index += len(delimiter)
                state = "code"
            elif not raw and source[index] == "\\":
                result[index] = " "
                if index + 1 < len(source) and source[index + 1] != "\n":
                    result[index + 1] = " "
                index += 2
            else:
                if source[index] != "\n":
                    result[index] = " "
                index += 1
    return "".join(result)


def _validate_deadline(deadline: float | None) -> None:
    if deadline is not None and deadline != deadline:
        raise ValueError("Dart framework-metadata deadline must not be NaN")


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline
