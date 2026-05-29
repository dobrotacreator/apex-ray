import fnmatch
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from apex_ray.models import (
    ContextPack,
    Finding,
    FindingSeverity,
    MemoryCard,
    MemoryConfig,
    MemoryKind,
    MemoryMatch,
    MemoryOmission,
    ReviewReport,
)

DEFAULT_MEMORY_PATHS = (".apex-ray/memory",)
_WHITESPACE_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class MemoryError(RuntimeError):
    pass


def load_memory_cards(root: Path, paths: list[str]) -> list[MemoryCard]:
    cards: list[MemoryCard] = []
    seen_ids: set[str] = set()
    for configured_path in paths:
        path = _resolve_memory_path(root, configured_path)
        if not path.exists():
            if configured_path in DEFAULT_MEMORY_PATHS:
                continue
            raise MemoryError(f"Memory path does not exist: {path}")
        for card_path in _memory_files(path):
            card = load_memory_file(card_path)
            card = card.model_copy(update={"source_path": _display_memory_path(root, card_path)})
            if card.id in seen_ids:
                raise MemoryError(f"Duplicate memory card id '{card.id}' in {card_path}")
            seen_ids.add(card.id)
            cards.append(card)
    return cards


def load_memory_file(path: Path) -> MemoryCard:
    try:
        text = path.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(text)
    except OSError as exc:
        raise MemoryError(f"Unable to read memory file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise MemoryError(f"Invalid YAML frontmatter in memory file {path}: {exc}") from exc
    raw = {
        "id": metadata.get("id") or path.stem,
        "title": metadata.get("title") or metadata.get("id") or path.stem,
        "body": body.strip(),
        **metadata,
    }
    try:
        return MemoryCard.model_validate(raw)
    except ValidationError as exc:
        raise MemoryError(f"Invalid memory file {path}: {exc}") from exc


def select_memory_for_pack(
    pack: ContextPack,
    cards: list[MemoryCard],
    config: MemoryConfig,
    *,
    base_chars: int,
) -> tuple[list[MemoryMatch], list[MemoryOmission]]:
    if not config.enabled or not cards or config.max_cards_per_pack == 0 or config.max_chars_per_pack == 0:
        return [], []

    scored: list[tuple[int, MemoryCard, str]] = []
    omitted: list[MemoryOmission] = []
    for card in cards:
        score, reason = _memory_card_score(card, pack)
        if score <= 0:
            continue
        scored.append((score, card, reason))

    scored.sort(
        key=lambda item: (
            -item[0],
            -_severity_rank(item[1].severity),
            -_kind_rank(item[1].kind),
            item[1].id,
            item[1].source_path or "",
        ),
    )

    max_chars = _memory_char_budget(config, base_chars)
    matches: list[MemoryMatch] = []
    used_chars = 0
    for score, card, reason in scored:
        if len(matches) >= config.max_cards_per_pack:
            omitted.append(_memory_omission(card, "memory card count budget exceeded", score))
            continue
        rendered = render_memory_card(card, config)
        prompt_chars = len(rendered)
        if used_chars + prompt_chars > max_chars:
            omitted.append(_memory_omission(card, "memory character budget exceeded", score))
            continue
        matches.append(
            MemoryMatch(
                id=card.id,
                title=card.title or card.id,
                kind=card.kind,
                severity=card.severity,
                applies_to=_memory_applies_to(card),
                source_path=card.source_path,
                score=score,
                reason=reason,
                rendered=rendered,
                prompt_chars=prompt_chars,
            )
        )
        used_chars += prompt_chars

    return matches, omitted


def render_memory_card(card: MemoryCard, config: MemoryConfig) -> str:
    applies_to = _memory_applies_to(card)
    title = card.title or card.id
    header = f"[memory:{card.id}] {title} (kind={card.kind}, severity={card.severity}, applies_to={applies_to})"
    body = _compact_text(card.body)
    max_chars = min(card.max_prompt_chars or config.max_chars_per_card, config.max_chars_per_card)
    if len(body) > max_chars:
        body = body[: max(0, max_chars - 18)].rstrip() + " ... [truncated]"
    return f"{header}\n{body}" if body else header


def memory_suggestions_from_report(report: ReviewReport, *, include_unverified: bool = False) -> str:
    findings = report.findings if include_unverified else _verified_report_findings(report)
    lines = [
        "# Apex Ray Memory Suggestions",
        "",
        "Review these suggestions before committing them under `.apex-ray/memory/`.",
        (
            "They are generated from unverified report findings and must be manually validated before use."
            if include_unverified
            else "They are generated from approved verifier findings and should be edited into stable project knowledge."
        ),
        "",
    ]
    if not findings:
        lines.append(
            "No findings were available for memory suggestions."
            if include_unverified
            else "No verified approved findings were available for memory suggestions. "
            "Use --include-unverified only when manually triaging an unverified report."
        )
        lines.append("")
        return "\n".join(lines)

    seen: set[str] = set()
    for finding in findings:
        slug = _slugify(finding.title) or "review-memory"
        if slug in seen:
            suffix = 2
            while f"{slug}-{suffix}" in seen:
                suffix += 1
            slug = f"{slug}-{suffix}"
        seen.add(slug)
        frontmatter = {
            "id": slug,
            "title": finding.title,
            "kind": "bug_pattern",
            "severity": str(finding.severity),
            "paths": [finding.file],
            "triggers": {"text": [finding.title]},
        }
        lines.extend(
            [
                f"## {finding.title}",
                "",
                "```md",
                "---",
                yaml.safe_dump(frontmatter, sort_keys=False).strip(),
                "---",
                finding.failure_mode,
                "",
                f"Evidence observed in `{finding.file}`: {finding.evidence}",
                "",
                f"Preferred fix/test direction: {finding.suggested_fix} {finding.suggested_test}",
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def _verified_report_findings(report: ReviewReport) -> list[Finding]:
    approved = {
        _finding_identity(verification.finding) for verification in report.verifications if verification.approved
    }
    if not approved:
        return []
    return [finding for finding in report.findings if _finding_identity(finding) in approved]


def _finding_identity(finding: object) -> tuple[object, ...]:
    return (
        getattr(finding, "title", ""),
        getattr(finding, "file", ""),
        getattr(finding, "line", None),
        getattr(finding, "failure_mode", ""),
        getattr(finding, "evidence", ""),
    )


def memory_cards_for_audience(pack: ContextPack, audience: str) -> list[MemoryMatch]:
    return [match for match in pack.memory_matches if match.applies_to == "both" or match.applies_to == audience]


def pack_prompt_payload(pack: ContextPack, audience: str, depth: str = "deep") -> dict[str, object]:
    payload = pack.model_dump(mode="json")
    payload["symbol"] = _compact_symbol_for_prompt(pack.symbol)
    payload["symbols"] = [_compact_symbol_for_prompt(symbol) for symbol in pack.symbols]
    payload["memory_matches"] = [match.model_dump(mode="json") for match in memory_cards_for_audience(pack, audience)]
    payload.pop("memory_omissions", None)
    if depth == "shallow":
        payload = {
            key: payload[key]
            for key in (
                "id",
                "file",
                "file_kind",
                "changed_lines",
                "impact_notes",
                "diff_snippet",
                "changed_snippets",
                "symbol",
                "symbols",
                "imports",
                "exports",
                "related_tests",
                "risk_signals",
                "rules",
                "rule_matches",
                "memory_matches",
                "warnings",
                "stats",
            )
            if key in payload
        }
    return payload


def _compact_symbol_for_prompt(symbol: object | None) -> dict[str, object] | None:
    if symbol is None:
        return None
    return {
        "name": getattr(symbol, "name", ""),
        "kind": getattr(symbol, "kind", "unknown"),
        "start_line": getattr(symbol, "start_line", None),
        "end_line": getattr(symbol, "end_line", None),
        "exported": getattr(symbol, "exported", False),
        "signature": getattr(symbol, "signature", ""),
    }


def _memory_card_score(card: MemoryCard, pack: ContextPack) -> tuple[int, str]:
    if card.exclude_paths and _path_matches(pack.file, card.exclude_paths):
        return 0, "excluded path"

    score = 0
    reasons: list[str] = []
    if card.paths:
        if not _path_matches(pack.file, card.paths):
            return 0, "path mismatch"
        score += 100
        reasons.append("path")

    pack_paths = _pack_paths(pack)
    if card.context_paths:
        if not any(_path_matches(path, card.context_paths) for path in pack_paths):
            return 0, "context path mismatch"
        score += 30
        reasons.append("context_path")

    triggers = card.triggers
    has_triggers = any((triggers.imports, triggers.symbols, triggers.risk, triggers.text))
    corpus = _pack_text_corpus(pack)
    if triggers.imports and any(token in corpus for token in triggers.imports):
        score += 60
        reasons.append("imports")
    if triggers.symbols:
        symbol_names = {symbol.name for symbol in pack.symbols}
        if pack.symbol:
            symbol_names.add(pack.symbol.name)
        if any(symbol in symbol_names or symbol in pack.id or symbol in corpus for symbol in triggers.symbols):
            score += 60
            reasons.append("symbols")
    if triggers.risk:
        risk_kinds = {signal.kind for signal in pack.risk_signals}
        if any(risk in risk_kinds for risk in triggers.risk):
            score += 40
            reasons.append("risk")
    if triggers.text and any(token in corpus for token in triggers.text):
        score += 20
        reasons.append("text")

    if has_triggers and not any(reason in reasons for reason in {"imports", "symbols", "risk", "text"}):
        return 0, "trigger mismatch"
    if score == 0 and not has_triggers and not card.paths and not card.context_paths:
        score = 1
        reasons.append("global")
    return score, ",".join(reasons)


def _memory_char_budget(config: MemoryConfig, base_chars: int) -> int:
    ratio_budget = int(max(0, base_chars) * config.max_context_ratio)
    if config.max_context_ratio == 0:
        return config.max_chars_per_pack
    return min(config.max_chars_per_pack, max(config.max_chars_per_card, ratio_budget))


def _memory_applies_to(card: MemoryCard) -> Literal["review", "verify", "both"]:
    if card.applies_to:
        return card.applies_to
    if card.kind in {MemoryKind.FALSE_POSITIVE, MemoryKind.SEVERITY_CALIBRATION}:
        return "verify"
    if card.kind == MemoryKind.GLOSSARY:
        return "review"
    return "both"


def _memory_omission(card: MemoryCard, reason: str, score: int) -> MemoryOmission:
    return MemoryOmission(
        id=card.id,
        title=card.title or card.id,
        kind=card.kind,
        reason=reason,
        score=score,
        source_path=card.source_path,
    )


def _resolve_memory_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def _display_memory_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _memory_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(file for file in path.rglob("*") if file.is_file() and file.suffix.lower() in {".md", ".markdown"})


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            frontmatter = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            raw = yaml.safe_load(frontmatter) or {}
            if not isinstance(raw, dict):
                raise MemoryError("Memory frontmatter must be a mapping")
            return raw, body
    return {}, text


def _path_matches(path: str, patterns: list[str]) -> bool:
    normalized = path.strip().replace("\\", "/").removeprefix("./")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)


def _pack_paths(pack: ContextPack) -> set[str]:
    paths = {pack.file}
    paths.update(reference.file for reference in pack.references)
    paths.update(callee.file for callee in pack.callees)
    paths.update(contract.file for contract in pack.contracts)
    paths.update(reference.file for reference in pack.metadata)
    paths.update(snippet.file for snippet in pack.changed_snippets)
    paths.update(snippet.file for snippet in pack.reference_snippets)
    paths.update(snippet.file for snippet in pack.callee_snippets)
    paths.update(snippet.file for snippet in pack.contract_snippets)
    paths.update(snippet.file for snippet in pack.metadata_snippets)
    paths.update(snippet.file for snippet in pack.related_test_snippets)
    paths.update(pack.related_tests)
    return paths


def _pack_text_corpus(pack: ContextPack) -> str:
    parts: list[str] = [
        pack.id,
        pack.file,
        *pack.imports,
        *pack.exports,
        *pack.diff_snippet,
        *pack.impact_notes,
    ]
    parts.extend(symbol.name for symbol in pack.symbols)
    if pack.symbol:
        parts.append(pack.symbol.name)
    for collection in (pack.references, pack.callees, pack.contracts, pack.metadata):
        parts.extend(reference.text for reference in collection)
        parts.extend(reference.file for reference in collection)
    for collection in (
        pack.changed_snippets,
        pack.reference_snippets,
        pack.callee_snippets,
        pack.contract_snippets,
        pack.metadata_snippets,
        pack.related_test_snippets,
    ):
        parts.extend(snippet.code for snippet in collection)
        parts.extend(snippet.file for snippet in collection)
    return "\n".join(parts)


def _compact_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip())


def _slugify(value: str) -> str:
    normalized = _SLUG_RE.sub("-", value.lower()).strip("-")
    return normalized[:80].strip("-")


def _severity_rank(severity: FindingSeverity | str) -> int:
    return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(str(severity), 0)


def _kind_rank(kind: MemoryKind | str) -> int:
    return {
        "invariant": 5,
        "bug_pattern": 4,
        "false_positive": 3,
        "severity_calibration": 2,
        "glossary": 1,
    }.get(str(kind), 0)
