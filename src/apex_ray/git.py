from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    def __init__(self, args: list[str], stderr: str, returncode: int) -> None:
        self.args_list = args
        self.stderr = stderr.strip()
        self.returncode = returncode
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {self.stderr}")


def git_available() -> bool:
    return shutil.which("git") is not None


def run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(args, proc.stderr, proc.returncode)
    return proc


def repo_root(cwd: Path) -> Path | None:
    if not git_available():
        return None
    proc = run_git(["rev-parse", "--show-toplevel"], cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def is_git_repo(cwd: Path) -> bool:
    if not git_available():
        return False
    proc = run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd, check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def diff_base(cwd: Path, base: str) -> str:
    return run_git(["diff", "--find-renames", "--find-copies", f"{base}...HEAD"], cwd=cwd).stdout


def diff_staged(cwd: Path) -> str:
    return run_git(["diff", "--cached", "--find-renames", "--find-copies"], cwd=cwd).stdout


def diff_worktree(cwd: Path) -> str:
    tracked_diff = run_git(["diff", "--find-renames", "--find-copies"], cwd=cwd).stdout
    untracked_diff = diff_untracked(cwd)
    return "\n".join(part for part in (tracked_diff.rstrip(), untracked_diff.rstrip()) if part) + (
        "\n" if tracked_diff or untracked_diff else ""
    )


def diff_untracked(cwd: Path) -> str:
    chunks: list[str] = []
    for file in untracked_files(cwd):
        proc = run_git(["diff", "--no-index", "--", "/dev/null", file], cwd=cwd, check=False)
        if proc.returncode not in {0, 1}:
            raise GitError(["diff", "--no-index", "--", "/dev/null", file], proc.stderr, proc.returncode)
        if proc.stdout:
            chunks.append(proc.stdout.rstrip())
    return "\n".join(chunks) + ("\n" if chunks else "")


def tracked_files(cwd: Path) -> list[str]:
    proc = run_git(["ls-files"], cwd=cwd, check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def untracked_files(cwd: Path) -> list[str]:
    proc = run_git(["ls-files", "--others", "--exclude-standard"], cwd=cwd, check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]
