from apex_ray.models import ChangedFile, ChangedHunk, DiffLineKind, RiskConfig, RiskRule, RiskSeverity, RiskSignal
from apex_ray.path_matching import path_matches_any

RISK_SEVERITY_SCORES: dict[str, int] = {
    RiskSeverity.CRITICAL: 100,
    RiskSeverity.HIGH: 75,
    RiskSeverity.MEDIUM: 45,
    RiskSeverity.LOW: 20,
}


def apply_project_risk_policy(file: ChangedFile, config: RiskConfig) -> None:
    """Attach explainable project risk signals after built-in classification."""
    built_in_risks = {signal.kind for signal in file.risk_signals if signal.source == "built_in"}
    for rule in config.rules:
        if not _rule_filters_match(rule, file):
            continue
        matched_lines = _matched_changed_lines(rule, file)
        risk_triggered = bool(rule.risk and built_in_risks.intersection(rule.risk))
        has_triggers = bool(rule.text or rule.risk)
        if has_triggers and not matched_lines and not risk_triggered:
            continue
        if matched_lines:
            for hunk, line_number in matched_lines:
                signal = _signal_for_rule(rule, file, line_number)
                file.risk_signals.append(signal)
                hunk.risk_signals.append(signal)
            continue
        file.risk_signals.append(_signal_for_rule(rule, file, None))


def risk_signal_score(signal: RiskSignal) -> int:
    if signal.source == "project":
        return signal.score
    return signal.score or RISK_SEVERITY_SCORES.get(str(signal.severity), 0)


def _rule_filters_match(rule: RiskRule, file: ChangedFile) -> bool:
    if rule.paths and not _path_matches(file.path, rule.paths):
        return False
    if rule.exclude_paths and _path_matches(file.path, rule.exclude_paths):
        return False
    if rule.languages and file.language not in rule.languages:
        return False
    if rule.file_kinds and file.file_kind not in rule.file_kinds:
        return False
    return not rule.statuses or file.status in rule.statuses


def _matched_changed_lines(rule: RiskRule, file: ChangedFile) -> list[tuple[ChangedHunk, int]]:
    if not rule.text:
        return []
    tokens = [token.casefold() for token in rule.text if token]
    matches: list[tuple[ChangedHunk, int]] = []
    for hunk in file.hunks:
        matched_anchors: set[int] = set()
        for index, line in enumerate(hunk.lines):
            if line.kind == DiffLineKind.CONTEXT:
                continue
            content = line.content.casefold()
            if any(token in content for token in tokens):
                anchor = current_line_anchor(hunk, index)
                if anchor in matched_anchors:
                    continue
                matched_anchors.add(anchor)
                matches.append((hunk, anchor))
    return matches


def current_line_anchor(hunk: ChangedHunk, line_index: int) -> int:
    """Map a changed diff line to the closest coordinate in the current file."""
    line = hunk.lines[line_index]
    if line.new_line is not None:
        return line.new_line
    for candidate in hunk.lines[line_index + 1 :]:
        if candidate.new_line is not None:
            return candidate.new_line
    for candidate in reversed(hunk.lines[:line_index]):
        if candidate.new_line is not None:
            return candidate.new_line
    return hunk.new_start


def _signal_for_rule(rule: RiskRule, file: ChangedFile, line: int | None) -> RiskSignal:
    title = rule.title or rule.id
    return RiskSignal(
        kind=f"policy:{rule.id}",
        severity=rule.severity,
        score=rule.score if rule.score is not None else RISK_SEVERITY_SCORES[str(rule.severity)],
        reason=f"Project risk policy matched: {title}.",
        file=file.path,
        line=line,
        source="project",
        rule_id=rule.id,
        categories=rule.categories,
        reviewer_tags=rule.reviewer_tags,
        guidance=rule.guidance,
    )


def _path_matches(path: str, patterns: list[str]) -> bool:
    return path_matches_any(path, patterns)
