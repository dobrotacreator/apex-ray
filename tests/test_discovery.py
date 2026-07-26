import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from apex_ray import git
from apex_ray.discovery import (
    DEFAULT_GIT_ROOT_TIMEOUT_SECONDS,
    PACKAGE_JSON_BYTE_LIMIT,
    DiscoveryError,
    DiscoveryTimeoutError,
    discover_project,
    discover_project_with_files,
    discover_repo_root,
    list_project_files,
)


def test_discovery_prunes_large_generated_and_worktree_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const app = true;\n", encoding="utf-8")
    (tmp_path / "src" / "generated").mkdir()
    (tmp_path / "src" / "generated" / "model.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "model.go").write_text("package generated\n", encoding="utf-8")
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


def test_discover_repo_root_uses_bounded_default_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_timeout: float | None = None

    def fake_repo_root(cwd: Path, *, timeout: float | None = None) -> Path | None:
        assert cwd == tmp_path
        nonlocal seen_timeout
        seen_timeout = timeout
        return None

    monkeypatch.setattr("apex_ray.discovery.git.repo_root", fake_repo_root)

    assert discover_repo_root(tmp_path) == tmp_path.resolve()
    assert seen_timeout == DEFAULT_GIT_ROOT_TIMEOUT_SECONDS


def test_discovery_detects_modern_javascript_and_typescript_module_extensions(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for filename in (
        "browser.mjs",
        "worker.cjs",
        "service.mts",
        "config.cts",
        "public-api.d.mts",
        "legacy-api.d.cts",
    ):
        (tmp_path / "src" / filename).write_text("export {};\n", encoding="utf-8")

    profile = discover_project(tmp_path)

    assert profile.detected_languages == ["javascript", "typescript"]


@pytest.mark.parametrize(
    "config_name",
    ["vite.config.mjs", "vite.config.cjs", "vite.config.mts", "vite.config.cts"],
)
def test_discovery_detects_vite_from_modern_config_extensions(
    tmp_path: Path,
    config_name: str,
) -> None:
    (tmp_path / config_name).write_text("export default {};\n", encoding="utf-8")

    profile = discover_project(tmp_path)

    assert "vite" in profile.framework_hints


def test_discovery_does_not_follow_package_json_symlink(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-package.json"
    outside.write_text(
        '{"dependencies":{"react":"latest"}}',
        encoding="utf-8",
    )
    (tmp_path / "package.json").symlink_to(outside)

    profile = discover_project(
        tmp_path,
        timeout_seconds=0.5,
    )

    assert "react" not in profile.framework_hints


def test_discovery_skips_oversized_package_json(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_bytes(b" " * (PACKAGE_JSON_BYTE_LIMIT + 1))

    profile = discover_project(
        tmp_path,
        timeout_seconds=0.5,
    )

    assert profile.framework_hints == []


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


def test_git_inventory_preserves_unicode_paths(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "core.quotePath", "true"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "café.ts").write_text("export const café = true;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/café.ts"], cwd=tmp_path, check=True)

    assert list_project_files(tmp_path) == [Path("src/café.ts")]


def test_non_git_inventory_prunes_review_ignored_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_directories: list[str] = []

    def fake_walk(root: str | Path) -> Iterator[tuple[str, list[str], list[str]]]:
        root_path = Path(root)
        dirnames = ["generated", "src"]
        yield str(root_path), dirnames, []
        if "generated" in dirnames:
            entered_directories.append("generated")
            yield str(root_path / "generated"), [], ["client.ts"]
        if "src" in dirnames:
            entered_directories.append("src")
            yield str(root_path / "src"), [], ["app.ts"]

    monkeypatch.setattr("apex_ray.discovery.os.walk", fake_walk)

    files = list_project_files(
        tmp_path,
        ignored_patterns=["**/generated/**"],
        is_git_repo=False,
    )

    assert files == [Path("src/app.ts")]
    assert entered_directories == ["src"]


def test_non_git_inventory_stops_when_timeout_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "first.ts").write_text("export {};\n", encoding="utf-8")
    clock = [0.0]

    def advancing_clock() -> float:
        clock[0] += 0.6
        return clock[0]

    monkeypatch.setattr("apex_ray.discovery.time.monotonic", advancing_clock)

    with pytest.raises(TimeoutError, match="Project file discovery timed out"):
        list_project_files(
            tmp_path,
            is_git_repo=False,
            timeout_seconds=1.0,
        )


def test_git_inventory_bounds_the_git_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_timeout: float | None = None

    def timed_out_git(
        _args: list[str],
        cwd: Path,
        *,
        timeout: float | None = None,
        max_output_bytes: int,
        max_entries: int,
    ) -> git.GitNulOutput:
        del cwd, max_output_bytes, max_entries
        nonlocal seen_timeout
        seen_timeout = timeout
        raise subprocess.TimeoutExpired(cmd=["git", "ls-files"], timeout=timeout or 0)

    monkeypatch.setattr("apex_ray.discovery.git.read_git_nul_output", timed_out_git)

    with pytest.raises(TimeoutError, match="Project file discovery timed out"):
        list_project_files(
            tmp_path,
            is_git_repo=True,
            timeout_seconds=0.25,
        )

    assert seen_timeout is not None
    assert 0 < seen_timeout <= 0.25


def test_git_inventory_checks_deadline_after_the_subprocess_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]

    def slow_git(
        _args: list[str],
        cwd: Path,
        *,
        timeout: float | None = None,
        max_output_bytes: int,
        max_entries: int,
    ) -> git.GitNulOutput:
        del cwd, timeout, max_output_bytes, max_entries
        clock[0] = 2.0
        return git.GitNulOutput(returncode=1, entries=[])

    monkeypatch.setattr("apex_ray.discovery.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("apex_ray.discovery.git.read_git_nul_output", slow_git)

    with pytest.raises(TimeoutError, match="Project file discovery timed out"):
        list_project_files(
            tmp_path,
            is_git_repo=True,
            timeout_seconds=1.0,
        )


def test_project_discovery_shares_one_timeout_across_git_and_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [10.0]
    seen_timeouts: list[tuple[str, float | None]] = []

    def fake_repo_root(cwd: Path, *, timeout: float | None = None) -> Path:
        assert cwd == tmp_path
        seen_timeouts.append(("root", timeout))
        clock[0] += 0.2
        return tmp_path

    def fake_is_git_repo(cwd: Path, *, timeout: float | None = None) -> bool:
        assert cwd == tmp_path
        seen_timeouts.append(("is_git", timeout))
        clock[0] += 0.3
        return True

    def fake_list_project_files(
        root: Path,
        ignored_patterns: list[str] | None = None,
        *,
        is_git_repo: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> list[Path]:
        assert root == tmp_path
        assert ignored_patterns == []
        assert is_git_repo is True
        seen_timeouts.append(("inventory", timeout_seconds))
        return []

    monkeypatch.setattr("apex_ray.discovery.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("apex_ray.discovery.git.repo_root", fake_repo_root)
    monkeypatch.setattr("apex_ray.discovery.git.is_git_repo", fake_is_git_repo)
    monkeypatch.setattr("apex_ray.discovery.list_project_files", fake_list_project_files)

    discover_project_with_files(tmp_path, timeout_seconds=1.0)

    assert seen_timeouts == [
        ("root", pytest.approx(1.0)),
        ("is_git", pytest.approx(0.8)),
        ("inventory", pytest.approx(0.5)),
    ]


def test_project_discovery_translates_git_root_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out_repo_root(cwd: Path, *, timeout: float | None = None) -> Path | None:
        del cwd
        raise subprocess.TimeoutExpired(cmd=["git", "rev-parse"], timeout=timeout or 0)

    monkeypatch.setattr("apex_ray.discovery.git.repo_root", timed_out_repo_root)

    with pytest.raises(DiscoveryTimeoutError, match="locating the Git repository root") as error:
        discover_project_with_files(tmp_path, timeout_seconds=0.25)

    assert isinstance(error.value, DiscoveryError)
    assert isinstance(error.value, TimeoutError)
    assert "review.analyzer.timeout_seconds" in str(error.value)


def test_git_inventory_translates_output_limit_to_discovery_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def limited_git(
        _args: list[str],
        cwd: Path,
        *,
        timeout: float | None = None,
        max_output_bytes: int,
        max_entries: int,
    ) -> git.GitNulOutput:
        del cwd, timeout, max_output_bytes, max_entries
        raise git.GitOutputLimitError("Git output exceeded the entry safety limit of 250000.")

    monkeypatch.setattr("apex_ray.discovery.git.read_git_nul_output", limited_git)

    with pytest.raises(DiscoveryError, match="Git inventory exceeded its safety limit") as error:
        list_project_files(tmp_path, is_git_repo=True)

    assert "generated or untracked files" in str(error.value)
