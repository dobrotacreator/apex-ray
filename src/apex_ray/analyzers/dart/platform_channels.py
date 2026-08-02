import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apex_ray.models import AnalyzerReference

from .constants import (
    DART_PLATFORM_CHANNEL_ENDPOINT_LIMIT,
    DART_PLATFORM_CHANNEL_FILE_LIMIT,
    DART_PLATFORM_CHANNEL_FILE_SIZE_LIMIT,
    DART_SCAN_IGNORED_DIRS,
    PLATFORM_CHANNEL_LANGUAGES,
)
from .directives import parse_dart_directives
from .generated import is_generated_dart_path

_IDENTIFIER = r"[_$A-Za-z][_$A-Za-z0-9]*"
_STRING = r"(?:[rR]?'(?:\\.|[^'\\])*'|[rR]?\"(?:\\.|[^\"\\])*\")"
_VALUE = rf"(?:{_STRING}|{_IDENTIFIER})"
_CHANNEL_TYPES = {
    "MethodChannel": "method",
    "EventChannel": "event",
    "BasicMessageChannel": "basic-message",
    "FlutterMethodChannel": "method",
    "FlutterEventChannel": "event",
    "FlutterBasicMessageChannel": "basic-message",
}
DEFAULT_PLATFORM_CHANNEL_TOTAL_SOURCE_BYTES = 32 * 1024 * 1024


class _PlatformChannelDeadlineExpired(RuntimeError):
    pass


@dataclass(slots=True)
class _StringLexerState:
    delimiter: str
    raw: bool


@dataclass(slots=True)
class _InterpolationLexerState:
    brace_depth: int = 0


@dataclass(frozen=True, slots=True)
class PlatformChannelMethod:
    name: str
    line: int
    direction: Literal["invoke", "handle"]


@dataclass(frozen=True, slots=True)
class PlatformChannelEndpoint:
    file: str
    language: str
    side: Literal["dart", "native"]
    channel_type: str
    channel_name: str
    line: int
    variable: str | None = None
    methods: tuple[PlatformChannelMethod, ...] = ()


@dataclass(frozen=True, slots=True)
class PlatformChannelIndex:
    endpoints: tuple[PlatformChannelEndpoint, ...]
    scanned_files: int = 0
    skipped_files: int = 0
    truncated: bool = False
    scanned_bytes: int = 0


def extract_platform_channel_endpoints(path: str, source: str) -> list[PlatformChannelEndpoint]:
    """Extract endpoints backed by exact local string literals.

    Simple local constants are resolved. Runtime expressions, interpolation,
    concatenation, and ambiguous method-handler ownership are ignored.
    """

    return _extract_platform_channel_endpoints(path, source, deadline=None)


def _extract_platform_channel_endpoints(
    path: str,
    source: str,
    *,
    deadline: float | None,
) -> list[PlatformChannelEndpoint]:
    _check_deadline(deadline)
    suffix = Path(path).suffix.casefold()
    language = PLATFORM_CHANNEL_LANGUAGES.get(suffix)
    if language is None or (language == "dart" and is_generated_dart_path(path)):
        return []
    if language == "dart":
        imports = {directive.target for directive in parse_dart_directives(source) if directive.kind == "import"}
        _check_deadline(deadline)
        if "package:flutter/services.dart" not in imports:
            return []

    code_positions = _source_code_positions(source, language=language, deadline=deadline)
    constants = _literal_constants(source, language, code_positions=code_positions, deadline=deadline)
    declarations = _channel_declarations(
        source,
        language,
        constants,
        code_positions=code_positions,
        deadline=deadline,
    )
    if not declarations:
        return []
    method_candidates = _method_literals(
        source,
        language,
        code_positions=code_positions,
        deadline=deadline,
    )
    endpoint_count = len(declarations)
    endpoints: list[PlatformChannelEndpoint] = []
    for declaration in declarations:
        _check_deadline(deadline)
        methods: list[PlatformChannelMethod] = []
        for receiver, method in method_candidates:
            _check_deadline(deadline)
            if declaration.variable is not None and receiver == declaration.variable:
                methods.append(method)
            elif receiver is None and endpoint_count == 1:
                methods.append(method)
        methods = sorted(set(methods), key=lambda item: (item.line, item.name, item.direction))
        endpoints.append(
            PlatformChannelEndpoint(
                file=path,
                language=declaration.language,
                side=declaration.side,
                channel_type=declaration.channel_type,
                channel_name=declaration.channel_name,
                line=declaration.line,
                variable=declaration.variable,
                methods=tuple(methods),
            )
        )
    _check_deadline(deadline)
    return sorted(endpoints, key=lambda item: (item.line, item.channel_name, item.channel_type))


