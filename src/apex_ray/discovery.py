from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

from apex_ray import git
from apex_ray.config import find_config
from apex_ray.models import ProjectProfile

LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
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
    "dist",
    "build",
    "out",
    "coverage",
    "sim-results",
}


def discover_project(
    cwd: Path,
    ignored_patterns: list[str] | None = None,
    config_path: Path | None = None,
) -> ProjectProfile:
    root = git.repo_root(cwd) or cwd.resolve()
    project_config_path = config_path or find_config(root)
    ignored_patterns = ignored_patterns or []
    is_git_repo = git.is_git_repo(root)
    files = (
        _list_git_project_files(root, ignored_patterns) if is_git_repo else _list_project_files(root, ignored_patterns)
    )

    return ProjectProfile(
        root=str(root),
        is_git_repo=is_git_repo,
        config_path=str(project_config_path) if project_config_path else None,
        detected_languages=sorted(_detect_languages(files)),
        package_managers=sorted(_detect_package_managers(root)),
        framework_hints=sorted(_detect_frameworks(root)),
        ignored_patterns=ignored_patterns,
    )


def _list_project_files(root: Path, ignored_patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root):
        current_path = Path(current_root)
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in DISCOVERY_IGNORED_DIRS
            and not _matches_ignored_patterns(_relative_posix(current_path / dirname, root), ignored_patterns)
        ]
        for filename in filenames:
            rel = (current_path / filename).relative_to(root)
            if _matches_ignored_patterns(rel.as_posix(), ignored_patterns):
                continue
            files.append(rel)
    return files


def _list_git_project_files(root: Path, ignored_patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for rel_path in [*git.tracked_files(root), *git.untracked_files(root)]:
        if _should_ignore_path(rel_path, ignored_patterns):
            continue
        files.append(Path(rel_path))
    return files


def _should_ignore_path(rel_path: str, ignored_patterns: list[str]) -> bool:
    normalized = rel_path.replace("\\", "/")
    if any(part in DISCOVERY_IGNORED_DIRS for part in Path(normalized).parts):
        return True
    return _matches_ignored_patterns(normalized, ignored_patterns)


def _matches_ignored_patterns(rel_path: str, ignored_patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in ignored_patterns)


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _detect_languages(files: list[Path]) -> set[str]:
    languages: set[str] = set()
    for path in files:
        language = LANGUAGE_EXTENSIONS.get(path.suffix.lower())
        if language:
            languages.add(language)
    return languages


def _detect_package_managers(root: Path) -> set[str]:
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
        if (root / filename).exists():
            managers.add(manager)
    return managers


def _detect_frameworks(root: Path) -> set[str]:
    frameworks: set[str] = set()
    filenames = {path.name for path in root.iterdir()} if root.exists() else set()
    if "next.config.js" in filenames or "next.config.mjs" in filenames or "next.config.ts" in filenames:
        frameworks.add("nextjs")
    if "vite.config.js" in filenames or "vite.config.ts" in filenames:
        frameworks.add("vite")
    if "manage.py" in filenames:
        frameworks.add("django")

    package_json = root / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
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
    return frameworks
