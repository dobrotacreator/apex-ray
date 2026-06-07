import warnings
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
  local_data:
    root: git_common
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
    enabled: true
    provider: codex_cli
    effort: medium
    coverage_mode: balanced
    max_packs: 64
    max_deep_packs: 48
    max_input_tokens: 300000
    verify: true
    cache_dir: ${{local_data}}/cache/llm
  telemetry:
    enabled: true
    path: ${{local_data}}/telemetry/review-runs.jsonl
  reports:
    archive: true
    archive_dir: ${{local_data}}/reports/runs
    retention: 20
  gates:
    pre_push:
      enabled: true
      min_finding_severity: high
      require_verified_findings: true
      fail_on_quality_gate: true
      fail_on_partial_severity: critical
      max_stdout_findings: 10
      stdout_format: agent
      auto_followup_p0: true
      progress: auto
      progress_interval_seconds: 5
"""


APEX_RAY_GITIGNORE_LINES = (
    "config.local.yml",
    "cache/",
    "telemetry/",
    "reports/",
    "eval/telemetry/",
    "eval/runs/",
    "evals/runs/",
    "*.tmp",
)
APEX_RAY_GITIGNORE_TEXT = "\n".join(APEX_RAY_GITIGNORE_LINES) + "\n"
# Codex discovers repository-scoped skills from .agents/skills; .codex is local Codex configuration.
CODEX_REPO_SKILL_DIR = ".agents"

APEX_RAY_AGENT_BLOCK_START = "<!-- APEX_RAY_START -->"
APEX_RAY_AGENT_BLOCK_END = "<!-- APEX_RAY_END -->"
APEX_RAY_AGENT_BLOCK = f"""{APEX_RAY_AGENT_BLOCK_START}
## Apex Ray

This project uses Apex Ray for local diff-aware review. Use the `$apex-ray` skill for review, gate, report, telemetry, and eval workflows. Apex Ray runs that use LLM analysis can be long-running and may appear idle; do not interrupt or kill the process just because it takes a long time. Wait for completion unless it exits, errors, or the user asks to stop. Do not bypass the configured pre-push gate by default; if bypassing is unavoidable, explain why and name the equivalent checks or review already run. Use `$apex-ray-improve` after merged PRs or review feedback to produce recommendation-only improvements for Apex Ray memory, rules, eval labels, telemetry, and config. Keep `.apex-ray/config.local.yml`, Apex Ray caches/telemetry/reports/eval runs, generated review artifacts, and local provider, model, API, or cost settings out of commits.
{APEX_RAY_AGENT_BLOCK_END}
"""
APEX_RAY_AGENT_BLOCK_NO_SKILL = f"""{APEX_RAY_AGENT_BLOCK_START}
## Apex Ray

This project uses Apex Ray for local diff-aware review. Run `apex-ray doctor` to check setup, `apex-ray review --no-llm` for deterministic local reports under `.apex-ray/reports/`, and `apex-ray gate pre-push` for the hook-equivalent gate. Apex Ray runs that use LLM analysis can be long-running and may appear idle; do not interrupt or kill the process just because it takes a long time. Wait for completion unless it exits, errors, or the user asks to stop. Do not bypass the configured pre-push gate by default; if bypassing is unavoidable, explain why and name the equivalent checks or review already run. Keep `.apex-ray/config.local.yml`, Apex Ray caches/telemetry/reports/eval runs, generated review artifacts, and local provider, model, API, or cost settings out of commits.
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
- For deterministic local review, run `apex-ray review --no-llm`; default reports are written under `.apex-ray/reports/`.
- For pre-push gate parity, run `apex-ray gate pre-push`; blocking findings and critical partial coverage are printed to stdout and the full report is written under `.apex-ray/reports/`.
- Do not bypass the configured pre-push gate by default. If bypassing is unavoidable, explain why and name the equivalent checks or review already run.
- Use `--no-llm` or `.apex-ray/config.local.yml` when the configured local provider is unavailable or LLM cost is not appropriate.
- If a report has partial coverage, continue unreviewed work with `apex-ray review --continue-from .apex-ray/reports/review.json --residual-priority p0 --llm` or review a specific skipped pack with `--only-pack`.
- Use `.apex-ray/config.yml` for shared team policy and `.apex-ray/config.local.yml` for personal provider/model/cost overrides.
- Use `.apex-ray/rules/` for stable review rules and `.apex-ray/memory/` for curated team learning.
- Use `apex-ray telemetry-summary` when tuning cost, latency, coverage, or model routing.
- Treat `.apex-ray/reports/*.md/json/html` as latest snapshots. Archived run reports live under configured local data when `review.reports.archive: true`.
- Use `apex-ray eval capture-prs` and `apex-ray eval run-prs` only for historical PR benchmark/eval work.

## Outputs

Prefer writing generated review artifacts under `.apex-ray/reports/`. Keep Markdown, JSON, and HTML reports together when possible so humans and automation can inspect the same run.

