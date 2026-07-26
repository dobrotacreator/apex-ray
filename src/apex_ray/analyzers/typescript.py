import json
import os
import posixpath
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Protocol

from pydantic import ValidationError

from apex_ray.discovery import list_project_files
from apex_ray.models import (
    AnalyzerConfig,
    AnalyzerIndexCacheStats,
    AnalyzerResult,
    AnalyzerShardFailure,
    ChangedFile,
    FileKind,
    RiskSeverity,
)
from apex_ray.risk import risk_signal_score

from .common import AnalyzerError, _collapse_ranges

TS_JS_LANGUAGES = {"typescript", "javascript"}
TS_JS_INDEX_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
TS_CONFIG_ROOT_NAMES = {"jsconfig.json", "tsconfig.json"}
TS_CONFIG_METADATA_SUFFIXES = {".json", ".jsonc"}
TS_CONFIG_MAX_BYTES = 4 * 1024 * 1024
# Keep these producer bounds in sync with workspace/inventory.ts. The optional
# v2 partial_reason marker lets the consumer retain changed-file analysis
# without mistaking a truncated repository inventory for complete context.
TS_FILE_MANIFEST_ENTRY_LIMIT = 50_000
TS_FILE_MANIFEST_BYTE_LIMIT = 16 * 1024 * 1024
_TS_FILE_MANIFEST_HEADER = b'{"version":2'
_TS_FILE_MANIFEST_SECTIONS = ("files", "package_files", "config_files")
_TS_FILE_MANIFEST_PARTIAL_PREFIX = b',"partial_reason":'
_TS_FILE_MANIFEST_PARTIAL_SUFFIX = "; repository context is partial."


@dataclass(frozen=True)
class _TypescriptPackageConfig:
    directory: str
    tsconfig: str | None
    exports: object


@dataclass(frozen=True)
class _TypescriptManifestPlan:
    partial_reason: str | None
    byte_limited: bool


class _BinaryManifestStream(Protocol):
    def write(self, data: bytes, /) -> int: ...

    def tell(self) -> int: ...


def has_ts_js_changes(files: list[ChangedFile]) -> bool:
    return bool(ts_js_changed_files(files))


def ts_js_changed_files(files: list[ChangedFile]) -> list[ChangedFile]:
    return [
        file
        for file in files
        if file.language in TS_JS_LANGUAGES
        and file.file_kind in {FileKind.SOURCE, FileKind.TEST}
        and not file.is_ignored
        and file.new_path is not None
    ]


def run_typescript_analyzer(
    repo_root: Path,
    files: list[ChangedFile],
    config: AnalyzerConfig | None = None,
    *,
    ignored_patterns: list[str] | None = None,
    project_files: list[Path] | None = None,
) -> AnalyzerResult | None:
    changed_files = ts_js_changed_files(files)
    if not changed_files:
        return None
    config = config or AnalyzerConfig()
    if shutil.which("node") is None:
        raise AnalyzerError("Node.js is required for the TypeScript analyzer but was not found on PATH.")

    script = typescript_analyzer_script(config, repo_root)
    if not script.exists():
        raise AnalyzerError(f"TypeScript analyzer is not built: {script}")

    started_at = time.monotonic()
    shards = list(_shard_changed_files(changed_files, config))
    total_timeout_seconds = _typescript_total_timeout_seconds(changed_files, shards, config)
    deadline = started_at + total_timeout_seconds
    with tempfile.TemporaryDirectory(prefix="apex-ray-typescript-inventory-") as inventory_dir:
        file_manifest_path = Path(inventory_dir) / "files.json"
        _write_typescript_file_manifest(
            repo_root,
            file_manifest_path,
            ignored_patterns,
            project_files=project_files,
            deadline=deadline,
            total_timeout_seconds=total_timeout_seconds,
        )
        return _run_typescript_analyzer_shards(
            repo_root,
            changed_files,
            config,
            script,
            file_manifest_path,
            shards=shards,
            deadline=deadline,
            total_timeout_seconds=total_timeout_seconds,
        )


