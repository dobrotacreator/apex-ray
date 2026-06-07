from pathlib import Path

from apex_ray.local_data import resolve_config_path, resolve_local_data_root, resolve_runtime_config_paths
from apex_ray.models import LocalDataConfig, ReviewConfig


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_resolve_local_data_git_common_root_from_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "worktree", "add", str(worktree), "-b", "feature")

    root = resolve_local_data_root(worktree, LocalDataConfig(root="git_common"))

    assert root == repo / ".git" / "apex-ray"


def test_resolve_config_path_expands_local_data_token(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    path = resolve_config_path(repo, LocalDataConfig(root=".apex-ray/shared"), "${local_data}/telemetry/runs.jsonl")

    assert path == repo / ".apex-ray" / "shared" / "telemetry" / "runs.jsonl"


def test_resolve_config_path_keeps_ordinary_relative_paths_repo_scoped(tmp_path: Path) -> None:
    path = resolve_config_path(tmp_path, LocalDataConfig(root="git_common"), ".apex-ray/reports/review.md")

    assert path == tmp_path / ".apex-ray" / "reports" / "review.md"


def test_resolve_runtime_config_paths_expands_accumulative_paths(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.local_data.root = ".apex-ray/shared"
    config.llm.cache_dir = "${local_data}/cache/llm"
    config.reports.archive_dir = "${local_data}/reports/runs"

    resolved = resolve_runtime_config_paths(tmp_path, config)

    assert resolved.llm.cache_dir == str(tmp_path / ".apex-ray" / "shared" / "cache" / "llm")
    assert resolved.reports.archive_dir == str(tmp_path / ".apex-ray" / "shared" / "reports" / "runs")
    assert config.llm.cache_dir == "${local_data}/cache/llm"
