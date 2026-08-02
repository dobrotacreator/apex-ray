from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .generated import is_generated_dart_path

DART_WORKSPACE_PACKAGE_LIMIT = 512
DART_PUBSPEC_BYTE_LIMIT = 1024 * 1024
DART_WORKSPACE_MANIFEST_TOTAL_BYTE_LIMIT = 8 * 1024 * 1024
DART_ANCHOR_SOURCE_BYTE_LIMIT = 512 * 1024


@dataclass(frozen=True, slots=True)
class _Package:
    root: Path
    relative_root: Path
    name: str
    dependency_paths: tuple[str, ...]
    dependency_names: tuple[str, ...]
    workspace_members: tuple[str, ...]
    uses_workspace_resolution: bool
    workspace_root: Path | None = None


def reverse_dependency_anchors(
    repo_root: Path,
    changed_paths: list[str],
    project_files: list[Path] | None,
    *,
    limit: int,
    deadline: float | None = None,
    max_manifest_bytes: int = DART_WORKSPACE_MANIFEST_TOTAL_BYTE_LIMIT,
) -> list[Path]:
    """Select minimal open-document anchors for local reverse dependencies.

    Dart Analysis Server can intentionally restrict analysis to contexts that
    contain an open file. Opening one stable handwritten library file in each
    reverse-dependent local package makes cross-package callers visible without
    transmitting every project file over LSP.
    """

    if limit <= 0 or max_manifest_bytes <= 0 or not project_files or _deadline_expired(deadline):
        return []
    root = repo_root.resolve()
    inventory = _relative_inventory(root, project_files, deadline=deadline)
    packages = _packages(root, inventory, deadline=deadline, max_manifest_bytes=max_manifest_bytes)
    if not packages:
        return []

    by_root = {package.root: package for package in packages}
    workspace_packages: dict[Path, dict[str, list[Path]]] = {}
    for package in packages:
        if package.workspace_root is None:
            continue
        names = workspace_packages.setdefault(package.workspace_root, {})
        names.setdefault(package.name, []).append(package.root)
    reverse_dependencies: dict[Path, set[Path]] = {}
    for package in packages:
        if _deadline_expired(deadline):
            return []
        for raw_dependency in package.dependency_paths:
            dependency_root = _resolved_local_dependency(root, package.root, raw_dependency)
            if dependency_root not in by_root:
                continue
            reverse_dependencies.setdefault(dependency_root, set()).add(package.root)
        if package.workspace_root is None:
            continue
        local_names = workspace_packages[package.workspace_root]
        for dependency_name in package.dependency_names:
            dependency_roots = local_names.get(dependency_name, ())
            if len(dependency_roots) != 1:
                continue
            dependency_root = dependency_roots[0]
            if dependency_root != package.root:
                reverse_dependencies.setdefault(dependency_root, set()).add(package.root)

    changed_package_roots = {
        package.root
        for raw_path in changed_paths
        if (package := _containing_package(root, raw_path, packages)) is not None
    }
    if not changed_package_roots:
        return []

    queue = deque(sorted(changed_package_roots, key=lambda path: path.as_posix()))
    visited = set(changed_package_roots)
    dependents: set[Path] = set()
    while queue:
        if _deadline_expired(deadline):
            return []
        dependency = queue.popleft()
        for consumer in sorted(reverse_dependencies.get(dependency, ()), key=lambda path: path.as_posix()):
            if consumer in visited:
                continue
            visited.add(consumer)
            dependents.add(consumer)
            queue.append(consumer)

    changed = set(changed_paths)
    anchors: list[Path] = []
    for package_root in sorted(dependents, key=lambda path: by_root[path].relative_root.as_posix()):
        if _deadline_expired(deadline):
            return []
        anchor = _package_anchor(root, by_root[package_root], inventory, changed, deadline=deadline)
        if anchor is None:
            continue
        anchors.append(anchor)
        if len(anchors) >= limit:
            break
    return anchors


def _relative_inventory(root: Path, project_files: list[Path], *, deadline: float | None) -> list[Path]:
    inventory: set[Path] = set()
    for entry in project_files:
        if _deadline_expired(deadline):
            return []
        try:
            relative = entry if not entry.is_absolute() else entry.resolve().relative_to(root)
        except OSError, ValueError:
            continue
        if relative.is_absolute() or ".." in relative.parts:
            continue
        inventory.add(relative)
    return sorted(inventory, key=lambda path: path.as_posix())


def _packages(
    root: Path,
    inventory: list[Path],
    *,
    deadline: float | None,
    max_manifest_bytes: int,
) -> list[_Package]:
    packages: list[_Package] = []
    total_bytes = 0
    manifests = [path for path in inventory if path.name in {"pubspec.yaml", "pubspec.yml"}]
    for relative in manifests[:DART_WORKSPACE_PACKAGE_LIMIT]:
        if _deadline_expired(deadline):
            return []
        manifest = _bounded_regular_file(root, relative, DART_PUBSPEC_BYTE_LIMIT)
        if manifest is None:
            continue
        try:
            size = manifest.stat().st_size
            if total_bytes + size > max_manifest_bytes:
                break
            total_bytes += size
            payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except OSError, UnicodeError, yaml.YAMLError, RecursionError:
            continue
        if _deadline_expired(deadline):
            return []
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        package_root = manifest.parent.resolve()
        dependencies = tuple(_dependency_paths(payload))
        packages.append(
            _Package(
                root=package_root,
                relative_root=relative.parent,
                name=name.strip(),
                dependency_paths=dependencies,
                dependency_names=tuple(_dependency_names(payload)),
                workspace_members=tuple(_workspace_members(payload)),
                uses_workspace_resolution=payload.get("resolution") == "workspace",
            )
        )
    packages = _assign_workspace_roots(packages, deadline=deadline)
    return sorted(packages, key=lambda package: package.relative_root.as_posix())


