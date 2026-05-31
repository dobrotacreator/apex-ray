import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import yaml

from apex_ray import git
from apex_ray.pr_eval.greptile import load_greptile_comments as load_greptile_comments_from_github
from apex_ray.pr_eval.models import GreptileComment, GreptileFinding, PullRequestEvalCase
from apex_ray.pr_eval.store import PrEvalError, atomic_write_text

RunGhJson = Callable[[list[str], Path], Any]
RunGhText = Callable[[list[str], Path], str]
RunGhApiPaginatedArray = Callable[[str, Path], list[dict[str, Any]]]


class EnsureCommitAvailable(Protocol):
    def __call__(self, repo_root: Path, sha: str, *, pr_number: int | None = None) -> None: ...


def load_prs(
    repo_root: Path,
    pr_numbers: list[int] | None,
    limit: int,
    *,
    run_gh_json: RunGhJson | None = None,
) -> list[dict[str, Any]]:
    run_json = run_gh_json or run_gh_json_default
    fields = "number,title,url,author,baseRefName,headRefName,baseRefOid,headRefOid,mergeCommit,createdAt,mergedAt"
    if pr_numbers:
        return [run_json(["pr", "view", str(number), "--json", fields], repo_root) for number in pr_numbers]
    return run_json(["pr", "list", "--state", "merged", "--limit", str(limit), "--json", fields], repo_root)


def load_pr_commit_oids(
    repo_root: Path,
    number: int,
    *,
    run_gh_json: RunGhJson | None = None,
) -> list[str]:
    run_json = run_gh_json or run_gh_json_default
    data = run_json(["pr", "view", str(number), "--json", "commits"], repo_root)
    commits = data.get("commits", [])
    if not isinstance(commits, list):
        return []
    return [str(commit.get("oid")) for commit in commits if isinstance(commit, dict) and commit.get("oid")]


def load_greptile_comments(
    owner_repo: str,
    number: int,
    repo_root: Path,
    *,
    run_gh_json: RunGhJson | None = None,
    run_gh_api_paginated_array: RunGhApiPaginatedArray | None = None,
) -> list[GreptileComment]:
    run_json = run_gh_json or run_gh_json_default
    run_paginated = run_gh_api_paginated_array or run_gh_api_paginated_array_default
    return load_greptile_comments_from_github(
        owner_repo,
        number,
        repo_root,
        run_gh_json=run_json,
        run_gh_api_paginated_array=run_paginated,
    )


def github_name_with_owner(repo_root: Path, *, run_gh_json: RunGhJson | None = None) -> str:
    run_json = run_gh_json or run_gh_json_default
    data = run_json(["repo", "view", "--json", "nameWithOwner"], repo_root)
    value = data.get("nameWithOwner")
    if not value:
        raise PrEvalError("Unable to resolve GitHub repository nameWithOwner via gh.")
    return str(value)


def run_gh_json_default(args: list[str], cwd: Path) -> Any:
    proc = run_gh(args, cwd)
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise PrEvalError(f"Unable to parse gh JSON output for {' '.join(args)}: {exc}") from exc


