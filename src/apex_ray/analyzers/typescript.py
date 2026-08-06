import json
import os
import posixpath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import BinaryIO, Literal, Protocol

from pydantic import ValidationError

from apex_ray.discovery import DISCOVERY_IGNORED_DIRS
from apex_ray.models import (
    AnalyzerConfig,
    AnalyzerCoverageScope,
    AnalyzerCoverageSignal,
    AnalyzerIndexCacheStats,
    AnalyzerMetrics,
    AnalyzerPartialReasonCode,
    AnalyzerResult,
    AnalyzerShardFailure,
    AnalyzerShardMetrics,
    AnalyzerWarningSummary,
    ChangedFile,
    FileKind,
    RiskSeverity,
)
from apex_ray.path_matching import path_matches_any
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
TS_FALLBACK_INVENTORY_ENTRY_LIMIT = 250_000
TS_FALLBACK_GIT_OUTPUT_BYTE_LIMIT = 64 * 1024 * 1024
TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT = 16 * 1024 * 1024
TS_PACKAGE_EXPORT_ENTRY_LIMIT = 4_096
TS_PACKAGE_EXPORT_TARGET_LIMIT = 256
TS_PACKAGE_EXPORT_SINGLE_TARGET_BYTE_LIMIT = 64 * 1024
TS_PACKAGE_EXPORT_TARGET_BYTES_LIMIT = 1024 * 1024
TS_PACKAGE_INDEX_BYTE_LIMIT = 4 * 1024 * 1024
TS_PACKAGE_INDEX_ENTRY_LIMIT = 512
TS_PACKAGE_EXTENDS_CANDIDATE_GROUP_LIMIT = 512
TS_INVENTORY_CASE_SENSITIVE = sys.platform not in {"darwin", "win32"}
_TS_GIT_VISIBILITY_TIMEOUT_SECONDS = 1.0
_TS_FILE_MANIFEST_HEADER = b'{"version":2'
_TS_FILE_MANIFEST_SECTIONS = ("files", "package_files", "config_files")
_TS_FILE_MANIFEST_PARTIAL_PREFIX = b',"partial_reason":'
_TS_FILE_MANIFEST_PARTIAL_SUFFIX = "; repository context is partial."
_TS_GIT_INVENTORY_INCLUDE_PATHSPECS = tuple(
    f":(glob,icase)**/*{suffix}" for suffix in sorted(TS_JS_INDEX_SUFFIXES | TS_CONFIG_METADATA_SUFFIXES)
)
_TS_GIT_INVENTORY_EXCLUDE_PATHSPECS = tuple(
    f":(exclude,glob)**/{directory}/**" for directory in sorted(DISCOVERY_IGNORED_DIRS)
)


@dataclass(frozen=True)
class _TypescriptPackageConfig:
    directory: str
    tsconfig: str | None
    exports: object


@dataclass(frozen=True)
class _TypescriptPackageIndex:
    packages: dict[str, tuple[_TypescriptPackageConfig, ...]]
    partial_reason: str | None


@dataclass(frozen=True)
class _TypescriptReachableConfigs:
    paths: set[str]
    partial_reason: str | None


@dataclass(frozen=True)
class _TypescriptManifestPlan:
    partial_reason: str | None
    byte_limited: bool
    selected_section_keys: dict[str, frozenset[str] | None]


@dataclass(frozen=True)
class _TypescriptInventory:
    paths: list[Path]
    partial_reason: str | None


class _FallbackConfigVisibility(Protocol):
    def visible(
        self,
        path_key: str,
        *,
        check_deadline: Callable[[], None],
    ) -> bool | None: ...


@dataclass
class _RetainedTypescriptInventoryPath:
    path: Path
    sections: tuple[str, ...]
    path_bytes: int
    critical: bool


class _TypescriptInventoryRetention:
    """Bound retained paths without allowing source entries to starve metadata."""

    def __init__(
        self,
        reason_prefix: str,
        *,
        partial_reason: str | None = None,
        enforce_section_entry_limits: bool = True,
    ) -> None:
        self.reason_prefix = reason_prefix
        self.partial_reason = partial_reason
        self._enforce_section_entry_limits = enforce_section_entry_limits
        self._source_paths: list[Path] = []
        self._source_index: dict[str, int] | None = None
        self._metadata_paths: dict[str, _RetainedTypescriptInventoryPath] = {}
        self._section_counts = {section: 0 for section in _TS_FILE_MANIFEST_SECTIONS}
        self._retained_path_bytes = 0
        self._source_path_bytes = 0
        self._ordinary_metadata_path_bytes = 0
        self._ordinary_metadata_count = 0

    @property
    def paths(self) -> list[Path]:
        return [
            *self._source_paths,
            *(retained.path for retained in self._metadata_paths.values()),
        ]

    def add_reason(self, reason: str) -> None:
        self.partial_reason = _combine_typescript_partial_reasons(
            self.partial_reason,
            reason,
        )

    def add(
        self,
        path: Path,
        *,
        force_config: bool = False,
    ) -> bool:
        path_key = _inventory_path_key(path)
        if path_key is None:
            return False
        canonical_key = _canonical_typescript_inventory_path_key(path_key)

        sections = _typescript_inventory_sections(path, force_config=force_config)
        if not sections:
            return False
        is_metadata = any(section != "files" for section in sections)
        critical = _is_critical_typescript_inventory_path(
            path,
            force_config=force_config,
        )
        existing = self._metadata_paths.get(canonical_key)
        existing_source_index: int | None = None
        if existing is None and critical:
            self._ensure_source_index()
            if self._source_index is not None:
                existing_source_index = self._source_index.get(canonical_key)
            if existing_source_index is not None:
                existing_path = self._source_paths[existing_source_index]
                existing_path_key = _inventory_path_key(existing_path)
                if existing_path_key is not None:
                    existing = _RetainedTypescriptInventoryPath(
                        path=existing_path,
                        sections=("files",),
                        path_bytes=len(os.fsencode(existing_path_key)) + 1,
                        critical=False,
                    )
        if existing is not None:
            added_sections = tuple(section for section in sections if section not in existing.sections)
            if not added_sections and (not critical or existing.critical):
                return True
            if not self._make_room_for_sections(
                added_sections,
                critical=critical or existing.critical,
                protected_key=canonical_key,
            ):
                self._mark_entry_limited(TS_FILE_MANIFEST_ENTRY_LIMIT, relevant=True)
                return False
            merged_sections = tuple(
                section for section in _TS_FILE_MANIFEST_SECTIONS if section in {*existing.sections, *sections}
            )
            upgraded = _RetainedTypescriptInventoryPath(
                path=existing.path,
                sections=merged_sections,
                path_bytes=existing.path_bytes,
                critical=critical or existing.critical,
            )
            was_source = existing_source_index is not None
            was_ordinary_metadata = self._metadata_paths.pop(canonical_key, None) is not None and not existing.critical
            if was_source:
                self._detach_source_path(existing_source_index)
                self._source_path_bytes -= existing.path_bytes
            if was_ordinary_metadata and upgraded.critical:
                self._ordinary_metadata_path_bytes -= existing.path_bytes
                self._ordinary_metadata_count -= 1
            self._metadata_paths[canonical_key] = upgraded
            for section in added_sections:
                self._section_counts[section] += 1
            return True

        if not self._make_room_for_sections(
            sections,
            critical=critical,
            protected_key=None,
        ):
            self._mark_entry_limited(TS_FILE_MANIFEST_ENTRY_LIMIT, relevant=True)
            return False

        path_bytes = len(os.fsencode(path_key)) + 1
        while (
            self._retained_count >= TS_FALLBACK_INVENTORY_ENTRY_LIMIT
            or self._retained_path_bytes + path_bytes > TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT
        ):
            entry_pressure = self._retained_count >= TS_FALLBACK_INVENTORY_ENTRY_LIMIT
            byte_pressure = self._retained_path_bytes + path_bytes > TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT
            if entry_pressure:
                self._mark_entry_limited(
                    TS_FALLBACK_INVENTORY_ENTRY_LIMIT,
                    relevant=False,
                )
            if byte_pressure:
                self._mark_byte_limited()
            if not self._drop_capacity_candidate(
                critical=critical,
                incoming_is_metadata=is_metadata,
                entry_pressure=entry_pressure,
                byte_pressure=byte_pressure,
            ):
                break

        if self._retained_count >= TS_FALLBACK_INVENTORY_ENTRY_LIMIT:
            self._mark_entry_limited(
                TS_FALLBACK_INVENTORY_ENTRY_LIMIT,
                relevant=False,
            )
            return False
        if self._retained_path_bytes + path_bytes > TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT:
            self._mark_byte_limited()
            return False

        retained = _RetainedTypescriptInventoryPath(
            path=path,
            sections=sections,
            path_bytes=path_bytes,
            critical=critical,
        )
        if is_metadata:
            self._metadata_paths[canonical_key] = retained
        else:
            if self._source_index is not None:
                self._source_index[canonical_key] = len(self._source_paths)
            self._source_paths.append(path)
        self._retained_path_bytes += path_bytes
        if is_metadata and not critical:
            self._ordinary_metadata_path_bytes += path_bytes
            self._ordinary_metadata_count += 1
        elif not is_metadata:
            self._source_path_bytes += path_bytes
        for section in sections:
            self._section_counts[section] += 1
        return True

    def build(self) -> _TypescriptInventory:
        return _TypescriptInventory(
            paths=self.paths,
            partial_reason=self.partial_reason,
        )

    @property
    def _retained_count(self) -> int:
        return len(self._source_paths) + len(self._metadata_paths)

    def _make_room_for_sections(
        self,
        sections: tuple[str, ...],
        *,
        critical: bool,
        protected_key: str | None,
    ) -> bool:
        if not self._enforce_section_entry_limits:
            return True
        while saturated := {
            section for section in sections if self._section_counts[section] >= TS_FILE_MANIFEST_ENTRY_LIMIT
        }:
            if not critical:
                return False
            candidate_key = self._find_noncritical_candidate(
                saturated,
                protected_key=protected_key,
            )
            if candidate_key is None:
                return False
            self._mark_entry_limited(TS_FILE_MANIFEST_ENTRY_LIMIT, relevant=True)
            self._drop_retained_path(candidate_key)
        return True

    def _find_noncritical_candidate(
        self,
        sections: set[str],
        *,
        protected_key: str | None,
    ) -> str | None:
        for key in reversed(self._metadata_paths):
            retained = self._metadata_paths[key]
            if (
                key != protected_key
                and not retained.critical
                and any(section in sections for section in retained.sections)
            ):
                return key
        if "files" not in sections:
            return None
        for path in reversed(self._source_paths):
            path_key = _inventory_path_key(path)
            if path_key is None:
                continue
            canonical_key = _canonical_typescript_inventory_path_key(path_key)
            if canonical_key != protected_key:
                return canonical_key
        return None

    def _drop_capacity_candidate(
        self,
        *,
        critical: bool,
        incoming_is_metadata: bool,
        entry_pressure: bool,
        byte_pressure: bool,
    ) -> bool:
        if critical:
            candidate_key = self._find_noncritical_candidate(
                set(_TS_FILE_MANIFEST_SECTIONS),
                protected_key=None,
            )
            if candidate_key is not None:
                self._drop_retained_path(candidate_key)
                return True
            return False

        reserve_count = TS_FALLBACK_INVENTORY_ENTRY_LIMIT // 4
        reserve_bytes = TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT // 4
        current_count = len(self._source_paths) if incoming_is_metadata else self._ordinary_metadata_count
        current_bytes = self._source_path_bytes if incoming_is_metadata else self._ordinary_metadata_path_bytes
        if incoming_is_metadata:
            if not self._source_paths:
                return False
            candidate_path = self._source_paths[-1]
            candidate_key = _inventory_path_key(candidate_path)
            if candidate_key is None:  # pragma: no cover - validated by add
                return False
            candidate_bytes = len(os.fsencode(candidate_key)) + 1
            canonical_key = _canonical_typescript_inventory_path_key(candidate_key)
        else:
            candidate_key = next(
                (key for key in reversed(self._metadata_paths) if not self._metadata_paths[key].critical),
                None,
            )
            if candidate_key is None:
                return False
            candidate_bytes = self._metadata_paths[candidate_key].path_bytes
            canonical_key = candidate_key
        if entry_pressure and current_count - 1 < reserve_count:
            return False
        if byte_pressure and current_bytes - candidate_bytes < reserve_bytes:
            return False
        self._drop_retained_path(canonical_key)
        return True

    def _drop_retained_path(self, canonical_key: str) -> None:
        retained = self._metadata_paths.pop(canonical_key, None)
        was_metadata = retained is not None
        if retained is None:
            self._ensure_source_index()
            if self._source_index is None:  # pragma: no cover - populated above
                raise KeyError(canonical_key)
            source_index = self._source_index[canonical_key]
            source_path = self._source_paths[source_index]
            path_key = _inventory_path_key(source_path)
            if path_key is None:  # pragma: no cover - validated by add
                raise KeyError(canonical_key)
            retained = _RetainedTypescriptInventoryPath(
                path=source_path,
                sections=("files",),
                path_bytes=len(os.fsencode(path_key)) + 1,
                critical=False,
            )
            self._detach_source_path(source_index)
        self._retained_path_bytes -= retained.path_bytes
        if was_metadata and not retained.critical:
            self._ordinary_metadata_path_bytes -= retained.path_bytes
            self._ordinary_metadata_count -= 1
        elif not was_metadata:
            self._source_path_bytes -= retained.path_bytes
        for section in retained.sections:
            self._section_counts[section] -= 1

    def _ensure_source_index(self) -> None:
        if self._source_index is not None:
            return
        self._source_index = {
            _canonical_typescript_inventory_path_key(path_key): index
            for index, path in enumerate(self._source_paths)
            if (path_key := _inventory_path_key(path)) is not None
        }

    def _detach_source_path(self, source_index: int) -> None:
        removed = self._source_paths[source_index]
        last = self._source_paths.pop()
        if source_index < len(self._source_paths):
            self._source_paths[source_index] = last
        if self._source_index is None:
            return
        removed_key = _inventory_path_key(removed)
        if removed_key is not None:
            self._source_index.pop(
                _canonical_typescript_inventory_path_key(removed_key),
                None,
            )
        if source_index < len(self._source_paths):
            last_key = _inventory_path_key(last)
            if last_key is not None:
                self._source_index[_canonical_typescript_inventory_path_key(last_key)] = source_index

    def _mark_entry_limited(self, limit: int, *, relevant: bool) -> None:
        limit_description = f"{limit} relevant-file safety limit" if relevant else f"{limit}-entry safety limit"
        self.add_reason(f"{self.reason_prefix} reached the {limit_description}{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}")

    def _mark_byte_limited(self) -> None:
        self.add_reason(
            f"{self.reason_prefix} reached the "
            f"{TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT}-byte retained-path safety limit"
            f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
        )


