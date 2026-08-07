import os
import subprocess
import threading
from pathlib import Path

import pytest

from apex_ray import git


def _stub_pre_push_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    remotes: str = "origin\n",
    ls_remote_returncode: int = 2,
    ls_remote_stdout: str = "",
    ls_remote_stderr: str = "",
    object_returncode: int = 1,
    object_format: str = "sha1",
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run_git(
        args: list[str],
        cwd: Path,
        check: bool = True,
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        assert cwd == tmp_path
        calls.append(args)
        if args == ["remote"]:
            result = subprocess.CompletedProcess(["git", *args], 0, stdout=remotes, stderr="")
        elif args == ["rev-parse", "--show-object-format"]:
            result = subprocess.CompletedProcess(["git", *args], 0, stdout=f"{object_format}\n", stderr="")
        elif args[:1] == ["check-ref-format"]:
            result = subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")
        elif args[:1] == ["ls-remote"]:
            result = subprocess.CompletedProcess(
                ["git", *args],
                ls_remote_returncode,
                stdout=ls_remote_stdout,
                stderr=ls_remote_stderr,
            )
        elif args[:1] == ["fetch"]:
            result = subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")
        elif args[:1] == ["cat-file"]:
            result = subprocess.CompletedProcess(["git", *args], object_returncode, stdout="", stderr="")
        else:
            raise AssertionError(f"unexpected git call: {args}")
        if check and result.returncode != 0:
            raise git.GitError(args, result.stderr, result.returncode)
        return result

    monkeypatch.setattr("apex_ray.git.run_git", fake_run_git)
    return calls


def test_run_git_decodes_output_as_utf8_without_locale_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout="Unicode: — Привет \N{REPLACEMENT CHARACTER}\n",
            stderr="",
        )

    monkeypatch.setattr("apex_ray.git.subprocess.run", fake_run)

    result = git.run_git(["diff"], cwd=tmp_path)

    assert result.stdout == "Unicode: — Привет \N{REPLACEMENT CHARACTER}\n"
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"
    assert "text" not in observed


def test_run_git_allows_strict_utf8_for_machine_consumed_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, returncode=0, stdout="content\n", stderr="")

    monkeypatch.setattr("apex_ray.git.subprocess.run", fake_run)

    result = git.run_git(["show", "HEAD:file"], cwd=tmp_path, errors="strict")

    assert result.stdout == "content\n"
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "strict"


def test_fetch_remote_tracking_ref_uses_exact_validated_refspec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run_git(
        args: list[str],
        cwd: Path,
        check: bool = True,
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, timeout
        assert cwd == tmp_path
        calls.append(args)
        if args == ["remote"]:
            return subprocess.CompletedProcess(["git", *args], 0, stdout="origin\nteam/upstream\n", stderr="")
        if args[:1] == ["check-ref-format"]:
            return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")
        if args[:1] == ["fetch"]:
            return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr("apex_ray.git.run_git", fake_run_git)

    git.fetch_remote_tracking_ref(tmp_path, "refs/remotes/team/upstream/main")

    assert calls[-1] == [
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--",
        "team/upstream",
        "+refs/heads/main:refs/remotes/team/upstream/main",
    ]


def test_fetch_remote_tracking_ref_rejects_non_remote_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apex_ray.git.run_git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git", "remote"],
            0,
            stdout="origin\n",
            stderr="",
        ),
    )

    with pytest.raises(git.GitRemoteRefError, match="exact remote-tracking ref"):
        git.fetch_remote_tracking_ref(tmp_path, "main")


def test_resolve_pre_push_base_fetches_and_canonicalizes_exact_remote_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_pre_push_git(tmp_path, monkeypatch, remotes="origin\nteam/upstream\n")

    resolved = git.resolve_pre_push_base(tmp_path, "refs/remotes/team/upstream/main")

    assert resolved == "team/upstream/main"
    assert calls[-1] == [
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--",
        "team/upstream",
        "+refs/heads/main:refs/remotes/team/upstream/main",
    ]


