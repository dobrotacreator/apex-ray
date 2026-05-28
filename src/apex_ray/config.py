from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from apex_ray.memory import MemoryError, load_memory_cards
from apex_ray.models import ReviewConfig
from apex_ray.rules import RuleError, load_rule_definitions

DEFAULT_CONFIG_TEXT = """review:
  base: main
  ignore:
    - "**/*.lock"
    - "**/generated/**"
  rule_paths:
    - .apex-ray/rules
  memory:
    enabled: true
    paths:
      - .apex-ray/memory
  llm:
    enabled: false
    provider: codex_cli
    coverage_mode: balanced
    max_input_tokens: 120000
    verify: true
  telemetry:
    enabled: false
    path: .apex-ray/telemetry/review-runs.jsonl
"""

APEX_RAY_GITIGNORE_TEXT = """config.local.yml
cache/
telemetry/
reports/
eval/telemetry/
eval/runs/
evals/runs/
*.tmp
"""

ROOT_GITIGNORE_BLOCK = """# Apex Ray
.apex-ray/config.local.yml
.apex-ray/cache/
.apex-ray/telemetry/
.apex-ray/reports/
.apex-ray/eval/telemetry/
.apex-ray/eval/runs/
.apex-ray/evals/runs/
/review*.md
/review*.json
/review*.html
"""

AGENTS_TEMPLATE = """# Apex Ray

This project uses Apex Ray for local diff-aware code review.

## Commands

```bash
apex-ray doctor
apex-ray review --base main --no-llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json
apex-ray review --continue-from .apex-ray/reports/review.json --residual-priority p0 --no-llm
```

Add `--llm` only when the configured local provider is available and the cost is appropriate.
Do not commit `.apex-ray/config.local.yml`, caches, telemetry, eval runs, or generated reports.
"""

LEFTHOOK_APEX_RAY_COMMAND = (
    "apex-ray review --base main --no-llm --output .apex-ray/reports/pre-push.md --json .apex-ray/reports/pre-push.json"
)


class ConfigError(RuntimeError):
    pass


def default_config_path(root: Path) -> Path:
    return root / ".apex-ray" / "config.yml"


def default_local_config_path(root: Path) -> Path:
    return root / ".apex-ray" / "config.local.yml"


def find_config(root: Path) -> Path | None:
    path = default_config_path(root)
    return path if path.exists() else None


def find_local_config(root: Path) -> Path | None:
    path = default_local_config_path(root)
    return path if path.exists() else None


def init_config(root: Path, overwrite: bool = False) -> Path:
    path = default_config_path(root)
    if path.exists() and not overwrite:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return path


