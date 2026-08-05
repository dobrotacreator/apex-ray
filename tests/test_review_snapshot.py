from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from apex_ray import git
from apex_ray.models import ReviewInputSnapshot, TargetMode
from apex_ray.pipeline.snapshot import (
    ReviewInputSnapshotError,
    capture_review_input_snapshot,
    validate_review_input_snapshot,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "snapshot@example.invalid")
    _git(root, "config", "user.name", "Snapshot Test")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "initial")
    _git(root, "branch", "-M", "main")


def test_base_snapshot_rejects_a_new_head_before_continuation(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "app.py").write_text("value = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "feature")
    snapshot = capture_review_input_snapshot(
        tmp_path,
        git.diff_base(tmp_path, "main"),
        TargetMode.BASE,
        base_ref="main",
    )

    validate_review_input_snapshot(snapshot, tmp_path)
    (tmp_path / "app.py").write_text("value = 3\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "more changes")

    with pytest.raises(ReviewInputSnapshotError, match="HEAD changed"):
        validate_review_input_snapshot(snapshot, tmp_path)


def test_staged_snapshot_rejects_a_changed_staging_area(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("value = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    snapshot = capture_review_input_snapshot(
        tmp_path,
        git.diff_staged(tmp_path),
        TargetMode.STAGED,
    )

    validate_review_input_snapshot(snapshot, tmp_path)
    (tmp_path / "app.py").write_text("value = 3\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")

    with pytest.raises(ReviewInputSnapshotError, match="staged diff changed"):
        validate_review_input_snapshot(snapshot, tmp_path)


def test_patch_snapshot_is_explicitly_detached_from_live_git_state(tmp_path: Path) -> None:
    snapshot = capture_review_input_snapshot(
        tmp_path,
        "diff --git a/app.py b/app.py\n",
        TargetMode.PATCH,
    )

    assert validate_review_input_snapshot(snapshot, tmp_path) == "detached"


def test_live_range_snapshot_rejects_a_new_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    range_start = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "app.py").write_text("value = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "feature")
    diff_text = git.diff_range(tmp_path, range_start, "HEAD")
    snapshot = capture_review_input_snapshot(
        tmp_path,
        diff_text,
        TargetMode.PATCH,
        base_ref=f"{range_start}..HEAD",
        range_start_ref=range_start,
    )

    assert validate_review_input_snapshot(snapshot, tmp_path) == "current"
    assert snapshot.range_start_sha == range_start

    (tmp_path / "app.py").write_text("value = 3\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "more changes")

    with pytest.raises(ReviewInputSnapshotError, match="HEAD changed"):
        validate_review_input_snapshot(snapshot, tmp_path)


def test_snapshot_rejects_option_like_git_identity_before_validation() -> None:
    with pytest.raises(ValidationError, match="range_start_sha"):
        ReviewInputSnapshot(
            target_mode=TargetMode.PATCH,
            head_sha="a" * 40,
            range_start_sha="--output=/tmp/apex-ray-snapshot-injection",
            diff_sha256="0" * 64,
        )


def test_snapshot_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        ReviewInputSnapshot(
            schema_version="review-input-snapshot/v2",
            target_mode=TargetMode.PATCH,
            diff_sha256="0" * 64,
        )


def test_snapshot_rejects_report_target_mismatch(tmp_path: Path) -> None:
    snapshot = capture_review_input_snapshot(tmp_path, "", TargetMode.PATCH)

    with pytest.raises(ReviewInputSnapshotError, match="target mode does not match"):
        validate_review_input_snapshot(
            snapshot,
            tmp_path,
            expected_target_mode=TargetMode.BASE,
            expected_base_ref="main",
        )


def test_snapshot_rejects_report_base_mismatch(tmp_path: Path) -> None:
    snapshot = capture_review_input_snapshot(tmp_path, "", TargetMode.PATCH)

    with pytest.raises(ReviewInputSnapshotError, match="base does not match"):
        validate_review_input_snapshot(
            snapshot,
            tmp_path,
            expected_target_mode=TargetMode.PATCH,
            expected_base_ref="main..HEAD",
        )


def test_live_snapshot_capture_fails_closed_when_git_identity_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    monkeypatch.setattr(
        "apex_ray.pipeline.snapshot.git.rev_parse",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(git.GitError(["rev-parse"], "failed", 1)),
    )

    with pytest.raises(ReviewInputSnapshotError, match="unable to capture review target"):
        capture_review_input_snapshot(tmp_path, "", TargetMode.WORKTREE)