class _PackageExportTraversal:
    def __init__(self, on_limit: Callable[[str], None] | None = None) -> None:
        self.entries = 0
        self.targets: dict[str, None] = {}
        self.target_bytes = 0
        self.truncated = False
        self._on_limit = on_limit

    def visit(self, check_deadline: Callable[[], None]) -> bool:
        check_deadline()
        if self.entries >= TS_PACKAGE_EXPORT_ENTRY_LIMIT:
            self.mark_truncated()
            return False
        self.entries += 1
        return True

    def add_target(
        self,
        item: str,
        wildcard_value: str | None,
    ) -> bool:
        if wildcard_value is None:
            target = item
            if target in self.targets:
                return True
            target_bytes = _typescript_safe_text_byte_size(target)
            if target_bytes is None:
                self.mark_truncated()
                return False
        else:
            wildcard_count = item.count("*")
            item_bytes = _typescript_safe_text_byte_size(item)
            wildcard_bytes = _typescript_safe_text_byte_size(wildcard_value)
            if item_bytes is None or wildcard_bytes is None:
                self.mark_truncated()
                return False
            target_bytes = item_bytes + wildcard_count * (wildcard_bytes - 1)
            if target_bytes > TS_PACKAGE_EXPORT_SINGLE_TARGET_BYTE_LIMIT:
                self.mark_truncated()
                return False
            target = item.replace("*", wildcard_value)
            if target in self.targets:
                return True
        if (
            len(self.targets) >= TS_PACKAGE_EXPORT_TARGET_LIMIT
            or target_bytes > TS_PACKAGE_EXPORT_SINGLE_TARGET_BYTE_LIMIT
            or target_bytes > TS_PACKAGE_EXPORT_TARGET_BYTES_LIMIT - self.target_bytes
        ):
            self.mark_truncated()
            return False
        self.targets[target] = None
        self.target_bytes += target_bytes
        return True

    def mark_truncated(self) -> None:
        if self.truncated:
            return
        self.truncated = True
        if self._on_limit is not None:
            self._on_limit(_typescript_package_export_partial_reason())


def _typescript_safe_text_byte_size(value: str) -> int | None:
    if any(0xD800 <= ord(character) <= 0xDFFF and not 0xDC80 <= ord(character) <= 0xDCFF for character in value):
        return None
    try:
        return len(os.fsencode(value))
    except UnicodeEncodeError:
        return None


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
        manifest_started_at = time.monotonic()
        _write_typescript_file_manifest(
            repo_root,
            file_manifest_path,
            ignored_patterns,
            project_files=project_files,
            deadline=deadline,
            total_timeout_seconds=total_timeout_seconds,
        )
        manifest_duration_ms = _monotonic_elapsed_ms(manifest_started_at)
        result = _run_typescript_analyzer_shards(
            repo_root,
            changed_files,
            config,
            script,
            file_manifest_path,
            shards=shards,
            deadline=deadline,
            total_timeout_seconds=total_timeout_seconds,
        )
        if result.metrics is not None:
            result.metrics.wall_duration_ms = _monotonic_elapsed_ms(started_at)
            result.metrics.stage_durations_ms["manifest"] = manifest_duration_ms
        return result


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
    successful_shard_indexes: list[int] = []
    failures: list[AnalyzerShardFailure] = []
    failed_shard_metrics: list[AnalyzerShardMetrics] = []
    large_change_set_size = len(changed_files) if len(changed_files) >= config.large_change_file_threshold else None
    for index, shard in enumerate(shards, start=1):
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            timeout_error = AnalyzerError(
                f"TypeScript analyzer total timeout after {_format_seconds(total_timeout_seconds)}"
            )
            skipped_failures = [
                _shard_failure(
                    skipped_index,
                    len(shards),
                    skipped_shard,
                    timeout_error,
                    status="timeout",
                )
                for skipped_index, skipped_shard in enumerate(shards[index - 1 :], start=index)
            ]
            failures.extend(skipped_failures)
            failed_shard_metrics.extend(_failed_shard_metrics(failure, duration_ms=0) for failure in skipped_failures)
            break
        shard_started_at = time.monotonic()
        try:
            shard_result = _run_typescript_analyzer_shard(
                repo_root,
                script,
                shard,
                config,
                timeout_seconds=min(config.timeout_seconds, remaining_seconds),
                large_change_set_size=large_change_set_size,
                file_manifest_path=file_manifest_path,
            )
            shard_duration_ms = _monotonic_elapsed_ms(shard_started_at)
            _annotate_successful_shard(
                shard_result,
                index=index,
                total=len(shards),
                changed_file_count=len(shard),
                duration_ms=shard_duration_ms,
            )
            results.append(shard_result)
            successful_shard_indexes.append(index)
        except AnalyzerError as exc:
            failure = _shard_failure(index, len(shards), shard, exc)
            failures.append(failure)
            failed_shard_metrics.append(
                _failed_shard_metrics(
                    failure,
                    duration_ms=_monotonic_elapsed_ms(shard_started_at),
                )
            )

    if not results:
        if len(shards) == 1 and len(failures) == 1:
            raise AnalyzerError(_format_shard_failure(failures[0]))
        raise AnalyzerError(
            "TypeScript analyzer failed for all shards: "
            + "; ".join(_format_shard_failure(failure) for failure in failures)
        )

    result = _merge_analyzer_results(
        results,
        shard_indexes=successful_shard_indexes,
        total_shards=len(shards),
    )
    if failures:
        _append_analyzer_warning(
            result,
            f"Returning partial TypeScript analyzer result because {len(failures)} of {len(shards)} shards failed.",
            shard_indexes=[failure.index for failure in failures],
        )
        for failure in failures:
            _append_analyzer_warning(
                result,
                _format_shard_failure(failure),
                shard_indexes=[failure.index],
            )
        result.partial = True
        outer_failed_files = [path for failure in failures for path in failure.files]
        result.failed_files = list(dict.fromkeys([*result.failed_files, *outer_failed_files]))
        combined_shard_failures = list(result.shard_failures)
        retained_failure_keys = {
            (failure.index, failure.total, tuple(failure.files), failure.reason, failure.status)
            for failure in combined_shard_failures
        }
        for failure in failures:
            failure_key = (failure.index, failure.total, tuple(failure.files), failure.reason, failure.status)
            if failure_key in retained_failure_keys:
                continue
            retained_failure_keys.add(failure_key)
            combined_shard_failures.append(failure)
        result.shard_failures = combined_shard_failures
        failure_reason_codes: list[AnalyzerPartialReasonCode] = list(
            dict.fromkeys(
                [
                    *(result.coverage.reason_codes if result.coverage is not None else ["partial_reason_unspecified"]),
                    *(_shard_partial_reason_code(failure.status) for failure in failures),
                    "changed_file_analysis_incomplete",
                ]
            )
        )
        result.coverage = AnalyzerCoverageSignal(
            partial=True,
            reasonCodes=failure_reason_codes,
            scopes=list(
                dict.fromkeys(
                    [
                        *(result.coverage.scopes if result.coverage is not None else ["analyzer"]),
                        "shards",
                        "changed_files",
                    ]
                )
            ),
            failedFileCount=len(result.failed_files),
        )
        if result.metrics is None:
            result.metrics = AnalyzerMetrics()
        result.metrics.shards.extend(failed_shard_metrics)
        result.metrics.shards.sort(key=lambda shard: shard.index)
        result.metrics.wall_duration_ms = sum(shard.wall_duration_ms for shard in result.metrics.shards)
    return result


