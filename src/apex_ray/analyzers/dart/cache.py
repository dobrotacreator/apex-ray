import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import yaml
from pydantic import ValidationError

from apex_ray import __version__
from apex_ray.models import AnalyzerConfig, AnalyzerIndexCacheStats, AnalyzerResult, ChangedFile

from .constants import DART_SCAN_IGNORED_DIRS, PLATFORM_CHANNEL_LANGUAGES

DART_ANALYSIS_CACHE_VERSION = 1
DART_ANALYSIS_CACHE_FILE = f"dart-analysis-v{DART_ANALYSIS_CACHE_VERSION}.json"
DART_ANALYSIS_CACHE_BYTE_LIMIT = 16 * 1024 * 1024
DART_ANALYSIS_FINGERPRINT_FILE_LIMIT = 20_000
DART_ANALYSIS_FINGERPRINT_BYTE_LIMIT = 256 * 1024 * 1024
DART_SEMANTIC_INVENTORY_ENTRY_LIMIT = 250_000
DART_ANALYSIS_OPTIONS_INCLUDE_FILE_LIMIT = 256
DART_ANALYSIS_OPTIONS_INCLUDE_BYTE_LIMIT = 8 * 1024 * 1024
DART_CACHE_CONFIG_FILE_BYTE_LIMIT = 4 * 1024 * 1024
DART_SEMANTIC_INVENTORY_IGNORED_DIRS = DART_SCAN_IGNORED_DIRS | frozenset(
    {
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        ".worktrees",
        "__pycache__",
        "dist",
        "out",
        "venv",
    }
)
_DART_CONTEXT_FILENAMES = {
    ".fvmrc",
    "analysis_options.yaml",
    "analysis_options.yml",
    "package_config.json",
    "pubspec.lock",
    "pubspec.yaml",
    "pubspec.yml",
}


@dataclass(frozen=True, slots=True)
class DartAnalysisCacheKey:
    """Reusable result of one bounded workspace fingerprint traversal."""

    fingerprint: str
    project_files: int


class _CacheDeadlineExpired(RuntimeError):
    pass


def dart_analysis_cache_path(repo_root: Path, config: AnalyzerConfig) -> Path:
    if config.index_cache_dir:
        cache_dir = Path(config.index_cache_dir).expanduser()
        if not cache_dir.is_absolute():
            cache_dir = repo_root / cache_dir
    else:
        repo_hash = hashlib.sha256(str(repo_root.resolve()).encode()).hexdigest()[:16]
        cache_dir = _default_cache_home() / "repos" / repo_hash / "dart"
    return cache_dir.resolve() / DART_ANALYSIS_CACHE_FILE


def build_dart_analysis_cache_key(
    repo_root: Path,
    changed_files: list[ChangedFile],
    project_files: list[Path] | None,
    command: list[str],
    config: AnalyzerConfig,
    *,
    toolchain_version: str | None = None,
    deadline: float | None = None,
) -> DartAnalysisCacheKey | None:
    """Fingerprint Dart analysis inputs once within the analyzer deadline.

    The returned key is intentionally reusable across a cache lookup and the
    eventual cache write so a miss does not hash the workspace twice.
    """

    if not config.index_cache_enabled or project_files is None or _deadline_expired(deadline):
        return None
    fingerprint = _analysis_fingerprint(
        repo_root,
        changed_files,
        project_files,
        command,
        config,
        toolchain_version=toolchain_version,
        deadline=deadline,
    )
    if fingerprint is None or _deadline_expired(deadline):
        return None
    return DartAnalysisCacheKey(fingerprint=fingerprint, project_files=len(project_files))


