import json
import os
import stat
import subprocess
import time
from pathlib import Path

import yaml

from apex_ray import git
from apex_ray.config import find_config
from apex_ray.models import ProjectProfile
from apex_ray.path_matching import path_matches_any

LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".cs": "csharp",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
    ".sql": "sql",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".dart": "dart",
}
DISCOVERY_IGNORED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".pnpm-store",
    ".worktrees",
    ".next",
    ".turbo",
    ".sim-data",
    ".dart_tool",
    "dist",
    "build",
    "out",
    "coverage",
    "sim-results",
}
VITE_CONFIG_NAMES = {
    "vite.config.js",
    "vite.config.mjs",
    "vite.config.cjs",
    "vite.config.ts",
    "vite.config.mts",
    "vite.config.cts",
}
GIT_INVENTORY_BYTE_LIMIT = 64 * 1024 * 1024
GIT_INVENTORY_ENTRY_LIMIT = 250_000
DEFAULT_GIT_ROOT_TIMEOUT_SECONDS = 30.0
PACKAGE_JSON_BYTE_LIMIT = 4 * 1024 * 1024
PUBSPEC_BYTE_LIMIT = 4 * 1024 * 1024
PUBSPEC_NAMES = {"pubspec.yaml", "pubspec.yml"}


class DiscoveryError(RuntimeError):
    pass


class DiscoveryTimeoutError(DiscoveryError, TimeoutError):
    pass


def discover_repo_root(
    cwd: Path,
    *,
    timeout_seconds: float | None = DEFAULT_GIT_ROOT_TIMEOUT_SECONDS,
) -> Path:
    try:
        root = git.repo_root(
            cwd,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise DiscoveryTimeoutError(
            "Project file discovery timed out while locating the Git repository root. "
            "Increase review.analyzer.timeout_seconds where configurable, or reduce filesystem "
            "or Git wrapper latency before retrying."
        ) from exc
    return root or cwd.resolve()


def discover_project(
    cwd: Path,
    ignored_patterns: list[str] | None = None,
    config_path: Path | None = None,
    timeout_seconds: float | None = None,
) -> ProjectProfile:
    profile, _files = discover_project_with_files(
        cwd,
        ignored_patterns=ignored_patterns,
        config_path=config_path,
        timeout_seconds=timeout_seconds,
    )
    return profile


def discover_project_with_files(
    cwd: Path,
    ignored_patterns: list[str] | None = None,
    config_path: Path | None = None,
    timeout_seconds: float | None = None,
) -> tuple[ProjectProfile, list[Path]]:
    """Discover a project and its files within an optional shared deadline.

    ``timeout_seconds=None`` leaves the overall discovery workflow unbounded.
    The external Git root lookup still uses its independent safety timeout so a
    stalled Git wrapper cannot hang otherwise-unbounded discovery indefinitely.
    """
    deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)
    root = (
        discover_repo_root(
            cwd,
            timeout_seconds=DEFAULT_GIT_ROOT_TIMEOUT_SECONDS,
        )
        if deadline is None
        else discover_repo_root(
            cwd,
            timeout_seconds=_remaining_discovery_seconds(deadline),
        )
    )
    _check_discovery_deadline(deadline)
    project_config_path = config_path or find_config(root)
    _check_discovery_deadline(deadline)
    ignored_patterns = ignored_patterns or []
    try:
        is_git_repo = git.is_git_repo(
            root,
            timeout=_remaining_discovery_seconds(deadline),
        )
    except subprocess.TimeoutExpired as exc:
        raise DiscoveryTimeoutError(
            "Project file discovery timed out while checking the Git repository. "
            "Increase review.analyzer.timeout_seconds or reduce the repository inventory before retrying."
        ) from exc
    files = list_project_files(
        root,
        ignored_patterns,
        is_git_repo=is_git_repo,
        timeout_seconds=_remaining_discovery_seconds(deadline),
    )
    detected_languages = sorted(_detect_languages(files, deadline))
    package_managers = sorted(_detect_package_managers(root, deadline, files=files))
    framework_hints = sorted(_detect_frameworks(root, deadline, files=files))

    return (
        ProjectProfile(
            root=str(root),
            is_git_repo=is_git_repo,
            config_path=str(project_config_path) if project_config_path else None,
            detected_languages=detected_languages,
            package_managers=package_managers,
            framework_hints=framework_hints,
            ignored_patterns=ignored_patterns,
        ),
        files,
    )