## Boundaries

Do not treat Apex Ray as a replacement for tests, linters, typecheck, CI, dependency scanners, SAST, or human review. Do not commit `.apex-ray/config.local.yml`, `.apex-ray/cache/`, `.apex-ray/telemetry/`, `.apex-ray/reports/`, eval run directories, generated review artifacts, or local provider, model, API, or cost settings unless the team intentionally curates a specific artifact.
"""

APEX_RAY_IMPROVE_SKILL_TEXT = """---
name: apex-ray-improve
description: Use after merged PRs or review feedback to produce recommendation-only improvements for Apex Ray memory, rules, eval labels, telemetry, coverage, model routing, or config from PR comments, Greptile findings, Apex reports, and telemetry.
---

# Apex Ray Improve

## Purpose

Run a post-merge learning pass. The goal is not to review the PR again; it is to decide whether Apex Ray should learn from what happened through repo memory, rules, eval labels, telemetry interpretation, coverage tuning, or config changes.

## Process

- Identify the PR number, repository root, base branch, merge commit, and whether the PR is merged. If the PR is not merged, label the output as a review-feedback learning pass instead of a post-merge pass.
- Collect PR signals with GitHub CLI when available: `gh pr view <number> --json number,title,state,mergedAt,mergeCommit,baseRefName,headRefName,author,comments,reviews,files,url` and review-thread comments from `gh api repos/<owner>/<repo>/pulls/<number>/comments --paginate`.
- Separate Greptile comments, human comments, CI/bot comments, and author follow-up commits. Treat comments as evidence, not ground truth.
- Inspect Apex Ray artifacts when present: `.apex-ray/reports/`, `.apex-ray/evals/cases/pr-<number>/`, `.apex-ray/evals/runs/*/pr-<number>/`, `.apex-ray/eval/labels/`, local review telemetry, and PR eval telemetry.
- If a comparable eval case is missing and the user asked for a fresh analysis, capture or replay narrowly with `apex-ray eval capture-prs --pr <number>` and `apex-ray eval run-prs` rather than running a broad historical benchmark.
- Compare external findings with Apex Ray findings. Call out missed issues, duplicate findings, false positives, findings outside scope, and true positives that Apex Ray found first.
- Look for durable learning candidates: recurring domain invariants, security or money-movement bug patterns, known false positives, severity calibration, rule gaps, coverage gaps, oversized packs, token budget pressure, timeout/provider failures, and poor model routing.
- Prefer small, reviewable suggestions. Draft memory/rule/config changes as proposals only; do not edit `.apex-ray/memory/`, `.apex-ray/rules/`, labels, or config unless the user explicitly asks to apply them.

## Output

Produce a concise recommendation report with these sections when relevant:

- `Summary`: whether Apex Ray needs tuning for this PR.
- `Missed Or Weak Signals`: external findings Apex Ray missed or under-ranked, with evidence.
- `False Positives Or Noise`: Apex Ray findings that appear wrong, duplicated, or not actionable.
- `Coverage And Cost`: partial severity, unreviewed P0/P1 packs, token estimates, duration, cache behavior, provider failures, and model route observations.
- `Recommended Memory`: draft card intent, paths/triggers, and why it is stable enough to consider.
- `Recommended Rules`: rule intent, matching scope, severity, and examples.
- `Recommended Config Or Eval Changes`: concrete tuning or label suggestions.
- `No Action`: items reviewed but intentionally not recommended.

## Boundaries