def load_dart_analysis_cache(
    repo_root: Path,
    changed_files: list[ChangedFile],
    project_files: list[Path] | None,
    command: list[str],
    config: AnalyzerConfig,
    *,
    cache_key: DartAnalysisCacheKey | None = None,
    toolchain_version: str | None = None,
    deadline: float | None = None,
) -> AnalyzerResult | None:
    if (
        not config.index_cache_enabled
        or config.refresh_index_cache
        or project_files is None
        or _deadline_expired(deadline)
    ):
        return None
    key = cache_key or build_dart_analysis_cache_key(
        repo_root,
        changed_files,
        project_files,
        command,
        config,
        toolchain_version=toolchain_version,
        deadline=deadline,
    )
    if key is None or _deadline_expired(deadline):
        return None
    path = dart_analysis_cache_path(repo_root, config)
    if _deadline_expired(deadline):
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > DART_ANALYSIS_CACHE_BYTE_LIMIT:
            return None
        if _deadline_expired(deadline):
            return None
        serialized = path.read_text(encoding="utf-8")
        if _deadline_expired(deadline):
            return None
        payload = json.loads(serialized)
    except OSError, UnicodeError, json.JSONDecodeError, RecursionError:
        return None
    if _deadline_expired(deadline):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != DART_ANALYSIS_CACHE_VERSION or payload.get("fingerprint") != key.fingerprint:
        return None
    try:
        result = AnalyzerResult.model_validate(payload.get("result"))
    except ValidationError:
        return None
    if result.language != "dart" or _deadline_expired(deadline):
        return None
    stats = AnalyzerIndexCacheStats(
        path=str(path),
        files=key.project_files,
        hits=1,
        misses=0,
        written=False,
    )
    return result.model_copy(
        update={
            "project_root": str(repo_root.resolve(strict=False)),
            "index_cache": stats,
        }
    )


