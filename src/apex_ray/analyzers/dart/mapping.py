import os
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apex_ray.models import AnalyzerReference, AnalyzerSymbol

from .protocol import file_uri_to_path

_LSP_SYMBOL_KINDS = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum_member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type_parameter",
}

DART_REFERENCE_SOURCE_FILE_SIZE_LIMIT = 512_000
DART_REFERENCE_SOURCE_TOTAL_BYTE_LIMIT = 32 * 1024 * 1024
DART_REFERENCE_LINE_CHECKPOINT_INTERVAL = 128
DART_DOCUMENT_SYMBOL_NAME_CHAR_LIMIT = 1_000
DART_DOCUMENT_SYMBOL_SIGNATURE_CHAR_LIMIT = 1_000


@dataclass(frozen=True, slots=True)
class DartDocumentSymbol:
    symbol: AnalyzerSymbol
    uri: str
    position: dict[str, int]


@dataclass(frozen=True, slots=True)
class DartDocumentSymbolCollection:
    symbols: list[DartDocumentSymbol]
    truncated: bool


@dataclass(frozen=True, slots=True)
class DartReferenceCollection:
    references: list[AnalyzerReference]
    excluded_count: int
    partial: bool = False


@dataclass(frozen=True, slots=True)
class _CachedReferenceSource:
    text: str
    line_checkpoints: tuple[int, ...]
    line_count: int

    def line(self, zero_based_line: int) -> str:
        if zero_based_line < 0 or zero_based_line >= self.line_count:
            return ""
        checkpoint = zero_based_line // DART_REFERENCE_LINE_CHECKPOINT_INTERVAL
        current_line = checkpoint * DART_REFERENCE_LINE_CHECKPOINT_INTERVAL
        start = self.line_checkpoints[checkpoint]
        while current_line < zero_based_line:
            newline = self.text.find("\n", start)
            if newline < 0:
                return ""
            start = newline + 1
            current_line += 1
        end = self.text.find("\n", start)
        if end < 0:
            end = len(self.text)
        while end > start and self.text[end - 1] in "\r\n":
            end -= 1
        return self.text[start:end][:1_000]