def build_platform_channel_index(
    repo_root: Path,
    *,
    candidate_paths: Iterable[str] | None = None,
    max_files: int = DART_PLATFORM_CHANNEL_FILE_LIMIT,
    max_file_bytes: int = DART_PLATFORM_CHANNEL_FILE_SIZE_LIMIT,
    max_endpoints: int = DART_PLATFORM_CHANNEL_ENDPOINT_LIMIT,
    max_total_source_bytes: int = DEFAULT_PLATFORM_CHANNEL_TOTAL_SOURCE_BYTES,
    deadline: float | None = None,
) -> PlatformChannelIndex:
    if deadline is not None and deadline != deadline:
        raise ValueError("Dart platform-channel deadline must not be NaN")
    if _deadline_reached(deadline):
        return PlatformChannelIndex((), truncated=True)
    root = repo_root.resolve()
    if max_files <= 0 or max_file_bytes <= 0 or max_endpoints <= 0 or max_total_source_bytes <= 0:
        return PlatformChannelIndex((), truncated=True)
    try:
        paths = (
            _candidate_platform_paths(candidate_paths, deadline=deadline)
            if candidate_paths is not None
            else _platform_source_paths(root, deadline=deadline)
        )
    except _PlatformChannelDeadlineExpired:
        return PlatformChannelIndex((), truncated=True)
    endpoints: list[PlatformChannelEndpoint] = []
    scanned = 0
    skipped = 0
    scanned_bytes = 0
    processed = 0
    truncated = False
    for relative in paths:
        if _deadline_reached(deadline) or processed >= max_files or len(endpoints) >= max_endpoints:
            truncated = True
            break
        processed += 1
        path = _repo_source_file(root, relative)
        if path is None:
            skipped += 1
            continue
        try:
            _check_deadline(deadline)
            file_size = path.stat().st_size
            if file_size > max_file_bytes:
                skipped += 1
                continue
            remaining_source_bytes = max_total_source_bytes - scanned_bytes
            if file_size > remaining_source_bytes:
                truncated = True
                break
            with path.open("rb") as handle:
                raw_source = handle.read(min(max_file_bytes, remaining_source_bytes) + 1)
            if len(raw_source) > remaining_source_bytes:
                truncated = True
                break
            if len(raw_source) > max_file_bytes:
                skipped += 1
                continue
            scanned_bytes += len(raw_source)
            source = raw_source.decode("utf-8")
            _check_deadline(deadline)
        except _PlatformChannelDeadlineExpired:
            truncated = True
            break
        except OSError, UnicodeDecodeError:
            skipped += 1
            continue
        scanned += 1
        repo_path = path.relative_to(root).as_posix()
        remaining = max_endpoints - len(endpoints)
        try:
            extracted = _extract_platform_channel_endpoints(repo_path, source, deadline=deadline)
        except _PlatformChannelDeadlineExpired:
            truncated = True
            break
        endpoints.extend(extracted[:remaining])
        if len(extracted) > remaining:
            truncated = True
            break
    return PlatformChannelIndex(
        tuple(sorted(endpoints, key=lambda item: (item.channel_name, item.file, item.line))),
        scanned_files=scanned,
        skipped_files=skipped,
        scanned_bytes=scanned_bytes,
        truncated=truncated,
    )


