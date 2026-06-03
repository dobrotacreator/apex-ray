import ast
import json
import os
import re
import shutil
import signal
import subprocess
import time
import tokenize
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from apex_ray.models import (
    AnalyzerConfig,
    AnalyzerFile,
    AnalyzerIndexCacheStats,
    AnalyzerReference,
    AnalyzerResult,
    AnalyzerShardFailure,
    AnalyzerSymbol,
    ChangedFile,
    ChangedHunk,
    FileKind,
)


class AnalyzerError(RuntimeError):
    pass


TS_JS_LANGUAGES = {"typescript", "javascript"}
PYTHON_LANGUAGES = {"python"}
PYTHON_READ_ERRORS = (OSError, UnicodeDecodeError, SyntaxError)
PYTHON_RELATED_TEST_LIMIT = 10
PYTHON_SCAN_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "site-packages",
}
PYTHON_DELETED_SYMBOL_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+[A-Za-z_]\w*")


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
    return [
        _AnalyzerBackend(
            name="typescript",
            display_name="TypeScript",
            changed_files=ts_js_changed_files,
            run=run_typescript_analyzer,
            partial_fallback_reason="TypeScript analyzer shard failed; using diff-only fallback context.",
        ),
        _AnalyzerBackend(
            name="python",
            display_name="Python",
            changed_files=python_changed_files,
            run=run_python_analyzer,
            partial_fallback_reason="Python analyzer failed; using diff-only fallback context.",
        ),
    ]


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


def has_python_changes(files: list[ChangedFile]) -> bool:
    return bool(python_changed_files(files))


def python_changed_files(files: list[ChangedFile]) -> list[ChangedFile]:
    return [
        file
        for file in files
        if file.language in PYTHON_LANGUAGES
        and file.file_kind in {FileKind.SOURCE, FileKind.TEST}
        and not file.is_ignored
        and file.new_path is not None
    ]


def run_python_analyzer(
    repo_root: Path,
    files: list[ChangedFile],
    config: AnalyzerConfig | None = None,
) -> AnalyzerResult | None:
    changed_files = python_changed_files(files)
    if not changed_files:
        return None

    analyzed_files: list[AnalyzerFile] = []
    warnings: list[str] = []
    failed_files: list[str] = []

    for changed_file in changed_files:
        path = changed_file.path
        source_path = _resolve_python_repo_path(repo_root, path)
        if source_path is None:
            warnings.append(f"Unsafe Python file path {path}; using diff-only fallback context.")
            failed_files.append(path)
            continue
        try:
            source = _read_python_source(source_path)
        except PYTHON_READ_ERRORS as exc:
            warnings.append(f"Unable to read Python file {path}: {exc}")
            failed_files.append(path)
            continue

        try:
            module = ast.parse(source, filename=path)
        except SyntaxError as exc:
            location = f" at line {exc.lineno}" if exc.lineno else ""
            warnings.append(f"Unable to parse Python file {path}{location}: {exc.msg}")
            failed_files.append(path)
            continue

        symbols = _collect_python_symbols(path, source, module)
        changed_symbols = _changed_python_symbols(changed_file, symbols)
        related_tests = _python_related_tests(repo_root, changed_file, symbols)
        analyzed_files.append(
            AnalyzerFile(
                path=path,
                tsconfigPath=None,
                symbols=symbols,
                imports=_python_imports(source, module),
                exports=_python_exports(module, symbols),
                relatedTests=related_tests,
                changedSymbols=changed_symbols,
            )
        )

    return AnalyzerResult(
        language="python",
        projectRoot=str(repo_root),
        tsconfigPath=None,
        files=analyzed_files,
        warnings=warnings,
        indexCache=None,
        partial=bool(failed_files),
        failedFiles=failed_files,
        shardFailures=[],
    )


def _resolve_python_repo_path(repo_root: Path, rel_path: str) -> Path | None:
    candidate = Path(rel_path)
    if candidate.is_absolute():
        return None

    resolved_root = repo_root.resolve()
    resolved_path = (resolved_root / candidate).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved_path


def _read_python_source(path: Path) -> str:
    with tokenize.open(path) as source_file:
        return source_file.read()


def _collect_python_symbols(path: str, source: str, module: ast.Module) -> list[AnalyzerSymbol]:
    lines = source.splitlines()
    return sorted(
        _collect_python_body_symbols(
            path,
            source,
            lines,
            module.body,
            parent_name=None,
            parent_exported=True,
            scope_kind="module",
        ),
        key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name),
    )