@dataclass(slots=True)
class DartReferenceSourceReader:
    """Read and cache repository reference lines within one analyzer run.

    Limits apply to source bytes retained across the whole reader. ``deadline``
    is an absolute ``time.monotonic()`` value shared with the analyzer run.
    Rejected, unreadable, oversized, or deadline-truncated sources set
    ``partial`` and are negatively cached so repeated LSP locations cannot
    trigger repeated filesystem work.
    """

    repo_root: Path
    max_file_bytes: int = DART_REFERENCE_SOURCE_FILE_SIZE_LIMIT
    max_total_source_bytes: int = DART_REFERENCE_SOURCE_TOTAL_BYTE_LIMIT
    deadline: float | None = None
    source_bytes_read: int = field(init=False, default=0)
    files_read: int = field(init=False, default=0)
    skipped_files: int = field(init=False, default=0)
    line_index_entries: int = field(init=False, default=0)
    partial: bool = field(init=False, default=False)
    _lexical_root: Path = field(init=False, repr=False)
    _sources: dict[Path, _CachedReferenceSource | None] = field(init=False, default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.max_file_bytes <= 0 or self.max_total_source_bytes <= 0:
            raise ValueError("Dart reference source byte limits must be positive")
        if self.deadline is not None and self.deadline != self.deadline:
            raise ValueError("Dart reference source deadline must not be NaN")
        self._lexical_root = _lexical_absolute(self.repo_root)
        self.repo_root = self._lexical_root.resolve(strict=False)

    def source_line(self, path: Path, zero_based_line: int) -> str:
        """Return one cached source line, or an empty string when unavailable."""

        if self.deadline_expired():
            return ""
        key = _lexical_absolute(path)
        cached = self._sources.get(key)
        if key in self._sources:
            return cached.line(zero_based_line) if cached is not None else ""

        safe_path = self._safe_repo_file(key)
        if safe_path is None:
            self._skip(key)
            return ""
        try:
            size = safe_path.stat().st_size
        except OSError:
            self._skip(key)
            return ""
        remaining = self.max_total_source_bytes - self.source_bytes_read
        if size > self.max_file_bytes or size > remaining:
            self._skip(key)
            return ""

        text = _read_bounded_repo_text(
            self.repo_root,
            safe_path,
            max_bytes=min(self.max_file_bytes, remaining),
            deadline=self.deadline,
        )
        if text is None or self.deadline_expired():
            self._skip(key)
            return ""
        cached_source = _cached_reference_source(text, deadline=self.deadline)
        if cached_source is None or self.deadline_expired():
            self._skip(key)
            return ""

        self._sources[key] = cached_source
        self.source_bytes_read += size
        self.files_read += 1
        self.line_index_entries += len(cached_source.line_checkpoints)
        return cached_source.line(zero_based_line)

    def deadline_expired(self) -> bool:
        if not _deadline_reached(self.deadline):
            return False
        self.partial = True
        return True

    def _skip(self, key: Path) -> None:
        if key not in self._sources:
            self._sources[key] = None
            self.skipped_files += 1
        self.partial = True

    def _safe_repo_file(self, path: Path) -> Path | None:
        bases: tuple[Path, ...] = (
            (self._lexical_root,) if self._lexical_root == self.repo_root else (self._lexical_root, self.repo_root)
        )
        for base in bases:
            try:
                relative = path.relative_to(base)
            except ValueError:
                continue
            if not relative.parts:
                return None
            current = base
            try:
                for component in relative.parts:
                    current /= component
                    if current.is_symlink():
                        return None
                resolved = path.resolve(strict=True)
                resolved.relative_to(self.repo_root)
            except OSError, ValueError:
                return None
            return resolved if resolved.is_file() else None
        return None


@dataclass(frozen=True, slots=True)
class _DartReferenceCandidate:
    path: Path
    file: str
    line: int
    end_line: int
    kind: str

    @property
    def identity(self) -> tuple[str, int, int, str]:
        return (self.file, self.line, self.end_line, self.kind)


def flatten_document_symbols(payload: object, *, uri: str) -> list[DartDocumentSymbol]:
    return collect_document_symbols(payload, uri=uri).symbols


def collect_document_symbols(
    payload: object,
    *,
    uri: str,
    limit: int | None = None,
) -> DartDocumentSymbolCollection:
    if not isinstance(payload, list):
        return DartDocumentSymbolCollection(symbols=[], truncated=False)
    if limit is not None and limit <= 0:
        return DartDocumentSymbolCollection(symbols=[], truncated=bool(payload))

    flattened: list[DartDocumentSymbol] = []
    truncated = [False]
    for item in payload:
        if isinstance(item, dict):
            _flatten_document_symbol(
                item,
                default_uri=uri,
                parent_name="",
                output=flattened,
                limit=limit,
                truncated=truncated,
            )
        if truncated[0]:
            break
    return DartDocumentSymbolCollection(symbols=flattened, truncated=truncated[0])


def _flatten_document_symbol(
    payload: dict[str, Any],
    *,
    default_uri: str,
    parent_name: str,
    output: list[DartDocumentSymbol],
    limit: int | None = None,
    truncated: list[bool] | None = None,
) -> None:
    stack = [_document_symbol_items([payload], default_uri=default_uri, parent_name=parent_name)]
    while stack:
        try:
            current, current_default_uri, current_parent_name = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if limit is not None and len(output) >= limit:
            if truncated is not None:
                truncated[0] = True
            return
        name = current.get("name")
        if not isinstance(name, str) or not name:
            continue

        location = current.get("location")
        if isinstance(location, dict):
            raw_symbol_uri = location.get("uri")
            symbol_uri: str = raw_symbol_uri if isinstance(raw_symbol_uri, str) else current_default_uri
            range_payload = location.get("range")
            selection_range = range_payload
            container = current.get("containerName")
            qualified_name = _bounded_symbol_name(
                container if isinstance(container, str) else "",
                name,
            )
        else:
            symbol_uri = current_default_uri
            range_payload = current.get("range")
            selection_range = current.get("selectionRange", range_payload)
            qualified_name = _bounded_symbol_name(current_parent_name, name)

        parsed_range = _parse_range(range_payload)
        parsed_selection = _parse_range(selection_range)
        if parsed_range is None or parsed_selection is None:
            continue
        start, end = parsed_range
        selection_start, _selection_end = parsed_selection
        detail = current.get("detail")
        signature = detail[:DART_DOCUMENT_SYMBOL_SIGNATURE_CHAR_LIMIT].strip() if isinstance(detail, str) else ""
        kind = current.get("kind")
        simple_name = qualified_name.rsplit(".", 1)[-1]
        output.append(
            DartDocumentSymbol(
                symbol=AnalyzerSymbol(
                    name=qualified_name,
                    kind=_LSP_SYMBOL_KINDS.get(kind, "unknown") if isinstance(kind, int) else "unknown",
                    startLine=start["line"] + 1,
                    endLine=_inclusive_end_line(start, end),
                    exported=not simple_name.startswith("_"),
                    signature=signature,
                ),
                uri=symbol_uri,
                position=selection_start,
            )
        )

        children = current.get("children")
        if isinstance(children, list):
            stack.append(
                _document_symbol_items(
                    children,
                    default_uri=symbol_uri,
                    parent_name=qualified_name,
                )
            )


def _document_symbol_items(
    items: list[object],
    *,
    default_uri: str,
    parent_name: str,
) -> Iterator[tuple[dict[str, Any], str, str]]:
    for item in items:
        if isinstance(item, dict):
            yield item, default_uri, parent_name


def _bounded_symbol_name(parent_name: str, name: str) -> str:
    leaf = name[:DART_DOCUMENT_SYMBOL_NAME_CHAR_LIMIT]
    if not parent_name or len(leaf) >= DART_DOCUMENT_SYMBOL_NAME_CHAR_LIMIT:
        return leaf
    parent_budget = DART_DOCUMENT_SYMBOL_NAME_CHAR_LIMIT - len(leaf) - 1
    return f"{parent_name[-parent_budget:]}.{leaf}" if parent_budget > 0 else leaf


def changed_document_symbols(
    symbols: list[DartDocumentSymbol],
    changed_ranges: list[tuple[int, int]],
) -> list[DartDocumentSymbol]:
    return [
        item
        for item in symbols
        if any(
            item.symbol.start_line <= changed_end and changed_start <= item.symbol.end_line
            for changed_start, changed_end in changed_ranges
        )
    ]


def analyzer_reference_from_lsp_location(
    repo_root: Path,
    payload: object,
    *,
    kind: str,
    text: str | None = None,
    reader: DartReferenceSourceReader | None = None,
) -> AnalyzerReference | None:
    source_reader = reader or DartReferenceSourceReader(repo_root)
    _require_reader_root(repo_root, source_reader)
    if source_reader.deadline_expired():
        return None
    candidate = _reference_candidate_from_lsp_location(repo_root, payload, kind=kind)
    if candidate is None:
        return None
    reference_text = (
        text.strip() if isinstance(text, str) else source_reader.source_line(candidate.path, candidate.line - 1)
    )
    return AnalyzerReference(
        file=candidate.file,
        line=candidate.line,
        endLine=candidate.end_line,
        text=reference_text,
        kind=candidate.kind,
    )


def analyzer_references_from_lsp_locations(
    repo_root: Path,
    payload: object,
    *,
    kind: str,
    limit: int,
    exclude: Callable[[str], bool] | None = None,
    allowed: Callable[[str], bool] | None = None,
    reader: DartReferenceSourceReader | None = None,
) -> list[AnalyzerReference]:
    return collect_analyzer_references_from_lsp_locations(
        repo_root,
        payload,
        kind=kind,
        limit=limit,
        exclude=exclude,
        allowed=allowed,
        reader=reader,
    ).references


def collect_analyzer_references_from_lsp_locations(
    repo_root: Path,
    payload: object,
    *,
    kind: str,
    limit: int,
    exclude: Callable[[str], bool] | None = None,
    allowed: Callable[[str], bool] | None = None,
    reader: DartReferenceSourceReader | None = None,
) -> DartReferenceCollection:
    if not isinstance(payload, list) or limit <= 0:
        return DartReferenceCollection(references=[], excluded_count=0, partial=False)
    source_reader = reader or DartReferenceSourceReader(repo_root)
    _require_reader_root(repo_root, source_reader)
    candidates: dict[tuple[str, int, int, str], _DartReferenceCandidate] = {}
    excluded_count = 0
    for raw_location in payload:
        if source_reader.deadline_expired():
            break
        candidate = _reference_candidate_from_lsp_location(repo_root, raw_location, kind=kind)
        if source_reader.deadline_expired():
            break
        if candidate is None:
            continue
        if exclude is not None and exclude(candidate.file):
            excluded_count += 1
            continue
        if allowed is not None and not allowed(candidate.file):
            continue
        candidates.setdefault(candidate.identity, candidate)

    retained = sorted(candidates.values(), key=lambda item: item.identity)[:limit]
    references: list[AnalyzerReference] = []
    for candidate in retained:
        if source_reader.deadline_expired():
            break
        references.append(
            AnalyzerReference(
                file=candidate.file,
                line=candidate.line,
                endLine=candidate.end_line,
                text=source_reader.source_line(candidate.path, candidate.line - 1),
                kind=candidate.kind,
            )
        )
    return DartReferenceCollection(
        references=references,
        excluded_count=excluded_count,
        partial=source_reader.partial,
    )


def _reference_candidate_from_lsp_location(
    repo_root: Path,
    payload: object,
    *,
    kind: str,
) -> _DartReferenceCandidate | None:
    if not isinstance(payload, dict):
        return None
    uri = payload.get("uri") or payload.get("targetUri")
    range_payload = payload.get("range") or payload.get("targetRange")
    if not isinstance(uri, str):
        return None
    parsed_range = _parse_range(range_payload)
    if parsed_range is None:
        return None

    uri_path = _path_from_file_uri(uri)
    if uri_path is None:
        return None
    try:
        lexical_root = _lexical_absolute(repo_root)
        root = lexical_root.resolve(strict=False)
        path = _lexical_absolute(uri_path)
        relative = path.relative_to(lexical_root)
        resolved = path.resolve(strict=False)
        resolved.relative_to(root)
    except OSError, ValueError:
        return None
    if not relative.parts:
        return None

    start, end = parsed_range
    return _DartReferenceCandidate(
        path=path,
        file=relative.as_posix(),
        line=start["line"] + 1,
        end_line=_inclusive_end_line(start, end),
        kind=kind,
    )


def _parse_range(payload: object) -> tuple[dict[str, int], dict[str, int]] | None:
    if not isinstance(payload, dict):
        return None
    start = _parse_position(payload.get("start"))
    end = _parse_position(payload.get("end"))
    if start is None or end is None:
        return None
    return start, end


def _parse_position(payload: object) -> dict[str, int] | None:
    if not isinstance(payload, dict):
        return None
    line = payload.get("line")
    character = payload.get("character")
    if not isinstance(line, int) or isinstance(line, bool) or line < 0:
        return None
    if not isinstance(character, int) or isinstance(character, bool) or character < 0:
        return None
    return {"line": line, "character": character}


def _inclusive_end_line(start: dict[str, int], end: dict[str, int]) -> int:
    if end["line"] > start["line"] and end["character"] == 0:
        return end["line"]
    return end["line"] + 1


def _path_from_file_uri(uri: str) -> Path | None:
    try:
        path = file_uri_to_path(uri)
    except ValueError:
        return None
    return path if isinstance(path, Path) else None


def _require_reader_root(repo_root: Path, reader: DartReferenceSourceReader) -> None:
    if repo_root.resolve(strict=False) != reader.repo_root:
        raise ValueError("Dart reference source reader belongs to a different repository root")


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _cached_reference_source(text: str, *, deadline: float | None) -> _CachedReferenceSource | None:
    checkpoints: list[int] = []
    cursor = 0
    line_count = 0
    while cursor < len(text):
        if line_count % 1_024 == 0 and _deadline_reached(deadline):
            return None
        if line_count % DART_REFERENCE_LINE_CHECKPOINT_INTERVAL == 0:
            checkpoints.append(cursor)
        newline = text.find("\n", cursor)
        line_count += 1
        if newline < 0:
            break
        cursor = newline + 1
    if _deadline_reached(deadline):
        return None
    return _CachedReferenceSource(
        text=text,
        line_checkpoints=tuple(checkpoints),
        line_count=line_count,
    )


def _read_bounded_repo_text(
    repo_root: Path,
    path: Path,
    *,
    max_bytes: int,
    deadline: float | None,
) -> str | None:
    """Read one stable regular repository file without following symlinks."""

    if max_bytes < 0 or _deadline_reached(deadline):
        return None
    try:
        root = repo_root.resolve(strict=True)
        candidate = _lexical_absolute(path)
        relative = candidate.relative_to(root)
        if not relative.parts:
            return None
        current = root
        for component in relative.parts:
            if _deadline_reached(deadline):
                return None
            current /= component
            if current.is_symlink():
                return None
        before_path = candidate.lstat()
        if not stat.S_ISREG(before_path.st_mode) or before_path.st_size > max_bytes:
            return None
    except OSError, ValueError:
        return None

    descriptor: int | None = None
    try:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > max_bytes
            or _stable_file_identity(before) != _stable_file_identity(before_path)
        ):
            return None

        payload = bytearray()
        while len(payload) <= max_bytes:
            if _deadline_reached(deadline):
                return None
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes or _deadline_reached(deadline):
            return None

        after = os.fstat(descriptor)
        after_path = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if (
            _stable_file_identity(before) != _stable_file_identity(after)
            or _stable_file_identity(after) != _stable_file_identity(after_path)
            or not stat.S_ISREG(after_path.st_mode)
        ):
            return None
    except OSError, ValueError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return bytes(payload).decode("utf-8", errors="replace")


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _source_line(path: Path, zero_based_line: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index == zero_based_line:
                    return line.rstrip("\r\n")[:1_000]
    except OSError:
        return ""
    return ""
