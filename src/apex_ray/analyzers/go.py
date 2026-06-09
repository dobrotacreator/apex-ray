import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

from pydantic import ValidationError

from apex_ray.models import AnalyzerConfig, AnalyzerResult, ChangedFile, FileKind

from .common import AnalyzerError, _collapse_ranges

GO_LANGUAGES = {"go"}


def has_go_changes(files: list[ChangedFile]) -> bool:
    return bool(go_changed_files(files))


def go_changed_files(files: list[ChangedFile]) -> list[ChangedFile]:
    return [
        file
        for file in files
        if file.language in GO_LANGUAGES
        and file.file_kind in {FileKind.SOURCE, FileKind.TEST}
        and not file.is_ignored
        and (file.new_path is not None or file.old_path is not None)
    ]


def run_go_analyzer(
    repo_root: Path,
    files: list[ChangedFile],
    config: AnalyzerConfig | None = None,
) -> AnalyzerResult | None:
    changed_files = go_changed_files(files)
    if not changed_files:
        return None
    config = config or AnalyzerConfig()
    if shutil.which("go") is None:
        raise AnalyzerError("Go is required for the Go analyzer but was not found on PATH.")

    runtime_dir = go_analyzer_runtime_dir()
    if not runtime_dir.exists():
        raise AnalyzerError(f"Go analyzer runtime is not available: {runtime_dir}")

    args = _go_analyzer_args(repo_root, changed_files, config)
    try:
        proc = _run_analyzer_process(
            args,
            cwd=runtime_dir,
            timeout=config.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalyzerError(f"Go analyzer timed out after {_format_seconds(config.timeout_seconds)}") from exc
    if proc.returncode != 0:
        raise AnalyzerError(proc.stderr.strip() or proc.stdout.strip() or "Go analyzer failed")

    try:
        return AnalyzerResult.model_validate(json.loads(proc.stdout))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise AnalyzerError(f"Invalid Go analyzer output: {exc}") from exc


def _go_analyzer_args(repo_root: Path, changed_files: list[ChangedFile], config: AnalyzerConfig) -> list[str]:
    args = [
        "go",
        "run",
        "./cmd/apex-ray-go-analyzer",
        "--repo",
        str(repo_root),
        "--changed",
    ]
    args.extend(file.path for file in changed_files)
    args.extend(["--analysis-time-budget-ms", str(_analysis_time_budget_ms(config.timeout_seconds))])
    for file in changed_files:
        for start, end in _changed_new_line_ranges(file):
            args.extend(["--range", f"{file.path}:{start}-{end}"])
        for line, content in _deleted_lines(file):
            args.extend(["--deleted-line", file.path, str(line), content])
    return args


def go_analyzer_runtime_dir() -> Path:
    bundled = Path(__file__).resolve().parents[1] / "_bundled" / "go"
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parents[3] / "analyzer-runtimes" / "go"


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


def _format_seconds(seconds: float) -> str:
    rounded = round(seconds)
    if abs(seconds - rounded) < 0.05:
        return f"{rounded}s"
    return f"{seconds:.1f}s"


def _analysis_time_budget_ms(timeout_seconds: float) -> int:
    margin_seconds = min(5.0, max(0.25, timeout_seconds * 0.05))
    budget_seconds = max(0.001, timeout_seconds - margin_seconds)
    return max(1, round(budget_seconds * 1000))


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
        next_new_line = hunk.new_start
        for line in hunk.lines:
            if line.new_line is not None:
                next_new_line = line.new_line + 1
            if line.kind == "delete":
                lines.append((next_new_line, line.content))
    return lines