def platform_channel_contracts(
    index: PlatformChannelIndex,
    dart_path: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    limit: int = 16,
) -> list[AnalyzerReference]:
    if limit <= 0:
        return []
    contracts: list[AnalyzerReference] = []
    seen: set[tuple[str, int, str]] = set()
    for dart_endpoint in index.endpoints:
        if dart_endpoint.side != "dart" or dart_endpoint.file != dart_path:
            continue
        relevant_lines = [dart_endpoint.line, *(method.line for method in dart_endpoint.methods)]
        if not any(line >= start_line and (end_line is None or line <= end_line) for line in relevant_lines):
            continue
        dart_invoked = {method.name for method in dart_endpoint.methods if method.direction == "invoke"}
        dart_handled = {method.name for method in dart_endpoint.methods if method.direction == "handle"}
        for native in index.endpoints:
            if (
                native.side != "native"
                or native.channel_name != dart_endpoint.channel_name
                or native.channel_type != dart_endpoint.channel_type
            ):
                continue
            native_invoked = {method.name for method in native.methods if method.direction == "invoke"}
            native_handled = {method.name for method in native.methods if method.direction == "handle"}
            exact_methods = sorted((dart_invoked & native_handled) | (dart_handled & native_invoked))
            method_summary = ", ".join(exact_methods) if exact_methods else "none observed"
            text = (
                f"native {native.channel_type} channel {native.channel_name!r}; "
                f"exact direction-matched methods: {method_summary}"
            )
            key = (native.file, native.line, text)
            if key in seen:
                continue
            seen.add(key)
            contracts.append(
                AnalyzerReference(
                    file=native.file,
                    line=native.line,
                    endLine=native.line,
                    text=text,
                    kind="contract",
                )
            )
    return sorted(contracts, key=lambda item: (item.file, item.line, item.text))[:limit]


def _channel_declarations(
    source: str,
    language: str,
    constants: dict[str, str],
    *,
    code_positions: bytearray,
    deadline: float | None,
) -> list[PlatformChannelEndpoint]:
    if language == "dart":
        pattern = re.compile(
            rf"(?:(?:static\s+)?(?:final|const|var|late\s+final)\s+)?"
            rf"(?:(?:MethodChannel|EventChannel|BasicMessageChannel)(?:\s*<[^;()]+>)?\s+)?"
            rf"(?:(?P<variable>{_IDENTIFIER})\s*=\s*)?"
            rf"(?:const\s+)?(?P<type>MethodChannel|EventChannel|BasicMessageChannel)"
            rf"(?:\s*<[^;()]+>)?\s*\(\s*(?P<value>{_VALUE})(?=\s*[,\)])"
        )
    elif language in {"kotlin", "java"}:
        assignment = (
            rf"(?:(?:val|var)\s+(?P<variable>{_IDENTIFIER})\s*=\s*)?"
            if language == "kotlin"
            else rf"(?:(?:final\s+)?(?:MethodChannel|EventChannel|BasicMessageChannel)\s+(?P<variable>{_IDENTIFIER})\s*=\s*)?"
        )
        pattern = re.compile(
            rf"{assignment}(?:new\s+)?(?P<type>MethodChannel|EventChannel|BasicMessageChannel)"
            rf"\s*\(\s*[^,\n]{{1,300}},\s*(?P<value>{_VALUE})(?=\s*[,\)])"
        )
    elif language == "swift":
        pattern = re.compile(
            rf"(?:(?:let|var)\s+(?P<variable>{_IDENTIFIER})\s*=\s*)?"
            rf"(?P<type>FlutterMethodChannel|FlutterEventChannel|FlutterBasicMessageChannel)"
            rf"\s*\(\s*name\s*:\s*(?P<value>{_VALUE})(?=\s*,)"
        )
    else:
        pattern = re.compile(
            rf"(?P<type>FlutterMethodChannel|FlutterEventChannel|FlutterBasicMessageChannel)"
            rf"\s+methodChannelWithName\s*:\s*(?P<value>@?{_STRING}|{_IDENTIFIER})"
            rf"(?=\s+binaryMessenger\s*:)"
        )

    endpoints: list[PlatformChannelEndpoint] = []
    for match in pattern.finditer(source):
        _check_deadline(deadline)
        if not code_positions[match.start("type")] or not code_positions[match.start("value")]:
            continue
        value = match.group("value")
        if value.startswith("@"):
            value = value[1:]
        channel_name = _literal_value(value) if _is_string_token(value) else constants.get(value)
        if channel_name is None or "$" in channel_name or not channel_name:
            continue
        channel_type = _CHANNEL_TYPES[match.group("type")]
        endpoints.append(
            PlatformChannelEndpoint(
                file="",
                language=language,
                side="dart" if language == "dart" else "native",
                channel_type=channel_type,
                channel_name=channel_name,
                line=_line_at(source, match.start("type")),
                variable=match.groupdict().get("variable"),
            )
        )
    # The caller supplies the path; dataclass replacement keeps extraction pure.
    return endpoints


