import subprocess
import threading
from pathlib import Path

import pytest

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


def test_read_git_nul_output_enforces_exact_byte_boundary(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "café.ts").write_text("export {};\n", encoding="utf-8")
    subprocess.run(["git", "add", "café.ts"], cwd=tmp_path, check=True)
    expected_bytes = len("café.ts\0".encode())

    result = git.read_git_nul_output(
        ["ls-files", "-z"],
        cwd=tmp_path,
        max_output_bytes=expected_bytes,
        max_entries=1,
    )

    assert result.returncode == 0
    assert result.entries == ["café.ts"]
    with pytest.raises(git.GitOutputLimitError, match=f"byte safety limit of {expected_bytes - 1}"):
        git.read_git_nul_output(
            ["ls-files", "-z"],
            cwd=tmp_path,
            max_output_bytes=expected_bytes - 1,
            max_entries=1,
        )


def test_read_git_nul_output_enforces_entry_boundary(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "a.ts").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "b.ts").write_text("export {};\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.ts", "b.ts"], cwd=tmp_path, check=True)

    with pytest.raises(git.GitOutputLimitError, match="entry safety limit of 1"):
        git.read_git_nul_output(
            ["ls-files", "-z"],
            cwd=tmp_path,
            max_output_bytes=1024,
            max_entries=1,
        )


def test_read_git_nul_output_handles_records_split_across_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ChunkedStdout:
        def __init__(self) -> None:
            self.chunks = iter([b"src/caf\xc3", b"\xa9.ts\0src/b", b".ts\0"])

        def read(self, _size: int) -> bytes:
            return next(self.chunks, b"")

        def close(self) -> None:
            pass

    class CompleteProcess:
        def __init__(self) -> None:
            self.stdout = ChunkedStdout()
            self.returncode = 0

        def poll(self) -> int:
            return self.returncode

        def kill(self) -> None:
            raise AssertionError("completed process must not be killed")

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode

    monkeypatch.setattr(
        "apex_ray.git.subprocess.Popen",
        lambda *_args, **_kwargs: CompleteProcess(),
    )

    result = git.read_git_nul_output(
        ["ls-files", "-z"],
        cwd=tmp_path,
        max_output_bytes=1024,
        max_entries=2,
    )

    assert result.entries == ["src/café.ts", "src/b.ts"]


def test_read_git_nul_output_kills_and_reaps_process_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = threading.Event()

    class BlockingStdout:
        closed = False

        def read(self, _size: int) -> bytes:
            released.wait(timeout=1.0)
            return b""

        def close(self) -> None:
            self.closed = True
            released.set()

    class BlockingProcess:
        def __init__(self) -> None:
            self.stdout = BlockingStdout()
            self.returncode: int | None = None
            self.killed = False
            self.waited = False

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            released.set()

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.waited = True
            assert self.returncode is not None
            return self.returncode

    process = BlockingProcess()
    monkeypatch.setattr("apex_ray.git.subprocess.Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(subprocess.TimeoutExpired):
        git.read_git_nul_output(
            ["ls-files", "-z"],
            cwd=tmp_path,
            timeout=0.01,
            max_output_bytes=1024,
            max_entries=10,
        )

    assert process.killed is True
    assert process.waited is True
    assert process.stdout.closed is True


def test_repo_root_forwards_timeout_to_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen_timeout: float | None = None

    def fake_run_git(
        args: list[str],
        cwd: Path,
        check: bool = True,
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert args == ["rev-parse", "--show-toplevel"]
        assert cwd == tmp_path
        assert check is False
        nonlocal seen_timeout
        seen_timeout = timeout
        return subprocess.CompletedProcess(
            ["git", *args],
            returncode=0,
            stdout=str(tmp_path),
            stderr="",
        )

    monkeypatch.setattr("apex_ray.git.git_available", lambda: True)
    monkeypatch.setattr("apex_ray.git.run_git", fake_run_git)

    assert git.repo_root(tmp_path, timeout=0.25) == tmp_path.resolve()
    assert seen_timeout == 0.25
