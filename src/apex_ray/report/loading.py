import copy
import json
import zlib
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from apex_ray.models import ReviewReport

_MAX_REPORT_BYTES = 64 * 1024 * 1024
_MAX_GZIP_HEADER_BYTES = 64 * 1024
_COMPRESSED_READ_BYTES = 64 * 1024


class _BinaryReader(Protocol):
    def read(self, size: int | None = -1, /) -> bytes: ...


class _BoundedCompressedReader:
    def __init__(self, stream: _BinaryReader, path: Path) -> None:
        self._stream = stream
        self._path = path
        self._limit = _MAX_REPORT_BYTES + max(1024, _MAX_REPORT_BYTES // 64)
        self._read_bytes = 0

    def read(self, size: int | None = -1, /) -> bytes:
        if size == 0:
            return b""
        remaining = self._limit - self._read_bytes
        read_size = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
        chunk = self._stream.read(read_size)
        self._read_bytes += len(chunk)
        if self._read_bytes > self._limit:
            raise ReviewReportLoadError(f"compressed Apex Ray report {self._path} exceeds {self._limit} bytes")
        return chunk


class ReviewReportLoadError(ValueError):
    pass


def load_review_report(path: Path) -> ReviewReport:
    with path.open("rb") as stream:
        is_gzip = stream.read(2) == b"\x1f\x8b"
        stream.seek(0)
        try:
            if is_gzip:
                raw_bytes = _read_bounded_gzip_report(stream, path)
            else:
                raw_bytes = _read_bounded_report(stream, path)
        except (OSError, EOFError, zlib.error) as exc:
            kind = "gzip " if is_gzip else ""
            raise ReviewReportLoadError(f"Invalid {kind}Apex Ray report {path}: {exc}") from exc
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewReportLoadError(f"Invalid UTF-8 in Apex Ray report {path}: {exc}") from exc
    try:
        raw_payload = json.loads(raw_text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ReviewReportLoadError(f"Invalid JSON in Apex Ray report {path}: {exc}") from exc
    try:
        return ReviewReport.model_validate(_normalize_review_report_payload(raw_payload))
    except (RecursionError, ValidationError) as exc:
        raise ReviewReportLoadError(f"Invalid Apex Ray report {path}: {exc}") from exc


def _read_bounded_report(stream: _BinaryReader, path: Path) -> bytes:
    raw_bytes = stream.read(_MAX_REPORT_BYTES + 1)
    if len(raw_bytes) > _MAX_REPORT_BYTES:
        raise ReviewReportLoadError(f"Apex Ray report {path} exceeds {_MAX_REPORT_BYTES} bytes")
    return raw_bytes


def _read_bounded_gzip_report(stream: _BinaryReader, path: Path) -> bytes:
    compressed_stream = _BoundedCompressedReader(stream, path)
    chunk = compressed_stream.read(_MAX_GZIP_HEADER_BYTES + 1)
    _validate_gzip_header(chunk, path)

    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    output_parts: list[bytes] = []
    output_bytes = 0
    while chunk:
        output = decompressor.decompress(chunk, _MAX_REPORT_BYTES - output_bytes + 1)
        output_parts.append(output)
        output_bytes += len(output)
        if output_bytes > _MAX_REPORT_BYTES:
            raise ReviewReportLoadError(f"Apex Ray report {path} exceeds {_MAX_REPORT_BYTES} bytes")
        if decompressor.eof:
            if decompressor.unused_data or compressed_stream.read(1):
                raise ReviewReportLoadError(f"gzip Apex Ray report {path} contains trailing data")
            return b"".join(output_parts)
        chunk = decompressor.unconsumed_tail or compressed_stream.read(_COMPRESSED_READ_BYTES)

    raise ReviewReportLoadError(f"Invalid gzip Apex Ray report {path}: incomplete compressed stream")


def _validate_gzip_header(prefix: bytes, path: Path) -> None:
    if len(prefix) < 10:
        raise ReviewReportLoadError(f"Invalid gzip Apex Ray report {path}: truncated header")
    if prefix[:2] != b"\x1f\x8b" or prefix[2] != 8:
        raise ReviewReportLoadError(f"Invalid gzip Apex Ray report {path}: unsupported header")

    flags = prefix[3]
    if flags & 0xE0:
        raise ReviewReportLoadError(f"Invalid gzip Apex Ray report {path}: reserved header flags")

    offset = 10
    if flags & 0x04:
        extra_length_end = _require_gzip_header_bytes(prefix, offset, 2, path)
        extra_length = int.from_bytes(prefix[offset:extra_length_end], "little")
        offset = _require_gzip_header_bytes(prefix, extra_length_end, extra_length, path)
    for flag in (0x08, 0x10):
        if flags & flag:
            offset = _find_gzip_header_terminator(prefix, offset, path)
    if flags & 0x02:
        _require_gzip_header_bytes(prefix, offset, 2, path)


def _require_gzip_header_bytes(prefix: bytes, offset: int, size: int, path: Path) -> int:
    end = offset + size
    if end > _MAX_GZIP_HEADER_BYTES:
        raise ReviewReportLoadError(f"gzip header in Apex Ray report {path} exceeds {_MAX_GZIP_HEADER_BYTES} bytes")
    if end > len(prefix):
        raise ReviewReportLoadError(f"Invalid gzip Apex Ray report {path}: truncated header")
    return end


def _find_gzip_header_terminator(prefix: bytes, offset: int, path: Path) -> int:
    terminator = prefix.find(b"\0", offset, _MAX_GZIP_HEADER_BYTES + 1)
    if terminator < 0 or terminator + 1 > _MAX_GZIP_HEADER_BYTES:
        if len(prefix) > _MAX_GZIP_HEADER_BYTES:
            raise ReviewReportLoadError(f"gzip header in Apex Ray report {path} exceeds {_MAX_GZIP_HEADER_BYTES} bytes")
        raise ReviewReportLoadError(f"Invalid gzip Apex Ray report {path}: truncated header")
    return terminator + 1


def _normalize_review_report_payload(raw_payload: Any) -> Any:
    if not isinstance(raw_payload, dict):
        return raw_payload
    payload = copy.deepcopy(raw_payload)
    _normalize_analyzer_result_symbols(payload.get("analyzer_results"))
    _normalize_context_pack_symbols(payload.get("context_packs"))
    _normalize_legacy_verifications(payload)
    return payload


def _normalize_legacy_verifications(payload: dict[str, Any]) -> None:
    failed_verify_statuses: dict[tuple[str, str], str] = {}
    for run in _dict_items(payload.get("llm_runs")):
        if run.get("kind") != "verify" or run.get("status") == "ok":
            continue
        reviewer_id = str(run.get("reviewer_id") or "general")
        context_pack_id = str(run.get("context_pack_id") or "")
        failed_verify_statuses[(reviewer_id, context_pack_id)] = str(run.get("status") or "failed_provider")

    for verification in _dict_items(payload.get("verifications")):
        if "superseded" in verification or "superseded_reason" in verification:
            continue
        reason = verification.get("reason")
        finding = verification.get("finding")
        if not isinstance(reason, str) or not isinstance(finding, dict):
            continue
        reviewer_id = str(verification.get("reviewer_id") or "general")
        context_pack_id = str(finding.get("context_pack_id") or "")
        status: str | None = None
        if reason.startswith("Missing context pack:"):
            status = "missing_context_pack"
        elif reason.startswith(("Verifier failed for this finding:", "Verifier skipped because")):
            status = failed_verify_statuses.get((reviewer_id, context_pack_id))
        if status is None:
            continue
        verification["superseded"] = True
        verification["superseded_reason"] = f"Verification run did not complete successfully ({status})."


def _normalize_analyzer_result_symbols(analyzer_results: Any) -> None:
    for analyzer_result in _dict_items(analyzer_results):
        for analyzer_file in _dict_items(analyzer_result.get("files")):
            _normalize_symbol_list(analyzer_file.get("symbols"), fallback_start=1, fallback_end=1)
            _normalize_symbol_list(analyzer_file.get("changedSymbols"), fallback_start=1, fallback_end=1)
            _normalize_symbol_list(analyzer_file.get("changed_symbols"), fallback_start=1, fallback_end=1)


def _normalize_context_pack_symbols(context_packs: Any) -> None:
    for pack in _dict_items(context_packs):
        fallback_start, fallback_end = _pack_fallback_line_range(pack)
        _normalize_symbol(pack.get("symbol"), fallback_start=fallback_start, fallback_end=fallback_end)
        _normalize_symbol_list(pack.get("symbols"), fallback_start=fallback_start, fallback_end=fallback_end)


def _normalize_symbol_list(symbols: Any, *, fallback_start: int, fallback_end: int) -> None:
    for symbol in _dict_items(symbols):
        _normalize_symbol(symbol, fallback_start=fallback_start, fallback_end=fallback_end)


def _normalize_symbol(symbol: Any, *, fallback_start: int, fallback_end: int) -> None:
    if not isinstance(symbol, dict):
        return
    start_line = _line_value(symbol, "startLine", "start_line") or fallback_start
    end_line = _line_value(symbol, "endLine", "end_line") or fallback_end or start_line
    if end_line < start_line:
        end_line = start_line
    if _line_value(symbol, "startLine", "start_line") is None:
        symbol["startLine"] = start_line
    if _line_value(symbol, "endLine", "end_line") is None:
        symbol["endLine"] = end_line


def _pack_fallback_line_range(pack: dict[str, Any]) -> tuple[int, int]:
    changed_lines = pack.get("changed_lines") or pack.get("changedLines") or []
    if isinstance(changed_lines, list):
        for line_range in changed_lines:
            start_line, end_line = _line_range_values(line_range)
            if start_line is not None:
                return start_line, end_line or start_line
    for snippets_key in ("changed_snippets", "changedSnippets"):
        for snippet in _dict_items(pack.get(snippets_key)):
            start_line = _line_value(snippet, "startLine", "start_line")
            end_line = _line_value(snippet, "endLine", "end_line")
            if start_line is not None:
                return start_line, end_line or start_line
    return 1, 1


def _line_range_values(value: Any) -> tuple[int | None, int | None]:
    if isinstance(value, (list, tuple)) and value:
        start_line = _coerce_line(value[0])
        end_line = _coerce_line(value[1]) if len(value) > 1 else None
        return start_line, end_line
    if isinstance(value, dict):
        return _line_value(value, "start", "start_line"), _line_value(value, "end", "end_line")
    return None, None


def _line_value(data: dict[str, Any], alias: str, field_name: str) -> int | None:
    for key in (alias, field_name):
        if key in data:
            value = _coerce_line(data[key])
            if value is not None:
                return value
    return None


def _coerce_line(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(value, 1)
    if isinstance(value, str) and value.isdecimal():
        return max(int(value), 1)
    return None


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