def write_dart_analysis_cache(
    repo_root: Path,
    changed_files: list[ChangedFile],
    project_files: list[Path] | None,
    command: list[str],
    config: AnalyzerConfig,
    result: AnalyzerResult,
    *,
    cache_key: DartAnalysisCacheKey | None = None,
    toolchain_version: str | None = None,
    deadline: float | None = None,
) -> AnalyzerIndexCacheStats | None:
    if not config.index_cache_enabled or project_files is None or result.partial or _deadline_expired(deadline):
        return None
    key = cache_key or build_dart_analysis_cache_key(
        repo_root,
        changed_files,
        project_files,
        command,
        config,
        toolchain_version=toolchain_version,
        deadline=deadline,
    )
    if key is None or _deadline_expired(deadline):
        return None
    path = dart_analysis_cache_path(repo_root, config)
    payload = {
        "version": DART_ANALYSIS_CACHE_VERSION,
        "fingerprint": key.fingerprint,
        "result": result.model_copy(update={"project_root": ".", "index_cache": None}).model_dump(
            mode="json",
            by_alias=True,
        ),
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(serialized) > DART_ANALYSIS_CACHE_BYTE_LIMIT or _deadline_expired(deadline):
        return None
    try:
        if _deadline_expired(deadline):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        if _deadline_expired(deadline) or path.is_symlink():
            return None
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                for offset in range(0, len(serialized), 128 * 1024):
                    _require_deadline(deadline)
                    handle.write(serialized[offset : offset + 128 * 1024])
                    _require_deadline(deadline)
                handle.flush()
                _require_deadline(deadline)
                os.fsync(handle.fileno())
                _require_deadline(deadline)
            _require_deadline(deadline)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
    except OSError, _CacheDeadlineExpired:
        return None
    return AnalyzerIndexCacheStats(
        path=str(path),
        files=key.project_files,
        hits=0,
        misses=1,
        written=True,
    )


def _analysis_fingerprint(
    repo_root: Path,
    changed_files: list[ChangedFile],
    project_files: list[Path],
    command: list[str],
    config: AnalyzerConfig,
    *,
    toolchain_version: str | None = None,
    deadline: float | None = None,
) -> str | None:
    if _deadline_expired(deadline):
        return None
    root = repo_root.resolve()
    if _deadline_expired(deadline):
        return None
    candidates: list[tuple[str, Path]] = []
    seen: set[str] = set()
    project_inventory: set[str] = set()
    for entry in project_files:
        if _deadline_expired(deadline):
            return None
        try:
            if entry.is_absolute():
                relative = entry.resolve(strict=False).relative_to(root)
            else:
                if ".." in entry.parts:
                    return None
                relative = entry
        except OSError, ValueError:
            return None
        if not relative.parts:
            return None
        project_inventory.add(relative.as_posix())
        if len(project_inventory) > DART_SEMANTIC_INVENTORY_ENTRY_LIMIT:
            return None
        if _deadline_expired(deadline):
            return None
        relative_path = relative.as_posix()
        if relative_path in seen or not _is_fingerprint_input(relative):
            continue
        try:
            absolute = (root / relative).resolve(strict=True)
            absolute.relative_to(root)
        except OSError, ValueError:
            continue
        if _deadline_expired(deadline):
            return None
        if not absolute.is_file():
            continue
        seen.add(relative_path)
        candidates.append((relative_path, absolute))
        if len(candidates) > DART_ANALYSIS_FINGERPRINT_FILE_LIMIT:
            return None
    semantic_paths = _semantic_context_paths(root, deadline=deadline)
    if semantic_paths is None or not _append_context_paths(
        root,
        candidates,
        seen,
        semantic_paths,
        deadline=deadline,
    ):
        return None
    implicit_paths = _implicit_context_paths(root, candidates, deadline=deadline)
    if implicit_paths is None or not _append_context_paths(
        root,
        candidates,
        seen,
        implicit_paths,
        deadline=deadline,
    ):
        return None
    included_analysis_options = _analysis_options_include_paths(root, candidates, deadline=deadline)
    if included_analysis_options is None or not _append_context_paths(
        root,
        candidates,
        seen,
        included_analysis_options,
        deadline=deadline,
    ):
        return None
    external_path_dependency = _has_external_path_dependency(root, candidates, deadline=deadline)
    if external_path_dependency is None or external_path_dependency:
        return None
    candidates.sort(key=lambda item: item[0])
    if _deadline_expired(deadline):
        return None

    command_identity = _command_identity(root, command, deadline=deadline)
    if command_identity is None:
        return None
    changed_payload: list[dict[str, Any]] = []
    for file in changed_files:
        if _deadline_expired(deadline):
            return None
        changed_payload.append(file.model_dump(mode="json"))
    if _deadline_expired(deadline):
        return None
    digest = hashlib.sha256()
    _hash_json(
        digest,
        {
            "version": DART_ANALYSIS_CACHE_VERSION,
            "apex_ray_version": __version__,
            "command": command,
            "command_identity": command_identity,
            "toolchain_version": toolchain_version,
            "changed": changed_payload,
            "config": _dart_config_payload(config),
        },
    )
    digest.update(b"project-inventory\0")
    for relative_path in sorted(project_inventory):
        if _deadline_expired(deadline):
            return None
        digest.update(relative_path.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
    if _deadline_expired(deadline):
        return None
    total_bytes = 0
    for relative, absolute in candidates:
        if _deadline_expired(deadline):
            return None
        try:
            size = absolute.stat().st_size
            total_bytes += size
            if total_bytes > DART_ANALYSIS_FINGERPRINT_BYTE_LIMIT:
                return None
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            with absolute.open("rb") as handle:
                while True:
                    if _deadline_expired(deadline):
                        return None
                    chunk = handle.read(128 * 1024)
                    if _deadline_expired(deadline):
                        return None
                    if not chunk:
                        break
                    digest.update(chunk)
            digest.update(b"\0")
        except OSError:
            return None
    return None if _deadline_expired(deadline) else digest.hexdigest()


def _is_fingerprint_input(path: Path) -> bool:
    return (
        path.suffix.casefold() in PLATFORM_CHANNEL_LANGUAGES
        or path.name in _DART_CONTEXT_FILENAMES
        or (path.name == "package_config.json" and ".dart_tool" in path.parts)
    )


def _append_context_paths(
    root: Path,
    candidates: list[tuple[str, Path]],
    seen: set[str],
    paths: list[Path],
    *,
    deadline: float | None,
) -> bool:
    for relative in paths:
        if _deadline_expired(deadline):
            return False
        relative_path = relative.as_posix()
        if relative_path in seen:
            continue
        try:
            absolute = (root / relative).resolve(strict=True)
            absolute.relative_to(root)
        except OSError, ValueError:
            return False
        if _deadline_expired(deadline) or not absolute.is_file():
            return False
        seen.add(relative_path)
        candidates.append((relative_path, absolute))
        if len(candidates) > DART_ANALYSIS_FINGERPRINT_FILE_LIMIT:
            return False
    return not _deadline_expired(deadline)


def _implicit_context_paths(
    root: Path,
    candidates: list[tuple[str, Path]],
    *,
    deadline: float | None = None,
) -> list[Path] | None:
    if _deadline_expired(deadline):
        return None
    package_roots = {
        Path(relative).parent
        for relative, _absolute in candidates
        if Path(relative).name in {"pubspec.yaml", "pubspec.yml"}
    }
    package_roots.add(Path())
    paths = {package_root / ".dart_tool" / "package_config.json" for package_root in package_roots}
    paths.update({Path(".fvmrc"), Path(".fvm") / "fvm_config.json"})
    existing: list[Path] = []
    for path in paths:
        if _deadline_expired(deadline):
            return None
        if (root / path).exists():
            existing.append(path)
        if _deadline_expired(deadline):
            return None
    return sorted(existing, key=lambda path: path.as_posix())


def _semantic_context_paths(root: Path, *, deadline: float | None) -> list[Path] | None:
    """Inventory LSP-visible sources and config independent of review ignores."""

    paths: list[Path] = []
    scanned_entries = 0
    walk_errors: list[OSError] = []
    for current_root, dirnames, filenames in os.walk(root, onerror=walk_errors.append, followlinks=False):
        if walk_errors or _deadline_expired(deadline):
            return None
        current = Path(current_root)
        retained_dirs: list[str] = []
        for dirname in sorted(dirnames):
            scanned_entries += 1
            if scanned_entries > DART_SEMANTIC_INVENTORY_ENTRY_LIMIT or _deadline_expired(deadline):
                return None
            candidate = current / dirname
            if dirname in DART_SEMANTIC_INVENTORY_IGNORED_DIRS:
                continue
            try:
                if candidate.is_symlink():
                    continue
            except OSError:
                return None
            retained_dirs.append(dirname)
        dirnames[:] = retained_dirs
        for filename in sorted(filenames):
            scanned_entries += 1
            if scanned_entries > DART_SEMANTIC_INVENTORY_ENTRY_LIMIT or _deadline_expired(deadline):
                return None
            if not filename.casefold().endswith(".dart") and filename not in _DART_CONTEXT_FILENAMES:
                continue
            relative = (current / filename).relative_to(root)
            paths.append(relative)
            if len(paths) > DART_ANALYSIS_FINGERPRINT_FILE_LIMIT:
                return None
    if walk_errors or _deadline_expired(deadline):
        return None
    return paths


def _analysis_options_include_paths(
    root: Path,
    candidates: list[tuple[str, Path]],
    *,
    deadline: float | None,
) -> list[Path] | None:
    package_maps = _repo_package_include_maps(root, candidates, deadline=deadline)
    if package_maps is None:
        return None
    pending = sorted(
        (
            Path(relative)
            for relative, _absolute in candidates
            if Path(relative).name in {"analysis_options.yaml", "analysis_options.yml"}
        ),
        key=lambda path: path.as_posix(),
        reverse=True,
    )
    visited: set[str] = set()
    included: list[Path] = []
    retained_bytes = 0
    while pending:
        if _deadline_expired(deadline) or len(visited) >= DART_ANALYSIS_OPTIONS_INCLUDE_FILE_LIMIT:
            return None
        relative = pending.pop()
        relative_path = relative.as_posix()
        if relative_path in visited:
            continue
        visited.add(relative_path)
        absolute = _strict_repo_file(root, relative)
        if absolute is None:
            return None
        text = _read_bounded_config_text(
            absolute,
            max_bytes=min(
                DART_CACHE_CONFIG_FILE_BYTE_LIMIT,
                DART_ANALYSIS_OPTIONS_INCLUDE_BYTE_LIMIT - retained_bytes,
            ),
            deadline=deadline,
        )
        if text is None:
            return None
        retained_bytes += len(text.encode("utf-8"))
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError, RecursionError:
            return None
        if not isinstance(payload, dict) or "include" not in payload:
            continue
        raw_include = payload.get("include")
        if not isinstance(raw_include, str) or not raw_include.strip():
            return None
        raw_include = raw_include.strip()
        if raw_include.startswith("package:"):
            unresolved = _resolve_repo_package_include(root, relative, raw_include, package_maps)
            if unresolved is None:
                return None
            if unresolved is False:
                continue
            resolved = unresolved
            try:
                include_relative = resolved.relative_to(root)
            except ValueError:
                return None
            include_key = include_relative.as_posix()
            if include_key not in visited:
                pending.append(include_relative)
                pending.sort(key=lambda path: path.as_posix(), reverse=True)
            included.append(include_relative)
            continue
        parsed = urlsplit(raw_include)
        if parsed.query or parsed.fragment or parsed.scheme not in {"", "file"}:
            return None
        if parsed.scheme == "file" and parsed.netloc not in {"", "localhost"}:
            return None
        decoded_path = unquote(parsed.path)
        include_path = Path(decoded_path)
        unresolved = include_path if include_path.is_absolute() else absolute.parent / include_path
        try:
            resolved = unresolved.resolve(strict=True)
            include_relative = resolved.relative_to(root)
        except OSError, ValueError:
            return None
        if not resolved.is_file():
            return None
        include_key = include_relative.as_posix()
        if include_key not in visited:
            pending.append(include_relative)
            pending.sort(key=lambda path: path.as_posix(), reverse=True)
        included.append(include_relative)
    return sorted(set(included), key=lambda path: path.as_posix())


def _repo_package_include_maps(
    root: Path,
    candidates: list[tuple[str, Path]],
    *,
    deadline: float | None,
) -> list[tuple[Path, dict[str, Path]]] | None:
    maps: list[tuple[Path, dict[str, Path]]] = []
    package_configs = sorted(
        (
            (Path(relative), absolute)
            for relative, absolute in candidates
            if Path(relative).name == "package_config.json" and ".dart_tool" in Path(relative).parts
        ),
        key=lambda item: item[0].as_posix(),
    )
    for relative, absolute in package_configs:
        config_text = _read_bounded_config_text(
            absolute,
            max_bytes=DART_CACHE_CONFIG_FILE_BYTE_LIMIT,
            deadline=deadline,
        )
        if config_text is None:
            return None
        try:
            payload = json.loads(config_text)
        except json.JSONDecodeError, RecursionError:
            return None
        packages = payload.get("packages") if isinstance(payload, dict) else None
        if not isinstance(packages, list):
            return None
        package_map: dict[str, Path] = {}
        for package in packages:
            if not isinstance(package, dict):
                return None
            name = package.get("name")
            raw_root_uri = package.get("rootUri")
            raw_package_uri = package.get("packageUri", "")
            if not isinstance(name, str) or not isinstance(raw_root_uri, str) or not isinstance(raw_package_uri, str):
                return None
            package_root = _package_root_from_uri(absolute.parent, raw_root_uri)
            package_uri = urlsplit(raw_package_uri)
            if (
                package_root is None
                or package_uri.scheme
                or package_uri.netloc
                or package_uri.query
                or package_uri.fragment
            ):
                return None
            package_subpath = Path(unquote(package_uri.path))
            if package_subpath.is_absolute() or ".." in package_subpath.parts:
                return None
            try:
                package_map[name] = (package_root / package_subpath).resolve(strict=False)
            except OSError:
                return None
        maps.append((relative.parent.parent, package_map))
    return maps


def _resolve_repo_package_include(
    root: Path,
    including_path: Path,
    raw_include: str,
    package_maps: list[tuple[Path, dict[str, Path]]],
) -> Path | Literal[False] | None:
    package_specifier = raw_include.removeprefix("package:")
    package_name, separator, raw_package_path = package_specifier.partition("/")
    if not separator or not package_name or not raw_package_path:
        return None
    containing_maps: list[tuple[int, dict[str, Path]]] = []
    for package_root, package_map in package_maps:
        try:
            including_path.relative_to(package_root)
        except ValueError:
            continue
        containing_maps.append((len(package_root.parts), package_map))
    if not containing_maps:
        return None
    package_map = max(containing_maps, key=lambda item: item[0])[1]
    package_base = package_map.get(package_name)
    if package_base is None:
        return None
    include_subpath = Path(unquote(raw_package_path))
    if include_subpath.is_absolute() or ".." in include_subpath.parts:
        return None
    try:
        unresolved = (package_base / include_subpath).resolve(strict=True)
    except OSError:
        return None
    try:
        unresolved.relative_to(root)
    except ValueError:
        # Hosted/Git/SDK package contents are anchored by package_config,
        # pubspec.lock, and the resolved SDK version. External path packages
        # are rejected separately by `_has_external_path_dependency`.
        return False
    return unresolved if unresolved.is_file() else None


def _has_external_path_dependency(
    root: Path,
    candidates: list[tuple[str, Path]],
    *,
    deadline: float | None,
) -> bool | None:
    by_relative = {relative: absolute for relative, absolute in candidates}
    package_configs = sorted(
        (
            (Path(relative), absolute)
            for relative, absolute in candidates
            if Path(relative).name == "package_config.json" and ".dart_tool" in Path(relative).parts
        ),
        key=lambda item: item[0].as_posix(),
    )
    for relative, absolute in package_configs:
        if _deadline_expired(deadline):
            return None
        package_root = relative.parent.parent
        lock_path = package_root / "pubspec.lock"
        locked_path_packages: set[str] | None = None
        lock_absolute = by_relative.get(lock_path.as_posix())
        if lock_absolute is not None:
            lock_text = _read_bounded_config_text(
                lock_absolute,
                max_bytes=DART_CACHE_CONFIG_FILE_BYTE_LIMIT,
                deadline=deadline,
            )
            if lock_text is None:
                return None
            try:
                lock_payload = yaml.safe_load(lock_text)
            except yaml.YAMLError, RecursionError:
                return None
            if not isinstance(lock_payload, dict) or not isinstance(lock_payload.get("packages"), dict):
                return None
            locked_path_packages = {
                name
                for name, details in lock_payload["packages"].items()
                if isinstance(name, str) and isinstance(details, dict) and details.get("source") == "path"
            }
        config_text = _read_bounded_config_text(
            absolute,
            max_bytes=DART_CACHE_CONFIG_FILE_BYTE_LIMIT,
            deadline=deadline,
        )
        if config_text is None:
            return None
        try:
            config_payload = json.loads(config_text)
        except json.JSONDecodeError, RecursionError:
            return None
        packages = config_payload.get("packages") if isinstance(config_payload, dict) else None
        if not isinstance(packages, list):
            return None
        for package in packages:
            if not isinstance(package, dict):
                return None
            name = package.get("name")
            raw_root_uri = package.get("rootUri")
            if not isinstance(name, str) or not isinstance(raw_root_uri, str):
                return None
            package_path = _package_root_from_uri(absolute.parent, raw_root_uri)
            if package_path is None:
                return None
            try:
                package_path.relative_to(root)
            except ValueError:
                if locked_path_packages is None or name in locked_path_packages:
                    return True
    return False


def _package_root_from_uri(config_directory: Path, raw_uri: str) -> Path | None:
    parsed = urlsplit(raw_uri)
    if parsed.query or parsed.fragment or parsed.scheme not in {"", "file"}:
        return None
    if parsed.scheme == "file" and parsed.netloc not in {"", "localhost"}:
        return None
    decoded_path = unquote(parsed.path)
    path = Path(decoded_path)
    unresolved = path if path.is_absolute() else config_directory / path
    try:
        return unresolved.resolve(strict=False)
    except OSError:
        return None


def _strict_repo_file(root: Path, relative: Path) -> Path | None:
    try:
        absolute = (root / relative).resolve(strict=True)
        absolute.relative_to(root)
    except OSError, ValueError:
        return None
    return absolute if absolute.is_file() else None


def _read_bounded_config_text(
    path: Path,
    *,
    max_bytes: int,
    deadline: float | None,
) -> str | None:
    if max_bytes <= 0 or _deadline_expired(deadline):
        return None
    try:
        if path.stat().st_size > max_bytes:
            return None
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes or _deadline_expired(deadline):
            return None
        return payload.decode("utf-8")
    except OSError, UnicodeError:
        return None


def _command_identity(
    repo_root: Path,
    command: list[str],
    *,
    deadline: float | None = None,
) -> dict[str, object] | None:
    if _deadline_expired(deadline):
        return None
    if not command:
        return {"resolved": None}
    root = repo_root.resolve()
    executable = Path(command[0]).expanduser()
    if not executable.is_absolute():
        if executable.parent != Path("."):
            executable = root / executable
        else:
            resolved_from_path = shutil.which(command[0])
            if resolved_from_path is None:
                return {"resolved": None}
            executable = Path(resolved_from_path)
    if _deadline_expired(deadline):
        return None
    executable_identity = _command_file_identity(executable, deadline=deadline)
    if executable_identity is None:
        if _deadline_expired(deadline):
            return None
        return {"resolved": None}

    argument_files: list[dict[str, object]] = []
    for index, argument in enumerate(command[1:], start=1):
        if _deadline_expired(deadline):
            return None
        candidate = Path(argument).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        argument_identity = _command_file_identity(candidate, deadline=deadline)
        if argument_identity is not None:
            argument_files.append({"argv_index": index, **argument_identity})
        elif _deadline_expired(deadline):
            return None
    return {
        **executable_identity,
        "argument_files": argument_files,
    }


def _command_file_identity(path: Path, *, deadline: float | None) -> dict[str, object] | None:
    try:
        resolved = path.resolve(strict=True)
        file_stat = resolved.stat()
    except OSError:
        return None
    if not resolved.is_file() or _deadline_expired(deadline):
        return None
    identity: dict[str, object] = {
        "resolved": str(resolved),
        "size": file_stat.st_size,
        "mtime_ns": file_stat.st_mtime_ns,
    }
    if file_stat.st_size <= 1024 * 1024:
        try:
            digest = hashlib.sha256()
            with resolved.open("rb") as handle:
                while True:
                    if _deadline_expired(deadline):
                        return None
                    chunk = handle.read(128 * 1024)
                    if _deadline_expired(deadline):
                        return None
                    if not chunk:
                        break
                    digest.update(chunk)
            identity["sha256"] = digest.hexdigest()
        except OSError:
            pass
    return None if _deadline_expired(deadline) else identity


def _dart_config_payload(config: AnalyzerConfig) -> dict[str, Any]:
    dart_config = getattr(config, "dart", None)
    return {
        "timeout_seconds": config.timeout_seconds,
        "dart": dart_config.model_dump(mode="json") if dart_config is not None else None,
    }


def _hash_json(digest: Any, value: object) -> None:
    digest.update(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\0")


def _deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _require_deadline(deadline: float | None) -> None:
    if _deadline_expired(deadline):
        raise _CacheDeadlineExpired


def _default_cache_home() -> Path:
    explicit = os.environ.get("APEX_RAY_CACHE_HOME")
    if explicit and explicit.strip():
        return Path(explicit).expanduser().resolve()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg and xdg.strip():
        return Path(xdg).expanduser().resolve() / "apex-ray"
    try:
        return Path.home() / ".cache" / "apex-ray"
    except RuntimeError:
        return Path(tempfile.gettempdir()) / "apex-ray-cache"