def _method_literals(
    source: str,
    language: str,
    *,
    code_positions: bytearray,
    deadline: float | None,
) -> list[tuple[str | None, PlatformChannelMethod]]:
    methods: list[tuple[str | None, PlatformChannelMethod]] = []
    invoke_pattern = re.compile(
        rf"(?P<receiver>{_IDENTIFIER})\s*\.\s*"
        rf"(?P<invoke>invoke(?:Method|ListMethod|MapMethod))(?:\s*<[^;()]+>)?\s*\(\s*"
        rf"(?P<value>{_STRING})"
        rf"(?=\s*[,\)])"
    )
    for match in invoke_pattern.finditer(source):
        _check_deadline(deadline)
        if (
            not code_positions[match.start("receiver")]
            or not code_positions[match.start("invoke")]
            or not code_positions[match.start("value")]
        ):
            continue
        value = _literal_value(match.group("value"))
        if value is not None and "$" not in value:
            methods.append(
                (
                    match.group("receiver"),
                    PlatformChannelMethod(value, _line_at(source, match.start("value")), "invoke"),
                )
            )

    if language == "kotlin":
        handler_pattern = re.compile(rf"(?P<value>{_STRING})\s*->")
    elif language in {"swift", "java"}:
        handler_pattern = re.compile(rf"\bcase\s+(?P<value>{_STRING})")
    elif language.startswith("objective-c"):
        handler_pattern = re.compile(rf"isEqualToString\s*:\s*@?(?P<value>{_STRING})")
    else:
        handler_pattern = re.compile(rf"\bcase\s+(?P<value>{_STRING})")
    for receiver, region_start, region_end in _method_handler_regions(
        source,
        code_positions=code_positions,
        deadline=deadline,
    ):
        _check_deadline(deadline)
        for match in handler_pattern.finditer(source, region_start, region_end):
            _check_deadline(deadline)
            if not code_positions[match.start()] or not code_positions[match.start("value")]:
                continue
            value = _literal_value(match.group("value"))
            if value is not None and "$" not in value:
                methods.append(
                    (
                        receiver,
                        PlatformChannelMethod(value, _line_at(source, match.start("value")), "handle"),
                    )
                )
    return methods


def _literal_constants(
    source: str,
    language: str,
    *,
    code_positions: bytearray,
    deadline: float | None,
) -> dict[str, str]:
    if language == "dart":
        pattern = re.compile(
            rf"\b(?:static\s+)?const(?:\s+{_IDENTIFIER})?\s+(?P<name>{_IDENTIFIER})\s*=\s*"
            rf"(?P<value>{_STRING})(?=\s*(?:;|$))",
            re.MULTILINE,
        )
    elif language == "kotlin":
        pattern = re.compile(
            rf"\bconst\s+val\s+(?P<name>{_IDENTIFIER})\s*=\s*(?P<value>{_STRING})(?=\s*(?:;|$))",
            re.MULTILINE,
        )
    elif language == "java":
        pattern = re.compile(
            rf"\bstatic\s+final\s+String\s+(?P<name>{_IDENTIFIER})\s*=\s*"
            rf"(?P<value>{_STRING})(?=\s*(?:;|$))",
            re.MULTILINE,
        )
    elif language == "swift":
        pattern = re.compile(
            rf"\b(?:static\s+)?let\s+(?P<name>{_IDENTIFIER})\s*=\s*(?P<value>{_STRING})(?=\s*(?:;|$))",
            re.MULTILINE,
        )
    else:
        pattern = re.compile(
            rf"\bNSString\s*\*\s*const\s+(?P<name>{_IDENTIFIER})\s*=\s*@?"
            rf"(?P<value>{_STRING})(?=\s*(?:;|$))",
            re.MULTILINE,
        )
    constants: dict[str, str] = {}
    ambiguous: set[str] = set()
    for match in pattern.finditer(source):
        _check_deadline(deadline)
        if not code_positions[match.start("name")] or not code_positions[match.start("value")]:
            continue
        name = match.group("name")
        value = _literal_value(match.group("value"))
        if value is None or "$" in value:
            continue
        if name in constants and constants[name] != value:
            ambiguous.add(name)
        else:
            constants[name] = value
    for name in ambiguous:
        constants.pop(name, None)
    return constants


