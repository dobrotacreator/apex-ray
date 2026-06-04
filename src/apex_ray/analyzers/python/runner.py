import ast
from pathlib import Path

from apex_ray.models import AnalyzerConfig, AnalyzerFile, AnalyzerResult, ChangedFile, FileKind

from .constants import PYTHON_LANGUAGES, PYTHON_READ_ERRORS
from .references import _populate_python_symbol_graph
from .related_tests import _python_related_tests
from .state import _PythonWorkspaceFile
from .symbols import _changed_python_symbols, _collect_python_symbols, _python_exports, _python_imports
from .utils import _python_module_name
from .workspace import _build_python_workspace, _read_python_source, _resolve_python_repo_path


def has_python_changes(files: list[ChangedFile]) -> bool:
    return bool(python_changed_files(files))


def python_changed_files(files: list[ChangedFile]) -> list[ChangedFile]:
    return [
        file
        for file in files
        if file.language in PYTHON_LANGUAGES
        and file.file_kind in {FileKind.MIGRATION, FileKind.SOURCE, FileKind.TEST}
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
    workspace_scan = _build_python_workspace(repo_root)
    workspace = workspace_scan.files
    warnings.extend(workspace_scan.warnings)

    for changed_file in changed_files:
        path = changed_file.path
        workspace_file = workspace.get(path)
        if workspace_file is None:
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
            workspace_file = _PythonWorkspaceFile(
                path=path,
                module_name=_python_module_name(path),
                source=source,
                module=module,
                symbols=symbols,
            )
            workspace[path] = workspace_file
        elif _resolve_python_repo_path(repo_root, path) is None:
            warnings.append(f"Unsafe Python file path {path}; using diff-only fallback context.")
            failed_files.append(path)
            continue

        source = workspace_file.source
        module = workspace_file.module
        symbols = workspace_file.symbols
        changed_symbols = _changed_python_symbols(changed_file, symbols)
        _populate_python_symbol_graph(workspace_file, changed_symbols, workspace)
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
        partial=bool(failed_files) or workspace_scan.partial,
        failedFiles=failed_files,
        shardFailures=[],
    )