def list_project_files(
    root: Path,
    ignored_patterns: list[str] | None = None,
    *,
    is_git_repo: bool | None = None,
    timeout_seconds: float | None = None,
) -> list[Path]:
    deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)
    root = root.resolve()
    _check_discovery_deadline(deadline)
    ignored_patterns = ignored_patterns or []
    if is_git_repo is None:
        try:
            is_git_repo = (root / ".git").exists() and git.is_git_repo(
                root,
                timeout=_remaining_discovery_seconds(deadline),
            )
        except subprocess.TimeoutExpired as exc:
            raise DiscoveryTimeoutError(
                "Project file discovery timed out while checking the Git repository. "
                "Increase review.analyzer.timeout_seconds or reduce the repository inventory before retrying."
            ) from exc
    return (
        _list_git_project_files(root, ignored_patterns, deadline)
        if is_git_repo
        else _list_project_files(root, ignored_patterns, deadline)
    )


def _list_project_files(
    root: Path,
    ignored_patterns: list[str],
    deadline: float | None = None,
) -> list[Path]:
    files: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root):
        _check_discovery_deadline(deadline)
        current_path = Path(current_root)
        retained_directories: list[str] = []
        for dirname in dirnames:
            _check_discovery_deadline(deadline)
            if dirname in DISCOVERY_IGNORED_DIRS or _matches_ignored_directory(
                _relative_posix(current_path / dirname, root),
                ignored_patterns,
            ):
                continue
            retained_directories.append(dirname)
        dirnames[:] = retained_directories
        for filename in filenames:
            _check_discovery_deadline(deadline)
            rel = (current_path / filename).relative_to(root)
            if _matches_ignored_patterns(rel.as_posix(), ignored_patterns):
                continue
            files.append(rel)
    return files


def _list_git_project_files(
    root: Path,
    ignored_patterns: list[str],
    deadline: float | None = None,
) -> list[Path]:
    files: list[Path] = []
    try:
        output = git.read_git_nul_output(
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            timeout=_remaining_discovery_seconds(deadline),
            max_output_bytes=GIT_INVENTORY_BYTE_LIMIT,
            max_entries=GIT_INVENTORY_ENTRY_LIMIT,
        )
    except subprocess.TimeoutExpired as exc:
        raise DiscoveryTimeoutError(
            "Project file discovery timed out while reading the Git inventory. "
            "Increase review.analyzer.timeout_seconds or reduce the repository inventory before retrying."
        ) from exc
    except git.GitOutputLimitError as exc:
        raise DiscoveryError(
            "Project file discovery failed: Git inventory exceeded its safety limit "
            f"({GIT_INVENTORY_BYTE_LIMIT} bytes or {GIT_INVENTORY_ENTRY_LIMIT} entries). "
            "Remove generated or untracked files from the repository before retrying."
        ) from exc
    _check_discovery_deadline(deadline)
    if output.returncode != 0:
        raise DiscoveryError(
            "Project file discovery failed: "
            f"Git inventory command failed with exit code {output.returncode}. "
            "Check the repository and worktree state before retrying."
        )
    for rel_path in output.entries:
        _check_discovery_deadline(deadline)
        if _should_ignore_path(rel_path, ignored_patterns):
            continue
        files.append(Path(rel_path))
    return files


def _remaining_discovery_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DiscoveryTimeoutError(
            "Project file discovery timed out. Increase review.analyzer.timeout_seconds "
            "or reduce the repository inventory before retrying."
        )
    return remaining


def _check_discovery_deadline(deadline: float | None) -> None:
    _remaining_discovery_seconds(deadline)


def _should_ignore_path(rel_path: str, ignored_patterns: list[str]) -> bool:
    normalized = rel_path.replace("\\", "/")
    if any(part in DISCOVERY_IGNORED_DIRS for part in Path(normalized).parts):
        return True
    return _matches_ignored_patterns(normalized, ignored_patterns)


def _matches_ignored_patterns(rel_path: str, ignored_patterns: list[str]) -> bool:
    return path_matches_any(rel_path, ignored_patterns)


def _matches_ignored_directory(rel_path: str, ignored_patterns: list[str]) -> bool:
    return _matches_ignored_patterns(rel_path, ignored_patterns) or _matches_ignored_patterns(
        f"{rel_path.rstrip('/')}/",
        ignored_patterns,
    )


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _detect_languages(files: list[Path], deadline: float | None = None) -> set[str]:
    languages: set[str] = set()
    for path in files:
        _check_discovery_deadline(deadline)
        language = LANGUAGE_EXTENSIONS.get(path.suffix.lower())
        if language:
            languages.add(language)
    return languages


