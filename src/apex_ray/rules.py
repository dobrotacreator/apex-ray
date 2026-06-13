import fnmatch
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from apex_ray.models import ContextPack, ReviewRule, RuleMatch

DEFAULT_RULE_PATHS = (".apex-ray/rules",)


class RuleError(RuntimeError):
    pass


def load_rule_definitions(root: Path, paths: list[str]) -> list[ReviewRule]:
    rules: list[ReviewRule] = []
    seen_ids: set[str] = set()
    for configured_path in paths:
        path = _resolve_rule_path(root, configured_path)
        if not path.exists():
            if configured_path in DEFAULT_RULE_PATHS:
                continue
            raise RuleError(f"Rule path does not exist: {path}")
        for rule_path in _rule_files(path):
            rule = load_rule_file(rule_path)
            rule = rule.model_copy(update={"source_path": _display_rule_path(root, rule_path)})
            if rule.id in seen_ids:
                raise RuleError(f"Duplicate rule id '{rule.id}' in {rule_path}")
            seen_ids.add(rule.id)
            rules.append(rule)
    return rules


def load_rule_file(path: Path) -> ReviewRule:
    try:
        text = path.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(text)
    except OSError as exc:
        raise RuleError(f"Unable to read rule file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RuleError(f"Invalid YAML frontmatter in rule file {path}: {exc}") from exc
    raw = {
        "id": metadata.get("id") or path.stem,
        "title": metadata.get("title") or metadata.get("id") or path.stem,
        "body": body.strip(),
        **metadata,
    }
    try:
        return ReviewRule.model_validate(raw)
    except ValidationError as exc:
        raise RuleError(f"Invalid rule file {path}: {exc}") from exc


def match_rules_for_pack(pack: ContextPack, rules: list[ReviewRule]) -> list[ReviewRule]:
    return [rule for rule in rules if rule_matches_pack(rule, pack)]


def rule_matches_pack(rule: ReviewRule, pack: ContextPack) -> bool:
    if rule.paths and not _path_matches(pack.file, rule.paths):
        return False
    if rule.context_paths and not any(_path_matches(path, rule.context_paths) for path in _pack_paths(pack)):
        return False
    if rule.exclude_paths and _path_matches(pack.file, rule.exclude_paths):
        return False
    return _triggers_match(rule, pack)


def rule_match_for_prompt(rule: ReviewRule) -> RuleMatch:
    return RuleMatch(
        id=rule.id,
        title=rule.title or rule.id,
        severity=rule.severity,
        mode=rule.mode,
        resolution_surfaces=rule.resolution_surfaces,
        model=rule.model,
        verify=rule.verify,
        source_path=rule.source_path,
    )


def render_rule_for_prompt(rule: ReviewRule) -> str:
    title = rule.title or rule.id
    header = f"[custom-rule:{rule.id}] {title} (severity={rule.severity}, mode={rule.mode})"
    body = rule.body.strip()
    return f"{header}\n{body}" if body else header


def _resolve_rule_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def _display_rule_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _rule_files(path: Path) -> list[Path]:
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
                raise RuleError("Rule frontmatter must be a mapping")
            return raw, body
    return {}, text


def _triggers_match(rule: ReviewRule, pack: ContextPack) -> bool:
    triggers = rule.triggers
    if not any((triggers.imports, triggers.symbols, triggers.risk, triggers.text)):
        return True

    corpus = _pack_text_corpus(pack)
    if triggers.imports and any(token in corpus for token in triggers.imports):
        return True
    if triggers.symbols:
        symbol_names = {symbol.name for symbol in pack.symbols}
        if pack.symbol:
            symbol_names.add(pack.symbol.name)
        if any(symbol in symbol_names or symbol in pack.id or symbol in corpus for symbol in triggers.symbols):
            return True
    if triggers.risk:
        risk_kinds = {signal.kind for signal in pack.risk_signals}
        if any(risk in risk_kinds for risk in triggers.risk):
            return True
    if triggers.text and any(token in corpus for token in triggers.text):
        return True
    return False


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


def _path_matches(path: str, patterns: list[str]) -> bool:
    normalized = path.strip().replace("\\", "/").removeprefix("./")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)


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