def _dependency_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for section_name in ("dependencies", "dev_dependencies", "dependency_overrides"):
        section = payload.get(section_name)
        if not isinstance(section, dict):
            continue
        for specification in section.values():
            if not isinstance(specification, dict):
                continue
            raw_path = specification.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                paths.append(raw_path.strip())
    return paths


def _dependency_names(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for section_name in ("dependencies", "dev_dependencies", "dependency_overrides"):
        section = payload.get(section_name)
        if not isinstance(section, dict):
            continue
        names.extend(name.strip() for name in section if isinstance(name, str) and name.strip())
    return names


def _workspace_members(payload: dict[str, Any]) -> list[str]:
    members = payload.get("workspace")
    if not isinstance(members, list):
        return []
    return [member.strip() for member in members if isinstance(member, str) and member.strip()]


def _assign_workspace_roots(packages: list[_Package], *, deadline: float | None) -> list[_Package]:
    """Associate packages in each valid (possibly nested) Pub workspace."""

    declarations: dict[Path, set[Path]] = {}
    declared_members: set[Path] = set()
    for owner in packages:
        if _deadline_expired(deadline):
            return packages
        members = {
            candidate.root
            for candidate in packages
            if candidate.root != owner.root
            and candidate.uses_workspace_resolution
            and _matches_workspace_member(owner, candidate)
        }
        if not members:
            continue
        declarations[owner.root] = members
        declared_members.update(members)

    top_level_roots = sorted(
        (workspace_root for workspace_root in declarations if workspace_root not in declared_members),
        key=lambda path: path.as_posix(),
    )
    memberships: dict[Path, set[Path]] = {}
    for workspace_root in top_level_roots:
        queue = deque([workspace_root])
        visited: set[Path] = set()
        while queue:
            if _deadline_expired(deadline):
                return packages
            package_root = queue.popleft()
            if package_root in visited:
                continue
            visited.add(package_root)
            memberships.setdefault(package_root, set()).add(workspace_root)
            queue.extend(sorted(declarations.get(package_root, ()), key=lambda path: path.as_posix()))

    assigned: list[_Package] = []
    for package in packages:
        owners = memberships.get(package.root, set())
        assigned.append(replace(package, workspace_root=next(iter(owners))) if len(owners) == 1 else package)
    return assigned


def _matches_workspace_member(owner: _Package, candidate: _Package) -> bool:
    try:
        relative = candidate.relative_root.relative_to(owner.relative_root)
    except ValueError:
        return False
    if not relative.parts:
        return False
    relative_posix = relative.as_posix()
    for raw_member in owner.workspace_members:
        member = PurePosixPath(raw_member.replace("\\", "/"))
        if member.is_absolute() or ".." in member.parts or not member.parts:
            continue
        pattern = member.as_posix().rstrip("/")
        if relative_posix == pattern or PurePosixPath(relative_posix).match(pattern):
            return True
    return False


def _resolved_local_dependency(root: Path, package_root: Path, raw_path: str) -> Path | None:
    try:
        candidate = (package_root / raw_path).resolve(strict=True)
        candidate.relative_to(root)
    except OSError, ValueError:
        return None
    return candidate if candidate.is_dir() else None


def _containing_package(root: Path, raw_path: str, packages: list[_Package]) -> _Package | None:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except OSError, ValueError:
        return None
    matches = [package for package in packages if resolved.is_relative_to(package.root)]
    return max(matches, key=lambda package: len(package.root.parts), default=None)


def _package_anchor(
    root: Path,
    package: _Package,
    inventory: list[Path],
    changed_paths: set[str],
    *,
    deadline: float | None,
) -> Path | None:
    candidates: list[Path] = []
    for relative in inventory:
        if _deadline_expired(deadline):
            return None
        if relative.suffix.casefold() != ".dart" or relative.as_posix() in changed_paths:
            continue
        try:
            within_package = relative.relative_to(package.relative_root)
        except ValueError:
            continue
        if not within_package.parts or within_package.parts[0] != "lib":
            continue
        if is_generated_dart_path(relative):
            continue
        if _bounded_regular_file(root, relative, DART_ANCHOR_SOURCE_BYTE_LIMIT) is None:
            continue
        candidates.append(relative)
    if not candidates:
        return None
    preferred = {
        Path("lib") / f"{package.name}.dart": 0,
        Path("lib/main.dart"): 1,
    }
    return min(
        candidates,
        key=lambda path: (
            preferred.get(path.relative_to(package.relative_root), 2),
            len(path.parts),
            path.as_posix(),
        ),
    )


def _deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _bounded_regular_file(root: Path, relative: Path, max_bytes: int) -> Path | None:
    candidate = root / relative
    try:
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        stat = resolved.stat()
    except OSError, ValueError:
        return None
    if not resolved.is_file() or stat.st_size > max_bytes:
        return None
    return resolved


__all__ = ["reverse_dependency_anchors"]
