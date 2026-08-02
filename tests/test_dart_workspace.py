import time
from pathlib import Path

import pytest

from apex_ray.analyzers.dart import workspace as dart_workspace_module
from apex_ray.analyzers.dart.workspace import reverse_dependency_anchors


def _write_package(root: Path, relative: str, name: str, dependencies: dict[str, str]) -> None:
    package = root / relative
    (package / "lib").mkdir(parents=True)
    dependency_lines = "".join(f"  {dependency}:\n    path: {path}\n" for dependency, path in dependencies.items())
    (package / "pubspec.yaml").write_text(
        f"name: {name}\nenvironment:\n  sdk: '>=3.4.0 <4.0.0'\ndependencies:\n{dependency_lines}",
        encoding="utf-8",
    )
    (package / "lib" / f"{name}.dart").write_text(f"library {name};\n", encoding="utf-8")


def _write_workspace_package(
    root: Path,
    relative: str,
    name: str,
    dependencies: dict[str, str],
) -> None:
    package = root / relative
    (package / "lib").mkdir(parents=True)
    dependency_lines = "".join(f"  {dependency}: {constraint}\n" for dependency, constraint in dependencies.items())
    (package / "pubspec.yaml").write_text(
        (
            f"name: {name}\n"
            "resolution: workspace\n"
            "environment:\n"
            "  sdk: '>=3.6.0 <4.0.0'\n"
            f"dependencies:\n{dependency_lines}"
        ),
        encoding="utf-8",
    )
    (package / "lib" / f"{name}.dart").write_text(f"library {name};\n", encoding="utf-8")


def test_reverse_dependency_anchors_cover_local_consumers_and_cycles(tmp_path: Path) -> None:
    _write_package(tmp_path, "packages/core", "core", {"feature": "../feature"})
    _write_package(tmp_path, "packages/feature", "feature", {"core": "../core"})
    _write_package(tmp_path, "apps/mobile", "mobile", {"core": "../../packages/core"})
    project_files = [
        Path("packages/core/pubspec.yaml"),
        Path("packages/core/lib/core.dart"),
        Path("packages/feature/pubspec.yaml"),
        Path("packages/feature/lib/feature.dart"),
        Path("apps/mobile/pubspec.yaml"),
        Path("apps/mobile/lib/mobile.dart"),
    ]

    anchors = reverse_dependency_anchors(
        tmp_path,
        ["packages/core/lib/core.dart"],
        project_files,
        limit=8,
    )

    assert anchors == [
        Path("apps/mobile/lib/mobile.dart"),
        Path("packages/feature/lib/feature.dart"),
    ]