def _annotate_successful_shard(
    result: AnalyzerResult,
    *,
    index: int,
    total: int,
    changed_file_count: int,
    duration_ms: int,
) -> None:
    result_is_partial = _analyzer_result_is_partial(result)
    if result.coverage is None:
        result.coverage = AnalyzerCoverageSignal(
            partial=result_is_partial,
            reasonCodes=["partial_reason_unspecified"] if result_is_partial else [],
            scopes=["analyzer"] if result_is_partial else [],
            failedFileCount=len(result.failed_files),
        )
    status: Literal["complete", "partial", "failed", "timeout", "skipped"] = (
        "timeout"
        if any(failure.status == "timeout" for failure in result.shard_failures)
        else "partial"
        if result_is_partial
        else "complete"
    )
    source_metrics = result.metrics
    source_shard = (
        source_metrics.shards[0] if source_metrics is not None and source_metrics.shards else AnalyzerShardMetrics()
    )
    cache_hits = result.index_cache.hits if result.index_cache is not None else source_shard.index_cache_hits
    cache_misses = result.index_cache.misses if result.index_cache is not None else source_shard.index_cache_misses
    shard_metrics = source_shard.model_copy(
        update={
            "index": index,
            "total": total,
            "status": status,
            "wall_duration_ms": duration_ms,
            "changed_file_count": changed_file_count,
            "analyzed_file_count": len(result.files),
            "failed_file_count": len(result.failed_files),
            "warning_count": sum(summary.occurrences for summary in result.warning_summaries)
            if result.warning_summaries
            else len(result.warnings),
            "partial_reason_codes": list(result.coverage.reason_codes),
            "index_cache_hits": cache_hits,
            "index_cache_misses": cache_misses,
        }
    )
    result.metrics = AnalyzerMetrics(
        wallDurationMs=duration_ms,
        stageDurationsMs=dict(source_metrics.stage_durations_ms) if source_metrics is not None else {},
        shards=[shard_metrics],
    )


def _failed_shard_metrics(
    failure: AnalyzerShardFailure,
    *,
    duration_ms: int,
) -> AnalyzerShardMetrics:
    return AnalyzerShardMetrics(
        index=failure.index,
        total=failure.total,
        status=failure.status,
        wallDurationMs=duration_ms,
        changedFileCount=len(failure.files),
        analyzedFileCount=0,
        failedFileCount=len(failure.files),
        warningCount=1,
        partialReasonCodes=[
            _shard_partial_reason_code(failure.status),
            "changed_file_analysis_incomplete",
        ],
    )


def _shard_partial_reason_code(
    status: Literal["failed", "timeout", "skipped"],
) -> AnalyzerPartialReasonCode:
    if status == "failed":
        return "shard_failed"
    if status == "timeout":
        return "shard_timeout"
    return "shard_skipped"


def _append_analyzer_warning(
    result: AnalyzerResult,
    message: str,
    *,
    shard_indexes: list[int],
) -> None:
    if message not in result.warnings:
        result.warnings.append(message)
    for summary in result.warning_summaries:
        if summary.message != message:
            continue
        summary.occurrences += 1
        summary.shard_indexes = list(dict.fromkeys([*summary.shard_indexes, *shard_indexes]))
        return
    result.warning_summaries.append(
        AnalyzerWarningSummary(
            message=message,
            occurrences=1,
            shardIndexes=list(dict.fromkeys(shard_indexes)),
        )
    )


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
        inventory_result = _bounded_supplied_typescript_inventory(
            repo_root,
            project_files,
            check_deadline=check_deadline,
        )
    else:
        inventory_result = _load_bounded_typescript_inventory(
            repo_root,
            ignored_patterns or [],
            check_deadline=check_deadline,
        )
    check_deadline()
    ordered_inventory = _ordered_typescript_inventory(
        inventory_result.paths,
        check_deadline=check_deadline,
    )
    reachable_configs = _reachable_typescript_config_paths(
        repo_root,
        ordered_inventory,
        check_deadline=check_deadline,
    )
    plan = _plan_typescript_file_manifest(
        ordered_inventory,
        reachable_configs.paths,
        inventory_partial_reason=_combine_typescript_partial_reasons(
            inventory_result.partial_reason,
            reachable_configs.partial_reason,
        ),
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
                reachable_configs.paths,
                plan,
                check_deadline=check_deadline,
            )
            check_deadline()
        temporary_path.replace(manifest_path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _bounded_supplied_typescript_inventory(
    repo_root: Path,
    project_files: list[Path],
    *,
    check_deadline: Callable[[], None],
) -> _TypescriptInventory:
    scan_limit = max(
        TS_FALLBACK_INVENTORY_ENTRY_LIMIT,
        TS_FILE_MANIFEST_ENTRY_LIMIT,
    )
    explicit_config_paths: list[Path] = []
    generic_config_paths: list[Path] = []
    explicit_config_bytes = 0
    generic_config_bytes = 0
    explicit_entry_reserve = min(
        TS_FILE_MANIFEST_ENTRY_LIMIT,
        TS_FALLBACK_INVENTORY_ENTRY_LIMIT,
    )
    generic_entry_limit = TS_FALLBACK_INVENTORY_ENTRY_LIMIT - explicit_entry_reserve
    explicit_byte_reserve = min(
        TS_CONFIG_MAX_BYTES,
        TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT // 4,
    )
    generic_byte_limit = TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT - explicit_byte_reserve
    partial_reason: str | None = None
    config_lookup_limited = False
    config_lookup_keys: set[str] = set()
    for category in ("critical", "generic", "ordinary"):
        for scanned_entries, path in enumerate(project_files, start=1):
            check_deadline()
            if scanned_entries > scan_limit:
                partial_reason = _combine_typescript_partial_reasons(
                    partial_reason,
                    "TypeScript supplied project inventory reached the "
                    f"{scan_limit} scanned-entry safety limit"
                    f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}",
                )
                break
            path_key = _inventory_path_key(path)
            if path_key is None:
                if _typescript_safe_text_byte_size(path.as_posix()) is None:
                    partial_reason = _combine_typescript_partial_reasons(
                        partial_reason,
                        "TypeScript supplied project inventory rejected "
                        "filesystem-unrepresentable paths"
                        f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}",
                    )
                continue
            normalized_path = path if path.as_posix() == path_key else Path(path_key)
            critical = _is_critical_typescript_inventory_path(normalized_path)
            ordinary = normalized_path.suffix.lower() in TS_CONFIG_METADATA_SUFFIXES
            explicit = critical or ordinary
            path_category = "critical" if critical else ("ordinary" if ordinary else "generic")
            if path_category != category:
                continue
            canonical_key = _canonical_typescript_inventory_path_key(path_key)
            if canonical_key in config_lookup_keys:
                continue
            path_bytes = len(os.fsencode(path_key)) + 1
            if explicit:
                if (
                    len(explicit_config_paths) >= explicit_entry_reserve
                    or explicit_config_bytes + generic_config_bytes + path_bytes > TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT
                ):
                    config_lookup_limited = True
                    continue
                explicit_config_paths.append(normalized_path)
                explicit_config_bytes += path_bytes
            else:
                if (
                    len(generic_config_paths) >= generic_entry_limit
                    or generic_config_bytes + path_bytes > generic_byte_limit
                    or explicit_config_bytes + generic_config_bytes + path_bytes > TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT
                ):
                    config_lookup_limited = True
                    continue
                generic_config_paths.append(normalized_path)
                generic_config_bytes += path_bytes
            config_lookup_keys.add(canonical_key)
        if category == "critical" and not any(
            path.name.lower() in TS_CONFIG_ROOT_NAMES for path in explicit_config_paths
        ):
            break

    # TypeScript configs can extend repo files with arbitrary suffixes. Resolve
    # those paths from a bounded metadata-only lookup before dropping unrelated
    # languages. Source paths remain in the already materialized project list.
    if config_lookup_limited and any(path.name.lower() in TS_CONFIG_ROOT_NAMES for path in explicit_config_paths):
        partial_reason = _combine_typescript_partial_reasons(
            partial_reason,
            "TypeScript supplied config lookup reached its bounded entry or retained-path byte safety limit"
            f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}",
        )
    explicit_config_paths.extend(generic_config_paths)
    del generic_config_paths
    reachable_configs = _reachable_typescript_config_paths(
        repo_root,
        explicit_config_paths,
        check_deadline=check_deadline,
    )
    retention = _TypescriptInventoryRetention(
        "TypeScript supplied project inventory",
        partial_reason=_combine_typescript_partial_reasons(
            partial_reason,
            reachable_configs.partial_reason,
        ),
        enforce_section_entry_limits=False,
    )
    relevant_entries = 0
    for scanned_entries, path in enumerate(project_files, start=1):
        check_deadline()
        if scanned_entries > scan_limit:
            break
        path_key = _inventory_path_key(path)
        if path_key is None:
            continue
        candidate = path if path.as_posix() == path_key else Path(path_key)
        canonical_key = _canonical_typescript_inventory_path_key(path_key)
        force_config = canonical_key in reachable_configs.paths
        if not _is_typescript_inventory_candidate(candidate) and not force_config:
            continue
        relevant_entries += 1
        if relevant_entries > TS_FALLBACK_INVENTORY_ENTRY_LIMIT:
            retention.add_reason(
                "TypeScript supplied project inventory reached the "
                f"{TS_FALLBACK_INVENTORY_ENTRY_LIMIT}-entry safety limit"
                f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
            )
            break
        retention.add(candidate, force_config=force_config)
    return retention.build()