def _method_handler_regions(
    source: str,
    *,
    code_positions: bytearray,
    deadline: float | None,
) -> list[tuple[str | None, int, int]]:
    call_pattern = re.compile(
        rf"(?:(?P<receiver>{_IDENTIFIER})\s*\.\s*)?"
        rf"(?P<handler>setMethodCallHandler)\b[^;{{]{{0,500}}\{{"
    )
    regions: list[tuple[str | None, int, int]] = []
    for match in call_pattern.finditer(source):
        _check_deadline(deadline)
        if not code_positions[match.start()] or not code_positions[match.start("handler")]:
            continue
        opening = source.find("{", match.start(), match.end())
        if opening < 0 or not code_positions[opening]:
            continue
        closing = _matching_source_brace(source, opening, deadline=deadline)
        if closing is not None:
            regions.append((match.group("receiver"), opening + 1, closing))
    return regions


def _source_code_positions(
    source: str,
    *,
    deadline: float | None,
    language: str = "dart",
) -> bytearray:
    """Mark executable code while masking comments and literal string text.

    Dart interpolation expressions are executable code, including expressions
    nested in interpolated strings. The iterative state stack avoids recursion;
    callers bound its work with the platform-channel source and deadline limits.
    """

    code_positions = bytearray(b"\x01") * len(source)
    states: list[_StringLexerState | _InterpolationLexerState] = []
    index = 0
    next_deadline_check = 0
    while index < len(source):
        if index >= next_deadline_check:
            _check_deadline(deadline)
            next_deadline_check = index + 1024

        state = states[-1] if states else None
        if isinstance(state, _StringLexerState):
            if source.startswith(state.delimiter, index):
                end = index + len(state.delimiter)
                code_positions[index:end] = b"\x00" * (end - index)
                states.pop()
                index = end
                continue
            if state.raw:
                code_positions[index] = 0
                index += 1
                continue
            if source[index] == "\\":
                end = min(len(source), index + 2)
                code_positions[index:end] = b"\x00" * (end - index)
                index = end
                continue
            if language == "dart" and source[index] == "$":
                if source[index + 1 : index + 2] == "{":
                    states.append(_InterpolationLexerState())
                    index += 2
                    continue
                if _is_dart_identifier_start(source[index + 1 : index + 2]):
                    index += 2
                    while index < len(source) and _is_dart_identifier_part(source[index]):
                        if index >= next_deadline_check:
                            _check_deadline(deadline)
                            next_deadline_check = index + 1024
                        index += 1
                    continue
            code_positions[index] = 0
            index += 1
            continue

        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = len(source) if end < 0 else end
            _check_deadline(deadline)
            code_positions[index:end] = b"\x00" * (end - index)
            index = end
            continue
        if source.startswith("/*", index):
            start = index
            depth = 1
            index += 2
            while index < len(source) and depth:
                if index >= next_deadline_check:
                    _check_deadline(deadline)
                    next_deadline_check = index + 1024
                if source.startswith("/*", index):
                    depth += 1
                    index += 2
                elif source.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            code_positions[start:index] = b"\x00" * (index - start)
            continue

        string_state = _source_string_state(source, index, allow_raw=language == "dart")
        if string_state is not None:
            next_state, content_start = string_state
            code_positions[index:content_start] = b"\x00" * (content_start - index)
            # Token-anchored native handler patterns begin at the literal's
            # opening offset. Literal contents remain masked.
            code_positions[index] = 1
            states.append(next_state)
            index = content_start
            continue

        if isinstance(state, _InterpolationLexerState):
            if source[index] == "{":
                state.brace_depth += 1
                index += 1
                continue
            if source[index] == "}":
                if state.brace_depth == 0:
                    states.pop()
                else:
                    state.brace_depth -= 1
                index += 1
                continue
        index += 1
    return code_positions


def _source_string_state(
    source: str,
    start: int,
    *,
    allow_raw: bool,
) -> tuple[_StringLexerState, int] | None:
    quote_index = start
    raw = False
    if allow_raw and source[start : start + 1] in {"r", "R"} and source[start + 1 : start + 2] in {"'", '"'}:
        raw = True
        quote_index += 1
    if source[quote_index : quote_index + 1] not in {"'", '"'}:
        return None
    quote = source[quote_index]
    delimiter = quote * (3 if source.startswith(quote * 3, quote_index) else 1)
    return _StringLexerState(delimiter=delimiter, raw=raw), quote_index + len(delimiter)


