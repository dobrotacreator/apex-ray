import subprocess
from pathlib import Path

from apex_ray import git


def test_diff_worktree_includes_untracked_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / ".gitignore").write_text("ignored.ts\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "new.ts").write_text("export const value = 1;\n", encoding="utf-8")
    (tmp_path / "ignored.ts").write_text("export const ignored = true;\n", encoding="utf-8")

    diff = git.diff_worktree(tmp_path)

    assert "diff --git a/src/new.ts b/src/new.ts" in diff
    assert "new file mode" in diff
    assert "+++ b/src/new.ts" in diff
    assert "+export const value = 1;" in diff
    assert "export const ignored = true;" not in diff


def test_diff_worktree_combines_tracked_and_untracked_changes(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "apex@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Apex Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.ts").write_text("export const tracked = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.ts"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "tracked.ts").write_text("export const tracked = 2;\n", encoding="utf-8")
    (tmp_path / "new.ts").write_text("export const created = true;\n", encoding="utf-8")

    diff = git.diff_worktree(tmp_path)

    assert "diff --git a/tracked.ts b/tracked.ts" in diff
    assert "+export const tracked = 2;" in diff
    assert "diff --git a/new.ts b/new.ts" in diff
    assert "+export const created = true;" in diff