def run_gh_api_paginated_array_default(
    path: str,
    cwd: Path,
    *,
    run_gh_json: RunGhJson | None = None,
) -> list[dict[str, Any]]:
    run_json = run_gh_json or run_gh_json_default
    payload = run_json(["api", path, "--paginate", "--slurp"], cwd)
    if isinstance(payload, list) and all(isinstance(page, list) for page in payload):
        return [item for page in payload for item in page if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise PrEvalError(f"Expected gh api {path} to return a JSON array.")


def run_gh_text_default(args: list[str], cwd: Path) -> str:
    return run_gh(args, cwd).stdout


def run_gh(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    if shutil.which("gh") is None:
        raise PrEvalError("GitHub CLI `gh` is not available.")
    proc = subprocess.run(["gh", *args], cwd=cwd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        details = proc.stderr.strip() or proc.stdout.strip()
        raise PrEvalError(f"gh {' '.join(args)} failed ({proc.returncode}): {details}")
    return proc


def merge_commit_oid(pr: dict[str, Any]) -> str | None:
    merge_commit = pr.get("mergeCommit")
    if isinstance(merge_commit, dict):
        return merge_commit.get("oid")
    return None


def write_case_manifest(path: Path, case: PullRequestEvalCase) -> None:
    atomic_write_text(
        path,
        yaml.safe_dump(case.model_dump(mode="json", exclude_none=True), sort_keys=False),
    )


def case_manifest_paths(cases_dir: Path, pr_numbers: list[int] | None, limit: int | None) -> list[Path]:
    if pr_numbers:
        paths = [cases_dir / f"pr-{number}" / "manifest.yml" for number in pr_numbers]
    else:
        paths = sorted(cases_dir.glob("pr-*/manifest.yml"), key=lambda path: pr_number_from_case_path(path))
    if limit is not None:
        paths = paths[:limit]
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise PrEvalError("Missing PR eval manifest(s): " + ", ".join(str(path) for path in missing))
    return paths


def pr_number_from_case_path(path: Path) -> int:
    match = re.search(r"pr-(\d+)", path.as_posix())
    return int(match.group(1)) if match else 0


def replay_head_sha_from_findings(findings: list[GreptileFinding]) -> str | None:
    first_pass_shas = [
        finding.original_commit_id or finding.commit_id
        for finding in findings
        if finding.first_pass and (finding.original_commit_id or finding.commit_id)
    ]
    if not first_pass_shas:
        return None
    return first_pass_shas[0]


def replay_base_sha(
    repo_root: Path,
    owner_repo: str,
    pr_commit_oids: list[str],
    replay_head_sha: str,
    default_base_sha: str,
    *,
    github_commit_first_parent: Callable[[str, str, Path], str | None] | None = None,
) -> str:
    if replay_head_sha not in pr_commit_oids or not pr_commit_oids:
        return default_base_sha
    first_pr_commit = pr_commit_oids[0]
    first_parent = github_commit_first_parent or github_commit_first_parent_default
    return first_parent(owner_repo, first_pr_commit, repo_root) or default_base_sha


def github_commit_first_parent_default(
    owner_repo: str,
    sha: str,
    repo_root: Path,
    *,
    run_gh_json: RunGhJson | None = None,
) -> str | None:
    run_json = run_gh_json or run_gh_json_default
    data = run_json(["api", f"repos/{owner_repo}/commits/{sha}"], repo_root)
    parents = data.get("parents", [])
    if isinstance(parents, list) and parents and isinstance(parents[0], dict):
        parent = parents[0].get("sha")
        return str(parent) if parent else None
    return None


def pr_diff_from_git(
    repo_root: Path,
    owner_repo: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    *,
    allow_pr_diff_fallback: bool = False,
    ensure_commit_available: EnsureCommitAvailable | None = None,
    github_compare_diff: Callable[[str, str, str, Path], str] | None = None,
    run_gh_text: RunGhText | None = None,
) -> str:
    if not base_sha or not head_sha:
        raise PrEvalError(f"PR #{pr_number}: missing base/head commit SHA for diff capture.")
    ensure_commit = ensure_commit_available or ensure_commit_available_default
    compare_diff = github_compare_diff or github_compare_diff_default
    run_text = run_gh_text or run_gh_text_default
    errors: list[str] = []
    try:
        ensure_commit(repo_root, base_sha, pr_number=pr_number)
        ensure_commit(repo_root, head_sha, pr_number=pr_number)
        proc = git.run_git(
            ["diff", "--no-ext-diff", "--find-renames", "--find-copies", f"{base_sha}...{head_sha}"],
            cwd=repo_root,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        errors.append(proc.stderr.strip() or f"git diff {base_sha}...{head_sha} returned {proc.returncode}")
    except PrEvalError as exc:
        errors.append(str(exc))
    diff = compare_diff(owner_repo, base_sha, head_sha, repo_root)
    if not diff.strip() and allow_pr_diff_fallback:
        diff = run_text(["pr", "diff", str(pr_number)], repo_root)
    if not diff.strip():
        detail = "; ".join(error for error in errors if error)
        suffix = f" Local git diff failed first: {detail}" if detail else ""
        raise PrEvalError(f"PR #{pr_number}: captured diff is empty.{suffix}")
    return diff


def github_compare_diff_default(
    owner_repo: str,
    base_sha: str,
    head_sha: str,
    repo_root: Path,
    *,
    run_gh_text: RunGhText | None = None,
) -> str:
    run_text = run_gh_text or run_gh_text_default
    try:
        return run_text(
            [
                "api",
                f"repos/{owner_repo}/compare/{base_sha}...{head_sha}",
                "-H",
                "Accept: application/vnd.github.v3.diff",
            ],
            repo_root,
        )
    except PrEvalError:
        return ""


def ensure_commit_available_default(repo_root: Path, sha: str, *, pr_number: int | None = None) -> None:
    if git.run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_root, check=False).returncode == 0:
        return
    fetch_attempts = [["fetch", "origin", sha, "--depth=1"]]
    if pr_number is not None:
        fetch_attempts.extend(
            [
                ["fetch", "origin", f"+refs/pull/{pr_number}/head:refs/apex-ray/pr-eval/{pr_number}/head", "--depth=1"],
                [
                    "fetch",
                    "origin",
                    f"+refs/pull/{pr_number}/merge:refs/apex-ray/pr-eval/{pr_number}/merge",
                    "--depth=1",
                ],
            ]
        )
    errors: list[str] = []
    for args in fetch_attempts:
        proc = git.run_git(args, cwd=repo_root, check=False)
        if (
            proc.returncode == 0
            and git.run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_root, check=False).returncode == 0
        ):
            return
        if proc.returncode != 0:
            errors.append(proc.stderr.strip() or f"git {' '.join(args)} failed with {proc.returncode}")
    detail = "; ".join(error for error in errors if error)
    suffix = f": {detail}" if detail else ""
    raise PrEvalError(f"Commit {sha} is not available locally and could not be fetched{suffix}")


def overlay_current_apex_config(source_repo: Path, worktree: Path) -> None:
    source = source_repo / ".apex-ray"
    if not source.exists():
        return
    target = worktree / ".apex-ray"
    if target.exists():
        shutil.rmtree(target)
    ignore = shutil.ignore_patterns("cache", "config.local.yml", "telemetry", "reports", "eval", "evals")
    shutil.copytree(source, target, ignore=ignore)
