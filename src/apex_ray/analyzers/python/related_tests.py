from pathlib import Path

from apex_ray.models import AnalyzerSymbol, ChangedFile, FileKind

from . import constants as _constants
from .utils import _python_module_name
from .workspace import _iter_python_test_files, _read_python_source, _resolve_python_repo_path


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

    return [
        path
        for _, path in sorted(candidates, key=lambda item: (-item[0], item[1]))[: _constants.PYTHON_RELATED_TEST_LIMIT]
    ]


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
    except _constants.PYTHON_READ_ERRORS:
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
