from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from apex_ray import git
from apex_ray.models import ReviewInputSnapshot, TargetMode


class ReviewInputSnapshotError(ValueError):
    """Raised when a saved report no longer describes the live review target."""


_EXPECTED_BASE_UNSET = object()


def capture_review_input_snapshot(
    repo_root: Path,
    diff_text: str,
    target_mode: TargetMode,
    *,
    base_ref: str | None = None,
    range_start_ref: str | None = None,
) -> ReviewInputSnapshot:
    head_sha: str | None = None
    merge_base_sha: str | None = None
    range_start_sha: str | None = None
    live_git_target = target_mode != TargetMode.PATCH or range_start_ref is not None
    if live_git_target:
        if not git.is_git_repo(repo_root):
            raise ReviewInputSnapshotError("unable to capture review target: repository is not available")
        try:
            head_sha = git.rev_parse(repo_root, "HEAD")
            if target_mode == TargetMode.BASE and base_ref is not None:
                merge_base_sha = git.merge_base(repo_root, base_ref, "HEAD")
            if range_start_ref is not None:
                range_start_sha = git.rev_parse(repo_root, range_start_ref)
        except git.GitError:
            raise ReviewInputSnapshotError("unable to capture review target Git identity") from None
    return ReviewInputSnapshot(
        target_mode=target_mode,
        base_ref=base_ref,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        range_start_sha=range_start_sha,
        diff_sha256=_diff_sha256(diff_text),
    )


def validate_review_input_snapshot(
    snapshot: ReviewInputSnapshot,
    repo_root: Path,
    *,
    expected_target_mode: TargetMode | None = None,
    expected_base_ref: str | None | object = _EXPECTED_BASE_UNSET,
) -> Literal["current", "detached"]:
    target_mode = TargetMode(snapshot.target_mode)
    if expected_target_mode is not None and target_mode != TargetMode(expected_target_mode):
        raise ReviewInputSnapshotError("saved snapshot target mode does not match the report diff; run a fresh review")
    if expected_base_ref is not _EXPECTED_BASE_UNSET and snapshot.base_ref != expected_base_ref:
        raise ReviewInputSnapshotError("saved snapshot base does not match the report diff; run a fresh review")
    if target_mode == TargetMode.PATCH and snapshot.range_start_sha is None:
        return "detached"
    if not git.is_git_repo(repo_root):
        raise ReviewInputSnapshotError("review repository is no longer available; run a fresh review")
    if snapshot.head_sha is None:
        raise ReviewInputSnapshotError("saved report has no Git HEAD identity; run a fresh review")
    try:
        current_head = git.rev_parse(repo_root, "HEAD")
        if current_head != snapshot.head_sha:
            raise ReviewInputSnapshotError(
                f"review target HEAD changed ({snapshot.head_sha} -> {current_head}); run a fresh review"
            )
        if target_mode == TargetMode.PATCH:
            if snapshot.range_start_sha is None:  # pragma: no cover - handled above
                return "detached"
            current_diff = git.diff_range(repo_root, snapshot.range_start_sha, "HEAD")
            changed_label = "Git range diff changed"
        elif target_mode == TargetMode.BASE:
            if snapshot.base_ref is None or snapshot.merge_base_sha is None:
                raise ReviewInputSnapshotError("saved base review has incomplete Git identity; run a fresh review")
            current_merge_base = git.merge_base(repo_root, snapshot.base_ref, "HEAD")
            if current_merge_base != snapshot.merge_base_sha:
                raise ReviewInputSnapshotError(
                    "review target merge-base changed "
                    f"({snapshot.merge_base_sha} -> {current_merge_base}); run a fresh review"
                )
            current_diff = git.diff_base(repo_root, snapshot.base_ref)
            changed_label = "base diff changed"
        elif target_mode == TargetMode.STAGED:
            current_diff = git.diff_staged(repo_root)
            changed_label = "staged diff changed"
        elif target_mode == TargetMode.WORKTREE:
            current_diff = git.diff_worktree(repo_root)
            changed_label = "worktree diff changed"
        else:  # pragma: no cover - TargetMode exhaustiveness
            raise ReviewInputSnapshotError(f"unsupported review target mode: {target_mode}")
    except git.GitError as exc:
        raise ReviewInputSnapshotError(f"unable to validate saved review target: {exc}") from exc
    if _diff_sha256(current_diff) != snapshot.diff_sha256:
        raise ReviewInputSnapshotError(f"review target {changed_label}; run a fresh review")
    return "current"


def _diff_sha256(diff_text: str) -> str:
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
