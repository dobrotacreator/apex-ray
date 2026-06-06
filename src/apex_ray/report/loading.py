import copy
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from apex_ray.models import ReviewReport


class ReviewReportLoadError(ValueError):
    pass


def load_review_report(path: Path) -> ReviewReport:
    raw_text = path.read_text(encoding="utf-8")
    try:
        raw_payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ReviewReportLoadError(f"Invalid JSON in Apex Ray report {path}: {exc}") from exc
    try:
        return ReviewReport.model_validate(_normalize_review_report_payload(raw_payload))
    except ValidationError as exc:
        raise ReviewReportLoadError(f"Invalid Apex Ray report {path}: {exc}") from exc


def _normalize_review_report_payload(raw_payload: Any) -> Any:
    if not isinstance(raw_payload, dict):
        return raw_payload
    payload = copy.deepcopy(raw_payload)
    _normalize_analyzer_result_symbols(payload.get("analyzer_results"))
    _normalize_context_pack_symbols(payload.get("context_packs"))
    return payload


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