Keep this workflow recommendation-only by default. Do not commit raw comments, raw telemetry, eval run directories, reports, provider settings, or private identifiers. Do not turn one-off PR feedback into repo memory unless it generalizes beyond that PR. Do not use Apex Ray learning as a substitute for fixing the product code, tests, CI, or human review process.
"""

LEFTHOOK_APEX_RAY_COMMAND = "apex-ray gate pre-push"


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


def ensure_apex_gitignore(root: Path, *, overwrite: bool = False) -> Path | None:
    path = root / ".apex-ray" / ".gitignore"
    return path if _ensure_gitignore_lines(root, path, APEX_RAY_GITIGNORE_LINES, overwrite=overwrite) else None


def init_project(
    root: Path,
    *,
    overwrite: bool = False,
    update_gitignore: bool = False,
    hooks: str = "lefthook",
    agent_files: str = "both",
    agent_skill: bool = True,
) -> list[Path]:
    if update_gitignore:
        warnings.warn(
            "update_gitignore is deprecated and no longer manages the root .gitignore; "
            "Apex Ray writes .apex-ray/.gitignore for Apex Ray local artifacts.",
            UserWarning,
            stacklevel=2,
        )
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
    apex_gitignore = ensure_apex_gitignore(root, overwrite=overwrite)
    if apex_gitignore is not None:
        written.append(apex_gitignore)
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
            "local_data",
            "memory",
            "analyzer",
            "context",
            "llm",
            "telemetry",
            "reports",
            "gates",
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
        "local_data": review.get("local_data", {}),
        "memory": review.get("memory", {}),
        "analyzer": review.get("analyzer", {}),
        "context": review.get("context", {}),
        "llm": review.get("llm", {}),
        "telemetry": review.get("telemetry", {}),
        "reports": review.get("reports", {}),
        "gates": review.get("gates", {}),
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


def _ensure_gitignore_lines(root: Path, path: Path, lines: tuple[str, ...], *, overwrite: bool) -> bool:
    write_path = _safe_repo_write_path(root, path)
    expected = "\n".join(lines) + "\n"
    if not write_path.exists():
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(expected, encoding="utf-8")
        return True
    text = write_path.read_text(encoding="utf-8")
    existing = set(text.splitlines())
    missing = [line for line in lines if line not in existing]
    if not missing:
        return False
    separator = "\n" if text and not text.endswith("\n") else ""
    missing_text = "\n".join(missing)
    write_path.write_text(f"{text}{separator}{missing_text}\n", encoding="utf-8")
    return True


def _safe_repo_write_path(root: Path, path: Path) -> Path:
    write_path = _safe_repo_symlink_target(root, path) if path.is_symlink() else path
    resolved_root = root.resolve()
    resolved_write_path = write_path.resolve(strict=False)
    if not resolved_write_path.is_relative_to(resolved_root):
        raise ConfigError(f"Repository setup path points outside the repository: {path} -> {resolved_write_path}")
    return write_path


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
    data.setdefault("no_tty", True)
    pre_push = data.setdefault("pre-push", {})
    if not isinstance(pre_push, dict):
        raise ConfigError(f"Invalid Lefthook config in {path}: pre-push must be a mapping")
    pre_push.setdefault("follow", True)
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


def _write_git_pre_push_hook(root: Path, *, overwrite: bool) -> Path | None:
    hook = _git_pre_push_hook_path(root)
    body = f"#!/bin/sh\nset -eu\n{LEFTHOOK_APEX_RAY_COMMAND}\n"
    hook.parent.mkdir(parents=True, exist_ok=True)
    if hook.exists():
        existing = hook.read_text(encoding="utf-8", errors="ignore")
        if ("apex-ray gate pre-push" in existing or "apex-ray review" in existing) and not overwrite:
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
        if "apex-ray gate pre-push" not in existing and "apex-ray review" not in existing and not overwrite:
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
        raise ConfigError(f"Repository setup symlink points outside the repository: {path} -> {resolved_target}")
    return resolved_target


def _agent_block(*, agent_skill: bool) -> str:
    if agent_skill:
        return APEX_RAY_AGENT_BLOCK
    return APEX_RAY_AGENT_BLOCK_NO_SKILL


def _write_agent_skill_files(root: Path, *, agent_files: str, overwrite: bool) -> list[Path]:
    if agent_files not in {"codex", "claude", "both"}:
        raise ConfigError("Unsupported agent-files value. Use none, codex, claude, or both.")
    written: list[Path] = []
    for skill_name, skill_text in (
        ("apex-ray", APEX_RAY_SKILL_TEXT),
        ("apex-ray-improve", APEX_RAY_IMPROVE_SKILL_TEXT),
    ):
        written.extend(_write_agent_skill(root, skill_name, skill_text, agent_files=agent_files, overwrite=overwrite))
    return written


def _write_agent_skill(
    root: Path,
    skill_name: str,
    skill_text: str,
    *,
    agent_files: str,
    overwrite: bool,
) -> list[Path]:
    written: list[Path] = []
    canonical = root / ".apex-ray" / "skills" / skill_name / "SKILL.md"
    if _write_if_missing_or_overwrite(canonical, skill_text, overwrite=overwrite):
        written.append(canonical)
    if agent_files in {"codex", "both"} and _write_skill_alias(
        _codex_skill_alias_path(root, skill_name),
        canonical,
        skill_text,
        overwrite=overwrite,
    ):
        written.append(_codex_skill_alias_path(root, skill_name))
    if agent_files in {"claude", "both"} and _write_skill_alias(
        root / ".claude" / "skills" / skill_name / "SKILL.md",
        canonical,
        skill_text,
        overwrite=overwrite,
    ):
        written.append(root / ".claude" / "skills" / skill_name / "SKILL.md")
    return written


def _codex_skill_alias_path(root: Path, skill_name: str) -> Path:
    return root / CODEX_REPO_SKILL_DIR / "skills" / skill_name / "SKILL.md"


def _write_skill_alias(path: Path, target: Path, fallback_text: str, *, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    try:
        path.symlink_to(_relative_symlink_target(path, target))
    except OSError:
        path.write_text(fallback_text, encoding="utf-8")
    return True


def _relative_symlink_target(link_path: Path, target: Path) -> str:
    return str(target.relative_to(link_path.parent, walk_up=True))
