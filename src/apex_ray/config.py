from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from apex_ray import git
from apex_ray.memory import MemoryError, load_memory_cards
from apex_ray.models import ReviewConfig
from apex_ray.rules import RuleError, load_rule_definitions

DEFAULT_BASE_BRANCH = "main"
HOOK_MODES = {"lefthook", "git", "none"}
AGENT_FILE_MODES = {"none", "codex", "claude", "both"}


def default_config_text(base: str = DEFAULT_BASE_BRANCH) -> str:
    return f"""review:
  base: {base}
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

ROOT_GITIGNORE_BLOCK_START = "# Apex Ray start"
ROOT_GITIGNORE_BLOCK_END = "# Apex Ray end"
ROOT_GITIGNORE_LINES = (
    ".apex-ray/config.local.yml",
    ".apex-ray/cache/",
    ".apex-ray/telemetry/",
    ".apex-ray/reports/",
    ".apex-ray/eval/telemetry/",
    ".apex-ray/eval/runs/",
    ".apex-ray/evals/runs/",
    ".claude/settings.local.json",
    ".codex/config.local.toml",
    "/review*.md",
    "/review*.json",
    "/review*.html",
)
ROOT_GITIGNORE_BLOCK = (
    f"{ROOT_GITIGNORE_BLOCK_START}\n" + "\n".join(ROOT_GITIGNORE_LINES) + f"\n{ROOT_GITIGNORE_BLOCK_END}\n"
)

APEX_RAY_AGENT_BLOCK_START = "<!-- APEX_RAY_START -->"
APEX_RAY_AGENT_BLOCK_END = "<!-- APEX_RAY_END -->"
APEX_RAY_AGENT_BLOCK = f"""{APEX_RAY_AGENT_BLOCK_START}
## Apex Ray

This project uses Apex Ray for local diff-aware review. Use the `$apex-ray` skill for Apex Ray review, configuration, telemetry, and eval workflows. Keep `.apex-ray/config.local.yml`, caches, telemetry, reports, and eval runs out of commits.
{APEX_RAY_AGENT_BLOCK_END}
"""
APEX_RAY_AGENT_BLOCK_NO_SKILL = f"""{APEX_RAY_AGENT_BLOCK_START}
## Apex Ray

This project uses Apex Ray for local diff-aware review. Run `apex-ray doctor` to check setup and `apex-ray review --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json` for local reports. Keep `.apex-ray/config.local.yml`, caches, telemetry, reports, and eval runs out of commits.
{APEX_RAY_AGENT_BLOCK_END}
"""

APEX_RAY_SKILL_TEXT = """---
name: apex-ray
description: Use when running or configuring Apex Ray local code reviews, interpreting reports, continuing partial reviews, tuning rules, memory, telemetry, or historical PR evals.
---

# Apex Ray

## Purpose

Apex Ray is the project's local diff-aware AI review tool. Use it to create deterministic local review reports, run configured LLM review, continue partial coverage, tune repo rules/memory, inspect telemetry, and replay historical PR evals.

## Process

- Run `apex-ray doctor` when setup, config, provider, or analyzer state is uncertain.
- For deterministic local review, run `apex-ray review --no-llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json`.
- Add `--llm` only when the configured local provider is available and cost is appropriate.
- If a report has partial coverage, continue unreviewed work with `apex-ray review --continue-from .apex-ray/reports/review.json --residual-priority p0 --llm` or review a specific skipped pack with `--only-pack`.
- Use `.apex-ray/config.yml` for shared team policy and `.apex-ray/config.local.yml` for personal provider/model/cost overrides.
- Use `.apex-ray/rules/` for stable review rules and `.apex-ray/memory/` for curated team learning.
- Use `apex-ray telemetry-summary --telemetry-path .apex-ray/telemetry/review-runs.jsonl` when tuning cost, latency, coverage, or model routing.
- Use `apex-ray eval capture-prs` and `apex-ray eval run-prs` only for historical PR benchmark/eval work.

## Outputs

Prefer writing generated review artifacts under `.apex-ray/reports/`. Keep Markdown, JSON, and HTML reports together when possible so humans and automation can inspect the same run.

## Boundaries