def _is_dart_identifier_start(character: str) -> bool:
    return bool(character) and (character == "_" or character.isalpha())


def _is_dart_identifier_part(character: str) -> bool:
    return character == "_" or character.isalnum()


def _matching_source_brace(source: str, opening: int, *, deadline: float | None) -> int | None:
    depth = 0
    block_comment_depth = 0
    index = opening
    next_deadline_check = index
    while index < len(source):
        if index >= next_deadline_check:
            _check_deadline(deadline)
            next_deadline_check = index + 1024
        if block_comment_depth:
            if source.startswith("/*", index):
                block_comment_depth += 1
                index += 2
            elif source.startswith("*/", index):
                block_comment_depth -= 1
                index += 2
            else:
                index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            block_comment_depth = 1
            index += 2
            continue
        string_end = _skip_source_string(source, index, deadline=deadline)
        if string_end is not None:
            index = string_end
            continue
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _skip_source_string(source: str, start: int, *, deadline: float | None) -> int | None:
    index = start
    raw = False
    if source[index : index + 1] in {"r", "R"} and source[index + 1 : index + 2] in {"'", '"'}:
        raw = True
        index += 1
    if source[index : index + 1] not in {"'", '"'}:
        return None
    quote = source[index]
    delimiter = quote * (3 if source.startswith(quote * 3, index) else 1)
    index += len(delimiter)
    next_deadline_check = index
    while index < len(source):
        if index >= next_deadline_check:
            _check_deadline(deadline)
            next_deadline_check = index + 1024
        if source.startswith(delimiter, index):
            return index + len(delimiter)
        if not raw and source[index] == "\\":
            index += 2
        else:
            index += 1
    return len(source)


def _candidate_platform_paths(candidate_paths: Iterable[str], *, deadline: float | None) -> list[str]:
    paths: set[str] = set()
    for candidate in candidate_paths:
        _check_deadline(deadline)
        if _is_platform_source_path(candidate):
            paths.add(candidate)
    _check_deadline(deadline)
    return sorted(paths)


def _platform_source_paths(root: Path, *, deadline: float | None) -> list[str]:
    paths: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        _check_deadline(deadline)
        dirnames[:] = sorted(name for name in dirnames if name not in DART_SCAN_IGNORED_DIRS)
        for filename in sorted(filenames):
            _check_deadline(deadline)
            path = Path(directory) / filename
            if _is_platform_source_path(path.as_posix()):
                try:
                    paths.append(path.relative_to(root).as_posix())
                except ValueError:
                    continue
    return paths


def _is_platform_source_path(path: str) -> bool:
    suffix = Path(path).suffix.casefold()
    return suffix in PLATFORM_CHANNEL_LANGUAGES and not (suffix == ".dart" and is_generated_dart_path(path))


def _repo_source_file(root: Path, relative: str) -> Path | None:
    lexical_root = Path(os.path.abspath(root))
    candidate = Path(relative)
    lexical_candidate = Path(os.path.abspath(candidate if candidate.is_absolute() else lexical_root / candidate))
    try:
        lexical_relative = lexical_candidate.relative_to(lexical_root)
        if not lexical_relative.parts:
            return None
        current = lexical_root
        for component in lexical_relative.parts:
            current /= component
            if current.is_symlink():
                return None
        resolved_root = lexical_root.resolve(strict=True)
        resolved = lexical_candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except OSError, ValueError:
        return None
    if not resolved.is_file() or resolved.suffix.casefold() not in PLATFORM_CHANNEL_LANGUAGES:
        return None
    return resolved


def _literal_value(token: str) -> str | None:
    raw = token[:1] in {"r", "R"}
    if raw:
        token = token[1:]
    if len(token) < 2 or token[0] not in "'\"" or token[-1] != token[0]:
        return None
    body = token[1:-1]
    if raw:
        return body
    result: list[str] = []
    index = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
    while index < len(body):
        if body[index] != "\\" or index + 1 >= len(body):
            result.append(body[index])
            index += 1
        else:
            result.append(escapes.get(body[index + 1], body[index + 1]))
            index += 2
    return "".join(result)


def _is_string_token(value: str) -> bool:
    return value.startswith(("'", '"', "r'", 'r"', "R'", 'R"'))


def _line_at(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _check_deadline(deadline: float | None) -> None:
    if _deadline_reached(deadline):
        raise _PlatformChannelDeadlineExpired