def test_resolve_pre_push_base_fetches_exact_short_origin_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_pre_push_git(
        tmp_path,
        monkeypatch,
        ls_remote_returncode=0,
        ls_remote_stdout="1234567890abcdef\trefs/heads/feature/stack\n",
    )

    resolved = git.resolve_pre_push_base(tmp_path, "feature/stack")

    assert resolved == "origin/feature/stack"
    assert ["ls-remote", "--exit-code", "--heads", "--", "origin", "refs/heads/feature/stack"] in calls
    assert calls[-1] == [
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--",
        "origin",
        "+refs/heads/feature/stack:refs/remotes/origin/feature/stack",
    ]


def test_resolve_pre_push_base_preserves_existing_local_ref_when_origin_branch_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_pre_push_git(
        tmp_path,
        monkeypatch,
        object_returncode=0,
    )

    assert git.resolve_pre_push_base(tmp_path, "local-only") == "local-only"
    assert not any(call[:1] == ["fetch"] for call in calls)


def test_resolve_pre_push_base_rejects_remote_lookup_failure_before_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_pre_push_git(
        tmp_path,
        monkeypatch,
        ls_remote_returncode=128,
        ls_remote_stderr="network unavailable",
        object_returncode=0,
    )

    with pytest.raises(git.GitError, match="network unavailable"):
        git.resolve_pre_push_base(tmp_path, "local-only")
    assert not any(call[:1] == ["fetch"] for call in calls)


@pytest.mark.parametrize(("commit_sha", "object_format"), [("a" * 40, "sha1"), ("b" * 64, "sha256")])
def test_resolve_pre_push_base_uses_full_commit_id_without_remote_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit_sha: str,
    object_format: str,
) -> None:
    calls = _stub_pre_push_git(
        tmp_path,
        monkeypatch,
        ls_remote_returncode=128,
        ls_remote_stderr="network unavailable",
        object_returncode=0,
        object_format=object_format,
    )

    assert git.resolve_pre_push_base(tmp_path, commit_sha) == commit_sha
    assert not any(call[:1] in (["ls-remote"], ["fetch"]) for call in calls)


@pytest.mark.parametrize(("branch", "object_format"), [("a" * 64, "sha1"), ("b" * 40, "sha256")])
def test_resolve_pre_push_base_fetches_hex_branch_with_non_oid_length(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    object_format: str,
) -> None:
    calls = _stub_pre_push_git(
        tmp_path,
        monkeypatch,
        ls_remote_returncode=0,
        ls_remote_stdout=f"1234567890abcdef\trefs/heads/{branch}\n",
        object_format=object_format,
    )

    assert git.resolve_pre_push_base(tmp_path, branch) == f"origin/{branch}"
    assert any(call[:1] == ["fetch"] for call in calls)


@pytest.mark.parametrize(("branch", "object_format"), [("a" * 40, "sha1"), ("b" * 64, "sha256")])
def test_resolve_pre_push_base_fetches_hash_shaped_branch_when_no_local_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    object_format: str,
) -> None:
    calls = _stub_pre_push_git(
        tmp_path,
        monkeypatch,
        ls_remote_returncode=0,
        ls_remote_stdout=f"1234567890abcdef\trefs/heads/{branch}\n",
        object_format=object_format,
        object_returncode=1,
    )

    assert git.resolve_pre_push_base(tmp_path, branch) == f"origin/{branch}"
    assert any(call[:1] == ["cat-file"] for call in calls)
    assert any(call[:1] == ["fetch"] for call in calls)


def test_resolve_pre_push_base_ignores_ls_remote_suffix_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_pre_push_git(
        tmp_path,
        monkeypatch,
        ls_remote_returncode=0,
        ls_remote_stdout="1234567890abcdef\trefs/heads/nested/refs/heads/main\n",
        object_returncode=0,
    )

    assert git.resolve_pre_push_base(tmp_path, "main") == "main"
    assert not any(call[:1] == ["fetch"] for call in calls)