def _run_typescript_analyzer_shards(
    repo_root: Path,
    changed_files: list[ChangedFile],
    config: AnalyzerConfig,
    script: Path,
    file_manifest_path: Path,
    *,
    shards: list[list[ChangedFile]],
    deadline: float,
    total_timeout_seconds: float,
) -> AnalyzerResult:
    results: list[AnalyzerResult] = []
    failures: list[AnalyzerShardFailure] = []
    large_change_set_size = len(changed_files) if len(changed_files) >= config.large_change_file_threshold else None
    for index, shard in enumerate(shards, start=1):
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            timeout_error = AnalyzerError(
                f"TypeScript analyzer total timeout after {_format_seconds(total_timeout_seconds)}"
            )
            failures.extend(
                _shard_failure(
                    skipped_index,
                    len(shards),
                    skipped_shard,
                    timeout_error,
                    status="timeout",
                )
                for skipped_index, skipped_shard in enumerate(shards[index - 1 :], start=index)
            )
            break
        try:
            results.append(
                _run_typescript_analyzer_shard(
                    repo_root,
                    script,
                    shard,
                    config,
                    timeout_seconds=min(config.timeout_seconds, remaining_seconds),
                    large_change_set_size=large_change_set_size,
                    file_manifest_path=file_manifest_path,
                )
            )
        except AnalyzerError as exc:
            failures.append(_shard_failure(index, len(shards), shard, exc))

    if not results:
        if len(shards) == 1 and len(failures) == 1:
            raise AnalyzerError(_format_shard_failure(failures[0]))
        raise AnalyzerError(
            "TypeScript analyzer failed for all shards: "
            + "; ".join(_format_shard_failure(failure) for failure in failures)
        )

    result = _merge_analyzer_results(results)
    if failures:
        result.warnings.append(
            f"Returning partial TypeScript analyzer result because {len(failures)} of {len(shards)} shards failed."
        )
        result.warnings.extend(_format_shard_failure(failure) for failure in failures)
        result.partial = True
        result.failed_files = [path for failure in failures for path in failure.files]
        result.shard_failures = failures
    return result


