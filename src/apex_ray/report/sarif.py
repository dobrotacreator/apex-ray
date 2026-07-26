"""Deterministic SARIF output for Apex Ray findings."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from apex_ray.findings import finding_fingerprint
from apex_ray.models import ContextPack, Finding, ReviewReport, RiskSignal

_RULE_ID = "APEX-RAY-REVIEW"
_SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_LEVELS = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
}
_URL = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^ \t\r\n`\"'<>]+",
    flags=re.IGNORECASE,
)
_MAX_URL_COMPONENT_DECODE_ROUNDS = 3
_LOCAL_URL_SCHEMES = frozenset(
    {
        "atom",
        "cursor",
        "cursor-insiders",
        "editor",
        "file",
        "idea",
        "jetbrains",
        "subl",
        "vscode",
        "vscode-insiders",
    }
)
_UNC_ABSOLUTE_PATH = re.compile(r"(?<![\\A-Za-z0-9_])\\\\[^ \t\r\n`\"'<>|]+")
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/])"
    r"(?:[^ \t\r\n`\"'<>|]+)"
)
_WINDOWS_ROOT_RELATIVE_PATH = re.compile(
    r"(?<![\\A-Za-z0-9_])\\(?!\\)"
    r"(?:[^ \t\r\n`\"'<>|\\/:]+[\\/])+"
    r"[^ \t\r\n`\"'<>|\\/:]+"
)
_WINDOWS_DRIVE_RELATIVE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:(?![\\/])"
    r"(?:[^ \t\r\n`\"'<>|\\/:]+[\\/])+"
    r"[^ \t\r\n`\"'<>|\\/:]+"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![/A-Za-z0-9_])/(?:[^ \t\r\n`\"'<>]+)")


def render_sarif(report: ReviewReport) -> str:
    """Render a review report as stable SARIF 2.1.0 JSON.

    Artifact locations are emitted only when they can be represented as paths
    inside the reviewed repository. This keeps local and CI workspace paths out
    of uploaded code-scanning artifacts.
    """

    packs_by_id = {pack.id: pack for pack in report.context_packs}
    results = [
        _finding_result(
            finding,
            root=report.project.root,
            context_pack=packs_by_id.get(finding.context_pack_id),
        )
        for finding in report.findings
    ]
    results.sort(
        key=lambda result: json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )
    payload = {
        "$schema": _SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Apex Ray",
                        "version": report.version,
                        "rules": [
                            {
                                "id": _RULE_ID,
                                "name": "ApexRayReviewFinding",
                                "shortDescription": {"text": "Apex Ray code review finding"},
                                "help": {
                                    "text": (
                                        "Review the failure mode and evidence, "
                                        "then apply the suggested fix and regression test."
                                    )
                                },
                            }
                        ],
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _finding_result(
    finding: Finding,
    *,
    root: str,
    context_pack: ContextPack | None,
) -> dict[str, Any]:
    severity = _enum_value(finding.severity)
    result: dict[str, Any] = {
        "ruleId": _RULE_ID,
        "level": _LEVELS.get(severity, "warning"),
        "message": {"text": _finding_message(finding, root)},
        "partialFingerprints": {
            "apexRayFinding/v1": _sarif_fingerprint(finding, root),
        },
        "properties": _finding_properties(finding, context_pack, root),
    }

    relative_path = _repo_relative_path(finding.file, root)
    if relative_path is not None:
        physical_location: dict[str, Any] = {
            "artifactLocation": {
                "uri": quote(relative_path, safe="/-._~"),
            }
        }
        if finding.line is not None and finding.line > 0:
            physical_location["region"] = {
                "startLine": finding.line,
                "startColumn": 1,
            }
        result["locations"] = [{"physicalLocation": physical_location}]

    return result


def _finding_message(finding: Finding, root: str) -> str:
    parts = [finding.title.strip()]
    details = (
        ("Failure mode", finding.failure_mode),
        ("Evidence", finding.evidence),
        ("Suggested fix", finding.suggested_fix),
        ("Suggested test", finding.suggested_test),
    )
    parts.extend(f"{label}: {value.strip()}" for label, value in details if value.strip())
    return _sanitize_text("\n".join(parts), root)


def _finding_properties(
    finding: Finding,
    context_pack: ContextPack | None,
    root: str,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "severity": _enum_value(finding.severity),
        "confidence": _enum_value(finding.confidence),
        "reviewerIds": _sorted_values(_sanitize_text(reviewer, root) for reviewer in finding.reviewer_ids),
    }
    signals = sorted(
        context_pack.risk_signals if context_pack is not None else [],
        key=_risk_signal_sort_key,
    )
    if not signals:
        return properties

    properties.update(
        {
            "riskKinds": _sorted_values(_sanitize_text(signal.kind, root) for signal in signals),
            "riskSeverities": _sorted_values(_enum_value(signal.severity) for signal in signals),
            "riskScore": max(signal.score for signal in signals),
            "riskCategories": _sorted_values(
                _sanitize_text(category, root) for signal in signals for category in signal.categories
            ),
            "riskReviewerTags": _sorted_values(
                _sanitize_text(reviewer, root) for signal in signals for reviewer in signal.reviewer_tags
            ),
            "riskSignals": [_risk_signal_properties(signal, root) for signal in signals],
        }
    )
    return properties


def _risk_signal_properties(signal: RiskSignal, root: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "kind": _sanitize_text(signal.kind, root),
        "severity": _enum_value(signal.severity),
        "score": signal.score,
        "source": _sanitize_text(signal.source, root),
        "categories": _sorted_values(_sanitize_text(category, root) for category in signal.categories),
        "reviewerTags": _sorted_values(_sanitize_text(reviewer, root) for reviewer in signal.reviewer_tags),
    }
    if signal.rule_id:
        properties["ruleId"] = _sanitize_text(signal.rule_id, root)
    return properties


def _sarif_fingerprint(finding: Finding, root: str) -> str:
    relative_path = _repo_relative_path(finding.file, root)
    if relative_path is None:
        identity = f"{finding.context_pack_id.strip()}\0{finding.file.strip()}"
        discriminator = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        relative_path = f"<outside-repository:{discriminator}>"
    canonical_finding = finding.model_copy(update={"file": relative_path})
    return finding_fingerprint(canonical_finding)


def _risk_signal_sort_key(signal: RiskSignal) -> tuple[Any, ...]:
    return (
        signal.kind,
        _enum_value(signal.severity),
        -signal.score,
        signal.source,
        signal.rule_id or "",
        tuple(sorted(set(signal.categories))),
        tuple(sorted(set(signal.reviewer_tags))),
    )


def _repo_relative_path(value: str, root: str) -> str | None:
    candidate = value.strip()
    if not candidate or candidate == "<unknown>" or "\x00" in candidate:
        return None

    if PurePosixPath(candidate).is_absolute():
        posix_candidate = PurePosixPath(candidate.replace("\\", "/"))
        posix_root = PurePosixPath(root.replace("\\", "/"))
        if not posix_root.is_absolute():
            return None
        try:
            relative = posix_candidate.relative_to(posix_root)
        except ValueError:
            return None
        parts = relative.parts
    elif _looks_windows_path(candidate) or _looks_windows_path(root):
        windows_candidate = PureWindowsPath(candidate)
        if windows_candidate.is_absolute():
            windows_root = PureWindowsPath(root)
            if not windows_root.is_absolute():
                return None
            try:
                relative = windows_candidate.relative_to(windows_root)
            except ValueError:
                return None
            parts = relative.parts
        elif windows_candidate.drive or windows_candidate.root:
            return None
        else:
            parts = windows_candidate.parts
    else:
        posix_candidate = PurePosixPath(candidate)
        parts = posix_candidate.parts

    safe_parts = [part for part in parts if part not in ("", ".")]
    if not safe_parts or any(part == ".." for part in safe_parts):
        return None
    return "/".join(safe_parts)


def _looks_windows_path(value: str) -> bool:
    path = PureWindowsPath(value)
    return bool(path.drive) or "\\" in value


def _sanitize_text(value: str, root: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _URL.finditer(value):
        parts.append(_redact_paths(value[cursor : match.start()], root))
        parts.append(_sanitize_url(match.group(0), root))
        cursor = match.end()
    parts.append(_redact_paths(value[cursor:], root))
    return "".join(parts)


def _sanitize_url(value: str, root: str) -> str:
    scheme = value.partition(":")[0].casefold()
    if scheme in _LOCAL_URL_SCHEMES:
        return "<local-url>"
    if _url_exposes_local_path(value, root):
        return "<remote-url-with-local-path>"
    return value


def _url_exposes_local_path(value: str, root: str) -> bool:
    decoded = unquote(value)
    try:
        parsed = urlsplit(decoded)
    except ValueError:
        return True
    if any(
        _url_component_exposes_local_path(component, root)
        for component in (parsed.netloc, parsed.path, parsed.fragment)
    ):
        return True
    for query_key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        for component in (query_key, query_value):
            if _url_component_exposes_local_path(
                component,
                root,
                include_absolute=True,
            ):
                return True
    return False


def _url_component_exposes_local_path(
    value: str,
    root: str,
    *,
    include_absolute: bool = False,
) -> bool:
    candidates = tuple(_decoded_url_component_variants(value))
    if any(
        _contains_windows_local_path(candidate)
        or _contains_repo_root_path(candidate, root)
        or (include_absolute and _is_absolute_path(candidate))
        for candidate in candidates
    ):
        return True
    return unquote(candidates[-1]) != candidates[-1]


def _decoded_url_component_variants(value: str) -> Iterable[str]:
    yield value
    for _ in range(_MAX_URL_COMPONENT_DECODE_ROUNDS):
        decoded = unquote(value)
        if decoded == value:
            return
        yield decoded
        value = decoded


def _contains_repo_root_path(value: str, root: str) -> bool:
    folded = value.replace("\\", "/").casefold()
    for raw_variant in _root_path_variants(root):
        variant = raw_variant.replace("\\", "/").casefold()
        if len(variant) <= 1:
            continue
        start = 0
        while (index := folded.find(variant, start)) >= 0:
            end = index + len(variant)
            before_is_boundary = (
                variant.startswith("/") or index == 0 or not (folded[index - 1].isalnum() or folded[index - 1] in "_.-")
            )
            after_is_boundary = (
                end == len(folded) or folded[end] in "/\\" or _is_terminal_local_path_suffix(folded[end:])
            )
            if before_is_boundary and after_is_boundary:
                return True
            start = index + 1
    return False


def _is_terminal_local_path_suffix(value: str) -> bool:
    return re.fullmatch(r"(?::\d+(?::\d+)?)?[.,;!?)}\]]*", value) is not None


def _contains_windows_local_path(value: str) -> bool:
    return any(
        pattern.search(value) is not None
        for pattern in (
            _UNC_ABSOLUTE_PATH,
            _WINDOWS_ABSOLUTE_PATH,
            _WINDOWS_ROOT_RELATIVE_PATH,
            _WINDOWS_DRIVE_RELATIVE_PATH,
        )
    )


def _is_absolute_path(value: str) -> bool:
    windows_path = PureWindowsPath(value)
    return (
        PurePosixPath(value).is_absolute()
        or windows_path.is_absolute()
        or value.startswith("\\\\")
        or _is_anchored_windows_local_path(windows_path)
    )


def _is_anchored_windows_local_path(path: PureWindowsPath) -> bool:
    if not path.drive and not path.root:
        return False
    # A second path component distinguishes local path syntax such as
    # ``C:Users\alice`` from an ordinary prose label such as ``C:Users``.
    return len(path.parts) >= 3


def _redact_paths(value: str, root: str) -> str:
    redacted = value
    for variant in sorted(
        (item for item in _root_path_variants(root) if len(item) > 1),
        key=len,
        reverse=True,
    ):
        redacted = redacted.replace(variant, "<repo>")
    redacted = _UNC_ABSOLUTE_PATH.sub("<absolute-path>", redacted)
    redacted = _WINDOWS_ABSOLUTE_PATH.sub("<absolute-path>", redacted)
    redacted = _WINDOWS_ROOT_RELATIVE_PATH.sub("<absolute-path>", redacted)
    redacted = _WINDOWS_DRIVE_RELATIVE_PATH.sub("<absolute-path>", redacted)
    return _POSIX_ABSOLUTE_PATH.sub("<absolute-path>", redacted)


def _root_path_variants(root: str) -> set[str]:
    return {
        root.rstrip("/\\"),
        root.replace("\\", "/").rstrip("/"),
        root.replace("/", "\\").rstrip("\\"),
    }


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _sorted_values(values: Iterable[str]) -> list[str]:
    return sorted(set(values))
