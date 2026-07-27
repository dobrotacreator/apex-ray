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
_TEXT_TOKEN = re.compile(r"[^ \t\r\n`\"'<>]+")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-F]{2}", flags=re.IGNORECASE)
_ENCODED_SLASH = re.compile(r"%2f", flags=re.ASCII | re.IGNORECASE)
_MAX_URL_COMPONENT_DECODE_ROUNDS = 3
_MAX_NESTED_URL_DEPTH = 3
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
_POSIX_LOCAL_ROOT_NAMES = frozenset(
    {
        "__w",
        "applications",
        "bin",
        "boot",
        "dev",
        "etc",
        "github",
        "home",
        "lib",
        "lib64",
        "library",
        "media",
        "mnt",
        "nix",
        "opt",
        "private",
        "proc",
        "root",
        "run",
        "runner",
        "sbin",
        "snap",
        "srv",
        "sys",
        "system",
        "tmp",
        "users",
        "usr",
        "var",
        "volumes",
        "workspace",
        "workspaces",
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
    is_outside_repository = relative_path is None
    if relative_path is None:
        identity = f"{finding.context_pack_id.strip()}\0{finding.file.strip()}"
        discriminator = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        relative_path = f"<outside-repository:{discriminator}>"
    canonical_finding = finding.model_copy(update={"file": relative_path})
    fingerprint = finding_fingerprint(canonical_finding)
    if is_outside_repository:
        # Keep synthetic outside identities disjoint from every valid
        # repository-relative path accepted by ``finding_fingerprint``.
        return f"apex-outside-{fingerprint.removeprefix('apex-')}"
    return fingerprint


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


def _url_exposes_local_path(
    value: str,
    root: str,
    *,
    preserve_leading_remote_path: bool = False,
    nested_depth: int = 0,
) -> bool:
    for decode_index, candidate in enumerate(_decoded_url_component_variants(value)):
        if _parsed_url_exposes_local_path(
            candidate,
            root,
            preserve_leading_remote_path=preserve_leading_remote_path,
            nested_depth=nested_depth,
            full_url_decoded=decode_index > 0,
        ):
            return True
    return False


def _parsed_url_exposes_local_path(
    value: str,
    root: str,
    *,
    preserve_leading_remote_path: bool,
    nested_depth: int,
    full_url_decoded: bool,
) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if not parsed.netloc and _is_absolute_path(parsed.path):
        return True
    if (
        _url_component_exposes_local_path(
            parsed.netloc,
            root,
            include_absolute=True,
            nested_depth=nested_depth,
        )
        or _url_component_exposes_local_path(
            parsed.path,
            root,
            ignore_leading_repo_root=preserve_leading_remote_path,
            nested_depth=nested_depth,
        )
        or _url_path_exposes_explicit_absolute_path(parsed.path)
        or _component_contains_encoded_absolute_path(parsed.path)
        or _url_component_exposes_local_path(
            parsed.fragment,
            root,
            include_encoded_absolute=True,
            include_decoded_absolute=full_url_decoded,
            ignore_leading_repo_root=preserve_leading_remote_path,
            nested_depth=nested_depth,
        )
        or _component_contains_encoded_absolute_path(
            parsed.fragment,
            require_structured_tail=True,
        )
    ):
        return True
    for query_key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        for component in (query_key, query_value):
            if _url_component_exposes_local_path(
                component,
                root,
                include_absolute=True,
                nested_depth=nested_depth,
            ):
                return True
    return False


def _url_component_exposes_local_path(
    value: str,
    root: str,
    *,
    include_absolute: bool = False,
    include_encoded_absolute: bool = False,
    include_decoded_absolute: bool = False,
    ignore_leading_repo_root: bool = False,
    nested_depth: int = 0,
) -> bool:
    candidates = tuple(_decoded_url_component_variants(value))
    original_is_absolute = _is_absolute_path(value)
    for candidate in candidates:
        if _contains_windows_local_path(candidate):
            return True
        if _contains_delimited_posix_absolute_path(candidate):
            return True
        if include_absolute:
            nested_url_status = _nested_url_exposes_local_path(
                candidate,
                root,
                nested_depth=nested_depth,
            )
            if nested_url_status is True:
                return True
            if nested_url_status is False:
                continue
        if (
            _contains_repo_root_path(
                candidate,
                root,
                ignore_leading=ignore_leading_repo_root,
            )
            or (
                include_absolute and (_is_absolute_path(candidate) or _contains_embedded_posix_absolute_path(candidate))
            )
            or ((include_encoded_absolute and not original_is_absolute) and _looks_like_local_posix_path(candidate))
            or (include_decoded_absolute and _looks_like_local_posix_path(candidate))
        ):
            return True
    return unquote(candidates[-1]) != candidates[-1]


def _nested_url_exposes_local_path(
    value: str,
    root: str,
    *,
    nested_depth: int,
) -> bool | None:
    """Return local-path exposure for a complete nested URL, if one is present."""

    if any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    scheme = parsed.scheme.casefold()
    if not scheme:
        return None
    if scheme in _LOCAL_URL_SCHEMES:
        return True
    if not parsed.netloc:
        return True if _is_absolute_path(parsed.path) else None
    if nested_depth >= _MAX_NESTED_URL_DEPTH:
        return True
    return _url_exposes_local_path(
        value,
        root,
        preserve_leading_remote_path=True,
        nested_depth=nested_depth + 1,
    )


def _contains_embedded_posix_absolute_path(value: str) -> bool:
    return any(not match.group(0).startswith("//") for match in _POSIX_ABSOLUTE_PATH.finditer(value))


def _url_path_exposes_explicit_absolute_path(value: str) -> bool:
    """Detect an absolute path deliberately embedded after a remote URL route."""

    for candidate in _decoded_url_component_variants(value):
        search_from = 0
        while (index := candidate.find("//", search_from)) >= 0:
            if index > 0 and candidate[index - 1] == ":":
                scheme = _scheme_before_double_slash(candidate, index)
                if scheme in _LOCAL_URL_SCHEMES:
                    return True
                search_from = index + 2
                continue
            component, search_from = _path_component_after_double_slash(candidate, index)
            if component is not None and component.casefold() in _POSIX_LOCAL_ROOT_NAMES:
                return True
    return False


def _component_contains_encoded_absolute_path(
    value: str,
    *,
    require_structured_tail: bool = False,
) -> bool:
    """Detect an encoded slash that explicitly starts an absolute path."""

    for candidate in _decoded_url_component_variants(value):
        skip_until = 0
        for match in _ENCODED_SLASH.finditer(candidate):
            index = match.start()
            if index < skip_until:
                continue
            previous = candidate[index - 1] if index > 0 else ""
            following_slash = _ENCODED_SLASH.match(candidate, match.end())
            if previous == ":" and following_slash is not None:
                scheme = _scheme_before_double_slash(candidate, index)
                if scheme:
                    third_slash = _ENCODED_SLASH.match(
                        candidate,
                        following_slash.end(),
                    )
                    if scheme in _LOCAL_URL_SCHEMES or third_slash is not None:
                        return True
                    skip_until = following_slash.end()
                    continue
            if index == 0 or previous == "/" or previous.isspace() or previous in "=;,:([{|":
                if not require_structured_tail:
                    return True
                decoded_tail = unquote(candidate[index:]).rstrip(".,;!?)}]")
                parts = [part for part in PurePosixPath(decoded_tail).parts if part != "/"]
                if _looks_like_local_posix_path(decoded_tail) or len(parts) >= 3:
                    return True
    return False


def _contains_delimited_posix_absolute_path(value: str) -> bool:
    """Detect a path following a field delimiter or prose boundary."""

    cursor = 0
    while (index := value.find("/", cursor)) >= 0:
        if (
            index == 0
            or (index + 1 < len(value) and value[index + 1] == "/")
            or not (value[index - 1].isspace() or value[index - 1] in "=;,:([{|")
        ):
            cursor = index + 1
            continue
        end = index + 1
        while end < len(value) and value[end] not in " \t\r\n?#&;":
            end += 1
        candidate = value[index:end].rstrip(".,;!?)}]")
        if _looks_like_local_posix_path(candidate):
            return True
        cursor = max(end, index + 1)
    return False


def _path_component_after_double_slash(value: str, slash_index: int) -> tuple[str | None, int]:
    cursor = slash_index + 2
    while cursor < len(value) and value[cursor] == "/":
        cursor += 1
    component_start = cursor
    while cursor < len(value) and value[cursor] not in "/?#":
        cursor += 1
    component = value[component_start:cursor] or None
    return component, max(cursor, slash_index + 2)


def _scheme_before_double_slash(value: str, slash_index: int) -> str | None:
    scheme_end = slash_index - 1
    cursor = scheme_end - 1
    while cursor >= 0 and (value[cursor].isascii() and (value[cursor].isalnum() or value[cursor] in "+.-")):
        cursor -= 1
    scheme = value[cursor + 1 : scheme_end]
    if not scheme or not scheme[0].isascii() or not scheme[0].isalpha():
        return None
    return scheme.casefold()


def _looks_like_local_posix_path(value: str) -> bool:
    value = value.rstrip(".,;!?)}]")
    if not _is_absolute_path(value):
        return False
    parts = [part for part in PurePosixPath(value).parts if part != "/"]
    if not parts:
        return False
    final_part = parts[-1]
    if final_part.startswith(".") or "." in final_part:
        return True
    return len(parts) >= 3 and parts[0].casefold() in _POSIX_LOCAL_ROOT_NAMES


def _decoded_url_component_variants(value: str) -> Iterable[str]:
    yield value
    for _ in range(_MAX_URL_COMPONENT_DECODE_ROUNDS):
        decoded = unquote(value)
        if decoded == value:
            return
        yield decoded
        value = decoded


def _contains_repo_root_path(
    value: str,
    root: str,
    *,
    ignore_leading: bool = False,
) -> bool:
    folded = value.replace("\\", "/").casefold()
    for raw_variant in _root_path_variants(root):
        variant = raw_variant.replace("\\", "/").casefold()
        if len(variant) <= 1:
            continue
        start = 0
        while (index := folded.find(variant, start)) >= 0:
            end = index + len(variant)
            if ignore_leading and index == 0:
                start = index + 1
                continue
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
    redacted = _TEXT_TOKEN.sub(
        lambda match: (
            "<encoded-local-reference>"
            if _PERCENT_ESCAPE.search(match.group(0)) and _encoded_token_exposes_local_reference(match.group(0), root)
            else match.group(0)
        ),
        value,
    )
    return _redact_literal_paths(redacted, root)


def _encoded_token_exposes_local_reference(value: str, root: str) -> bool:
    candidates = tuple(_decoded_url_component_variants(value))
    for candidate in candidates[1:]:
        if _decoded_text_exposes_local_reference(candidate, root):
            return True
    # Fail closed when bounded decoding still leaves another encoded layer.
    return bool(candidates[1:] and unquote(candidates[-1]) != candidates[-1])


def _decoded_text_exposes_local_reference(value: str, root: str) -> bool:
    cursor = 0
    for match in _URL.finditer(value):
        if _redact_literal_paths(value[cursor : match.start()], root) != value[cursor : match.start()]:
            return True
        if _sanitize_url(match.group(0), root) != match.group(0):
            return True
        cursor = match.end()
    return _redact_literal_paths(value[cursor:], root) != value[cursor:]


def _redact_literal_paths(value: str, root: str) -> str:
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