def test_resolve_pre_push_base_rejects_unknown_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_pre_push_git(tmp_path, monkeypatch)

    with pytest.raises(git.GitRemoteRefError, match="does not resolve to a commit"):
        git.resolve_pre_push_base(tmp_path, "missing")


def test_resolve_pre_push_base_uses_local_ref_without_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_pre_push_git(tmp_path, monkeypatch, remotes="upstream\n", object_returncode=0)

    assert git.resolve_pre_push_base(tmp_path, "local-only") == "local-only"
    assert not any(call[:1] in (["ls-remote"], ["fetch"]) for call in calls)


@pytest.mark.parametrize("ref", ["origin/main", "refs/remotes/origin/main"])
def test_resolve_pre_push_base_rejects_explicit_origin_ref_without_origin_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ref: str,
) -> None:
    _stub_pre_push_git(tmp_path, monkeypatch, remotes="upstream\n", object_returncode=0)

    with pytest.raises(git.GitRemoteRefError, match="configured remote"):
        git.resolve_pre_push_base(tmp_path, ref)


def test_resolve_pre_push_base_keeps_option_like_branch_operands_after_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_pre_push_git(
        tmp_path,
        monkeypatch,
        ls_remote_returncode=0,
        ls_remote_stdout="1234567890abcdef\trefs/heads/-n\n",
    )

    assert git.resolve_pre_push_base(tmp_path, "-n") == "origin/-n"
    assert ["ls-remote", "--exit-code", "--heads", "--", "origin", "refs/heads/-n"] in calls
    fetch_call = next(call for call in calls if call[:1] == ["fetch"])
    assert fetch_call[5:7] == ["--", "origin"]


def test_resolve_pre_push_base_with_real_local_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    remote = tmp_path / "remote.git"
    worktree = tmp_path / "worktree"
    empty_hooks = tmp_path / "empty-hooks"
    empty_hooks.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "core.hooksPath", str(empty_hooks)], cwd=remote, check=True)
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "apex@example.test"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Apex Test"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "push.gpgSign", "false"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "core.hooksPath", str(empty_hooks)], cwd=worktree, check=True)
    (worktree / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=worktree, check=True)
    subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/feature/stack"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "update-ref", "-d", "refs/remotes/origin/feature/stack"],
        cwd=worktree,
        check=True,
    )
    head_sha = git.rev_parse(worktree, "HEAD")

    assert git.resolve_pre_push_base(worktree, "feature/stack") == "origin/feature/stack"
    assert git.rev_parse(worktree, "origin/feature/stack") == head_sha
    assert git.resolve_pre_push_base(worktree, head_sha) == head_sha
    blob_sha = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=worktree,
        input="not a commit\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(git.GitRemoteRefError, match="does not resolve to a commit"):
        git.resolve_pre_push_base(worktree, blob_sha)


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


