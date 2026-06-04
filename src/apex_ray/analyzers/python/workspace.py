import ast
import os
import tokenize
from pathlib import Path

from . import constants as _constants
from .state import _PythonWorkspaceFile, _PythonWorkspaceScan
from .symbols import _collect_python_symbols
from .utils import _python_module_name


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


def _build_python_workspace(repo_root: Path) -> _PythonWorkspaceScan:
    workspace: dict[str, _PythonWorkspaceFile] = {}
    warnings: list[str] = []
    partial = False
    for index, path in enumerate(_iter_python_files(repo_root)):
        if index >= _constants.PYTHON_WORKSPACE_FILE_LIMIT:
            warnings.append(
                f"Python workspace scan reached file limit ({_constants.PYTHON_WORKSPACE_FILE_LIMIT}); "
                "reference context may be incomplete."
            )
            partial = True
            break
        source_path = _resolve_python_repo_path(repo_root, path)
        if source_path is None:
            continue
        try:
            source_size = source_path.stat().st_size
        except OSError:
            continue
        if source_size > _constants.PYTHON_WORKSPACE_FILE_SIZE_LIMIT:
            warnings.append(
                f"Skipping Python workspace file {path}: size {source_size} bytes exceeds "
                f"limit {_constants.PYTHON_WORKSPACE_FILE_SIZE_LIMIT} bytes; reference context may be incomplete."
            )
            partial = True
            continue
        try:
            source = _read_python_source(source_path)
            module = ast.parse(source, filename=path)
        except _constants.PYTHON_READ_ERRORS:
            continue
        workspace[path] = _PythonWorkspaceFile(
            path=path,
            module_name=_python_module_name(path),
            source=source,
            module=module,
            symbols=_collect_python_symbols(path, source, module),
        )
    return _PythonWorkspaceScan(files=workspace, warnings=warnings, partial=partial)


def _iter_python_test_files(repo_root: Path) -> list[str]:
    return [path for path in _iter_python_files(repo_root) if _is_python_test_path(path)]


def _iter_python_files(repo_root: Path) -> list[str]:
    paths: list[str] = []
    for current_root, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in _constants.PYTHON_SCAN_IGNORED_DIRS]
        current_path = Path(current_root)
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = current_path / filename
            rel_path = path.relative_to(repo_root).as_posix()
            paths.append(rel_path)
    return sorted(paths)


def _is_python_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name
    return "tests" in parts or "test" in parts or name.startswith("test_") or name.endswith("_test.py")