def init_project(
    root: Path,
    *,
    overwrite: bool = False,
    update_gitignore: bool = True,
    hooks: str = "lefthook",
    agent_files: str = "both",
) -> list[Path]:
    written: list[Path] = []
    config_path = init_config(root, overwrite=overwrite)
    written.append(config_path)
    for directory in (
        root / ".apex-ray" / "rules",
        root / ".apex-ray" / "memory",
        root / ".apex-ray" / "reports",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    apex_gitignore = root / ".apex-ray" / ".gitignore"
    if _write_if_missing_or_overwrite(apex_gitignore, APEX_RAY_GITIGNORE_TEXT, overwrite=overwrite):
        written.append(apex_gitignore)
    if update_gitignore and _append_root_gitignore_block(root / ".gitignore"):
        written.append(root / ".gitignore")
    if hooks == "lefthook":
        if _write_lefthook_hook(root / "lefthook.yml", overwrite=overwrite):
            written.append(root / "lefthook.yml")
    elif hooks == "git":
        if _write_git_pre_push_hook(root, overwrite=overwrite):
            written.append(root / ".git" / "hooks" / "pre-push")
    elif hooks != "none":
        raise ConfigError("Unsupported hooks value. Use lefthook, git, or none.")
    written.extend(_write_agent_files(root, agent_files=agent_files, overwrite=overwrite))
    return written


def load_config(root: Path, explicit_path: Path | None = None) -> tuple[ReviewConfig, Path | None]:
    config_path = explicit_path or find_config(root)
    local_config_path = None if explicit_path is not None else find_local_config(root)
    raw_review: dict[str, Any] = {}
    validation_path = config_path or local_config_path or root

    if config_path is not None:
        raw_review = _deep_merge(raw_review, _read_review_config(config_path))
    elif explicit_path is not None:
        raise ConfigError(f"Config file does not exist: {explicit_path}")
    if local_config_path is not None:
        raw_review = _deep_merge(raw_review, _read_review_config(local_config_path))
        validation_path = local_config_path

    try:
        config = ReviewConfig.model_validate(_normalize_review_config(raw_review))
    except ValidationError as exc:
        raise ConfigError(f"Invalid config in {validation_path}: {exc}") from exc
    _validate_llm_routing_profiles(config, validation_path)
    try:
        config.rule_definitions = load_rule_definitions(root, config.rule_paths)
    except RuleError as exc:
        raise ConfigError(f"Invalid rules for {validation_path}: {exc}") from exc
    try:
        if config.memory.enabled:
            config.memory_definitions = load_memory_cards(root, config.memory.paths)
    except MemoryError as exc:
        raise ConfigError(f"Invalid memory for {validation_path}: {exc}") from exc
    return config, config_path


def _read_review_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigError(f"Unable to read config file {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid config in {config_path}: expected a mapping at document root")
    _reject_unknown_keys(raw, {"review"}, f"{config_path}")
    review = raw.get("review", {})
    if review is None:
        review = {}
    if not isinstance(review, dict):
        raise ConfigError(f"Invalid config in {config_path}: review must be a mapping")
    _reject_unknown_keys(
        review,
        {
            "base",
            "ignore",
            "languages",
            "rules",
            "rule_paths",
            "memory",
            "analyzer",
            "context",
            "llm",
            "telemetry",
        },
        f"{config_path}:review",
    )
    return review


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _normalize_review_config(review: dict[str, Any]) -> dict[str, Any]:
    return {
        "base": review.get("base", "main"),
        "ignore": review.get("ignore", ["**/*.lock", "**/generated/**"]),
        "languages": review.get("languages", []),
        "rules": review.get("rules", []),
        "rule_paths": review.get("rule_paths", [".apex-ray/rules"]),
        "memory": review.get("memory", {}),
        "analyzer": review.get("analyzer", {}),
        "context": review.get("context", {}),
        "llm": review.get("llm", {}),
        "telemetry": review.get("telemetry", {}),
    }


def _reject_unknown_keys(raw: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        joined = ", ".join(unknown)
        raise ConfigError(f"Invalid config in {location}: unknown key(s): {joined}")


def _validate_llm_routing_profiles(config: ReviewConfig, config_path: Path) -> None:
    profiles = set(config.llm.profiles)
    routing = config.llm.routing
    for field in (
        "review_profile",
        "verify_profile",
        "escalated_review_profile",
        "escalated_verify_profile",
    ):
        value = getattr(routing, field)
        if value and value not in profiles:
            raise ConfigError(
                f"Invalid config in {config_path}: review.llm.routing.{field} references unknown profile '{value}'"
            )


def _write_if_missing_or_overwrite(path: Path, text: str, *, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _append_root_gitignore_block(path: Path) -> bool:
    marker = "# Apex Ray"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if marker in text:
        return False
    separator = "\n" if text and not text.endswith("\n") else ""
    path.write_text(f"{text}{separator}{ROOT_GITIGNORE_BLOCK}", encoding="utf-8")
    return True


def _write_lefthook_hook(path: Path, *, overwrite: bool) -> bool:
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    data = yaml.safe_load(raw) if raw.strip() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"Invalid Lefthook config in {path}: expected a mapping")
    pre_push = data.setdefault("pre-push", {})
    if not isinstance(pre_push, dict):
        raise ConfigError(f"Invalid Lefthook config in {path}: pre-push must be a mapping")
    commands = pre_push.setdefault("commands", {})
    if not isinstance(commands, dict):
        raise ConfigError(f"Invalid Lefthook config in {path}: pre-push.commands must be a mapping")
    if "apex-ray-review" in commands and not overwrite:
        return False
    commands["apex-ray-review"] = {"run": LEFTHOOK_APEX_RAY_COMMAND}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return True


def _write_git_pre_push_hook(root: Path, *, overwrite: bool) -> bool:
    git_dir = root / ".git"
    if not git_dir.exists():
        raise ConfigError("Direct git hook setup requires a .git directory. Use --hooks lefthook or --hooks none.")
    hook = git_dir / "hooks" / "pre-push"
    body = f"#!/bin/sh\nset -eu\n{LEFTHOOK_APEX_RAY_COMMAND}\n"
    hook.parent.mkdir(parents=True, exist_ok=True)
    if hook.exists():
        existing = hook.read_text(encoding="utf-8", errors="ignore")
        if "apex-ray review" in existing:
            return False
        if not overwrite:
            raise ConfigError("Git pre-push hook already exists. Use --force to replace it or --hooks lefthook.")
    hook.write_text(body, encoding="utf-8")
    hook.chmod(0o755)
    return True


def _write_agent_files(root: Path, *, agent_files: str, overwrite: bool) -> list[Path]:
    if agent_files not in {"none", "codex", "claude", "both"}:
        raise ConfigError("Unsupported agent-files value. Use none, codex, claude, or both.")
    written: list[Path] = []
    if agent_files in {"codex", "both"} and _write_if_missing_or_overwrite(
        root / "AGENTS.md", AGENTS_TEMPLATE, overwrite=overwrite
    ):
        written.append(root / "AGENTS.md")
    if agent_files in {"claude", "both"}:
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        claude_file = claude_dir / "CLAUDE.md"
        if claude_file.exists() and not overwrite:
            return written
        if claude_file.exists() or claude_file.is_symlink():
            claude_file.unlink()
        if (root / "AGENTS.md").exists():
            try:
                claude_file.symlink_to("../AGENTS.md")
            except OSError:
                claude_file.write_text("See [AGENTS.md](../AGENTS.md).\n", encoding="utf-8")
        else:
            claude_file.write_text(AGENTS_TEMPLATE, encoding="utf-8")
        written.append(claude_file)
    return written
