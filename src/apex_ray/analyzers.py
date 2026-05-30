import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from apex_ray.models import (
    AnalyzerConfig,
    AnalyzerIndexCacheStats,
    AnalyzerResult,
    AnalyzerShardFailure,
    ChangedFile,
    FileKind,
)


class AnalyzerError(RuntimeError):
    pass


TS_JS_LANGUAGES = {"typescript", "javascript"}


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

    results: list[AnalyzerResult] = []
    failures: list[AnalyzerShardFailure] = []
    shards = list(_shard_changed_files(changed_files, config))
    large_change_set_size = len(changed_files) if len(changed_files) >= config.large_change_file_threshold else None
    deadline = time.monotonic() + config.timeout_seconds
    for index, shard in enumerate(shards, start=1):
        remaining_seconds = config.timeout_seconds if len(shards) == 1 else deadline - time.monotonic()
        if remaining_seconds <= 0:
            timeout_error = AnalyzerError(
                f"TypeScript analyzer total timeout after {_format_seconds(config.timeout_seconds)}"
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
) -> AnalyzerResult:
    args = _typescript_analyzer_args(
        repo_root,
        script,
        changed_files,
        config,
        large_change_set_size=large_change_set_size,
    )
    actual_timeout = config.timeout_seconds if timeout_seconds is None else max(0.001, timeout_seconds)
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
) -> list[str]:
    args = ["node", str(script), "--repo", str(repo_root), "--changed"]
    args.extend(file.new_path for file in changed_files if file.new_path)
    if large_change_set_size is not None:
        args.extend(["--large-change-set-size", str(large_change_set_size)])
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


def _changed_file_shard_priority(file: ChangedFile) -> tuple[int, int, int]:
    high_risk = sum(1 for signal in file.risk_signals if str(signal.severity) == "high")
    medium_risk = sum(1 for signal in file.risk_signals if str(signal.severity) == "medium")
    kind_score = {
        FileKind.SOURCE: 6,
        FileKind.SCHEMA: 5,
        FileKind.MIGRATION: 5,
        FileKind.CONFIG: 4,
        FileKind.DEPENDENCY: 3,
        FileKind.UNKNOWN: 2,
        FileKind.TEST: 1,
    }.get(file.file_kind, 2)
    return (high_risk * 100 + medium_risk * 10, kind_score, -len(file.path))


def _format_seconds(seconds: float) -> str:
    rounded = round(seconds)
    if abs(seconds - rounded) < 0.05:
        return f"{rounded}s"
    return f"{seconds:.1f}s"


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

    bundled = Path(__file__).resolve().parent / "_bundled" / "typescript" / "analyze.js"
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parents[2] / "analyzers" / "typescript" / "dist" / "analyze.js"


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


def _collapse_ranges(lines: list[int]) -> list[tuple[int, int]]:
    if not lines:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = lines[0]
    for line in lines[1:]:
        if line == previous + 1:
            previous = line
            continue
        ranges.append((start, previous))
        start = previous = line
    ranges.append((start, previous))
    return ranges