def _collect_python_body_symbols(
    path: str,
    source: str,
    lines: list[str],
    body: list[ast.stmt],
    *,
    parent_name: str | None,
    parent_exported: bool,
    scope_kind: Literal["module", "class", "function"],
) -> list[AnalyzerSymbol]:
    symbols: list[AnalyzerSymbol] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            name = _qualified_python_name(parent_name, node.name)
            exported = scope_kind != "function" and parent_exported and not node.name.startswith("_")
            class_symbol = _python_class_symbol(path, source, lines, node, name=name, exported=exported)
            symbols.append(class_symbol)
            symbols.extend(
                _collect_python_body_symbols(
                    path,
                    source,
                    lines,
                    node.body,
                    parent_name=name,
                    parent_exported=exported,
                    scope_kind="class",
                )
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            name = _qualified_python_name(parent_name, node.name)
            exported = (
                not node.name.startswith("_")
                if scope_kind == "module"
                else scope_kind == "class" and parent_exported and not node.name.startswith("_")
            )
            symbols.append(
                _python_function_symbol(
                    path,
                    source,
                    lines,
                    node,
                    name=name,
                    exported=exported,
                    kind="method" if scope_kind == "class" else "function",
                )
            )
            symbols.extend(
                _collect_python_body_symbols(
                    path,
                    source,
                    lines,
                    node.body,
                    parent_name=name,
                    parent_exported=False,
                    scope_kind="function",
                )
            )
        elif scope_kind == "module" and isinstance(node, ast.Assign | ast.AnnAssign):
            symbols.extend(_python_assignment_symbols(path, source, lines, node))
    return symbols


def _qualified_python_name(parent_name: str | None, name: str) -> str:
    return f"{parent_name}.{name}" if parent_name else name


def _python_function_symbol(
    path: str,
    source: str,
    lines: list[str],
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    name: str,
    exported: bool,
    kind: str,
) -> AnalyzerSymbol:
    return AnalyzerSymbol(
        name=name,
        kind=kind,
        startLine=_python_node_start_line(node),
        endLine=_python_node_end_line(node),
        exported=exported,
        signature=_python_signature(source, lines, node),
        references=[],
        callees=[],
        contracts=[],
        metadata=_python_decorator_references(path, source, node.decorator_list),
    )


def _python_class_symbol(
    path: str,
    source: str,
    lines: list[str],
    node: ast.ClassDef,
    *,
    name: str,
    exported: bool,
) -> AnalyzerSymbol:
    return AnalyzerSymbol(
        name=name,
        kind="class",
        startLine=_python_node_start_line(node),
        endLine=_python_node_end_line(node),
        exported=exported,
        signature=_python_signature(source, lines, node),
        references=[],
        callees=[],
        contracts=_python_base_references(path, source, node.bases),
        metadata=_python_decorator_references(path, source, node.decorator_list),
    )


def _python_assignment_symbols(
    path: str,
    source: str,
    lines: list[str],
    node: ast.Assign | ast.AnnAssign,
) -> list[AnalyzerSymbol]:
    names = _python_assignment_target_names(node)
    if not names:
        return []
    return [
        AnalyzerSymbol(
            name=name,
            kind="variable",
            startLine=_python_node_start_line(node),
            endLine=_python_node_end_line(node),
            exported=not name.startswith("_"),
            signature=_python_signature(source, lines, node),
            references=[],
            callees=[],
            contracts=_python_annotation_references(path, source, node),
            metadata=[],
        )
        for name in names
    ]


def _python_assignment_target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        names.extend(_python_target_names(target))
    return names


def _python_target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        names: list[str] = []
        for element in target.elts:
            names.extend(_python_target_names(element))
        return names
    return []


def _changed_python_symbols(changed_file: ChangedFile, symbols: list[AnalyzerSymbol]) -> list[AnalyzerSymbol]:
    changed_ranges = _changed_python_line_ranges(changed_file)
    return [
        symbol
        for symbol in symbols
        if any(_ranges_overlap(symbol.start_line, symbol.end_line, start, end) for start, end in changed_ranges)
    ]


def _changed_python_line_ranges(file: ChangedFile) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for hunk in file.hunks:
        added_lines = sorted(line.new_line for line in hunk.lines if line.new_line is not None and line.kind == "add")
        if added_lines:
            ranges.extend(_collapse_ranges(added_lines))
        elif not _deletes_python_symbol_definition(hunk):
            ranges.append((hunk.new_start, hunk.new_start))
    return ranges


def _deletes_python_symbol_definition(hunk: ChangedHunk) -> bool:
    return any(line.kind == "delete" and PYTHON_DELETED_SYMBOL_RE.match(line.content) for line in hunk.lines)


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def _python_imports(source: str, module: ast.Module) -> list[str]:
    imports: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        text = _python_node_text(source, node)
        if text and text not in seen:
            seen.add(text)
            imports.append(text)
    return imports


def _python_exports(module: ast.Module, symbols: list[AnalyzerSymbol]) -> list[str]:
    explicit_exports = _python_dunder_all(module)
    if explicit_exports is not None:
        return explicit_exports
    return sorted(symbol.name for symbol in symbols if symbol.exported and "." not in symbol.name)


def _python_dunder_all(module: ast.Module) -> list[str] | None:
    for node in module.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__":
            value = node.value
        if value is None:
            continue
        try:
            parsed = ast.literal_eval(value)
        except ValueError:
            continue
        except SyntaxError:
            continue
        if isinstance(parsed, list | tuple) and all(isinstance(item, str) for item in parsed):
            return sorted(parsed)
    return None


def _python_related_tests(repo_root: Path, changed_file: ChangedFile, symbols: list[AnalyzerSymbol]) -> list[str]:
    if changed_file.file_kind == FileKind.TEST:
        return []

    changed_path = changed_file.path
    module_name = _python_module_name(changed_path)
    short_module_name = module_name.removeprefix("src.") if module_name.startswith("src.") else module_name
    stem = Path(changed_path).stem
    symbol_names = {symbol.name.split(".")[-1] for symbol in symbols}
    candidates: list[tuple[int, str]] = []

    for test_path in _iter_python_test_files(repo_root):
        if test_path == changed_path:
            continue
        score = _python_related_test_score(repo_root, test_path, stem, module_name, short_module_name, symbol_names)
        if score:
            candidates.append((score, test_path))

    return [path for _, path in sorted(candidates, key=lambda item: (-item[0], item[1]))[:PYTHON_RELATED_TEST_LIMIT]]


def _python_related_test_score(
    repo_root: Path,
    test_path: str,
    stem: str,
    module_name: str,
    short_module_name: str,
    symbol_names: set[str],
) -> int:
    score = 0
    test_name = Path(test_path).name
    if test_name in {f"test_{stem}.py", f"{stem}_test.py"} or f"/{stem}/" in f"/{test_path}/":
        score += 40

    source_path = _resolve_python_repo_path(repo_root, test_path)
    if source_path is None:
        return 0

    try:
        text = _read_python_source(source_path)
    except PYTHON_READ_ERRORS:
        return score

    import_needles = {
        f"from {module_name} import",
        f"import {module_name}",
        f"from {short_module_name} import",
        f"import {short_module_name}",
    }
    if any(needle in text for needle in import_needles):
        score += 60
    if stem in text:
        score += 10
    if any(name and name in text for name in symbol_names):
        score += 20
    return score


def _iter_python_test_files(repo_root: Path) -> list[str]:
    paths: list[str] = []
    for current_root, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in PYTHON_SCAN_IGNORED_DIRS]
        current_path = Path(current_root)
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = current_path / filename
            rel_path = path.relative_to(repo_root).as_posix()
            if _is_python_test_path(rel_path):
                paths.append(rel_path)
    return sorted(paths)