def _run_typescript_analyzer_shard(
    repo_root: Path,
    script: Path,
    changed_files: list[ChangedFile],
    config: AnalyzerConfig,
    timeout_seconds: float | None = None,
    large_change_set_size: int | None = None,
    file_manifest_path: Path | None = None,
) -> AnalyzerResult:
    actual_timeout = config.timeout_seconds if timeout_seconds is None else max(0.001, timeout_seconds)
    args = _typescript_analyzer_args(
        repo_root,
        script,
        changed_files,
        config,
        large_change_set_size=large_change_set_size,
        analysis_time_budget_ms=_analysis_time_budget_ms(actual_timeout),
        file_manifest_path=file_manifest_path,
    )
    try:
        proc = _run_analyzer_process(
            args,
            cwd=repo_root,
            timeout=actual_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalyzerError(f"TypeScript analyzer timed out after {_format_seconds(actual_timeout)}") from exc
    if proc.returncode != 0:
        raise AnalyzerError(proc.stderr.strip() or proc.stdout.strip() or "TypeScript analyzer failed")

    try:
        return AnalyzerResult.model_validate(json.loads(proc.stdout))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise AnalyzerError(f"Invalid TypeScript analyzer output: {exc}") from exc


def _typescript_analyzer_args(
    repo_root: Path,
    script: Path,
    changed_files: list[ChangedFile],
    config: AnalyzerConfig,
    *,
    large_change_set_size: int | None = None,
    analysis_time_budget_ms: int | None = None,
    file_manifest_path: Path | None = None,
) -> list[str]:
    args = ["node", str(script), "--repo", str(repo_root), "--changed"]
    args.extend(file.new_path for file in changed_files if file.new_path)
    if large_change_set_size is not None:
        args.extend(["--large-change-set-size", str(large_change_set_size)])
    if analysis_time_budget_ms is not None:
        args.extend(["--analysis-time-budget-ms", str(analysis_time_budget_ms)])
    if file_manifest_path is not None:
        args.extend(["--file-manifest", str(file_manifest_path)])
    if not config.index_cache_enabled:
        args.append("--no-index-cache")
    if config.index_cache_dir:
        args.extend(["--index-cache-dir", config.index_cache_dir])
    if config.refresh_index_cache:
        args.append("--refresh-index-cache")
    for file in changed_files:
        for start, end in _changed_new_line_ranges(file):
            args.extend(["--range", f"{file.path}:{start}-{end}"])
        for line, content in _deleted_lines(file):
            args.extend(["--deleted-line", file.path, str(line), content])
    return args


def _typescript_inventory_deadline_check(
    deadline: float | None,
    total_timeout_seconds: float | None,
) -> Callable[[], None]:
    if deadline is None:
        return lambda: None

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise _typescript_inventory_timeout_error(total_timeout_seconds)

    return check_deadline


def _remaining_typescript_inventory_seconds(
    deadline: float | None,
    total_timeout_seconds: float | None,
) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _typescript_inventory_timeout_error(total_timeout_seconds)
    return remaining


def _typescript_inventory_timeout_error(total_timeout_seconds: float | None) -> AnalyzerError:
    if total_timeout_seconds is None:
        return AnalyzerError("TypeScript analyzer timed out while building repository inventory")
    return AnalyzerError(
        "TypeScript analyzer total timeout after "
        f"{_format_seconds(total_timeout_seconds)} while building repository inventory"
    )


def _write_typescript_file_manifest(
    repo_root: Path,
    manifest_path: Path,
    ignored_patterns: list[str] | None = None,
    *,
    project_files: list[Path] | None = None,
    deadline: float | None = None,
    total_timeout_seconds: float | None = None,
) -> None:
    check_deadline = _typescript_inventory_deadline_check(
        deadline,
        total_timeout_seconds,
    )
    check_deadline()
    if project_files is not None:
        inventory = project_files
    else:
        try:
            inventory = list_project_files(
                repo_root,
                ignored_patterns,
                timeout_seconds=_remaining_typescript_inventory_seconds(
                    deadline,
                    total_timeout_seconds,
                ),
            )
        except TimeoutError as exc:
            raise _typescript_inventory_timeout_error(total_timeout_seconds) from exc
    check_deadline()
    ordered_inventory = _ordered_typescript_inventory(
        inventory,
        check_deadline=check_deadline,
    )
    reachable_config_paths = _reachable_typescript_config_paths(
        repo_root,
        ordered_inventory,
        check_deadline=check_deadline,
    )
    plan = _plan_typescript_file_manifest(
        ordered_inventory,
        reachable_config_paths,
        check_deadline=check_deadline,
    )
    temporary_path: Path | None = None
    try:
        check_deadline()
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=manifest_path.parent,
            prefix=f".{manifest_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            _stream_typescript_file_manifest(
                temporary_file,
                ordered_inventory,
                reachable_config_paths,
                plan,
                check_deadline=check_deadline,
            )
            check_deadline()
        temporary_path.replace(manifest_path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _plan_typescript_file_manifest(
    inventory: list[Path],
    reachable_config_paths: set[str],
    *,
    check_deadline: Callable[[], None],
) -> _TypescriptManifestPlan:
    # Plan sizes without materializing the JSON payload. A second pass streams
    # the selected entries atomically while reserving the marker and all
    # structural bytes inside the consumer's byte limit.
    complete_size = _typescript_file_manifest_base_size(None)
    entry_limited = False
    for section in _TS_FILE_MANIFEST_SECTIONS:
        entry_count = 0
        for path in _iter_typescript_manifest_section(
            section,
            inventory,
            reachable_config_paths,
            check_deadline=check_deadline,
        ):
            entry_count += 1
            if entry_count > TS_FILE_MANIFEST_ENTRY_LIMIT:
                entry_limited = True
                continue
            encoded_path = _encode_typescript_manifest_path(path)
            complete_size += len(encoded_path) + (1 if entry_count > 1 else 0)

    entry_reason = _typescript_manifest_partial_reason(
        entry_limited=entry_limited,
        byte_limited=False,
    )
    candidate_size = complete_size + _typescript_file_manifest_partial_size(entry_reason)
    byte_limited = candidate_size > TS_FILE_MANIFEST_BYTE_LIMIT
    partial_reason = _typescript_manifest_partial_reason(
        entry_limited=entry_limited,
        byte_limited=byte_limited,
    )
    if _typescript_file_manifest_base_size(partial_reason) > TS_FILE_MANIFEST_BYTE_LIMIT:
        raise AnalyzerError(
            "TypeScript file manifest byte safety limit is too small to encode a partial inventory marker."
        )
    return _TypescriptManifestPlan(
        partial_reason=partial_reason,
        byte_limited=byte_limited,
    )


def _stream_typescript_file_manifest(
    stream: _BinaryManifestStream,
    inventory: list[Path],
    reachable_config_paths: set[str],
    plan: _TypescriptManifestPlan,
    *,
    check_deadline: Callable[[], None],
) -> None:
    write = stream.write
    write(_TS_FILE_MANIFEST_HEADER)
    planned_size = _typescript_file_manifest_base_size(plan.partial_reason)
    for section in _TS_FILE_MANIFEST_SECTIONS:
        write(f',"{section}":['.encode())
        emitted_count = 0
        for path in _iter_typescript_manifest_section(
            section,
            inventory,
            reachable_config_paths,
            check_deadline=check_deadline,
        ):
            if emitted_count >= TS_FILE_MANIFEST_ENTRY_LIMIT:
                continue
            encoded_path = _encode_typescript_manifest_path(path)
            additional_size = len(encoded_path) + (1 if emitted_count else 0)
            if plan.byte_limited and planned_size + additional_size > TS_FILE_MANIFEST_BYTE_LIMIT:
                continue
            if emitted_count:
                write(b",")
            write(encoded_path)
            planned_size += additional_size
            emitted_count += 1
        write(b"]")
    if plan.partial_reason is not None:
        write(_TS_FILE_MANIFEST_PARTIAL_PREFIX)
        write(json.dumps(plan.partial_reason).encode())
    write(b"}")
    actual_size = stream.tell()
    if actual_size != planned_size:
        raise AssertionError("TypeScript file manifest byte plan did not match the streamed output")
    if actual_size > TS_FILE_MANIFEST_BYTE_LIMIT:
        raise AssertionError("TypeScript file manifest plan exceeded its byte safety limit")


def _iter_typescript_manifest_section(
    section: str,
    inventory: list[Path],
    reachable_config_paths: set[str],
    *,
    check_deadline: Callable[[], None],
) -> Iterator[Path]:
    for path in inventory:
        check_deadline()
        if section == "files":
            matches = path.suffix.lower() in TS_JS_INDEX_SUFFIXES
        elif section == "package_files":
            matches = path.name == "package.json"
        else:
            path_key = _inventory_path_key(path)
            matches = path.suffix.lower() in TS_CONFIG_METADATA_SUFFIXES or path_key in reachable_config_paths
        if matches:
            yield path


def _encode_typescript_manifest_path(path: Path) -> bytes:
    return json.dumps(path.as_posix()).encode()


def _typescript_file_manifest_base_size(partial_reason: str | None) -> int:
    size = len(_TS_FILE_MANIFEST_HEADER) + 1
    size += sum(len(f',"{section}":[]'.encode()) for section in _TS_FILE_MANIFEST_SECTIONS)
    if partial_reason is not None:
        size += len(_TS_FILE_MANIFEST_PARTIAL_PREFIX)
        size += len(json.dumps(partial_reason).encode())
    return size


def _typescript_file_manifest_partial_size(partial_reason: str | None) -> int:
    if partial_reason is None:
        return 0
    return len(_TS_FILE_MANIFEST_PARTIAL_PREFIX) + len(json.dumps(partial_reason).encode())


def _typescript_manifest_partial_reason(
    *,
    entry_limited: bool,
    byte_limited: bool,
) -> str | None:
    if entry_limited and byte_limited:
        limit = f"{TS_FILE_MANIFEST_ENTRY_LIMIT}-entry and {TS_FILE_MANIFEST_BYTE_LIMIT}-byte safety limits"
    elif entry_limited:
        limit = f"{TS_FILE_MANIFEST_ENTRY_LIMIT}-entry safety limit"
    elif byte_limited:
        limit = f"{TS_FILE_MANIFEST_BYTE_LIMIT}-byte safety limit"
    else:
        return None
    return f"TypeScript file manifest producer reached the {limit}{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"


def _ordered_typescript_inventory(
    inventory: list[Path],
    *,
    check_deadline: Callable[[], None] = lambda: None,
) -> list[Path]:
    previous_path: str | None = None
    for path in inventory:
        check_deadline()
        normalized = path.as_posix()
        if previous_path is not None and normalized < previous_path:

            def checked_path_key(candidate: Path) -> str:
                check_deadline()
                return candidate.as_posix()

            ordered = sorted(inventory, key=checked_path_key)
            check_deadline()
            return ordered
        previous_path = normalized
    return inventory


def _reachable_typescript_config_paths(
    repo_root: Path,
    inventory: list[Path],
    *,
    check_deadline: Callable[[], None] = lambda: None,
) -> set[str]:
    config_roots: list[str] = []
    for path in inventory:
        check_deadline()
        if path.name not in TS_CONFIG_ROOT_NAMES:
            continue
        path_key = _inventory_path_key(path)
        if path_key is not None:
            config_roots.append(path_key)
    if not config_roots:
        return set()

    inventory_by_path: dict[str, Path] = {}
    for path in inventory:
        check_deadline()
        path_key = _inventory_path_key(path)
        if path_key is not None:
            inventory_by_path[path_key] = path
    reachable = set(config_roots)
    pending = list(config_roots)
    package_index: dict[str, tuple[_TypescriptPackageConfig, ...]] | None = None
    while pending:
        check_deadline()
        config_path = pending.pop()
        config_text = _read_inventory_config(
            repo_root,
            inventory_by_path[config_path],
            check_deadline=check_deadline,
        )
        if config_text is None:
            continue
        for extends_value in _parse_typescript_config_extends(config_text):
            if _is_relative_config_extends(extends_value):
                extended_path = _resolve_inventory_config_extends(
                    config_path,
                    extends_value,
                    inventory_by_path,
                )
                extended_paths = () if extended_path is None else (extended_path,)
            else:
                package_specifier = _parse_package_config_specifier(extends_value)
                if package_specifier is None:
                    continue
                if package_index is None:
                    package_index = _build_typescript_package_index(
                        repo_root,
                        inventory_by_path,
                        check_deadline=check_deadline,
                    )
                extended_paths = _resolve_inventory_package_extends(
                    package_specifier,
                    package_index,
                    inventory_by_path,
                )
            for extended_path in extended_paths:
                if extended_path in reachable:
                    continue
                reachable.add(extended_path)
                pending.append(extended_path)
    return reachable


def _inventory_path_key(path: Path) -> str | None:
    raw_path = path.as_posix()
    posix_path = PurePosixPath(raw_path)
    if (
        path.is_absolute()
        or posix_path.is_absolute()
        or PureWindowsPath(raw_path).is_absolute()
        or ".." in posix_path.parts
    ):
        return None
    normalized = posix_path.as_posix()
    return normalized if normalized not in {"", "."} else None


def _read_inventory_config(
    repo_root: Path,
    relative_path: Path,
    *,
    check_deadline: Callable[[], None] = lambda: None,
) -> str | None:
    check_deadline()
    path_key = _inventory_path_key(relative_path)
    if path_key is None:
        return None
    try:
        root = repo_root.resolve(strict=True)
    except OSError:
        return None
    candidate = root.joinpath(*PurePosixPath(path_key).parts)
    current = root
    descriptor: int | None = None
    try:
        for component in PurePosixPath(path_key).parts:
            check_deadline()
            current /= component
            if current.is_symlink():
                return None
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > TS_CONFIG_MAX_BYTES:
            return None
        raw = _read_bounded_config_descriptor(
            descriptor,
            check_deadline=check_deadline,
        )
        check_deadline()
        after = os.fstat(descriptor)
        if (
            len(raw) > TS_CONFIG_MAX_BYTES
            or not stat.S_ISREG(after.st_mode)
            or _stable_file_identity(before) != _stable_file_identity(after)
        ):
            return None

        path_stat = candidate.stat(follow_symlinks=False)
        resolved_candidate = candidate.resolve(strict=True)
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or _stable_file_identity(after) != _stable_file_identity(path_stat)
            or not resolved_candidate.is_relative_to(root)
        ):
            return None
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16")
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _read_bounded_config_descriptor(
    descriptor: int,
    *,
    check_deadline: Callable[[], None] = lambda: None,
) -> bytes:
    chunks: list[bytes] = []
    remaining = TS_CONFIG_MAX_BYTES + 1
    while remaining > 0:
        check_deadline()
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _stable_file_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _parse_typescript_config_extends(config_text: str) -> tuple[str, ...]:
    try:
        config = json.loads(_jsonc_to_json(config_text))
    except json.JSONDecodeError, RecursionError:
        return ()
    if not isinstance(config, dict):
        return ()
    extends_value = config.get("extends")
    if isinstance(extends_value, str):
        return (extends_value,)
    if isinstance(extends_value, list):
        return tuple(value for value in extends_value if isinstance(value, str))
    return ()