def test_diff_range_does_not_interpret_untrusted_ref_as_git_option(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "apex@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Apex Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.ts").write_text("export const value = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.ts"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    injected_output = tmp_path / "injected.diff"

    with pytest.raises(git.GitError):
        git.diff_range(tmp_path, f"--output={injected_output}", "HEAD")

    assert not injected_output.exists()


def test_worktree_output_path_stability_requires_untracked_ignored_path(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("reports/\ntracked-report.md\n", encoding="utf-8")
    tracked = tmp_path / "tracked-report.md"
    tracked.write_text("old report\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".gitignore", "tracked-report.md"], cwd=tmp_path, check=True)

    assert git.worktree_output_path_is_stable(tmp_path, tmp_path / "reports" / "review.json") is True
    assert git.worktree_output_path_is_stable(tmp_path, tracked) is False
    assert git.worktree_output_path_is_stable(tmp_path, tmp_path / "review.json") is False
    assert git.worktree_output_path_is_stable(tmp_path, tmp_path.parent / "external-review.json") is True
    external_target = tmp_path.parent / f"{tmp_path.name}-external-review.json"
    linked_output = tmp_path / "linked-review.json"
    linked_output.symlink_to(external_target)
    assert git.worktree_output_path_is_stable(tmp_path, linked_output) is False
    external_alias = tmp_path.parent / f"{tmp_path.name}-repo-alias"
    external_alias.symlink_to(tmp_path, target_is_directory=True)
    assert git.worktree_output_path_is_stable(tmp_path, external_alias / tracked.name) is False
    assert git.worktree_output_path_is_stable(tmp_path, external_alias / "reports" / "review.json") is True
    external_direct_link = tmp_path.parent / f"{tmp_path.name}-tracked-link.md"
    external_direct_link.symlink_to(tracked)
    assert git.worktree_output_path_is_stable(tmp_path, external_direct_link) is False


def test_worktree_output_path_stability_rejects_git_metadata(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    git_dir = tmp_path / ".git"

    assert git.worktree_output_path_is_stable(tmp_path, git_dir / "index") is False
    assert git.worktree_output_path_is_stable(tmp_path, git_dir / "config") is False
    assert git.worktree_output_path_is_stable(tmp_path, git_dir / "refs" / "heads" / "main") is False
    assert git.worktree_output_path_is_stable(tmp_path, git_dir / "apex-ray" / "reports", directory=True) is True


def test_worktree_output_path_stability_rejects_linked_worktree_git_metadata(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    primary.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=primary, check=True)
    subprocess.run(["git", "config", "user.email", "apex@example.test"], cwd=primary, check=True)
    subprocess.run(["git", "config", "user.name", "Apex Test"], cwd=primary, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "initial"], cwd=primary, check=True)
    subprocess.run(["git", "worktree", "add", "-qb", "linked-test", str(linked)], cwd=primary, check=True)
    common_dir = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=linked,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    linked_git_dir = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
            cwd=linked,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    assert git.worktree_output_path_is_stable(linked, common_dir / "config") is False
    assert git.worktree_output_path_is_stable(linked, linked_git_dir / "index") is False
    assert git.worktree_output_path_is_stable(linked, common_dir / "apex-ray" / "cache", directory=True) is True


def test_worktree_output_directory_stability_honors_directory_only_ignore_before_creation(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("/cache/\n/ignored-file/\n/linked-cache/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)

    cache_dir = tmp_path / "cache"

    assert git.worktree_output_path_is_stable(tmp_path, cache_dir, directory=True) is True

    cache_dir.mkdir()
    tracked_cache_file = cache_dir / "tracked.json"
    tracked_cache_file.write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "cache/tracked.json"], cwd=tmp_path, check=True)

    assert git.worktree_output_path_is_stable(tmp_path, cache_dir, directory=True) is False

    ignored_file = tmp_path / "ignored-file"
    ignored_file.write_text("not a directory\n", encoding="utf-8")
    assert git.worktree_output_path_is_stable(tmp_path, ignored_file, directory=True) is False

    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    linked_cache = tmp_path / "linked-cache"
    linked_cache.symlink_to(real_cache, target_is_directory=True)
    assert git.worktree_output_path_is_stable(tmp_path, linked_cache, directory=True) is False


def test_worktree_output_path_stability_handles_case_insensitive_repo_alias(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    tracked = tmp_path / "Tracked-Report.json"
    tracked.write_text("old report\n", encoding="utf-8")
    subprocess.run(["git", "add", "Tracked-Report.json"], cwd=tmp_path, check=True)
    case_alias = tmp_path.with_name(tmp_path.name.swapcase())
    if not case_alias.exists() or not os.path.samefile(case_alias, tmp_path):
        pytest.skip("filesystem is case-sensitive")

    aliased_output = case_alias / tracked.name.swapcase()

    assert git.worktree_output_path_is_stable(tmp_path, aliased_output) is False


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