def _load_bounded_typescript_inventory(
    repo_root: Path,
    ignored_patterns: list[str],
    *,
    check_deadline: Callable[[], None],
) -> _TypescriptInventory:
    git_inventory = _stream_bounded_git_typescript_inventory(
        repo_root,
        ignored_patterns,
        check_deadline=check_deadline,
    )
    inventory = (
        git_inventory
        if git_inventory is not None
        else _walk_bounded_typescript_inventory(
            repo_root,
            ignored_patterns,
            check_deadline=check_deadline,
        )
    )
    return _retain_fallback_config_extends(
        repo_root,
        inventory,
        ignored_patterns,
        git_backed=git_inventory is not None,
        check_deadline=check_deadline,
    )


def _retain_fallback_config_extends(
    repo_root: Path,
    inventory: _TypescriptInventory,
    ignored_patterns: list[str],
    *,
    git_backed: bool,
    check_deadline: Callable[[], None],
) -> _TypescriptInventory:
    git_visibility_checker = _GitFallbackConfigVisibilityChecker(repo_root) if git_backed else None
    try:
        return _retain_fallback_config_extends_with_visibility(
            repo_root,
            inventory,
            ignored_patterns,
            git_backed=git_backed,
            check_deadline=check_deadline,
            git_visibility_checker=git_visibility_checker,
        )
    finally:
        if git_visibility_checker is not None:
            git_visibility_checker.close()


def _retain_fallback_config_extends_with_visibility(
    repo_root: Path,
    inventory: _TypescriptInventory,
    ignored_patterns: list[str],
    *,
    git_backed: bool,
    check_deadline: Callable[[], None],
    git_visibility_checker: _FallbackConfigVisibility | None,
) -> _TypescriptInventory:
    """Add only safe arbitrary-suffix configs reached by ``extends`` edges."""

    retention = _TypescriptInventoryRetention(
        "TypeScript fallback config discovery",
        partial_reason=inventory.partial_reason,
    )
    for path in inventory.paths:
        retention.add(path)
    inventory_by_path = {
        _canonical_typescript_inventory_path_key(path_key): path
        for path in inventory.paths
        if (path_key := _inventory_path_key(path)) is not None
    }
    pending = sorted(
        path_key
        for path in inventory.paths
        if (path_key := _inventory_path_key(path)) is not None
        and PurePosixPath(path_key).name.lower() in TS_CONFIG_ROOT_NAMES
    )
    reachable = {_canonical_typescript_inventory_path_key(path_key) for path_key in pending}
    package_index: dict[str, tuple[_TypescriptPackageConfig, ...]] | None = None
    git_visibility_by_path: dict[str, bool | None] = {}
    candidate_checks = 0

    while pending:
        check_deadline()
        config_path = pending.pop()
        config_key = _canonical_typescript_inventory_path_key(config_path)
        inventory_path = inventory_by_path.get(config_key)
        if inventory_path is None:
            continue
        config_text = _read_inventory_config(
            repo_root,
            inventory_path,
            check_deadline=check_deadline,
        )
        if config_text is None:
            continue
        for extends_value in _parse_typescript_config_extends(config_text):
            check_deadline()
            if _is_relative_config_extends(extends_value):
                relative_candidates = _relative_config_extends_candidates(
                    config_path,
                    extends_value,
                    on_invalid=retention.add_reason,
                )
                candidate_groups = (relative_candidates,) if relative_candidates else ()
            else:
                package_specifier = _parse_package_config_specifier(extends_value)
                if package_specifier is None:
                    continue
                if package_index is None:
                    package_index_result = _build_typescript_package_index(
                        repo_root,
                        inventory_by_path,
                        check_deadline=check_deadline,
                    )
                    package_index = package_index_result.packages
                    if package_index_result.partial_reason is not None:
                        retention.add_reason(package_index_result.partial_reason)
                candidate_groups = _inventory_package_extends_candidate_groups(
                    package_specifier,
                    package_index,
                    check_deadline=check_deadline,
                    on_limit=retention.add_reason,
                )

            for candidates in candidate_groups:
                resolved_path: str | None = None
                for candidate in candidates:
                    candidate_key = _canonical_typescript_inventory_path_key(candidate)
                    existing_path = inventory_by_path.get(candidate_key)
                    if existing_path is not None:
                        resolved_path = _inventory_path_key(existing_path)
                        break
                    if _fallback_config_path_ignored(candidate, ignored_patterns):
                        continue
                    candidate_checks += 1
                    if candidate_checks > TS_FILE_MANIFEST_ENTRY_LIMIT:
                        reason = (
                            "TypeScript fallback config discovery reached the "
                            f"{TS_FILE_MANIFEST_ENTRY_LIMIT} candidate-check safety limit"
                            f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
                        )
                        retention.add_reason(reason)
                        return retention.build()
                    candidate_path = Path(candidate)
                    if not _fallback_config_path_exists_safely(
                        repo_root,
                        candidate_path,
                        check_deadline=check_deadline,
                    ):
                        continue
                    if git_backed:
                        if candidate not in git_visibility_by_path:
                            if git_visibility_checker is None:
                                visible = None
                            else:
                                visible = git_visibility_checker.visible(
                                    candidate,
                                    check_deadline=check_deadline,
                                )
                            git_visibility_by_path[candidate] = visible
                        visible = git_visibility_by_path[candidate]
                        if visible is None:
                            retention.add_reason(
                                "TypeScript fallback config discovery could not verify Git ignore status"
                                f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}",
                            )
                            continue
                        if not visible:
                            continue
                    if (
                        _read_inventory_config(
                            repo_root,
                            candidate_path,
                            check_deadline=check_deadline,
                        )
                        is None
                    ):
                        continue
                    if not retention.add(candidate_path, force_config=True):
                        continue
                    inventory_by_path[candidate_key] = candidate_path
                    resolved_path = candidate
                    break
                if resolved_path is not None:
                    resolved_key = _canonical_typescript_inventory_path_key(resolved_path)
                else:
                    resolved_key = None
                if resolved_path is not None and resolved_key is not None and resolved_key not in reachable:
                    reachable.add(resolved_key)
                    pending.append(resolved_path)

    return retention.build()


def _fallback_config_path_ignored(
    path_key: str,
    ignored_patterns: list[str],
) -> bool:
    if _typescript_inventory_path_ignored(
        path_key,
        ignored_patterns,
        is_directory=False,
    ):
        return True
    for parent in PurePosixPath(path_key).parents:
        if parent == PurePosixPath("."):
            break
        if _typescript_inventory_path_ignored(
            parent.as_posix(),
            ignored_patterns,
            is_directory=True,
        ):
            return True
    return False


def _fallback_config_path_exists_safely(
    repo_root: Path,
    relative_path: Path,
    *,
    check_deadline: Callable[[], None],
) -> bool:
    """Preflight a config candidate without following symlinks or reading it."""

    check_deadline()
    path_key = _inventory_path_key(relative_path)
    if path_key is None:
        return False
    try:
        current = repo_root.resolve(strict=True)
        parts = PurePosixPath(path_key).parts
        entry: os.stat_result | None = None
        for index, component in enumerate(parts):
            check_deadline()
            current /= component
            entry = current.stat(follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode):
                return False
            if index < len(parts) - 1 and not stat.S_ISDIR(entry.st_mode):
                return False
        return entry is not None and stat.S_ISREG(entry.st_mode)
    except OSError:
        return False