def test_reverse_dependency_anchors_cover_pub_workspace_version_dependencies(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text(
        (
            "name: workspace_root\n"
            "environment:\n"
            "  sdk: '>=3.6.0 <4.0.0'\n"
            "workspace:\n"
            "  - packages/core\n"
            "  - apps/mobile\n"
        ),
        encoding="utf-8",
    )
    _write_workspace_package(tmp_path, "packages/core", "core", {})
    _write_workspace_package(tmp_path, "apps/mobile", "mobile", {"core": "^1.0.0"})
    project_files = [path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()]

    anchors = reverse_dependency_anchors(
        tmp_path,
        ["packages/core/lib/core.dart"],
        project_files,
        limit=8,
    )

    assert anchors == [Path("apps/mobile/lib/mobile.dart")]


def test_reverse_dependency_anchors_ignore_declared_members_without_workspace_resolution(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text(
        (
            "name: workspace_root\n"
            "environment:\n"
            "  sdk: '>=3.6.0 <4.0.0'\n"
            "workspace:\n"
            "  - packages/core\n"
            "  - apps/mobile\n"
        ),
        encoding="utf-8",
    )
    _write_workspace_package(tmp_path, "packages/core", "core", {})
    _write_workspace_package(tmp_path, "apps/mobile", "mobile", {"core": "^1.0.0"})
    mobile_pubspec = tmp_path / "apps/mobile/pubspec.yaml"
    mobile_pubspec.write_text(
        mobile_pubspec.read_text(encoding="utf-8").replace("resolution: workspace\n", ""),
        encoding="utf-8",
    )
    project_files = [path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()]

    anchors = reverse_dependency_anchors(
        tmp_path,
        ["packages/core/lib/core.dart"],
        project_files,
        limit=8,
    )

    assert anchors == []


def test_reverse_dependency_anchors_cover_nested_glob_workspaces(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text(
        ("name: workspace_root\nenvironment:\n  sdk: '>=3.11.0 <4.0.0'\nworkspace:\n  - packages/*\n"),
        encoding="utf-8",
    )
    _write_workspace_package(tmp_path, "packages/server", "server", {"auth": "^1.0.0"})
    server_pubspec = tmp_path / "packages/server/pubspec.yaml"
    server_pubspec.write_text(
        f"{server_pubspec.read_text(encoding='utf-8')}workspace:\n  - auth\n",
        encoding="utf-8",
    )
    _write_workspace_package(tmp_path, "packages/server/auth", "auth", {})
    project_files = [path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()]

    anchors = reverse_dependency_anchors(
        tmp_path,
        ["packages/server/auth/lib/auth.dart"],
        project_files,
        limit=8,
    )

    assert anchors == [Path("packages/server/lib/server.dart")]


def test_reverse_dependency_anchors_are_bounded_handwritten_and_repo_local(tmp_path: Path) -> None:
    _write_package(tmp_path, "packages/core", "core", {})
    _write_package(tmp_path, "apps/first", "first", {"core": "../../packages/core"})
    _write_package(tmp_path, "apps/second", "second", {"core": "../../packages/core"})
    generated = tmp_path / "apps" / "first" / "lib" / "first.g.dart"
    handwritten = tmp_path / "apps" / "first" / "lib" / "first.dart"
    generated.write_text("const generated = true;\n", encoding="utf-8")
    handwritten.unlink()
    project_files = [path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()]

    anchors = reverse_dependency_anchors(
        tmp_path,
        ["packages/core/lib/core.dart"],
        project_files,
        limit=1,
    )

    assert anchors == [Path("apps/second/lib/second.dart")]


def test_reverse_dependency_anchors_fall_back_from_oversized_preferred_file(tmp_path: Path) -> None:
    _write_package(tmp_path, "packages/core", "core", {})
    _write_package(tmp_path, "apps/mobile", "mobile", {"core": "../../packages/core"})
    (tmp_path / "apps/mobile/lib/mobile.dart").write_bytes(b"x" * (512 * 1024 + 1))
    fallback = tmp_path / "apps/mobile/lib/src/fallback.dart"
    fallback.parent.mkdir()
    fallback.write_text("class Fallback {}\n", encoding="utf-8")
    project_files = [path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()]

    anchors = reverse_dependency_anchors(
        tmp_path,
        ["packages/core/lib/core.dart"],
        project_files,
        limit=8,
    )

    assert anchors == [Path("apps/mobile/lib/src/fallback.dart")]


def test_reverse_dependency_anchors_honor_expired_deadline(tmp_path: Path) -> None:
    _write_package(tmp_path, "packages/core", "core", {})
    _write_package(tmp_path, "apps/mobile", "mobile", {"core": "../../packages/core"})
    project_files = [path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()]

    assert (
        reverse_dependency_anchors(
            tmp_path,
            ["packages/core/lib/core.dart"],
            project_files,
            limit=8,
            deadline=time.monotonic() - 1,
        )
        == []
    )


def test_reverse_dependency_anchors_treat_recursive_yaml_as_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "pubspec.yaml"
    manifest.write_text("name: workspace\n", encoding="utf-8")
    monkeypatch.setattr(
        dart_workspace_module.yaml,
        "safe_load",
        lambda _text: (_ for _ in ()).throw(RecursionError("synthetic nesting")),
    )

    assert (
        reverse_dependency_anchors(
            tmp_path,
            ["lib/resource.dart"],
            [Path("pubspec.yaml")],
            limit=8,
        )
        == []
    )