def _detect_package_managers(
    root: Path,
    deadline: float | None = None,
    *,
    files: list[Path] | None = None,
) -> set[str]:
    managers: set[str] = set()
    markers = {
        "pyproject.toml": "python",
        "uv.lock": "uv",
        "poetry.lock": "poetry",
        "package.json": "npm",
        "pnpm-lock.yaml": "pnpm",
        "yarn.lock": "yarn",
        "go.mod": "go",
        "Cargo.toml": "cargo",
    }
    for filename, manager in markers.items():
        _check_discovery_deadline(deadline)
        if (root / filename).exists():
            managers.add(manager)
    if _pubspec_paths(root, files):
        managers.add("pub")
    return managers


def _detect_frameworks(
    root: Path,
    deadline: float | None = None,
    *,
    files: list[Path] | None = None,
) -> set[str]:
    frameworks: set[str] = set()
    filenames: set[str] = set()
    _check_discovery_deadline(deadline)
    if root.exists():
        for path in root.iterdir():
            _check_discovery_deadline(deadline)
            filenames.add(path.name)
    if "next.config.js" in filenames or "next.config.mjs" in filenames or "next.config.ts" in filenames:
        frameworks.add("nextjs")
    if filenames & VITE_CONFIG_NAMES:
        frameworks.add("vite")
    if "manage.py" in filenames:
        frameworks.add("django")

    package_json = root / "package.json"
    _check_discovery_deadline(deadline)
    package_json_text = _read_bounded_regular_text(
        package_json,
        deadline=deadline,
        max_bytes=PACKAGE_JSON_BYTE_LIMIT,
    )
    if package_json_text is not None:
        try:
            parsed = json.loads(package_json_text)
            data = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            data = {}
        except RecursionError:
            data = {}
        dependencies = data.get("dependencies", {})
        dev_dependencies = data.get("devDependencies", {})
        deps = {
            **(dependencies if isinstance(dependencies, dict) else {}),
            **(dev_dependencies if isinstance(dev_dependencies, dict) else {}),
        }
        for dep, framework in {
            "react": "react",
            "vue": "vue",
            "svelte": "svelte",
            "express": "express",
            "nestjs": "nestjs",
            "next": "nextjs",
        }.items():
            if dep in deps:
                frameworks.add(framework)
    for pubspec_path in _pubspec_paths(root, files):
        pubspec_text = _read_bounded_regular_text(
            pubspec_path,
            deadline=deadline,
            max_bytes=PUBSPEC_BYTE_LIMIT,
        )
        if pubspec_text is None:
            continue
        try:
            parsed = yaml.safe_load(pubspec_text)
        except yaml.YAMLError, RecursionError:
            continue
        if _manifest_declares_flutter_sdk(parsed):
            frameworks.add("flutter")
            break
    _check_discovery_deadline(deadline)
    return frameworks


def _pubspec_paths(root: Path, files: list[Path] | None) -> list[Path]:
    candidates = [Path(name) for name in sorted(PUBSPEC_NAMES) if (root / name).exists()]
    if files is not None:
        candidates.extend(
            path
            for path in files
            if not path.is_absolute() and ".." not in path.parts and path.name.lower() in PUBSPEC_NAMES
        )
    return [root / path for path in sorted(set(candidates), key=lambda item: (len(item.parts), item.as_posix()))]


def _manifest_declares_flutter_sdk(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    for group_name in ("dependencies", "dev_dependencies"):
        dependencies = raw.get(group_name)
        if not isinstance(dependencies, dict):
            continue
        flutter = dependencies.get("flutter")
        if isinstance(flutter, dict) and flutter.get("sdk") == "flutter":
            return True
    return False


def _read_bounded_regular_text(
    path: Path,
    *,
    deadline: float | None,
    max_bytes: int,
) -> str | None:
    _check_discovery_deadline(deadline)
    try:
        path_metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(path_metadata.st_mode) or path_metadata.st_size > max_bytes:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > max_bytes
            or (metadata.st_dev, metadata.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            return None
        payload = bytearray()
        while len(payload) <= max_bytes:
            _check_discovery_deadline(deadline)
            chunk = os.read(
                descriptor,
                min(64 * 1024, max_bytes + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            return None
        _check_discovery_deadline(deadline)
        return payload.decode("utf-8")
    except OSError:
        return None
    except UnicodeDecodeError:
        return None
    finally:
        os.close(descriptor)