class _GitFallbackConfigVisibilityChecker:
    """Query Git ignore visibility over one bounded, lazily started process."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._process: subprocess.Popen[bytes] | None = None
        self._requests: Queue[tuple[bytes, Queue[bool | None]] | None] = Queue()
        self._worker: Thread | None = None
        self._failed = False

    def visible(
        self,
        path_key: str,
        *,
        check_deadline: Callable[[], None],
    ) -> bool | None:
        if self._failed or not self._ensure_started():
            return None
        try:
            encoded_path = os.fsencode(path_key)
        except UnicodeEncodeError:
            return None
        if not encoded_path or b"\0" in encoded_path:
            return None

        response: Queue[bool | None] = Queue(maxsize=1)
        self._requests.put((encoded_path, response))
        request_deadline = time.monotonic() + _TS_GIT_VISIBILITY_TIMEOUT_SECONDS
        try:
            while True:
                check_deadline()
                remaining_seconds = request_deadline - time.monotonic()
                if remaining_seconds <= 0:
                    self._abort()
                    return None
                try:
                    result = response.get(timeout=min(0.05, remaining_seconds))
                except Empty:
                    continue
                if result is None:
                    self._abort()
                return result
        except BaseException:
            self._abort()
            raise

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        worker = self._worker
        if worker is not None and worker.is_alive() and not self._failed:
            self._requests.put(None)
            worker.join(timeout=_TS_GIT_VISIBILITY_TIMEOUT_SECONDS)
        if worker is not None and worker.is_alive():
            self._abort()
            return
        try:
            process.wait(timeout=_TS_GIT_VISIBILITY_TIMEOUT_SECONDS)
        except OSError, subprocess.TimeoutExpired:
            self._abort()
            return
        self._close_pipes()

    def _ensure_started(self) -> bool:
        if self._process is not None:
            return not self._failed
        try:
            process = subprocess.Popen(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "check-ignore",
                    "--verbose",
                    "--non-matching",
                    "-z",
                    "--stdin",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError, ValueError:
            self._failed = True
            return False
        if process.stdin is None or process.stdout is None:  # pragma: no cover - PIPE guarantees both
            with suppress(OSError):
                process.kill()
            with suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=_TS_GIT_VISIBILITY_TIMEOUT_SECONDS)
            self._failed = True
            return False
        self._process = process
        self._worker = Thread(
            target=self._serve_requests,
            args=(process.stdin, process.stdout),
            daemon=True,
            name="apex-ray-typescript-git-visibility",
        )
        self._worker.start()
        return True

    def _serve_requests(
        self,
        stdin: BinaryIO,
        stdout: BinaryIO,
    ) -> None:
        try:
            while True:
                request = self._requests.get()
                if request is None:
                    return
                path, response = request
                result = self._exchange(stdin, stdout, path)
                response.put(result)
                if result is None:
                    self._failed = True
                    return
        finally:
            with suppress(OSError, ValueError):
                stdin.close()

    @staticmethod
    def _exchange(stdin: BinaryIO, stdout: BinaryIO, path: bytes) -> bool | None:
        try:
            written = stdin.write(path + b"\0")
            stdin.flush()
            if written != len(path) + 1:
                return None
            fields: list[bytes] = []
            response_bytes = 0
            for _ in range(4):
                field = bytearray()
                while True:
                    character = stdout.read(1)
                    if not character:
                        return None
                    response_bytes += len(character)
                    if response_bytes > TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT:
                        return None
                    if character == b"\0":
                        break
                    field.extend(character)
                fields.append(bytes(field))
        except OSError, ValueError:
            return None
        if len(fields) != 4 or fields[3] != path:
            return None
        source, line_number, pattern, _ = fields
        if not source and not line_number and not pattern:
            return True
        if not source or not line_number or line_number.startswith(b"0") or not line_number.isdigit() or not pattern:
            return None
        return pattern.startswith(b"!")

    def _abort(self) -> None:
        self._failed = True
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            with suppress(OSError):
                process.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=_TS_GIT_VISIBILITY_TIMEOUT_SECONDS)
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=_TS_GIT_VISIBILITY_TIMEOUT_SECONDS)
        self._close_pipes()

    def _close_pipes(self) -> None:
        process = self._process
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()


def _git_fallback_config_path_visible(
    repo_root: Path,
    path_key: str,
    *,
    check_deadline: Callable[[], None],
) -> bool | None:
    """Return whether Git exposes a path to inventory, failing closed on errors."""

    checker = _GitFallbackConfigVisibilityChecker(repo_root)
    try:
        return checker.visible(path_key, check_deadline=check_deadline)
    finally:
        checker.close()


def _stream_bounded_git_typescript_inventory(
    repo_root: Path,
    ignored_patterns: list[str],
    *,
    check_deadline: Callable[[], None],
) -> _TypescriptInventory | None:
    command = [
        "git",
        "-C",
        str(repo_root),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        *_TS_GIT_INVENTORY_INCLUDE_PATHSPECS,
        *_TS_GIT_INVENTORY_EXCLUDE_PATHSPECS,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    stdout = process.stdout
    if stdout is None:  # pragma: no cover - PIPE guarantees stdout
        process.kill()
        process.wait()
        return None

    chunks: Queue[bytes | BaseException | None] = Queue(maxsize=2)
    stop_reader = Event()

    def enqueue(item: bytes | BaseException | None) -> bool:
        while not stop_reader.is_set():
            try:
                chunks.put(item, timeout=0.05)
                return True
            except Full:
                continue
        return False

    def read_stdout() -> None:
        try:
            while not stop_reader.is_set():
                chunk = stdout.read(64 * 1024)
                if not chunk:
                    break
                if not enqueue(chunk):
                    return
        except BaseException as exc:
            enqueue(exc)
        finally:
            enqueue(None)

    reader = Thread(
        target=read_stdout,
        name="apex-ray-typescript-git-inventory",
        daemon=True,
    )
    reader.start()
    retention = _TypescriptInventoryRetention(
        "TypeScript Git fallback inventory",
    )
    pending = bytearray()
    output_bytes = 0
    relevant_entries = 0
    try:
        while True:
            check_deadline()
            try:
                item = chunks.get(timeout=0.05)
            except Empty:
                continue
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            output_bytes += len(item)
            if output_bytes > TS_FALLBACK_GIT_OUTPUT_BYTE_LIMIT:
                retention.add_reason(
                    "TypeScript Git fallback inventory reached the "
                    f"{TS_FALLBACK_GIT_OUTPUT_BYTE_LIMIT}-byte output safety limit"
                    f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
                )
                return retention.build()
            pending.extend(item)
            consumed = 0
            while (separator := pending.find(b"\0", consumed)) >= 0:
                raw_path = bytes(pending[consumed:separator])
                consumed = separator + 1
                if not raw_path:
                    continue
                check_deadline()
                limited = _append_bounded_git_typescript_path(
                    retention,
                    raw_path,
                    ignored_patterns,
                    relevant_entries=relevant_entries,
                )
                if isinstance(limited, str):
                    retention.add_reason(limited)
                    return retention.build()
                if limited:
                    relevant_entries += 1
            if consumed:
                del pending[:consumed]
        if pending:
            check_deadline()
            limited = _append_bounded_git_typescript_path(
                retention,
                bytes(pending),
                ignored_patterns,
                relevant_entries=relevant_entries,
            )
            if isinstance(limited, str):
                retention.add_reason(limited)
                return retention.build()
        while process.poll() is None:
            check_deadline()
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
        if process.returncode != 0:
            return None
        return retention.build()
    finally:
        stop_reader.set()
        if process.poll() is None:
            with suppress(OSError):
                process.kill()
        with suppress(OSError):
            stdout.close()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=1.0)
        reader.join(timeout=1.0)


def _append_bounded_git_typescript_path(
    retention: _TypescriptInventoryRetention,
    raw_path: bytes,
    ignored_patterns: list[str],
    *,
    relevant_entries: int,
) -> bool | str:
    decoded_path = os.fsdecode(raw_path)
    path_key = _inventory_path_key(Path(decoded_path))
    if path_key is None or _typescript_inventory_path_ignored(
        path_key,
        ignored_patterns,
        is_directory=False,
    ):
        return False
    relative_path = Path(path_key)
    if not _is_typescript_inventory_candidate(relative_path):
        return False
    if relevant_entries >= TS_FALLBACK_INVENTORY_ENTRY_LIMIT:
        return (
            "TypeScript Git fallback inventory reached the "
            f"{TS_FALLBACK_INVENTORY_ENTRY_LIMIT} relevant-entry safety limit"
            f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
        )
    retention.add(relative_path)
    return True


def _walk_bounded_typescript_inventory(
    repo_root: Path,
    ignored_patterns: list[str],
    *,
    check_deadline: Callable[[], None],
) -> _TypescriptInventory:
    try:
        root = repo_root.resolve(strict=True)
    except OSError:
        return _TypescriptInventory(
            paths=[],
            partial_reason=(
                f"TypeScript fallback inventory could not resolve the repository root{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
            ),
        )

    retention = _TypescriptInventoryRetention(
        "TypeScript fallback inventory",
    )
    pending_directories = [(root, 0)]
    visited_entries = 0
    pending_directory_bytes = 0
    inspection_failures = 0
    directory_read_failures = 0
    while pending_directories:
        check_deadline()
        directory, directory_bytes = pending_directories.pop()
        pending_directory_bytes -= directory_bytes
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_deadline()
                    visited_entries += 1
                    if visited_entries > TS_FALLBACK_INVENTORY_ENTRY_LIMIT:
                        partial_reason = (
                            "TypeScript fallback inventory reached the "
                            f"{TS_FALLBACK_INVENTORY_ENTRY_LIMIT} filesystem-entry safety limit"
                            f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
                        )
                        retention.add_reason(partial_reason)
                        _add_typescript_walk_failure_reasons(
                            retention,
                            inspection_failures=inspection_failures,
                            directory_read_failures=directory_read_failures,
                        )
                        return retention.build()

                    candidate = Path(entry.path)
                    try:
                        relative_path = candidate.relative_to(root)
                    except ValueError:
                        continue
                    relative_posix = relative_path.as_posix()
                    try:
                        is_directory = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        inspection_failures += 1
                        continue
                    if _typescript_inventory_path_ignored(
                        relative_posix,
                        ignored_patterns,
                        is_directory=is_directory,
                    ):
                        continue

                    if is_directory:
                        path_bytes = len(os.fsencode(relative_posix)) + 1
                        if pending_directory_bytes + path_bytes > TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT:
                            retention.add_reason(
                                "TypeScript fallback inventory reached the "
                                f"{TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT}-byte pending-directory safety limit"
                                f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
                            )
                            continue
                        pending_directory_bytes += path_bytes
                        pending_directories.append((candidate, path_bytes))
                        continue

                    try:
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError:
                        inspection_failures += 1
                        is_file = False
                    if not is_file or not _is_typescript_inventory_candidate(relative_path):
                        continue
                    retention.add(relative_path)
        except OSError:
            directory_read_failures += 1
    _add_typescript_walk_failure_reasons(
        retention,
        inspection_failures=inspection_failures,
        directory_read_failures=directory_read_failures,
    )
    return retention.build()


def _add_typescript_walk_failure_reasons(
    retention: _TypescriptInventoryRetention,
    *,
    inspection_failures: int,
    directory_read_failures: int,
) -> None:
    if inspection_failures:
        retention.add_reason(
            "TypeScript fallback inventory could not inspect "
            f"{inspection_failures} filesystem entries"
            f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
        )
    if directory_read_failures:
        retention.add_reason(
            "TypeScript fallback inventory could not read "
            f"{directory_read_failures} directories"
            f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
        )


def _typescript_inventory_path_ignored(
    relative_posix: str,
    ignored_patterns: list[str],
    *,
    is_directory: bool,
) -> bool:
    if any(part in DISCOVERY_IGNORED_DIRS for part in PurePosixPath(relative_posix).parts):
        return True
    if path_matches_any(relative_posix, ignored_patterns):
        return True
    return is_directory and path_matches_any(f"{relative_posix.rstrip('/')}/", ignored_patterns)


def _is_typescript_inventory_candidate(path: Path) -> bool:
    suffix = path.suffix.lower()
    return (
        suffix in TS_JS_INDEX_SUFFIXES
        or suffix in TS_CONFIG_METADATA_SUFFIXES
        or path.name in TS_CONFIG_ROOT_NAMES
        or path.name == "package.json"
    )


def _is_critical_typescript_inventory_path(
    path: Path,
    *,
    force_config: bool = False,
) -> bool:
    return force_config or path.name.lower() in TS_CONFIG_ROOT_NAMES or path.name == "package.json"


def _typescript_inventory_sections(
    path: Path,
    *,
    force_config: bool = False,
) -> tuple[str, ...]:
    sections: list[str] = []
    if path.suffix.lower() in TS_JS_INDEX_SUFFIXES:
        sections.append("files")
    if path.name == "package.json":
        sections.append("package_files")
    if force_config or path.suffix.lower() in TS_CONFIG_METADATA_SUFFIXES or path.name.lower() in TS_CONFIG_ROOT_NAMES:
        sections.append("config_files")
    return tuple(sections)


def _plan_typescript_file_manifest(
    inventory: list[Path],
    reachable_config_paths: set[str],
    *,
    inventory_partial_reason: str | None = None,
    check_deadline: Callable[[], None],
) -> _TypescriptManifestPlan:
    # Plan sizes without materializing the JSON payload. A second pass streams
    # the selected entries atomically while reserving the marker and all
    # structural bytes inside the consumer's byte limit.
    section_sizes = {section: 0 for section in _TS_FILE_MANIFEST_SECTIONS}
    selected_section_keys: dict[str, frozenset[str] | None] = {}
    entry_limited = False
    for section in _TS_FILE_MANIFEST_SECTIONS:
        candidate_count = 0
        full_section_size = 0
        for path in _iter_typescript_manifest_section(
            section,
            inventory,
            reachable_config_paths,
            check_deadline=check_deadline,
        ):
            encoded_path = _encode_typescript_manifest_path(path)
            full_section_size += len(encoded_path) + (1 if candidate_count else 0)
            candidate_count += 1
        entry_limited = entry_limited or candidate_count > TS_FILE_MANIFEST_ENTRY_LIMIT
        if candidate_count <= TS_FILE_MANIFEST_ENTRY_LIMIT:
            selected_section_keys[section] = None
            section_sizes[section] = full_section_size
            continue
        selected_keys: set[str] = set()
        for critical in (True, False):
            for path in _iter_typescript_manifest_section(
                section,
                inventory,
                reachable_config_paths,
                check_deadline=check_deadline,
            ):
                if len(selected_keys) >= TS_FILE_MANIFEST_ENTRY_LIMIT:
                    break
                if (
                    _is_critical_typescript_manifest_section_path(
                        section,
                        path,
                        reachable_config_paths,
                    )
                    != critical
                ):
                    continue
                path_key = _inventory_path_key(path)
                if path_key is not None:
                    selected_keys.add(_canonical_typescript_inventory_path_key(path_key))
        selected_section_keys[section] = frozenset(selected_keys)
        emitted_count = 0
        for path in _iter_typescript_manifest_section(
            section,
            inventory,
            reachable_config_paths,
            check_deadline=check_deadline,
        ):
            path_key = _inventory_path_key(path)
            if path_key is None or _canonical_typescript_inventory_path_key(path_key) not in selected_keys:
                continue
            encoded_path = _encode_typescript_manifest_path(path)
            section_sizes[section] += len(encoded_path) + (1 if emitted_count else 0)
            emitted_count += 1

    entry_reason = _typescript_manifest_partial_reason(
        entry_limited=entry_limited,
        byte_limited=False,
    )
    candidate_reason = _combine_typescript_partial_reasons(
        inventory_partial_reason,
        entry_reason,
    )
    candidate_size = _typescript_file_manifest_base_size(candidate_reason) + sum(section_sizes.values())
    byte_limited = candidate_size > TS_FILE_MANIFEST_BYTE_LIMIT
    partial_reason = _combine_typescript_partial_reasons(
        inventory_partial_reason,
        _typescript_manifest_partial_reason(
            entry_limited=entry_limited,
            byte_limited=byte_limited,
        ),
    )
    if _typescript_file_manifest_base_size(partial_reason) > TS_FILE_MANIFEST_BYTE_LIMIT:
        raise AnalyzerError(
            "TypeScript file manifest byte safety limit is too small to encode a partial inventory marker."
        )
    section_byte_limits = (
        _allocate_typescript_manifest_section_bytes(
            section_sizes,
            TS_FILE_MANIFEST_BYTE_LIMIT - _typescript_file_manifest_base_size(partial_reason),
        )
        if byte_limited
        else None
    )
    if section_byte_limits is not None:
        selected_section_keys = {
            section: _limit_typescript_manifest_section_keys_by_bytes(
                section,
                inventory,
                reachable_config_paths,
                selected_section_keys[section],
                section_byte_limits[section],
                check_deadline=check_deadline,
            )
            for section in _TS_FILE_MANIFEST_SECTIONS
        }
    return _TypescriptManifestPlan(
        partial_reason=partial_reason,
        byte_limited=byte_limited,
        selected_section_keys=selected_section_keys,
    )


def _limit_typescript_manifest_section_keys_by_bytes(
    section: str,
    inventory: list[Path],
    reachable_config_paths: set[str],
    candidate_keys: frozenset[str] | None,
    byte_limit: int,
    *,
    check_deadline: Callable[[], None],
) -> frozenset[str]:
    selected_keys: set[str] = set()
    selected_bytes = 0
    for critical in (True, False):
        for path in _iter_typescript_manifest_section(
            section,
            inventory,
            reachable_config_paths,
            check_deadline=check_deadline,
        ):
            path_key = _inventory_path_key(path)
            if path_key is None:
                continue
            canonical_key = _canonical_typescript_inventory_path_key(path_key)
            if (
                candidate_keys is not None and canonical_key not in candidate_keys
            ) or _is_critical_typescript_manifest_section_path(
                section,
                path,
                reachable_config_paths,
            ) != critical:
                continue
            encoded_size = len(_encode_typescript_manifest_path(path))
            additional_size = encoded_size + (1 if selected_keys else 0)
            if selected_bytes + additional_size > byte_limit:
                continue
            selected_keys.add(canonical_key)
            selected_bytes += additional_size
    return frozenset(selected_keys)


def _allocate_typescript_manifest_section_bytes(
    desired_sizes: dict[str, int],
    available_bytes: int,
) -> dict[str, int]:
    """Share a bounded manifest across non-empty sections before redistributing slack."""

    allocations = {section: 0 for section in _TS_FILE_MANIFEST_SECTIONS}
    remaining = max(0, available_bytes)
    nonempty_sections = [
        section for section in ("package_files", "config_files", "files") if desired_sizes[section] > 0
    ]
    for index, section in enumerate(nonempty_sections):
        sections_left = len(nonempty_sections) - index
        fair_share = remaining // sections_left
        allocation = min(desired_sizes[section], fair_share)
        allocations[section] = allocation
        remaining -= allocation

    for section in ("files", "config_files", "package_files"):
        if remaining <= 0:
            break
        deficit = desired_sizes[section] - allocations[section]
        extra = min(deficit, remaining)
        allocations[section] += extra
        remaining -= extra
    return allocations


def _combine_typescript_partial_reasons(*reasons: str | None) -> str | None:
    present = [reason for reason in reasons if reason]
    return " ".join(dict.fromkeys(present)) if present else None


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
            path_key = _inventory_path_key(path)
            if path_key is None:
                continue
            canonical_key = _canonical_typescript_inventory_path_key(path_key)
            selected_keys = plan.selected_section_keys[section]
            if selected_keys is not None and canonical_key not in selected_keys:
                continue
            encoded_path = _encode_typescript_manifest_path(path)
            additional_size = len(encoded_path) + (1 if emitted_count else 0)
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
            matches = path.suffix.lower() in TS_CONFIG_METADATA_SUFFIXES or (
                path_key is not None and _canonical_typescript_inventory_path_key(path_key) in reachable_config_paths
            )
        if not matches:
            continue
        path_key = _inventory_path_key(path)
        if path_key is None:
            continue
        yield path


def _is_critical_typescript_manifest_section_path(
    section: str,
    path: Path,
    reachable_config_paths: set[str],
) -> bool:
    path_key = _inventory_path_key(path)
    is_reachable_config = (
        path_key is not None and _canonical_typescript_inventory_path_key(path_key) in reachable_config_paths
    )
    if section == "files":
        return is_reachable_config
    if section == "package_files":
        return True
    return _is_critical_typescript_inventory_path(path) or is_reachable_config


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
    previous_canonical_key: str | None = None
    adjacent_duplicates = False
    for path in inventory:
        check_deadline()
        normalized = path.as_posix()
        path_key = _inventory_path_key(path)
        if path_key is None:
            continue
        canonical_key = _canonical_typescript_inventory_path_key(path_key)
        if previous_path is not None and (
            normalized < previous_path
            or (previous_canonical_key is not None and canonical_key < previous_canonical_key)
        ):

            def checked_path_key(candidate: Path) -> str:
                check_deadline()
                return candidate.as_posix()

            ordered = sorted(inventory, key=checked_path_key)
            deduplicated: list[Path] = []
            seen_keys: set[str] = set()
            for candidate in ordered:
                check_deadline()
                candidate_key = _inventory_path_key(candidate)
                if candidate_key is None:
                    continue
                candidate_canonical_key = _canonical_typescript_inventory_path_key(candidate_key)
                if candidate_canonical_key in seen_keys:
                    continue
                seen_keys.add(candidate_canonical_key)
                deduplicated.append(candidate)
            check_deadline()
            return deduplicated
        adjacent_duplicates = adjacent_duplicates or canonical_key == previous_canonical_key
        previous_path = normalized
        previous_canonical_key = canonical_key
    if adjacent_duplicates:
        deduplicated = []
        previous_canonical_key = None
        for path in inventory:
            check_deadline()
            path_key = _inventory_path_key(path)
            if path_key is None:
                continue
            canonical_key = _canonical_typescript_inventory_path_key(path_key)
            if canonical_key == previous_canonical_key:
                continue
            deduplicated.append(path)
            previous_canonical_key = canonical_key
        return deduplicated
    return inventory


def _reachable_typescript_config_paths(
    repo_root: Path,
    inventory: list[Path],
    *,
    check_deadline: Callable[[], None] = lambda: None,
) -> _TypescriptReachableConfigs:
    scan_limit = max(
        TS_FALLBACK_INVENTORY_ENTRY_LIMIT,
        TS_FILE_MANIFEST_ENTRY_LIMIT,
    )
    config_roots: list[str] = []
    for scanned_entries, path in enumerate(inventory, start=1):
        check_deadline()
        if scanned_entries > scan_limit:
            break
        if path.name.lower() not in TS_CONFIG_ROOT_NAMES:
            continue
        path_key = _inventory_path_key(path)
        if path_key is not None:
            config_roots.append(path_key)
    if not config_roots:
        return _TypescriptReachableConfigs(paths=set(), partial_reason=None)

    inventory_by_path: dict[str, Path] = {}
    for scanned_entries, path in enumerate(inventory, start=1):
        check_deadline()
        if scanned_entries > scan_limit:
            break
        path_key = _inventory_path_key(path)
        if path_key is None:
            continue
        canonical_key = _canonical_typescript_inventory_path_key(path_key)
        if canonical_key in inventory_by_path:
            continue
        inventory_by_path[canonical_key] = path
    reachable = {_canonical_typescript_inventory_path_key(path_key) for path_key in config_roots}
    pending = list(config_roots)
    package_index: dict[str, tuple[_TypescriptPackageConfig, ...]] | None = None
    partial_reason: str | None = None

    def record_candidate_limit(reason: str) -> None:
        nonlocal partial_reason
        partial_reason = _combine_typescript_partial_reasons(
            partial_reason,
            reason,
        )

    while pending:
        check_deadline()
        config_path = pending.pop()
        config_key = _canonical_typescript_inventory_path_key(config_path)
        inventory_path = inventory_by_path.get(config_key)
        if inventory_path is None:
            continue
        config_text = _read_inventory_config(
            repo_root,
            inventory_path,
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
                    on_invalid=record_candidate_limit,
                )
                extended_paths = () if extended_path is None else (extended_path,)
            else:
                package_specifier = _parse_package_config_specifier(extends_value)
                if package_specifier is None:
                    continue
                if package_index is None:
                    package_index_result = _build_typescript_package_index(
                        repo_root,
                        inventory_by_path,
                        check_deadline=check_deadline,
                    )
                    package_index = package_index_result.packages
                    partial_reason = _combine_typescript_partial_reasons(
                        partial_reason,
                        package_index_result.partial_reason,
                    )
                extended_paths = _resolve_inventory_package_extends(
                    package_specifier,
                    package_index,
                    inventory_by_path,
                    check_deadline=check_deadline,
                    on_candidate_limit=record_candidate_limit,
                )
            for extended_path in extended_paths:
                extended_key = _canonical_typescript_inventory_path_key(extended_path)
                if extended_key in reachable:
                    continue
                reachable.add(extended_key)
                pending.append(extended_path)
    return _TypescriptReachableConfigs(
        paths=reachable,
        partial_reason=partial_reason,
    )


def _inventory_path_key(path: Path) -> str | None:
    raw_path = path.as_posix()
    posix_path = PurePosixPath(raw_path)
    if (
        "\x00" in raw_path
        or path.is_absolute()
        or posix_path.is_absolute()
        or PureWindowsPath(raw_path).is_absolute()
        or ".." in posix_path.parts
    ):
        return None
    normalized = posix_path.as_posix()
    if normalized in {"", "."} or _typescript_safe_text_byte_size(normalized) is None:
        return None
    return normalized


def _canonical_typescript_inventory_path_key(path_key: str) -> str:
    return path_key if TS_INVENTORY_CASE_SENSITIVE else path_key.lower()


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
    except json.JSONDecodeError:
        return ()
    except RecursionError:
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
    *,
    on_invalid: Callable[[str], None] | None = None,
) -> str | None:
    for candidate in _relative_config_extends_candidates(
        config_path,
        extends_value,
        on_invalid=on_invalid,
    ):
        inventory_path = inventory_by_path.get(_canonical_typescript_inventory_path_key(candidate))
        if inventory_path is not None:
            return _inventory_path_key(inventory_path)
    return None


def _relative_config_extends_candidates(
    config_path: str,
    extends_value: str,
    *,
    on_invalid: Callable[[str], None] | None = None,
) -> tuple[str, ...]:
    normalized_extends = extends_value.replace("\\", "/")
    if not normalized_extends.startswith(("./", "../")):
        return ()
    candidate = posixpath.normpath(
        posixpath.join(
            posixpath.dirname(config_path),
            normalized_extends,
        )
    )
    candidate_path_key = _inventory_path_key(Path(candidate))
    if candidate in {"", ".", ".."} or candidate.startswith(("../", "/")) or candidate_path_key != candidate:
        if candidate_path_key is None and _typescript_safe_text_byte_size(candidate) is None and on_invalid is not None:
            on_invalid(
                "TypeScript config resolution rejected a "
                "filesystem-unrepresentable config path"
                f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
            )
        return ()
    candidates = [candidate]
    if not candidate.endswith(".json"):
        candidates.append(f"{candidate}.json")
    return tuple(candidates)


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
) -> _TypescriptPackageIndex:
    package_paths: list[tuple[str, Path]] = []
    for relative_path in inventory_by_path.values():
        check_deadline()
        package_path = _inventory_path_key(relative_path)
        if package_path is not None and PurePosixPath(package_path).name == "package.json":
            package_paths.append((package_path, relative_path))
    package_paths.sort(key=lambda item: item[0])

    mutable_index: dict[str, list[_TypescriptPackageConfig]] = {}
    retained_bytes = 0
    retained_objects = 0
    partial_reason: str | None = None
    for package_path, relative_path in package_paths:
        check_deadline()
        if retained_objects >= TS_PACKAGE_INDEX_ENTRY_LIMIT:
            partial_reason = _typescript_package_index_partial_reason()
            break
        candidate = repo_root.joinpath(*PurePosixPath(package_path).parts)
        try:
            candidate_stat = candidate.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(candidate_stat.st_mode) or stat.S_ISLNK(candidate_stat.st_mode):
            continue
        if candidate_stat.st_size > TS_PACKAGE_INDEX_BYTE_LIMIT:
            partial_reason = _typescript_package_index_partial_reason()
            break
        check_deadline()
        package_text = _read_inventory_config(
            repo_root,
            relative_path,
            check_deadline=check_deadline,
        )
        if package_text is None:
            continue
        try:
            package_json = json.loads(package_text)
        except json.JSONDecodeError:
            continue
        except RecursionError:
            continue
        if not isinstance(package_json, dict):
            continue
        package_name = package_json.get("name")
        if not isinstance(package_name, str) or not package_name:
            continue
        retained_size = len(package_text.encode("utf-8"))
        if retained_size > TS_PACKAGE_INDEX_BYTE_LIMIT - retained_bytes:
            partial_reason = _typescript_package_index_partial_reason()
            break
        tsconfig_value = package_json.get("tsconfig")
        package_config = _TypescriptPackageConfig(
            directory=posixpath.dirname(package_path),
            tsconfig=tsconfig_value if isinstance(tsconfig_value, str) else None,
            exports=package_json.get("exports"),
        )
        mutable_index.setdefault(package_name, []).append(package_config)
        retained_bytes += retained_size
        retained_objects += 1
    return _TypescriptPackageIndex(
        packages={
            package_name: tuple(sorted(configs, key=lambda config: config.directory))
            for package_name, configs in mutable_index.items()
        },
        partial_reason=partial_reason,
    )


def _typescript_package_index_partial_reason() -> str:
    return (
        "TypeScript package metadata index reached the "
        f"{TS_PACKAGE_INDEX_BYTE_LIMIT}-byte or "
        f"{TS_PACKAGE_INDEX_ENTRY_LIMIT}-object safety limit"
        f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
    )


def _resolve_inventory_package_extends(
    package_specifier: tuple[str, str],
    package_index: dict[str, tuple[_TypescriptPackageConfig, ...]],
    inventory_by_path: dict[str, Path],
    *,
    check_deadline: Callable[[], None] = lambda: None,
    on_candidate_limit: Callable[[str], None] | None = None,
) -> tuple[str, ...]:
    resolved: set[str] = set()
    for candidates in _inventory_package_extends_candidate_groups(
        package_specifier,
        package_index,
        check_deadline=check_deadline,
        on_limit=on_candidate_limit,
    ):
        for candidate in candidates:
            check_deadline()
            inventory_path = inventory_by_path.get(_canonical_typescript_inventory_path_key(candidate))
            if inventory_path is not None:
                resolved_path = _inventory_path_key(inventory_path)
                if resolved_path is not None:
                    resolved.add(resolved_path)
                break
    return tuple(sorted(resolved))


def _inventory_package_extends_candidate_groups(
    package_specifier: tuple[str, str],
    package_index: dict[str, tuple[_TypescriptPackageConfig, ...]],
    *,
    check_deadline: Callable[[], None] = lambda: None,
    on_limit: Callable[[str], None] | None = None,
) -> tuple[tuple[str, ...], ...]:
    package_name, package_subpath = package_specifier
    candidate_groups: set[tuple[str, ...]] = set()
    for package in package_index.get(package_name, ()):
        check_deadline()
        if not package_subpath and package.tsconfig:
            candidates = _inventory_package_target_candidates(
                package.directory,
                package.tsconfig,
            )
            if candidates:
                if not _add_typescript_package_candidate_group(
                    candidate_groups,
                    candidates,
                    on_limit=on_limit,
                ):
                    return tuple(sorted(candidate_groups))
            continue

        if package.exports is not None:
            for target in _package_export_targets(
                package.exports,
                package_subpath,
                check_deadline=check_deadline,
                on_limit=on_limit,
            ):
                check_deadline()
                candidates = _inventory_package_target_candidates(
                    package.directory,
                    target,
                    require_dot_relative=True,
                )
                if candidates:
                    if not _add_typescript_package_candidate_group(
                        candidate_groups,
                        candidates,
                        on_limit=on_limit,
                    ):
                        return tuple(sorted(candidate_groups))
            continue

        conventional_target = f"./{package_subpath}" if package_subpath else "./tsconfig"
        candidates = _inventory_package_target_candidates(
            package.directory,
            conventional_target,
        )
        if candidates:
            if not _add_typescript_package_candidate_group(
                candidate_groups,
                candidates,
                on_limit=on_limit,
            ):
                return tuple(sorted(candidate_groups))
    return tuple(sorted(candidate_groups))


def _add_typescript_package_candidate_group(
    candidate_groups: set[tuple[str, ...]],
    candidates: tuple[str, ...],
    *,
    on_limit: Callable[[str], None] | None,
) -> bool:
    if candidates in candidate_groups:
        return True
    if len(candidate_groups) >= TS_PACKAGE_EXTENDS_CANDIDATE_GROUP_LIMIT:
        if on_limit is not None:
            on_limit(
                "TypeScript package config resolution reached the "
                f"{TS_PACKAGE_EXTENDS_CANDIDATE_GROUP_LIMIT}-candidate-group safety limit"
                f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
            )
        return False
    candidate_groups.add(candidates)
    return True


def _package_export_targets(
    exports: object,
    package_subpath: str,
    *,
    check_deadline: Callable[[], None] = lambda: None,
    on_limit: Callable[[str], None] | None = None,
) -> tuple[str, ...]:
    traversal = _PackageExportTraversal(on_limit)
    export_key = f"./{package_subpath}" if package_subpath else "."
    if isinstance(exports, (str, list)):
        if not package_subpath:
            _iter_package_export_strings(
                exports,
                check_deadline=check_deadline,
                traversal=traversal,
            )
        return tuple(traversal.targets)
    if not isinstance(exports, dict):
        return ()

    subpath_exports = False
    for key in exports:
        if not traversal.visit(check_deadline):
            return tuple(traversal.targets)
        if isinstance(key, str) and key.startswith("."):
            subpath_exports = True
            break
    if not subpath_exports:
        if not package_subpath:
            _iter_package_export_strings(
                exports,
                check_deadline=check_deadline,
                traversal=traversal,
            )
        return tuple(traversal.targets)
    if export_key in exports:
        _iter_package_export_strings(
            exports[export_key],
            check_deadline=check_deadline,
            traversal=traversal,
        )
        return tuple(traversal.targets)

    for pattern, target in exports.items():
        if not traversal.visit(check_deadline):
            break
        if not isinstance(pattern, str):
            continue
        wildcard_value = _match_package_export_pattern(pattern, export_key)
        if wildcard_value is None:
            continue
        _iter_package_export_strings(
            target,
            wildcard_value,
            check_deadline=check_deadline,
            traversal=traversal,
        )
    return tuple(traversal.targets)


def _iter_package_export_strings(
    value: object,
    wildcard_value: str | None = None,
    *,
    check_deadline: Callable[[], None] = lambda: None,
    traversal: _PackageExportTraversal | None = None,
) -> tuple[str, ...]:
    active_traversal = traversal or _PackageExportTraversal()
    starting_targets = len(active_traversal.targets)
    iterators: list[Iterator[object]] = [iter((value,))]
    while iterators:
        try:
            item = next(iterators[-1])
        except StopIteration:
            iterators.pop()
            continue
        if not active_traversal.visit(check_deadline):
            break
        if isinstance(item, str):
            if not active_traversal.add_target(item, wildcard_value):
                break
        elif isinstance(item, list):
            iterators.append(iter(item))
        elif isinstance(item, dict):
            iterators.append(iter(item.values()))
    return tuple(active_traversal.targets)[starting_targets:]


def _typescript_package_export_partial_reason() -> str:
    return (
        "TypeScript package exports traversal reached the "
        f"{TS_PACKAGE_EXPORT_ENTRY_LIMIT}-entry, "
        f"{TS_PACKAGE_EXPORT_TARGET_LIMIT}-target, or "
        f"{TS_PACKAGE_EXPORT_SINGLE_TARGET_BYTE_LIMIT}-byte per-target/"
        f"{TS_PACKAGE_EXPORT_TARGET_BYTES_LIMIT}-byte retained-target-byte safety limits"
        f"{_TS_FILE_MANIFEST_PARTIAL_SUFFIX}"
    )


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


def _inventory_package_target_candidates(
    package_directory: str,
    target: str,
    *,
    require_dot_relative: bool = False,
) -> tuple[str, ...]:
    normalized_target = target.replace("\\", "/")
    if (
        not normalized_target
        or normalized_target.startswith(("../", "/"))
        or PureWindowsPath(normalized_target).is_absolute()
        or (require_dot_relative and not normalized_target.startswith("./"))
    ):
        return ()
    candidate = posixpath.normpath(posixpath.join(package_directory, normalized_target))
    if (
        candidate in {"", ".", ".."}
        or candidate.startswith(("../", "/"))
        or _inventory_path_key(Path(candidate)) != candidate
    ):
        return ()
    if package_directory and not candidate.startswith(f"{package_directory}/"):
        return ()
    candidates = [candidate]
    if not candidate.endswith(".json"):
        candidates.append(f"{candidate}.json")
    return tuple(candidates)


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


def _monotonic_elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


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


def _merge_analyzer_results(
    results: list[AnalyzerResult],
    *,
    shard_indexes: list[int] | None = None,
    total_shards: int | None = None,
) -> AnalyzerResult:
    first = results[0]
    source_shard_indexes = shard_indexes or list(range(1, len(results) + 1))
    if len(source_shard_indexes) != len(results):
        raise ValueError("TypeScript analyzer result/shard index count mismatch")
    total_shards = total_shards or len(results)
    warnings, warning_summaries = _merge_warning_summaries(results, source_shard_indexes)
    failed_files = list(dict.fromkeys(path for result in results for path in result.failed_files))

    tsconfig_paths = {result.tsconfig_path for result in results}
    tsconfig_path = tsconfig_paths.pop() if len(tsconfig_paths) == 1 else None
    return AnalyzerResult(
        language=first.language,
        projectRoot=first.project_root,
        tsconfigPath=tsconfig_path,
        files=[file for result in results for file in result.files],
        warnings=warnings,
        warningSummaries=warning_summaries,
        indexCache=_merge_index_cache_stats(results),
        partial=any(_analyzer_result_is_partial(result) for result in results),
        coverage=_merge_analyzer_coverage(results, failed_files),
        metrics=_merge_analyzer_metrics(results, source_shard_indexes, total_shards),
        failedFiles=failed_files,
        shardFailures=[
            failure.model_copy(update={"index": shard_index, "total": total_shards})
            for result, shard_index in zip(results, source_shard_indexes, strict=True)
            for failure in result.shard_failures
        ],
    )


def _merge_warning_summaries(
    results: list[AnalyzerResult],
    source_shard_indexes: list[int] | None = None,
) -> tuple[list[str], list[AnalyzerWarningSummary]]:
    ordered_messages: list[str] = []
    occurrence_counts: dict[str, int] = {}
    shard_indexes: dict[str, list[int]] = {}

    def add(message: str, occurrences: int, shard_index: int) -> None:
        if message not in occurrence_counts:
            ordered_messages.append(message)
            occurrence_counts[message] = 0
            shard_indexes[message] = []
        occurrence_counts[message] += occurrences
        if shard_index not in shard_indexes[message]:
            shard_indexes[message].append(shard_index)

    source_shard_indexes = source_shard_indexes or list(range(1, len(results) + 1))
    for result, shard_index in zip(results, source_shard_indexes, strict=True):
        summarized_messages: set[str] = set()
        for summary in result.warning_summaries:
            add(summary.message, summary.occurrences, shard_index)
            summarized_messages.add(summary.message)
        for warning in result.warnings:
            if warning in summarized_messages:
                continue
            add(warning, 1, shard_index)

    return ordered_messages, [
        AnalyzerWarningSummary(
            message=message,
            occurrences=occurrence_counts[message],
            shardIndexes=shard_indexes[message],
        )
        for message in ordered_messages
    ]


def _merge_analyzer_coverage(
    results: list[AnalyzerResult],
    failed_files: list[str],
) -> AnalyzerCoverageSignal | None:
    merged_partial = any(_analyzer_result_is_partial(result) for result in results)
    if not merged_partial and not any(result.coverage is not None for result in results):
        return None
    reason_codes: list[AnalyzerPartialReasonCode] = []
    scopes: list[AnalyzerCoverageScope] = []
    reported_failed_files = 0
    for result in results:
        if result.coverage is None:
            if result.partial and "partial_reason_unspecified" not in reason_codes:
                reason_codes.append("partial_reason_unspecified")
            continue
        reported_failed_files += result.coverage.failed_file_count
        for scope in result.coverage.scopes:
            if scope not in scopes:
                scopes.append(scope)
        for reason_code in result.coverage.reason_codes:
            if reason_code not in reason_codes:
                reason_codes.append(reason_code)
    if merged_partial and not reason_codes:
        reason_codes.append("partial_reason_unspecified")
    if merged_partial and not scopes:
        scopes.append("analyzer")
    return AnalyzerCoverageSignal(
        partial=merged_partial,
        reasonCodes=reason_codes,
        scopes=scopes,
        failedFileCount=max(len(failed_files), reported_failed_files),
    )


def _analyzer_result_is_partial(result: AnalyzerResult) -> bool:
    return result.partial or (result.coverage is not None and result.coverage.partial)


def _merge_analyzer_metrics(
    results: list[AnalyzerResult],
    source_shard_indexes: list[int],
    total_shards: int,
) -> AnalyzerMetrics | None:
    result_metrics = [
        (result.metrics, shard_index)
        for result, shard_index in zip(results, source_shard_indexes, strict=True)
        if result.metrics is not None
    ]
    if not result_metrics:
        return None
    stage_durations_ms: dict[str, int] = {}
    shards: list[AnalyzerShardMetrics] = []
    for metrics, shard_index in result_metrics:
        assert metrics is not None
        for stage, duration_ms in metrics.stage_durations_ms.items():
            stage_durations_ms[stage] = stage_durations_ms.get(stage, 0) + duration_ms
        source_shard = metrics.shards[0] if metrics.shards else AnalyzerShardMetrics()
        shards.append(source_shard.model_copy(update={"index": shard_index, "total": total_shards}))
    return AnalyzerMetrics(
        wallDurationMs=sum(metrics.wall_duration_ms for metrics, _ in result_metrics),
        stageDurationsMs=stage_durations_ms,
        shards=shards,
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
