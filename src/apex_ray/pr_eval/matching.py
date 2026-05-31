import hashlib
import re

from apex_ray.models import Finding
from apex_ray.pr_eval.models import GreptileFinding, PrEvalFindingMatch, PrEvalLabels
from apex_ray.pr_eval.text import clean_text

_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


def match_greptile_to_apex(
    greptile_findings: list[GreptileFinding],
    apex_findings: list[Finding],
) -> tuple[list[PrEvalFindingMatch], list[Finding]]:
    matches: list[PrEvalFindingMatch] = []
    used_apex: set[int] = set()
    for greptile_finding in greptile_findings:
        best_index = None
        best_score = 0.0
        for index, apex_finding in enumerate(apex_findings):
            if index in used_apex:
                continue
            score = _finding_similarity(greptile_finding, apex_finding)
            if score > best_score:
                best_index = index
                best_score = score
        if best_index is not None and best_score >= 0.28:
            used_apex.add(best_index)
            apex_finding = apex_findings[best_index]
            matches.append(
                PrEvalFindingMatch(
                    greptile_finding=greptile_finding,
                    matched=True,
                    matched_apex_title=apex_finding.title,
                    matched_apex_file=apex_finding.file,
                    matched_apex_line=apex_finding.line,
                    score=round(best_score, 4),
                )
            )
        else:
            matches.append(
                PrEvalFindingMatch(greptile_finding=greptile_finding, matched=False, score=round(best_score, 4))
            )
    extra = [finding for index, finding in enumerate(apex_findings) if index not in used_apex]
    return matches, extra


def apex_finding_fingerprint(finding: Finding) -> str:
    payload = "|".join(
        [
            _normalize_path(finding.file),
            str(finding.line or ""),
            clean_text(finding.title).lower(),
            clean_text(finding.failure_mode).lower()[:500],
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"apex-{digest}"


def apply_greptile_labels(
    findings: list[GreptileFinding],
    labels: PrEvalLabels | None,
) -> list[GreptileFinding]:
    if labels is None:
        return findings
    filtered: list[GreptileFinding] = []
    for finding in findings:
        label = labels.greptile_findings.get(finding.id)
        verdict = label.verdict if label else "valid"
        if verdict in {"not_issue", "out_of_scope"}:
            continue
        filtered.append(finding)
    return filtered


def apex_extra_label_counts(
    findings: list[Finding],
    labels: PrEvalLabels | None,
) -> dict[str, int]:
    counts = {
        "true_positive": 0,
        "false_positive": 0,
        "duplicate": 0,
        "not_actionable": 0,
        "unknown": 0,
    }
    for finding in findings:
        verdict = apex_label_verdict(finding, labels)
        counts[verdict] += 1
    return counts


def blocking_extra_findings(findings: list[Finding], labels: PrEvalLabels | None) -> list[Finding]:
    return [
        finding for finding in findings if apex_label_verdict(finding, labels) not in {"true_positive", "duplicate"}
    ]


def apex_label_verdict(finding: Finding, labels: PrEvalLabels | None) -> str:
    if labels is None:
        return "unknown"
    label = labels.apex_findings.get(apex_finding_fingerprint(finding))
    return label.verdict if label else "unknown"


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").removeprefix("./")


def _finding_similarity(greptile_finding: GreptileFinding, apex_finding: Finding) -> float:
    score = 0.0
    if greptile_finding.file and apex_finding.file == greptile_finding.file:
        score += 0.3
    elif greptile_finding.file:
        return 0.0
    greptile_text = " ".join([greptile_finding.title, greptile_finding.body])
    apex_text = " ".join([apex_finding.title, apex_finding.failure_mode, apex_finding.evidence])
    token_overlap = _token_jaccard(greptile_text, apex_text)
    line_close = False
    if greptile_finding.line and apex_finding.line:
        delta = abs(greptile_finding.line - apex_finding.line)
        if delta == 0:
            score += 0.2
            line_close = True
        elif delta <= 5:
            score += 0.1
            line_close = True
        elif delta <= 20:
            if token_overlap < 0.08:
                return 0.0
            score += 0.05
            line_close = True
    if greptile_finding.file and not line_close and token_overlap < 0.12:
        return 0.0
    score += 0.5 * token_overlap
    return score


def _token_jaccard(left: str, right: str) -> float:
    left_tokens = _important_tokens(left)
    right_tokens = _important_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _important_tokens(value: str) -> set[str]:
    stop = {
        "the",
        "and",
        "or",
        "to",
        "a",
        "an",
        "of",
        "in",
        "is",
        "are",
        "with",
        "for",
        "this",
        "that",
        "it",
        "be",
        "not",
        "no",
        "on",
        "line",
        "comment",
        "issue",
        "fix",
    }
    return {token.lower() for token in _TOKEN_RE.findall(value) if len(token) >= 3 and token.lower() not in stop}