Do not treat Apex Ray as a replacement for tests, linters, typecheck, CI, dependency scanners, SAST, or human review. Do not commit `.apex-ray/config.local.yml`, `.apex-ray/cache/`, `.apex-ray/telemetry/`, `.apex-ray/reports/`, eval run directories, or generated `review.*` files unless the team intentionally curates a specific artifact.
"""

LEFTHOOK_APEX_RAY_COMMAND = (
    "apex-ray review --output .apex-ray/reports/pre-push.md --json .apex-ray/reports/pre-push.json"
)


class ConfigError(RuntimeError):
    pass


def default_config_path(root: Path) -> Path:
    return root / ".apex-ray" / "config.yml"


def default_local_config_path(root: Path) -> Path:
    return root / ".apex-ray" / "config.local.yml"


def detect_default_base(root: Path) -> str:
    if not git.is_git_repo(root):
        return DEFAULT_BASE_BRANCH
    origin_head = git.run_git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd=root, check=False)
    if origin_head.returncode == 0 and origin_head.stdout.strip():
        return origin_head.stdout.strip().removeprefix("origin/")
    for branch in ("main", "master"):
        exists = git.run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root, check=False)
        if exists.returncode == 0:
            return branch
    current = git.run_git(["branch", "--show-current"], cwd=root, check=False)
    current_branch = current.stdout.strip()
    if current.returncode == 0 and current_branch and current_branch not in {"feature", "dev"}:
        if not any(token in current_branch for token in ("/", "-")):
            return current_branch
    return DEFAULT_BASE_BRANCH


def find_config(root: Path) -> Path | None:
    path = default_config_path(root)
    return path if path.exists() else None


def find_local_config(root: Path) -> Path | None:
    path = default_local_config_path(root)
    return path if path.exists() else None


def init_config(root: Path, overwrite: bool = False, *, base: str | None = None) -> Path:
    path = default_config_path(root)
    if path.exists() and not overwrite:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_config_text(base or DEFAULT_BASE_BRANCH), encoding="utf-8")
    return path


def init_project(
    root: Path,
    *,
    overwrite: bool = False,
    update_gitignore: bool = True,
    hooks: str = "lefthook",
    agent_files: str = "both",
    agent_skill: bool = True,
) -> list[Path]:
    _validate_init_options(hooks=hooks, agent_files=agent_files)
    _preflight_init_targets(root, hooks=hooks, agent_files=agent_files, overwrite=overwrite)
    written: list[Path] = []
    config_exists = default_config_path(root).exists()
    default_base = detect_default_base(root)
    config_path = init_config(root, overwrite=overwrite, base=default_base)
    if overwrite or not config_exists:
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
        hook_path = _write_git_pre_push_hook(root, overwrite=overwrite)
        if hook_path is not None:
            written.append(hook_path)
    written.extend(_write_agent_files(root, agent_files=agent_files, agent_skill=agent_skill, overwrite=overwrite))
    if agent_skill and agent_files != "none":
        written.extend(_write_agent_skill_files(root, agent_files=agent_files, overwrite=overwrite))
    return written


def _validate_init_options(*, hooks: str, agent_files: str) -> None:
    if hooks not in HOOK_MODES:
        raise ConfigError("Unsupported hooks value. Use lefthook, git, or none.")
    if agent_files not in AGENT_FILE_MODES:
        raise ConfigError("Unsupported agent-files value. Use none, codex, claude, or both.")


def _preflight_init_targets(root: Path, *, hooks: str, agent_files: str, overwrite: bool) -> None:
    if hooks == "lefthook":
        _validate_lefthook_target(root / "lefthook.yml", overwrite=overwrite)
    elif hooks == "git":
        _validate_git_hook_target(root, overwrite=overwrite)
    if agent_files in {"codex", "both"} and (root / "AGENTS.md").is_symlink():
        _safe_repo_symlink_target(root, root / "AGENTS.md")
    if agent_files in {"claude", "both"}:
        for candidate in (root / "CLAUDE.md", root / ".claude" / "CLAUDE.md"):
            if candidate.is_symlink():
                _safe_repo_symlink_target(root, candidate)


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


def _append_marked_block(path: Path, block: str, *, overwrite: bool) -> bool:
    start = APEX_RAY_AGENT_BLOCK_START
    end = APEX_RAY_AGENT_BLOCK_END
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if start in existing and end in existing:
        if not overwrite:
            return False
        before, remainder = existing.split(start, 1)
        _, after = remainder.split(end, 1)
        replacement = block.rstrip("\n")
        text = (
            f"{before.rstrip()}\n\n{replacement}\n{after.lstrip()}"
            if before.strip()
            else f"{replacement}\n{after.lstrip()}"
        )
    else:
        separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
        text = f"{existing}{separator}{block}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _append_root_gitignore_block(path: Path) -> bool:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if ROOT_GITIGNORE_BLOCK_START in text and ROOT_GITIGNORE_BLOCK_END in text:
        before, remainder = text.split(ROOT_GITIGNORE_BLOCK_START, 1)
        _, after = remainder.split(ROOT_GITIGNORE_BLOCK_END, 1)
        replacement = ROOT_GITIGNORE_BLOCK.rstrip("\n")
        updated = (
            f"{before.rstrip()}\n\n{replacement}\n{after.lstrip()}"
            if before.strip()
            else f"{replacement}\n{after.lstrip()}"
        )
        if updated == text:
            return False
        path.write_text(updated, encoding="utf-8")
        return True
    text = _strip_legacy_root_gitignore_block(text)
    separator = "\n" if text and not text.endswith("\n") else ""
    path.write_text(f"{text}{separator}{ROOT_GITIGNORE_BLOCK}", encoding="utf-8")
    return True


def _write_lefthook_hook(path: Path, *, overwrite: bool) -> bool:
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    _validate_lefthook_text(path, raw, overwrite=overwrite)
    try:
        data = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
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


def _validate_lefthook_target(path: Path, *, overwrite: bool) -> None:
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    _validate_lefthook_text(path, raw, overwrite=overwrite)


def _validate_lefthook_text(path: Path, raw: str, *, overwrite: bool) -> None:
    if raw.strip() and "apex-ray-review" not in raw and not overwrite:
        raise ConfigError(
            f"Lefthook config already exists at {path}. "
            "Add the apex-ray-review command manually, use --hooks none, or rerun with --force if YAML "
            "formatting/comments can be rewritten."
        )


def _strip_legacy_root_gitignore_block(text: str) -> str:
    if "# Apex Ray" not in text:
        return text
    output: list[str] = []
    in_legacy_block = False
    for line in text.splitlines():
        if line == "# Apex Ray":
            in_legacy_block = True
            continue
        if in_legacy_block and (line in ROOT_GITIGNORE_LINES or not line.strip()):
            if not line.strip():
                in_legacy_block = False
            continue
        in_legacy_block = False
        output.append(line)
    if not output:
        return ""
    return "\n".join(output) + ("\n" if text.endswith("\n") else "")


def _write_git_pre_push_hook(root: Path, *, overwrite: bool) -> Path | None:
    hook = _git_pre_push_hook_path(root)
    body = f"#!/bin/sh\nset -eu\n{LEFTHOOK_APEX_RAY_COMMAND}\n"
    hook.parent.mkdir(parents=True, exist_ok=True)
    if hook.exists():
        existing = hook.read_text(encoding="utf-8", errors="ignore")
        if "apex-ray review" in existing:
            return None
        if not overwrite:
            raise ConfigError("Git pre-push hook already exists. Use --force to replace it or --hooks lefthook.")
    hook.write_text(body, encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _validate_git_hook_target(root: Path, *, overwrite: bool) -> None:
    hook = _git_pre_push_hook_path(root)
    if hook.exists():
        existing = hook.read_text(encoding="utf-8", errors="ignore")
        if "apex-ray review" not in existing and not overwrite:
            raise ConfigError("Git pre-push hook already exists. Use --force to replace it or --hooks lefthook.")


def _git_pre_push_hook_path(root: Path) -> Path:
    if not git.is_git_repo(root):
        raise ConfigError("Direct git hook setup requires a git repository. Use --hooks lefthook or --hooks none.")
    hook_proc = git.run_git(["rev-parse", "--git-path", "hooks/pre-push"], cwd=root, check=False)
    if hook_proc.returncode != 0 or not hook_proc.stdout.strip():
        raise ConfigError("Unable to resolve git pre-push hook path. Use --hooks lefthook or --hooks none.")
    return (root / hook_proc.stdout.strip()).resolve()


def _write_agent_files(root: Path, *, agent_files: str, agent_skill: bool, overwrite: bool) -> list[Path]:
    written: list[Path] = []
    agents_path = root / "AGENTS.md"
    if agent_files in {"codex", "both"}:
        written_path = _append_agent_block(root, agents_path, agent_skill=agent_skill, overwrite=overwrite)
        if written_path is not None:
            written.append(written_path)
    if agent_files in {"claude", "both"}:
        root_claude_file = root / "CLAUDE.md"
        if root_claude_file.exists() or root_claude_file.is_symlink():
            written_path = _append_agent_block(root, root_claude_file, agent_skill=agent_skill, overwrite=overwrite)
            if written_path is not None:
                written.append(written_path)
            return written
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        claude_file = claude_dir / "CLAUDE.md"
        if claude_file.exists() or claude_file.is_symlink():
            written_path = _append_agent_block(root, claude_file, agent_skill=agent_skill, overwrite=overwrite)
            if written_path is not None:
                written.append(written_path)
            return written
        if agent_files == "both" and not claude_file.exists() and agents_path.exists():
            try:
                claude_file.symlink_to("../AGENTS.md")
                written.append(claude_file)
                return written
            except OSError:
                claude_file.write_text("See [AGENTS.md](../AGENTS.md).\n", encoding="utf-8")
                written.append(claude_file)
                return written
        if _append_marked_block(claude_file, _agent_block(agent_skill=agent_skill), overwrite=overwrite):
            written.append(claude_file)
    return written


def _append_agent_block(root: Path, path: Path, *, agent_skill: bool, overwrite: bool) -> Path | None:
    block = _agent_block(agent_skill=agent_skill)
    if path.is_symlink():
        target = _safe_repo_symlink_target(root, path)
        return target if _append_marked_block(target, block, overwrite=overwrite) else None
    return path if _append_marked_block(path, block, overwrite=overwrite) else None


def _safe_repo_symlink_target(root: Path, path: Path) -> Path:
    raw_target = path.readlink()
    target = raw_target if raw_target.is_absolute() else path.parent / raw_target
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise ConfigError(f"Agent instruction symlink points outside the repository: {path} -> {resolved_target}")
    return resolved_target


def _agent_block(*, agent_skill: bool) -> str:
    if agent_skill:
        return APEX_RAY_AGENT_BLOCK
    return APEX_RAY_AGENT_BLOCK_NO_SKILL


def _write_agent_skill_files(root: Path, *, agent_files: str, overwrite: bool) -> list[Path]:
    if agent_files not in {"codex", "claude", "both"}:
        raise ConfigError("Unsupported agent-files value. Use none, codex, claude, or both.")
    written: list[Path] = []
    canonical = root / ".apex-ray" / "skills" / "apex-ray" / "SKILL.md"
    if _write_if_missing_or_overwrite(canonical, APEX_RAY_SKILL_TEXT, overwrite=overwrite):
        written.append(canonical)
    if agent_files in {"codex", "both"} and _write_skill_alias(
        root / ".codex" / "skills" / "apex-ray" / "SKILL.md",
        canonical,
        overwrite=overwrite,
    ):
        written.append(root / ".codex" / "skills" / "apex-ray" / "SKILL.md")
    if agent_files in {"claude", "both"} and _write_skill_alias(
        root / ".claude" / "skills" / "apex-ray" / "SKILL.md",
        canonical,
        overwrite=overwrite,
    ):
        written.append(root / ".claude" / "skills" / "apex-ray" / "SKILL.md")
    return written


def _write_skill_alias(path: Path, target: Path, *, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    try:
        path.symlink_to(_relative_symlink_target(path, target))
    except OSError:
        path.write_text(APEX_RAY_SKILL_TEXT, encoding="utf-8")
    return True


def _relative_symlink_target(link_path: Path, target: Path) -> str:
    return str(target.relative_to(link_path.parent, walk_up=True))
