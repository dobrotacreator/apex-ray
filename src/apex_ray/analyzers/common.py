from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from apex_ray.models import AnalyzerConfig, AnalyzerResult, ChangedFile


class AnalyzerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AnalyzerBackendRun:
    name: str
    display_name: str
    changed_files_count: int
    result: AnalyzerResult | None = None
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class AnalyzerRun:
    results: list[AnalyzerResult]
    fallback_reasons_by_path: dict[str, str]
    warnings: list[str]
    backend_runs: list[AnalyzerBackendRun]


@dataclass(frozen=True, slots=True)
class _AnalyzerBackend:
    name: str
    display_name: str
    changed_files: Callable[[list[ChangedFile]], list[ChangedFile]]
    run: Callable[[Path, list[ChangedFile], AnalyzerConfig], AnalyzerResult | None]
    partial_fallback_reason: str


def run_analyzers(
    repo_root: Path,
    files: list[ChangedFile],
    config: AnalyzerConfig | None = None,
) -> AnalyzerRun:
    config = config or AnalyzerConfig()
    results: list[AnalyzerResult] = []
    fallback_reasons_by_path: dict[str, str] = {}
    warnings: list[str] = []
    backend_runs: list[AnalyzerBackendRun] = []

    for backend in _analyzer_backends():
        backend_changed_files = backend.changed_files(files)
        if not backend_changed_files:
            backend_runs.append(
                AnalyzerBackendRun(
                    name=backend.name,
                    display_name=backend.display_name,
                    changed_files_count=0,
                )
            )
            continue
        try:
            result = backend.run(repo_root, files, config)
        except AnalyzerError as exc:
            warning = f"{backend.display_name} analyzer unavailable: {exc}"
            warnings.append(warning)
            fallback_reason = f"{warning}; using diff-only fallback context."
            for changed_file in backend_changed_files:
                fallback_reasons_by_path[changed_file.path] = fallback_reason
            backend_runs.append(
                AnalyzerBackendRun(
                    name=backend.name,
                    display_name=backend.display_name,
                    changed_files_count=len(backend_changed_files),
                    warning=warning,
                )
            )
            continue

        if result is None:
            backend_runs.append(
                AnalyzerBackendRun(
                    name=backend.name,
                    display_name=backend.display_name,
                    changed_files_count=len(backend_changed_files),
                )
            )
            continue

        results.append(result)
        for failed_path in result.failed_files:
            fallback_reasons_by_path[failed_path] = backend.partial_fallback_reason
        backend_runs.append(
            AnalyzerBackendRun(
                name=backend.name,
                display_name=backend.display_name,
                changed_files_count=len(backend_changed_files),
                result=result,
            )
        )

    return AnalyzerRun(
        results=results,
        fallback_reasons_by_path=fallback_reasons_by_path,
        warnings=warnings,
        backend_runs=backend_runs,
    )


def _analyzer_backends() -> list[_AnalyzerBackend]:
    from apex_ray import analyzers

    return [
        _AnalyzerBackend(
            name="typescript",
            display_name="TypeScript",
            changed_files=analyzers.ts_js_changed_files,
            run=analyzers.run_typescript_analyzer,
            partial_fallback_reason="TypeScript analyzer shard failed; using diff-only fallback context.",
        ),
        _AnalyzerBackend(
            name="go",
            display_name="Go",
            changed_files=analyzers.go_changed_files,
            run=analyzers.run_go_analyzer,
            partial_fallback_reason="Go analyzer failed; using diff-only fallback context.",
        ),
        _AnalyzerBackend(
            name="python",
            display_name="Python",
            changed_files=analyzers.python_changed_files,
            run=analyzers.run_python_analyzer,
            partial_fallback_reason="Python analyzer failed; using diff-only fallback context.",
        ),
    ]


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