def _jsonc_to_json(value: str) -> str:
    without_comments = list(value)
    in_string = False
    escaped = False
    index = 0
    while index < len(without_comments):
        character = without_comments[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            index += 1
            continue
        next_character = without_comments[index + 1] if index + 1 < len(without_comments) else ""
        if character == "/" and next_character == "/":
            without_comments[index] = " "
            without_comments[index + 1] = " "
            index += 2
            while index < len(without_comments) and without_comments[index] not in "\r\n":
                without_comments[index] = " "
                index += 1
            continue
        if character == "/" and next_character == "*":
            without_comments[index] = " "
            without_comments[index + 1] = " "
            index += 2
            while index < len(without_comments):
                if (
                    without_comments[index] == "*"
                    and index + 1 < len(without_comments)
                    and without_comments[index + 1] == "/"
                ):
                    without_comments[index] = " "
                    without_comments[index + 1] = " "
                    index += 2
                    break
                if without_comments[index] not in "\r\n":
                    without_comments[index] = " "
                index += 1
            continue
        index += 1

    in_string = False
    escaped = False
    for index, character in enumerate(without_comments):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            continue
        if character != ",":
            continue
        lookahead = index + 1
        while lookahead < len(without_comments) and without_comments[lookahead].isspace():
            lookahead += 1
        if lookahead < len(without_comments) and without_comments[lookahead] in "]}":
            without_comments[index] = " "
    return "".join(without_comments)


def _is_relative_config_extends(extends_value: str) -> bool:
    return extends_value.replace("\\", "/").startswith(("./", "../"))


def _resolve_inventory_config_extends(
    config_path: str,
    extends_value: str,
    inventory_by_path: dict[str, Path],
) -> str | None:
    normalized_extends = extends_value.replace("\\", "/")
    if not normalized_extends.startswith(("./", "../")):
        return None
    candidate = posixpath.normpath(
        posixpath.join(
            posixpath.dirname(config_path),
            normalized_extends,
        )
    )
    if candidate == ".." or candidate.startswith("../") or candidate.startswith("/"):
        return None
    if candidate in inventory_by_path:
        return candidate
    json_candidate = f"{candidate}.json"
    if not candidate.endswith(".json") and json_candidate in inventory_by_path:
        return json_candidate
    return None


def _parse_package_config_specifier(extends_value: str) -> tuple[str, str] | None:
    normalized = extends_value.replace("\\", "/")
    if not normalized or normalized.startswith(("./", "../", "/", "#")) or PureWindowsPath(normalized).is_absolute():
        return None
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    if normalized.startswith("@"):
        if len(parts) < 2:
            return None
        package_name = "/".join(parts[:2])
        subpath_parts = parts[2:]
    else:
        package_name = parts[0]
        subpath_parts = parts[1:]
    return package_name, "/".join(subpath_parts)


def _build_typescript_package_index(
    repo_root: Path,
    inventory_by_path: dict[str, Path],
    *,
    check_deadline: Callable[[], None] = lambda: None,
) -> dict[str, tuple[_TypescriptPackageConfig, ...]]:
    mutable_index: dict[str, list[_TypescriptPackageConfig]] = {}
    for package_path, relative_path in inventory_by_path.items():
        check_deadline()
        if PurePosixPath(package_path).name != "package.json":
            continue
        package_text = _read_inventory_config(
            repo_root,
            relative_path,
            check_deadline=check_deadline,
        )
        if package_text is None:
            continue
        try:
            package_json = json.loads(package_text)
        except json.JSONDecodeError, RecursionError:
            continue
        if not isinstance(package_json, dict):
            continue
        package_name = package_json.get("name")
        if not isinstance(package_name, str) or not package_name:
            continue
        tsconfig_value = package_json.get("tsconfig")
        package_config = _TypescriptPackageConfig(
            directory=posixpath.dirname(package_path),
            tsconfig=tsconfig_value if isinstance(tsconfig_value, str) else None,
            exports=package_json.get("exports"),
        )
        mutable_index.setdefault(package_name, []).append(package_config)
    return {
        package_name: tuple(sorted(configs, key=lambda config: config.directory))
        for package_name, configs in mutable_index.items()
    }


def _resolve_inventory_package_extends(
    package_specifier: tuple[str, str],
    package_index: dict[str, tuple[_TypescriptPackageConfig, ...]],
    inventory_by_path: dict[str, Path],
) -> tuple[str, ...]:
    package_name, package_subpath = package_specifier
    resolved: set[str] = set()
    for package in package_index.get(package_name, ()):
        if not package_subpath and package.tsconfig:
            tsconfig_path = _resolve_inventory_package_target(
                package.directory,
                package.tsconfig,
                inventory_by_path,
            )
            if tsconfig_path is not None:
                resolved.add(tsconfig_path)
            continue

        if package.exports is not None:
            for target in _package_export_targets(package.exports, package_subpath):
                resolved_path = _resolve_inventory_package_target(
                    package.directory,
                    target,
                    inventory_by_path,
                    require_dot_relative=True,
                )
                if resolved_path is not None:
                    resolved.add(resolved_path)
            continue

        conventional_target = f"./{package_subpath}" if package_subpath else "./tsconfig"
        conventional_path = _resolve_inventory_package_target(
            package.directory,
            conventional_target,
            inventory_by_path,
        )
        if conventional_path is not None:
            resolved.add(conventional_path)
    return tuple(sorted(resolved))


def _package_export_targets(exports: object, package_subpath: str) -> tuple[str, ...]:
    export_key = f"./{package_subpath}" if package_subpath else "."
    if isinstance(exports, (str, list)):
        return tuple(_iter_package_export_strings(exports)) if not package_subpath else ()
    if not isinstance(exports, dict):
        return ()

    subpath_exports = any(isinstance(key, str) and key.startswith(".") for key in exports)
    if not subpath_exports:
        return tuple(_iter_package_export_strings(exports)) if not package_subpath else ()
    if export_key in exports:
        return tuple(_iter_package_export_strings(exports[export_key]))

    matched: list[str] = []
    for pattern, target in exports.items():
        if not isinstance(pattern, str):
            continue
        wildcard_value = _match_package_export_pattern(pattern, export_key)
        if wildcard_value is None:
            continue
        matched.extend(_iter_package_export_strings(target, wildcard_value))
    return tuple(matched)


def _iter_package_export_strings(value: object, wildcard_value: str | None = None) -> list[str]:
    if isinstance(value, str):
        return [value if wildcard_value is None else value.replace("*", wildcard_value)]
    if isinstance(value, list):
        return [target for item in value for target in _iter_package_export_strings(item, wildcard_value)]
    if isinstance(value, dict):
        return [target for item in value.values() for target in _iter_package_export_strings(item, wildcard_value)]
    return []


def _match_package_export_pattern(pattern: str, export_key: str) -> str | None:
    wildcard_index = pattern.find("*")
    if wildcard_index < 0:
        return None
    prefix = pattern[:wildcard_index]
    suffix = pattern[wildcard_index + 1 :]
    if not export_key.startswith(prefix) or not export_key.endswith(suffix):
        return None
    end_index = len(export_key) - len(suffix) if suffix else len(export_key)
    return export_key[len(prefix) : max(len(prefix), end_index)]


def _resolve_inventory_package_target(
    package_directory: str,
    target: str,
    inventory_by_path: dict[str, Path],
    *,
    require_dot_relative: bool = False,
) -> str | None:
    normalized_target = target.replace("\\", "/")
    if (
        not normalized_target
        or normalized_target.startswith(("../", "/"))
        or PureWindowsPath(normalized_target).is_absolute()
        or (require_dot_relative and not normalized_target.startswith("./"))
    ):
        return None
    candidate = posixpath.normpath(posixpath.join(package_directory, normalized_target))
    if candidate in {"", ".", ".."} or candidate.startswith(("../", "/")):
        return None
    if package_directory and not candidate.startswith(f"{package_directory}/"):
        return None
    if candidate in inventory_by_path:
        return candidate
    json_candidate = f"{candidate}.json"
    if not candidate.endswith(".json") and json_candidate in inventory_by_path:
        return json_candidate
    return None


def _run_analyzer_process(
    args: list[str],
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(proc)
        proc.communicate()
        raise exc
    return subprocess.CompletedProcess(args, proc.returncode, stdout=stdout, stderr=stderr)


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        proc.terminate()
    try:
        proc.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        proc.kill()
    proc.wait(timeout=1)


def _shard_changed_files(files: list[ChangedFile], config: AnalyzerConfig) -> list[list[ChangedFile]]:
    ordered = sorted(
        files,
        key=lambda file: _changed_file_shard_priority(file),
        reverse=True,
    )
    shard_size = config.changed_file_shard_size
    if config.adaptive_sharding and len(ordered) >= config.large_change_file_threshold:
        shard_size = min(shard_size, config.large_change_shard_size)
    return [ordered[index : index + shard_size] for index in range(0, len(ordered), shard_size)]


def _typescript_total_timeout_seconds(
    changed_files: list[ChangedFile],
    shards: list[list[ChangedFile]],
    config: AnalyzerConfig,
) -> float:
    if len(shards) <= 1:
        return config.timeout_seconds
    if not config.adaptive_sharding or len(changed_files) < config.large_change_file_threshold:
        return config.timeout_seconds
    # Large diffs are split specifically to improve coverage. Give those shards
    # enough shared wall-clock to finish without letting worst-case runs grow
    # linearly with every changed file.
    return config.timeout_seconds * min(len(shards), 4)


def _changed_file_shard_priority(file: ChangedFile) -> tuple[int, int, int, int]:
    severity_rank = {
        RiskSeverity.CRITICAL: 4,
        RiskSeverity.HIGH: 3,
        RiskSeverity.MEDIUM: 2,
        RiskSeverity.LOW: 1,
    }
    risk_band = max(
        (severity_rank.get(signal.severity, 0) for signal in file.risk_signals),
        default=0,
    )
    risk_score = sum(risk_signal_score(signal) for signal in file.risk_signals)
    kind_score = {
        FileKind.SOURCE: 6,
        FileKind.SCHEMA: 5,
        FileKind.MIGRATION: 5,
        FileKind.CONFIG: 4,
        FileKind.DEPENDENCY: 3,
        FileKind.UNKNOWN: 2,
        FileKind.TEST: 1,
    }.get(file.file_kind, 2)
    return (risk_band, risk_score, kind_score, -len(file.path))


def _format_seconds(seconds: float) -> str:
    rounded = round(seconds)
    if abs(seconds - rounded) < 0.05:
        return f"{rounded}s"
    return f"{seconds:.1f}s"


def _analysis_time_budget_ms(timeout_seconds: float) -> int:
    margin_seconds = min(5.0, max(0.25, timeout_seconds * 0.05))
    budget_seconds = max(0.001, timeout_seconds - margin_seconds)
    return max(1, round(budget_seconds * 1000))


def _shard_failure(
    index: int,
    total: int,
    changed_files: list[ChangedFile],
    error: AnalyzerError,
    *,
    status: Literal["failed", "timeout", "skipped"] | None = None,
) -> AnalyzerShardFailure:
    paths = [file.path for file in changed_files]
    status = status or ("timeout" if "timeout" in str(error).lower() or "timed out" in str(error).lower() else "failed")
    return AnalyzerShardFailure(
        index=index,
        total=total,
        files=paths,
        reason=str(error),
        status=status,
    )


def _format_shard_failure(failure: AnalyzerShardFailure) -> str:
    paths = failure.files
    preview = ", ".join(paths[:3])
    if len(paths) > 3:
        preview = f"{preview}, +{len(paths) - 3} more"
    return f"TypeScript analyzer shard {failure.index}/{failure.total} failed for {preview}: {failure.reason}"


def _merge_analyzer_results(results: list[AnalyzerResult]) -> AnalyzerResult:
    first = results[0]
    warnings: list[str] = []
    for result in results:
        warnings.extend(result.warnings)

    tsconfig_paths = {result.tsconfig_path for result in results}
    tsconfig_path = tsconfig_paths.pop() if len(tsconfig_paths) == 1 else None
    return AnalyzerResult(
        language=first.language,
        projectRoot=first.project_root,
        tsconfigPath=tsconfig_path,
        files=[file for result in results for file in result.files],
        warnings=warnings,
        indexCache=_merge_index_cache_stats(results),
        partial=any(result.partial for result in results),
        failedFiles=[path for result in results for path in result.failed_files],
        shardFailures=[failure for result in results for failure in result.shard_failures],
    )


def _merge_index_cache_stats(results: list[AnalyzerResult]) -> AnalyzerIndexCacheStats | None:
    stats = [result.index_cache for result in results if result.index_cache is not None]
    if not stats:
        return None
    first = stats[0]
    return first.model_copy(
        update={
            "files": max(stat.files for stat in stats),
            "hits": sum(stat.hits for stat in stats),
            "misses": sum(stat.misses for stat in stats),
            "written": any(stat.written for stat in stats),
        }
    )


def typescript_analyzer_script(config: AnalyzerConfig | None = None, repo_root: Path | None = None) -> Path:
    config = config or AnalyzerConfig()
    if config.script_path:
        script_path = Path(config.script_path).expanduser()
        if not script_path.is_absolute() and repo_root is not None:
            script_path = repo_root / script_path
        return script_path.resolve()

    bundled = Path(__file__).resolve().parents[1] / "_bundled" / "typescript" / "analyze.js"
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parents[3] / "analyzer-runtimes" / "typescript" / "dist" / "analyze.js"


def _changed_new_line_ranges(file: ChangedFile) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for hunk in file.hunks:
        added_lines = sorted(line.new_line for line in hunk.lines if line.new_line is not None and line.kind == "add")
        if added_lines:
            ranges.extend(_collapse_ranges(added_lines))
        else:
            ranges.append((hunk.new_start, hunk.new_start))
    return ranges


def _deleted_lines(file: ChangedFile) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for hunk in file.hunks:
        if any(line.kind == "add" for line in hunk.lines):
            continue
        next_new_line = hunk.new_start
        for line in hunk.lines:
            if line.new_line is not None:
                next_new_line = line.new_line + 1
            if line.kind == "delete":
                lines.append((next_new_line, line.content))
    return lines
