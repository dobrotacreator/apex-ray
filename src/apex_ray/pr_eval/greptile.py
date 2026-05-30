import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apex_ray.pr_eval.models import GreptileComment, GreptileFinding
from apex_ray.pr_eval.text import clean_text

GREPTILE_AUTHOR_PREFIXES = ("greptile", "greptile-apps")
DEFAULT_GREPTILE_BODY_CHARS = 4000

GhJsonLoader = Callable[[list[str], Path], Any]
GhPaginatedArrayLoader = Callable[[str, Path], list[dict[str, Any]]]


def load_greptile_comments(
    owner_repo: str,
    number: int,
    repo_root: Path,
    *,
    run_gh_json: GhJsonLoader,
    run_gh_api_paginated_array: GhPaginatedArrayLoader,
) -> list[GreptileComment]:
    pr = run_gh_json(["pr", "view", str(number), "--json", "comments,reviews"], repo_root)
    comments: list[GreptileComment] = []
    for raw in pr.get("comments", []):
        author = author_login(raw)
        if not is_greptile_author(author):
            continue
        comments.append(
            GreptileComment(
                id=str(raw.get("id") or raw.get("url") or f"issue-{len(comments)}"),
                source="issue_comment",
                author=author,
                body=str(raw.get("body") or ""),
                url=raw.get("url"),
                created_at=str(raw.get("createdAt") or ""),
                updated_at=raw.get("updatedAt"),
                includes_created_edit=bool(raw.get("includesCreatedEdit", False)),
            )
        )
    for raw in pr.get("reviews", []):
        author = author_login(raw)
        if not is_greptile_author(author):
            continue
        comments.append(
            GreptileComment(
                id=str(raw.get("id") or f"review-{len(comments)}"),
                source="review",
                author=author,
                body=str(raw.get("body") or ""),
                url=raw.get("url"),
                commit_id=(raw.get("commit") or {}).get("oid") if isinstance(raw.get("commit"), dict) else None,
                created_at=str(raw.get("submittedAt") or ""),
            )
        )
    review_comments = run_gh_api_paginated_array(f"repos/{owner_repo}/pulls/{number}/comments", repo_root)
    for raw in review_comments:
        author = author_login(raw)
        if not is_greptile_author(author):
            continue
        comments.append(
            GreptileComment(
                id=str(raw.get("id") or raw.get("html_url") or f"review-comment-{len(comments)}"),
                source="review_comment",
                author=author,
                body=str(raw.get("body") or ""),
                file=raw.get("path"),
                line=raw.get("line"),
                original_line=raw.get("original_line"),
                url=raw.get("html_url"),
                commit_id=raw.get("commit_id"),
                original_commit_id=raw.get("original_commit_id"),
                created_at=str(raw.get("created_at") or ""),
                updated_at=raw.get("updated_at"),
            )
        )
    return sorted(comments, key=lambda item: (parse_iso(item.created_at), item.source, item.id))


def greptile_findings_from_comments(
    comments: list[GreptileComment],
    first_pass_window_minutes: int,
) -> list[GreptileFinding]:
    first_at = min((parse_iso(comment.created_at) for comment in comments), default=None)
    first_pass_cutoff = first_at + timedelta(minutes=first_pass_window_minutes) if first_at else None
    findings: list[GreptileFinding] = []
    for comment in comments:
        created_at = parse_iso(comment.created_at)
        first_pass = first_pass_cutoff is None or created_at <= first_pass_cutoff
        if comment.source == "review_comment":
            findings.append(finding_from_review_comment(comment, first_pass))
        elif comment.source == "issue_comment":
            findings.extend(findings_from_summary_comment(comment, first_pass and not comment.includes_created_edit))
    return findings


def finding_from_review_comment(comment: GreptileComment, first_pass: bool) -> GreptileFinding:
    return GreptileFinding(
        id=comment.id,
        source="review_comment",
        title=greptile_title(comment.body),
        body=trim_body(strip_prompt_details(comment.body)),
        severity=greptile_priority(comment.body),
        file=comment.file,
        line=comment.line or comment.original_line,
        original_line=comment.original_line,
        url=comment.url,
        commit_id=comment.commit_id,
        original_commit_id=comment.original_commit_id,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
        first_pass=first_pass,
    )


def findings_from_summary_comment(comment: GreptileComment, first_pass: bool) -> list[GreptileFinding]:
    findings: list[GreptileFinding] = []
    prompt_match = re.search(
        r"<details><summary>Prompt To Fix All With AI</summary>(?P<body>.*?)</details>",
        comment.body,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not prompt_match:
        return findings
    body = prompt_match.group("body")
    issue_pattern = re.compile(
        r"### Issue (?P<index>\d+) of \d+\s*\n(?P<file>[^:\n`]+):(?P<line>\d+)(?:-\d+)?\s*\n(?P<issue>.*?)(?=\n### Issue \d+ of \d+|\n`````|$)",
        flags=re.DOTALL,
    )
    for match in issue_pattern.finditer(body):
        issue_body = match.group("issue").strip()
        findings.append(
            GreptileFinding(
                id=f"{comment.id}:summary-issue-{match.group('index')}",
                source="summary_issue",
                title=greptile_title(issue_body),
                body=trim_body(issue_body),
                file=match.group("file").strip(),
                line=int(match.group("line")),
                url=comment.url,
                created_at=comment.created_at,
                updated_at=comment.updated_at,
                first_pass=first_pass,
            )
        )
    return findings


def is_greptile_author(author: str) -> bool:
    normalized = author.removesuffix("[bot]").lower()
    return any(normalized.startswith(prefix) for prefix in GREPTILE_AUTHOR_PREFIXES)


def author_login(raw: dict[str, Any]) -> str:
    user = raw.get("user")
    if isinstance(user, dict):
        return str(user.get("login") or "")
    author = raw.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or "")
    return ""


def parse_iso(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def greptile_title(body: str) -> str:
    for pattern in (
        r"\*\*([^*\n]+)\*\*",
        r"### [^\n]*\n+\*\*([^*\n]+)\*\*",
        r"Comment:\s*\n\*\*([^*\n]+)\*\*",
    ):
        match = re.search(pattern, body)
        if match:
            return clean_text(match.group(1))[:180]
    first = next((line.strip() for line in body.splitlines() if line.strip()), "Greptile finding")
    first = re.sub(r"<[^>]+>", "", first)
    return clean_text(first)[:180] or "Greptile finding"


def greptile_priority(body: str) -> str | None:
    match = re.search(r'alt="(P\d)"|badges/(p\d)\.svg', body, flags=re.IGNORECASE)
    if not match:
        return None
    return (match.group(1) or match.group(2)).upper()


def strip_prompt_details(body: str) -> str:
    return re.sub(
        r"<details><summary>Prompt To Fix With AI</summary>.*?</details>", "", body, flags=re.DOTALL | re.IGNORECASE
    ).strip()


def trim_body(body: str) -> str:
    return clean_text(body)[:DEFAULT_GREPTILE_BODY_CHARS]
