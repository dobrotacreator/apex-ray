from __future__ import annotations

import subprocess
from pathlib import Path

from apex_ray.discovery import discover_project


def test_discovery_prunes_large_generated_and_worktree_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const app = true;\n", encoding="utf-8")
    (tmp_path / "src" / "generated").mkdir()
    (tmp_path / "src" / "generated" / "model.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / ".worktrees" / "old").mkdir(parents=True)
    (tmp_path / ".worktrees" / "old" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.go").write_text("package pkg\n", encoding="utf-8")
    (tmp_path / "apps" / "admin" / ".next" / "build").mkdir(parents=True)
    (tmp_path / "apps" / "admin" / ".next" / "build" / "generated.java").write_text(
        "class Generated {}\n",
        encoding="utf-8",
    )

    profile = discover_project(tmp_path, ignored_patterns=["**/generated/**"])

    assert profile.detected_languages == ["typescript"]
    assert profile.ignored_patterns == ["**/generated/**"]


def test_discovery_uses_git_inventory_with_untracked_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "apex@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Apex Test"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tracked.ts").write_text("export const tracked = true;\n", encoding="utf-8")
    (tmp_path / "src" / "untracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.go").write_text("package pkg\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/tracked.ts"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True, text=True)

    profile = discover_project(tmp_path)

    assert profile.is_git_repo is True
    assert profile.detected_languages == ["python", "typescript"]