def _is_python_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name
    return "tests" in parts or "test" in parts or name.startswith("test_") or name.endswith("_test.py")


def _python_module_name(path: str) -> str:
    without_suffix = path.removesuffix(".py").replace("/", ".")
    if without_suffix.endswith(".__init__"):
        without_suffix = without_suffix.removesuffix(".__init__")
    if without_suffix.startswith("src."):
        return without_suffix.removeprefix("src.")
    return without_suffix


def _python_decorator_references(path: str, source: str, decorators: list[ast.expr]) -> list[AnalyzerReference]:
    references: list[AnalyzerReference] = []
    for decorator in decorators:
        text = _python_node_text(source, decorator)
        if not text:
            continue
        references.append(
            AnalyzerReference(
                file=path,
                line=_python_node_start_line(decorator),
                endLine=_python_node_end_line(decorator),
                text=f"@{text}",
                kind="metadata",
            )
        )
    return references


def _python_base_references(path: str, source: str, bases: list[ast.expr]) -> list[AnalyzerReference]:
    references: list[AnalyzerReference] = []
    for base in bases:
        text = _python_node_text(source, base)
        if not text:
            continue
        references.append(
            AnalyzerReference(
                file=path,
                line=_python_node_start_line(base),
                endLine=_python_node_end_line(base),
                text=text,
                kind="contract",
            )
        )
    return references


def _python_annotation_references(path: str, source: str, node: ast.Assign | ast.AnnAssign) -> list[AnalyzerReference]:
    if not isinstance(node, ast.AnnAssign):
        return []
    text = _python_node_text(source, node.annotation)
    if not text:
        return []
    return [
        AnalyzerReference(
            file=path,
            line=_python_node_start_line(node.annotation),
            endLine=_python_node_end_line(node.annotation),
            text=text,
            kind="contract",
        )
    ]


def _python_signature(
    source: str,
    lines: list[str],
    node: ast.AST,
) -> str:
    segment = _python_node_text(source, node)
    if segment:
        first_line = segment.splitlines()[0].strip()
        return first_line.removesuffix(":")
    line_number = _python_node_start_line(node)
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1].strip().removesuffix(":")
    return ""


def _python_node_text(source: str, node: ast.AST) -> str:
    return (ast.get_source_segment(source, node) or "").strip()


def _python_node_start_line(node: ast.AST) -> int:
    lines = [getattr(node, "lineno", 1)]
    if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        lines.extend(getattr(decorator, "lineno", getattr(node, "lineno", 1)) for decorator in node.decorator_list)
    return min(line for line in lines if line is not None)


def _python_node_end_line(node: ast.AST) -> int:
    return getattr(node, "end_lineno", getattr(node, "lineno", 1)) or getattr(node, "lineno", 1)


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
