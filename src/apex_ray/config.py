import re
import shlex
import shutil
import warnings
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from apex_ray import git
from apex_ray.memory import MemoryError, load_memory_cards
from apex_ray.models import ReviewConfig
from apex_ray.rules import RuleError, load_rule_definitions
from apex_ray.version_lock import (
    assert_version_lock,
    ensure_version_lock,
    inspect_version_lock,
    render_uvx_command,
    validate_version_lock_target,
)

DEFAULT_BASE_BRANCH = "main"
HOOK_MODES = {"lefthook", "git", "none"}
AGENT_FILE_MODES = {"none", "codex", "claude", "both"}
AGENT_ARTIFACT_TEMPLATE_VERSION = 4
_LEFTHOOK_EXECUTABLE_NAMES = {"lefthook", "lefthook.bat", "lefthook.cmd", "lefthook.exe"}


@dataclass(frozen=True)
class AgentArtifactStatus:
    path: Path
    kind: str
    status: str
    reason: str = ""

    @property
    def needs_refresh(self) -> bool:
        return self.status in {"missing", "outdated", "unmanaged"}


@dataclass(frozen=True)
class ManagedHookStatus:
    path: Path
    kind: str
    status: str
    expected_command: str
    actual_command: str | None = None
    reason: str = ""

    @property
    def needs_refresh(self) -> bool:
        return self.status != "current"


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
    jobs: 2
    coverage_mode: balanced
    max_packs: 48
    max_deep_packs: 16
    max_input_tokens: 180000
    max_consecutive_provider_failures: 3
    verify: true
    cache_dir: ${{local_data}}/cache/llm
  telemetry:
    enabled: true
    path: ${{local_data}}/telemetry/review-runs.jsonl
    path_mode: anonymized
  reports:
    archive: true
    archive_dir: ${{local_data}}/reports/runs
    retention: 20
    compression: auto
    compression_min_bytes: 65536
  triage:
    enabled: true
    state_path: ${{local_data}}/triage/suppressions.json
    events_path: ${{local_data}}/triage/events.jsonl
    default_expiry_days: 14
    max_active_suppressions: 200
    events_retention_days: 90
  gates:
    pre_push:
      enabled: true
      min_finding_severity: high
      require_verified_findings: true
      fail_on_quality_gate: true
      fail_on_partial_severity: critical
      max_stdout_findings: 10
      stdout_format: agent
      auto_followup: true
      auto_followup_max_pack_reviews: 16
      progress: auto
      progress_interval_seconds: 5
"""


APEX_RAY_GITIGNORE_LINES = (
    "config.local.yml",
    "cache/",
    "telemetry/",
    "reports/",
    "triage/",
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
APEX_RAY_AGENT_TEMPLATE_MARKER = f"<!-- apex-ray-agent-artifacts: version={AGENT_ARTIFACT_TEMPLATE_VERSION} -->"
APEX_RAY_SKILL_TOKEN_RE = re.compile(r"(?<![\w-])\$apex-ray(?![\w-])")
APEX_RAY_CLI_COMMAND_RE = re.compile(
    r"(?<![\w$-])apex-ray(?=\s+(?:doctor|review|gate|findings|telemetry-summary|eval)\b)"
)
MANAGED_GIT_HOOK_MARKER = "# apex-ray-managed-hook: version=1"
LEGACY_GIT_HOOK_BODY = "#!/bin/sh\nset -eu\napex-ray gate pre-push\n"
APEX_RAY_AGENT_BLOCK = f"""{APEX_RAY_AGENT_BLOCK_START}
{APEX_RAY_AGENT_TEMPLATE_MARKER}
## Apex Ray

This project uses Apex Ray for local diff-aware review. Use the `$apex-ray` skill for review, gate, report, telemetry, and eval workflows. Apex Ray runs that use LLM analysis can be long-running and may appear idle; do not interrupt or kill the process just because it takes a long time. Wait for completion unless it exits, errors, or the user asks to stop. When a pre-push hook is configured, do not proactively run `apex-ray review` or `apex-ray gate pre-push` as a routine final verification step; let `git push` invoke the hook so the pre-push incremental retry state remains the source of truth. Run Apex Ray manually only when the user asks, when debugging/tuning Apex Ray, when the hook is unavailable, or when explicit gate parity is needed before a push. Do not bypass the configured pre-push gate by default; use `apex-ray findings suppress` only for a confirmed local false positive after checking the finding evidence, current code, and relevant tests or invariants, and always provide a concrete objective reason. Do not suppress uncertain findings, real defects, or findings merely to get a push through. If bypassing is unavoidable, explain why and name the equivalent checks or review already run. Use `$apex-ray-improve` after merged PRs or review feedback to produce recommendation-only improvements for Apex Ray memory, rules, eval labels, telemetry, and config. Keep `.apex-ray/config.local.yml`, Apex Ray caches/telemetry/reports/triage/eval runs, generated review artifacts, and local provider, model, API, or cost settings out of commits.
{APEX_RAY_AGENT_BLOCK_END}
"""
APEX_RAY_AGENT_BLOCK_NO_SKILL = f"""{APEX_RAY_AGENT_BLOCK_START}
{APEX_RAY_AGENT_TEMPLATE_MARKER}
## Apex Ray

This project uses Apex Ray for local diff-aware review. Use `apex-ray doctor` to check setup. For manual Apex Ray runs, `apex-ray review --no-llm` creates deterministic local reports under `.apex-ray/reports/`, and `apex-ray gate pre-push` runs the hook-equivalent gate. Apex Ray runs that use LLM analysis can be long-running and may appear idle; do not interrupt or kill the process just because it takes a long time. Wait for completion unless it exits, errors, or the user asks to stop. When a pre-push hook is configured, do not proactively run `apex-ray review` or `apex-ray gate pre-push` as a routine final verification step; let `git push` invoke the hook so the pre-push incremental retry state remains the source of truth. Run Apex Ray manually only when the user asks, when debugging/tuning Apex Ray, when the hook is unavailable, or when explicit gate parity is needed before a push. Do not bypass the configured pre-push gate by default; use `apex-ray findings suppress` only for a confirmed local false positive after checking the finding evidence, current code, and relevant tests or invariants, and always provide a concrete objective reason. Do not suppress uncertain findings, real defects, or findings merely to get a push through. If bypassing is unavoidable, explain why and name the equivalent checks or review already run. Keep `.apex-ray/config.local.yml`, Apex Ray caches/telemetry/reports/triage/eval runs, generated review artifacts, and local provider, model, API, or cost settings out of commits.
{APEX_RAY_AGENT_BLOCK_END}
"""

APEX_RAY_SKILL_TEXT = f"""---
name: apex-ray
description: Use when running or configuring Apex Ray local code reviews, interpreting reports, continuing partial reviews, tuning rules, memory, telemetry, or historical PR evals.
apex_ray_template_version: {AGENT_ARTIFACT_TEMPLATE_VERSION}
---

# Apex Ray

## Purpose

Apex Ray is the project's local diff-aware AI review tool. Use it to create deterministic local review reports, run configured LLM review, continue partial coverage, tune repo rules/memory, inspect telemetry, and replay historical PR evals.

## Process

- Run `apex-ray doctor` when setup, config, provider, or analyzer state is uncertain.
- When Apex Ray is configured in a pre-push hook, do not proactively run `apex-ray review` or `apex-ray gate pre-push` as a routine final verification step; let `git push` invoke the hook so the pre-push incremental retry state remains the source of truth.
- For deterministic local review outside pre-push, run `apex-ray review --no-llm` only when the user asks or when diagnosing Apex Ray; default reports are written under `.apex-ray/reports/`.
- When the user asks, the hook is unavailable, or explicit pre-push gate parity is needed before pushing, run `apex-ray gate pre-push`; blocking findings and critical partial coverage are printed to stdout and the full report is written under `.apex-ray/reports/`.
- Do not bypass the configured pre-push gate by default. Use `apex-ray findings suppress` only for confirmed local false positives after checking the finding evidence, current code, and relevant tests or invariants. Provide a concrete objective reason; do not suppress uncertain findings, real defects, or findings merely to get a push through.
- If bypassing is unavoidable, explain why and name the equivalent checks or review already run.
- Use `--no-llm` or `.apex-ray/config.local.yml` when the configured local provider is unavailable or LLM cost is not appropriate.
- If a report has partial coverage, continue unreviewed work with `apex-ray review --continue-from .apex-ray/reports/review.json --residual-priority p0 --llm` or review a specific skipped pack with `--only-pack`.
- Use `.apex-ray/config.yml` for shared team policy and `.apex-ray/config.local.yml` for personal provider/model/cost overrides.
- Use `.apex-ray/rules/` for stable review rules and `.apex-ray/memory/` for curated team learning.
- Use `apex-ray telemetry-summary` when tuning cost, latency, coverage, or model routing.
- Treat `.apex-ray/reports/*.md/json/html` as latest snapshots. Archived run reports live under configured local data when `review.reports.archive: true`.
- Treat `.apex-ray/triage/` as local ephemeral finding state and audit events; do not commit raw suppressions.
- Use `apex-ray eval capture-prs` and `apex-ray eval run-prs` only for historical PR benchmark/eval work.

## Outputs

Prefer writing generated review artifacts under `.apex-ray/reports/`. Keep Markdown, JSON, and HTML reports together when possible so humans and automation can inspect the same run.

## Boundaries

Do not treat Apex Ray as a replacement for tests, linters, typecheck, CI, dependency scanners, SAST, or human review. Do not commit `.apex-ray/config.local.yml`, `.apex-ray/cache/`, `.apex-ray/telemetry/`, `.apex-ray/reports/`, eval run directories, generated review artifacts, or local provider, model, API, or cost settings unless the team intentionally curates a specific artifact.

### Local Finding Triage

When a pre-push finding is a confirmed local false positive, suppress the specific finding locally instead of bypassing the hook:

```bash
apex-ray findings list --from-report .apex-ray/reports/pre-push.json
apex-ray findings suppress apex-ID \\
  --from-report .apex-ray/reports/pre-push.json \\
  --reason "The repository layer already enforces this invariant."
```

Use suppressions sparingly. Before suppressing, inspect the finding evidence, the current code, and relevant tests, invariants, or ownership assumptions. The reason must be concrete and objective enough for a later agent to audit. Do not suppress when the finding might be real, when you are unsure, or merely to get a push through.

Triage state is local and ignored by default. It is intended for frequent local review runs, not as shared team policy. A suppression applies only while the finding fingerprint and context-pack fingerprint still match; if relevant context changes, Apex Ray marks the suppression stale, prints the prior reason, and lets the finding block again. Re-check stale findings before suppressing again.

Useful cleanup commands:

```bash
apex-ray findings suppressions
apex-ray findings unsuppress sup-ID
apex-ray findings prune
```
"""

APEX_RAY_IMPROVE_SKILL_TEXT = f"""---
name: apex-ray-improve
description: Use after merged PRs or review feedback to produce recommendation-only improvements for Apex Ray memory, rules, eval labels, telemetry, coverage, model routing, or config from PR comments, Greptile findings, Apex reports, and telemetry.
apex_ray_template_version: {AGENT_ARTIFACT_TEMPLATE_VERSION}
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
    runtime_version: str | None = None,
    update_version_lock: bool = False,
) -> list[Path]:
    if runtime_version is None:
        from apex_ray import __version__

        runtime_version = __version__
    hook_command = render_uvx_command(runtime_version, "gate", "pre-push")
    if update_gitignore:
        warnings.warn(
            "update_gitignore is deprecated and no longer manages the root .gitignore; "
            "Apex Ray writes .apex-ray/.gitignore for Apex Ray local artifacts.",
            UserWarning,
            stacklevel=2,
        )
    _validate_init_options(hooks=hooks, agent_files=agent_files)
    _preflight_version_lock(root, runtime_version=runtime_version, update=update_version_lock)
    _preflight_init_targets(
        root,
        hooks=hooks,
        agent_files=agent_files,
        agent_skill=agent_skill,
        overwrite=overwrite,
        hook_command=hook_command,
    )
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
        if _write_lefthook_hook(root, root / "lefthook.yml", overwrite=overwrite, command=hook_command):
            written.append(root / "lefthook.yml")
    elif hooks == "git":
        hook_path = _write_git_pre_push_hook(root, overwrite=overwrite, command=hook_command)
        if hook_path is not None:
            written.append(hook_path)
    written.extend(
        _write_agent_files(
            root,
            agent_files=agent_files,
            agent_skill=agent_skill,
            overwrite=overwrite,
            runtime_version=runtime_version,
        )
    )
    if agent_skill and agent_files != "none":
        written.extend(
            _write_agent_skill_files(
                root,
                agent_files=agent_files,
                overwrite=overwrite,
                runtime_version=runtime_version,
            )
        )
    lock_path = ensure_version_lock(root, runtime_version=runtime_version, update=update_version_lock)
    if lock_path is not None:
        written.append(lock_path)
    return written


def refresh_agent_artifacts(
    root: Path,
    *,
    agent_files: str = "both",
    agent_skill: bool = True,
    dry_run: bool = False,
    runtime_version: str | None = None,
    enforce_version_lock: bool = True,
) -> list[Path]:
    """Refresh only Apex Ray-managed agent instruction artifacts."""
    runtime_version = _resolve_runtime_version(runtime_version)
    if enforce_version_lock:
        _preflight_version_lock(root, runtime_version=runtime_version, update=False)
    if agent_files not in AGENT_FILE_MODES:
        raise ConfigError("Unsupported agent-files value. Use none, codex, claude, or both.")
    if agent_files == "none":
        return []
    if dry_run:
        statuses = agent_artifact_statuses(
            root,
            agent_files=agent_files,
            agent_skill=agent_skill,
            include_missing=True,
            include_unmanaged=True,
            runtime_version=runtime_version,
        )
        if not agent_skill:
            statuses = [status for status in statuses if status.kind != "agent_skill"]
        return _dedupe_paths([status.path for status in statuses if status.needs_refresh])
    if agent_skill and agent_files in {"codex", "both"}:
        _preflight_codex_skill_aliases(root)
    if agent_skill and agent_files != "none":
        _preflight_canonical_skills(root)
    written = _write_agent_files(
        root,
        agent_files=agent_files,
        agent_skill=agent_skill,
        overwrite=True,
        runtime_version=runtime_version,
    )
    if agent_skill:
        written.extend(
            _write_agent_skill_files(
                root,
                agent_files=agent_files,
                overwrite=True,
                runtime_version=runtime_version,
            )
        )
    return _dedupe_paths(written)


def refresh_managed_artifacts(
    root: Path,
    *,
    hooks: str | None = None,
    agent_files: str = "both",
    agent_skill: bool = True,
    runtime_version: str,
    update_version_lock: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
) -> list[Path]:
    """Synchronize the lock-derived hook and agent artifacts without rewriting project config."""
    if hooks is not None and hooks not in HOOK_MODES:
        raise ConfigError("Unsupported hooks value. Use lefthook, git, or none.")
    if agent_files not in AGENT_FILE_MODES:
        raise ConfigError("Unsupported agent-files value. Use none, codex, claude, or both.")
    _preflight_version_lock(root, runtime_version=runtime_version, update=update_version_lock)
    effective_hooks = _resolve_managed_refresh_hook_mode(
        root,
        requested=hooks,
        runtime_version=runtime_version,
    )
    _preflight_init_targets(
        root,
        hooks=effective_hooks,
        agent_files=agent_files,
        agent_skill=agent_skill,
        overwrite=overwrite,
        hook_command=render_uvx_command(runtime_version, "gate", "pre-push"),
    )
    lock_status = inspect_version_lock(root, runtime_version=runtime_version)
    hook_command = render_uvx_command(runtime_version, "gate", "pre-push")
    if dry_run:
        paths: list[Path] = []
        if lock_status.state.value != "current":
            paths.append(lock_status.path)
        if effective_hooks == "lefthook":
            hook_path = root / "lefthook.yml"
            status = _lefthook_status(root, hook_path, expected=hook_command) if hook_path.exists() else None
            if status is None or status.needs_refresh:
                paths.append(hook_path)
        elif effective_hooks == "git":
            hook_path = _git_pre_push_hook_path(root)
            status = _git_hook_status(root, hook_path, expected=hook_command) if hook_path.exists() else None
            if status is None or status.needs_refresh:
                paths.append(hook_path)
        if agent_files != "none":
            paths.extend(
                refresh_agent_artifacts(
                    root,
                    agent_files=agent_files,
                    agent_skill=agent_skill,
                    dry_run=True,
                    runtime_version=runtime_version,
                    enforce_version_lock=False,
                )
            )
        return _dedupe_paths(paths)

    written: list[Path] = []
    if effective_hooks == "lefthook":
        hook_path = root / "lefthook.yml"
        if _write_lefthook_hook(root, hook_path, overwrite=overwrite, command=hook_command):
            written.append(hook_path)
    elif effective_hooks == "git":
        hook_path = _write_git_pre_push_hook(root, overwrite=overwrite, command=hook_command)
        if hook_path is not None:
            written.append(hook_path)
    if agent_files != "none":
        written.extend(
            refresh_agent_artifacts(
                root,
                agent_files=agent_files,
                agent_skill=agent_skill,
                runtime_version=runtime_version,
                enforce_version_lock=False,
            )
        )
    lock_path = ensure_version_lock(root, runtime_version=runtime_version, update=update_version_lock)
    if lock_path is not None:
        written.append(lock_path)
    return _dedupe_paths(written)


def agent_artifact_statuses(
    root: Path,
    *,
    agent_files: str = "both",
    agent_skill: bool | None = None,
    include_missing: bool = False,
    include_unmanaged: bool = False,
    runtime_version: str | None = None,
) -> list[AgentArtifactStatus]:
    """Return local generated-agent-artifact status without modifying files."""
    if agent_files not in AGENT_FILE_MODES:
        raise ConfigError("Unsupported agent-files value. Use none, codex, claude, or both.")
    if agent_files == "none":
        return []
    statuses: list[AgentArtifactStatus] = []
    seen: set[Path] = set()
    for path in _agent_file_status_targets(root, agent_files=agent_files, include_missing=include_missing):
        status = _agent_file_status(
            root,
            path,
            agent_skill=agent_skill,
            include_missing=include_missing,
            runtime_version=runtime_version,
        )
        if status is None or (status.status == "unmanaged" and not include_unmanaged):
            continue
        resolved = _status_identity(root, status.path)
        if resolved in seen:
            continue
        seen.add(resolved)
        statuses.append(status)
    for path, skill_name, expected, layout in _agent_skill_status_targets(
        root,
        agent_files=agent_files,
        include_missing=include_missing,
        runtime_version=runtime_version,
    ):
        status = _agent_skill_status(
            root,
            path,
            skill_name=skill_name,
            expected=expected,
            include_missing=include_missing,
            layout=layout,
        )
        if status is not None:
            statuses.append(status)
    return statuses


def agent_artifact_refresh_warning(root: Path) -> str | None:
    stale = [status for status in agent_artifact_statuses(root) if status.status in {"missing", "outdated"}]
    if not stale:
        return None
    paths = ", ".join(str(status.path.relative_to(root)) for status in stale[:5])
    suffix = "" if len(stale) <= 5 else f", and {len(stale) - 5} more"
    runtime_version = _resolve_runtime_version(None)
    lock_status = inspect_version_lock(root, runtime_version=runtime_version)
    target_version = lock_status.locked_version or runtime_version
    return (
        f"Apex Ray agent artifacts are outdated: {paths}{suffix}. "
        f"Run `{render_uvx_command(target_version, 'init', '--refresh-agent-artifacts')}` "
        "to update managed AGENTS/CLAUDE blocks and skills."
    )


def _validate_init_options(*, hooks: str, agent_files: str) -> None:
    if hooks not in HOOK_MODES:
        raise ConfigError("Unsupported hooks value. Use lefthook, git, or none.")
    if agent_files not in AGENT_FILE_MODES:
        raise ConfigError("Unsupported agent-files value. Use none, codex, claude, or both.")


def _preflight_version_lock(root: Path, *, runtime_version: str, update: bool) -> None:
    status = inspect_version_lock(root, runtime_version=runtime_version)
    if status.state.value in {"missing", "current"}:
        return
    if update:
        validate_version_lock_target(root)
        return
    assert_version_lock(root, runtime_version=runtime_version)


def _resolve_managed_refresh_hook_mode(
    root: Path,
    *,
    requested: str | None,
    runtime_version: str,
) -> str:
    statuses = managed_hook_statuses(root, runtime_version=runtime_version)
    existing_modes = {status.kind for status in statuses}
    if len(existing_modes) > 1:
        joined = ", ".join(sorted(existing_modes))
        raise ConfigError(
            f"Repository contains multiple Apex Ray hook modes ({joined}). "
            "Remove one hook mechanism before refreshing managed artifacts."
        )
    if existing_modes:
        existing = next(iter(existing_modes))
        if requested == "none":
            raise ConfigError(
                f"Repository already contains an Apex Ray {existing} hook. "
                "Refresh that hook or remove it before using --hooks none."
            )
        if requested is not None and requested != existing:
            raise ConfigError(
                f"Repository already uses the Apex Ray {existing} hook mode. "
                "Omit --hooks to preserve it, or migrate hook modes manually before refreshing."
            )
        return existing
    if requested is not None:
        return requested
    if git.is_git_repo(root):
        try:
            git_hook = _git_pre_push_hook_path(root)
        except ConfigError:
            git_hook = None
        if git_hook is not None and git_hook.exists():
            return "git"
    if (root / "lefthook.yml").exists():
        return "lefthook"
    return "lefthook"


def _preflight_init_targets(
    root: Path,
    *,
    hooks: str,
    agent_files: str,
    agent_skill: bool,
    overwrite: bool,
    hook_command: str,
) -> None:
    if hooks == "lefthook":
        _validate_lefthook_target(
            root,
            root / "lefthook.yml",
            overwrite=overwrite,
            expected_command=hook_command,
        )
    elif hooks == "git":
        _validate_git_hook_target(root, overwrite=overwrite)
    if agent_files in {"codex", "both"} and (root / "AGENTS.md").is_symlink():
        _safe_repo_symlink_target(root, root / "AGENTS.md")
    if agent_files in {"claude", "both"}:
        for candidate in (root / "CLAUDE.md", root / ".claude" / "CLAUDE.md"):
            if candidate.is_symlink():
                _safe_repo_symlink_target(root, candidate)
    if agent_skill and agent_files in {"codex", "both"}:
        _preflight_codex_skill_aliases(root)
    if agent_skill and agent_files != "none":
        _preflight_canonical_skills(root)


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
    _validate_risk_rules(config, validation_path)
    _validate_reviewers(config, validation_path)
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
            "risk",
            "analyzer",
            "context",
            "llm",
            "reviewers",
            "telemetry",
            "reports",
            "triage",
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
        "risk": review.get("risk", {}),
        "analyzer": review.get("analyzer", {}),
        "context": review.get("context", {}),
        "llm": review.get("llm", {}),
        "reviewers": review.get("reviewers", []),
        "telemetry": review.get("telemetry", {}),
        "reports": review.get("reports", {}),
        "triage": review.get("triage", {}),
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


def _validate_risk_rules(config: ReviewConfig, config_path: Path) -> None:
    seen: set[str] = set()
    for rule in config.risk.rules:
        if rule.id in seen:
            raise ConfigError(f"Invalid config in {config_path}: duplicate risk rule id '{rule.id}'")
        seen.add(rule.id)


def _validate_reviewers(config: ReviewConfig, config_path: Path) -> None:
    profiles = set(config.llm.profiles)
    seen: set[str] = set()
    for reviewer in config.reviewers:
        if reviewer.id in seen:
            raise ConfigError(f"Invalid config in {config_path}: duplicate reviewer id '{reviewer.id}'")
        seen.add(reviewer.id)
        for field in ("profile", "verify_profile"):
            profile = getattr(reviewer, field)
            if profile and profile not in profiles:
                raise ConfigError(
                    f"Invalid config in {config_path}: review.reviewers[{reviewer.id}].{field} "
                    f"references unknown profile '{profile}'"
                )


def _write_if_missing_or_overwrite(path: Path, text: str, *, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _agent_file_status_targets(root: Path, *, agent_files: str, include_missing: bool) -> list[Path]:
    targets: list[Path] = []
    agents_path = root / "AGENTS.md"
    if agent_files in {"codex", "both"} and (include_missing or agents_path.exists() or agents_path.is_symlink()):
        targets.append(agents_path)
    if agent_files in {"claude", "both"}:
        root_claude_file = root / "CLAUDE.md"
        claude_file = root / ".claude" / "CLAUDE.md"
        root_claude_exists = root_claude_file.exists() or root_claude_file.is_symlink()
        nested_claude_exists = claude_file.exists() or claude_file.is_symlink()
        if root_claude_exists:
            targets.append(root_claude_file)
        if nested_claude_exists:
            targets.append(claude_file)
        if include_missing and not root_claude_exists and not nested_claude_exists:
            targets.append(claude_file)
    return targets


def _agent_file_status(
    root: Path,
    path: Path,
    *,
    agent_skill: bool | None,
    include_missing: bool,
    runtime_version: str | None,
) -> AgentArtifactStatus | None:
    if not path.exists() and not path.is_symlink():
        return AgentArtifactStatus(path, "agent_file", "missing", "file does not exist") if include_missing else None
    read_path = _safe_repo_symlink_target(root, path) if path.is_symlink() else path
    text = read_path.read_text(encoding="utf-8")
    block = _extract_agent_block(text)
    if block is None:
        return AgentArtifactStatus(path, "agent_file", "unmanaged", "Apex Ray managed block not found")
    expected = _agent_block(
        agent_skill=_detect_agent_skill_from_block(block) if agent_skill is None else agent_skill,
        runtime_version=runtime_version,
    )
    if _normalize_artifact_text(block) == _normalize_artifact_text(expected):
        return AgentArtifactStatus(path, "agent_file", "current")
    return AgentArtifactStatus(path, "agent_file", "outdated", "managed block differs from current template")


def _agent_skill_status_targets(
    root: Path,
    *,
    agent_files: str,
    include_missing: bool,
    runtime_version: str | None,
) -> list[tuple[Path, str, str, str]]:
    targets: list[tuple[Path, str, str, str]] = []
    codex_skills_expected = include_missing or _codex_skills_are_managed(root)
    for skill_name, skill_text in _agent_skill_templates(runtime_version):
        canonical = root / ".apex-ray" / "skills" / skill_name / "SKILL.md"
        if include_missing or canonical.exists() or canonical.is_symlink():
            targets.append((canonical, skill_name, skill_text, "file"))
        codex = _codex_skill_alias_path(root, skill_name)
        if agent_files in {"codex", "both"} and (codex_skills_expected or codex.exists() or codex.is_symlink()):
            targets.append((codex, skill_name, skill_text, "codex_directory"))
        claude = root / ".claude" / "skills" / skill_name / "SKILL.md"
        if agent_files in {"claude", "both"} and (include_missing or claude.exists() or claude.is_symlink()):
            targets.append((claude, skill_name, skill_text, "file"))
    return targets


def _agent_skill_status(
    root: Path,
    path: Path,
    *,
    skill_name: str,
    expected: str,
    include_missing: bool,
    layout: str = "file",
) -> AgentArtifactStatus | None:
    try:
        _safe_repo_write_path(root, path)
    except (ConfigError, OSError) as exc:
        return AgentArtifactStatus(path, "agent_skill", "outdated", str(exc))
    if layout == "codex_directory":
        return _codex_skill_directory_status(
            root,
            path,
            skill_name=skill_name,
            expected=expected,
            include_missing=include_missing,
        )
    if not path.exists() and not path.is_symlink():
        return AgentArtifactStatus(path, "agent_skill", "missing", "file does not exist") if include_missing else None
    canonical = (root / ".apex-ray" / "skills" / skill_name / "SKILL.md").resolve(strict=False)
    if path.is_symlink():
        try:
            target = _safe_repo_symlink_target(root, path)
        except OSError:
            return AgentArtifactStatus(path, "agent_skill", "outdated", "symlink target cannot be resolved")
        if target.resolve(strict=False) == canonical:
            canonical_status = _agent_skill_status(
                root,
                canonical,
                skill_name=skill_name,
                expected=expected,
                include_missing=True,
            )
            if canonical_status is not None and canonical_status.status == "current":
                return AgentArtifactStatus(path, "agent_skill", "current")
            return AgentArtifactStatus(path, "agent_skill", "outdated", "canonical skill is not current")
        return AgentArtifactStatus(path, "agent_skill", "outdated", "symlink does not point to canonical skill")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return AgentArtifactStatus(path, "agent_skill", "outdated", f"unable to read skill file: {exc}")
    if _normalize_artifact_text(text) == _normalize_artifact_text(expected):
        return AgentArtifactStatus(path, "agent_skill", "current")
    return AgentArtifactStatus(path, "agent_skill", "outdated", "skill file differs from current template")


def _codex_skills_are_managed(root: Path) -> bool:
    for skill_name in ("apex-ray", "apex-ray-improve"):
        alias = _codex_skill_alias_path(root, skill_name)
        if alias.exists() or alias.is_symlink():
            return True
    agents_path = root / "AGENTS.md"
    if not agents_path.exists() and not agents_path.is_symlink():
        return False
    read_path = _safe_repo_symlink_target(root, agents_path) if agents_path.is_symlink() else agents_path
    block = _extract_agent_block(read_path.read_text(encoding="utf-8"))
    return block is not None and _detect_agent_skill_from_block(block)


def _codex_skill_directory_status(
    root: Path,
    path: Path,
    *,
    skill_name: str,
    expected: str,
    include_missing: bool,
) -> AgentArtifactStatus | None:
    try:
        _safe_repo_write_path(root, path)
    except (ConfigError, OSError) as exc:
        return AgentArtifactStatus(path, "agent_skill", "outdated", str(exc))
    if not path.exists() and not path.is_symlink():
        return (
            AgentArtifactStatus(path, "agent_skill", "missing", "skill directory does not exist")
            if include_missing or _codex_skills_are_managed(root)
            else None
        )
    canonical_directory = root / ".apex-ray" / "skills" / skill_name
    if path.is_symlink():
        try:
            target = _safe_repo_symlink_target(root, path)
        except (ConfigError, OSError) as exc:
            return AgentArtifactStatus(path, "agent_skill", "outdated", str(exc))
        if target.resolve(strict=False) != canonical_directory.resolve(strict=False):
            return AgentArtifactStatus(
                path,
                "agent_skill",
                "outdated",
                "skill directory symlink does not point to the canonical skill directory",
            )
        canonical_status = _agent_skill_status(
            root,
            canonical_directory / "SKILL.md",
            skill_name=skill_name,
            expected=expected,
            include_missing=True,
        )
        if canonical_status is not None and canonical_status.status == "current":
            return AgentArtifactStatus(path, "agent_skill", "current")
        return AgentArtifactStatus(path, "agent_skill", "outdated", "canonical skill is not current")
    if _is_git_materialized_codex_skill_symlink(root, path, canonical_directory):
        return AgentArtifactStatus(
            path,
            "agent_skill",
            "outdated",
            "Git materialized the Codex skill directory symlink as a regular file",
        )
    if not path.is_dir():
        return AgentArtifactStatus(path, "agent_skill", "outdated", "Codex skill alias is not a directory")
    skill_path = path / "SKILL.md"
    if skill_path.is_symlink():
        try:
            target = _safe_repo_symlink_target(root, skill_path)
        except (ConfigError, OSError) as exc:
            return AgentArtifactStatus(path, "agent_skill", "outdated", str(exc))
        canonical_skill = canonical_directory / "SKILL.md"
        if target.resolve(strict=False) == canonical_skill.resolve(strict=False):
            return AgentArtifactStatus(
                path,
                "agent_skill",
                "outdated",
                "legacy file-level SKILL.md symlink must be migrated to a skill-directory alias",
            )
        return AgentArtifactStatus(
            path,
            "agent_skill",
            "outdated",
            "SKILL.md symlink does not point to the canonical skill",
        )
    if not skill_path.is_file():
        return AgentArtifactStatus(path, "agent_skill", "outdated", "copied skill directory has no SKILL.md file")
    canonical_status = _agent_skill_status(
        root,
        canonical_directory / "SKILL.md",
        skill_name=skill_name,
        expected=expected,
        include_missing=True,
    )
    if (
        canonical_status is not None
        and canonical_status.status == "current"
        and _skill_directories_match(canonical_directory, path)
    ):
        return AgentArtifactStatus(path, "agent_skill", "current")
    if _is_managed_skill_copy(path, skill_name):
        return AgentArtifactStatus(path, "agent_skill", "outdated", "copied skill directory is not current")
    return AgentArtifactStatus(path, "agent_skill", "outdated", "conflicting unmanaged Codex skill directory")


def _extract_agent_block(text: str) -> str | None:
    start = APEX_RAY_AGENT_BLOCK_START
    end = APEX_RAY_AGENT_BLOCK_END
    if start not in text or end not in text:
        return None
    before, remainder = text.split(start, 1)
    if end not in remainder:
        return None
    block_body, _after = remainder.split(end, 1)
    return f"{start}{block_body}{end}\n" if before or text.endswith("\n") else f"{start}{block_body}{end}"


def _detect_agent_skill_from_block(block: str) -> bool:
    return APEX_RAY_SKILL_TOKEN_RE.search(block) is not None


def _normalize_artifact_text(text: str) -> str:
    return text.strip().replace("\r\n", "\n") + "\n"


def _status_identity(root: Path, path: Path) -> Path:
    if path.is_symlink():
        try:
            return _safe_repo_symlink_target(root, path).resolve(strict=False)
        except OSError:
            return path.resolve(strict=False)
    return path.resolve(strict=False)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        key = path.resolve(strict=False)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


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


def _write_lefthook_hook(root: Path, path: Path, *, overwrite: bool, command: str) -> bool:
    write_path = _safe_repo_write_path(root, path)
    raw = _read_setup_text(path) if path.exists() else ""
    _validate_lefthook_text(path, raw, overwrite=overwrite, expected_command=command)
    data = _load_lefthook_data(path, raw)
    data.setdefault("no_tty", True)
    pre_push = data.setdefault("pre-push", {})
    if not isinstance(pre_push, dict):
        raise ConfigError(f"Invalid Lefthook config in {path}: pre-push must be a mapping")
    pre_push.setdefault("follow", True)
    commands = pre_push.setdefault("commands", {})
    if not isinstance(commands, dict):
        raise ConfigError(f"Invalid Lefthook config in {path}: pre-push.commands must be a mapping")
    existing = commands.get("apex-ray-review")
    if existing is not None:
        actual = existing.get("run") if isinstance(existing, dict) else None
        if actual == command:
            return False
        state = _managed_gate_command_state(actual, expected=command)
        if state == "unmanaged" and not overwrite:
            raise ConfigError(
                f"Lefthook command apex-ray-review in {path} is not an Apex Ray-managed gate command. "
                "Move or rename it, or rerun with --force to replace that command."
            )
        if isinstance(actual, str) and raw:
            updated = _replace_lefthook_run_scalar(path, raw, command)
            if updated is not None:
                write_path.write_text(updated, encoding="utf-8")
                return True
    commands["apex-ray-review"] = {"run": command}
    write_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return True


def _validate_lefthook_target(
    root: Path,
    path: Path,
    *,
    overwrite: bool,
    expected_command: str,
) -> None:
    _safe_repo_write_path(root, path)
    raw = _read_setup_text(path) if path.exists() else ""
    _validate_lefthook_text(path, raw, overwrite=overwrite, expected_command=expected_command)


def _read_setup_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Unable to read repository setup file {path}: {exc}") from exc


def _validate_lefthook_text(
    path: Path,
    raw: str,
    *,
    overwrite: bool,
    expected_command: str,
) -> None:
    if not raw.strip():
        return
    data = _load_lefthook_data(path, raw)
    commands = _lefthook_commands(path, data)
    entries = _lefthook_pre_push_entries(path, data)
    conflicting = [
        label
        for label, entry in entries
        if label != "commands.apex-ray-review" and _looks_like_apex_ray_hook(label, entry)
    ]
    if conflicting:
        joined = ", ".join(conflicting)
        raise ConfigError(
            f"Lefthook config at {path} already contains another Apex Ray hook ({joined}). "
            "Migrate it manually to the managed apex-ray-review command before refreshing."
        )
    if "apex-ray-review" not in commands and not overwrite:
        raise ConfigError(
            f"Lefthook config already exists at {path}. "
            "Add the apex-ray-review command manually, use --hooks none, or rerun with --force if YAML "
            "formatting/comments can be rewritten."
        )
    managed_entry = commands.get("apex-ray-review")
    actual = managed_entry.get("run") if isinstance(managed_entry, dict) else None
    if isinstance(actual, str) and actual != expected_command:
        _assert_lefthook_run_replacement_safe(path, raw)


def _load_lefthook_data(path: Path, raw: str) -> dict[str, Any]:
    try:
        document = yaml.compose(raw) if raw.strip() else None
        data = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    duplicate_key = _find_duplicate_yaml_mapping_key(document)
    if duplicate_key is not None:
        raise ConfigError(f"Invalid Lefthook config in {path}: duplicate mapping key {duplicate_key!r}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Invalid Lefthook config in {path}: expected a mapping")
    return data


def _lefthook_commands(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    pre_push = data.get("pre-push", {})
    if pre_push is None:
        return {}
    if not isinstance(pre_push, dict):
        raise ConfigError(f"Invalid Lefthook config in {path}: pre-push must be a mapping")
    commands = pre_push.get("commands", {})
    if commands is None:
        return {}
    if not isinstance(commands, dict):
        raise ConfigError(f"Invalid Lefthook config in {path}: pre-push.commands must be a mapping")
    return commands


def _lefthook_pre_push_entries(path: Path, data: dict[str, Any]) -> list[tuple[str, Any]]:
    pre_push = data.get("pre-push", {})
    if pre_push is None:
        return []
    if not isinstance(pre_push, dict):
        raise ConfigError(f"Invalid Lefthook config in {path}: pre-push must be a mapping")
    entries: list[tuple[str, Any]] = []
    for section in ("commands", "scripts"):
        values = pre_push.get(section, {})
        if values is None:
            continue
        if not isinstance(values, dict):
            raise ConfigError(f"Invalid Lefthook config in {path}: pre-push.{section} must be a mapping")
        entries.extend((f"{section}.{name}", entry) for name, entry in values.items())
    return entries


def _looks_like_apex_ray_hook(name: str, entry: Any) -> bool:
    if "apex-ray" in name.lower():
        return True
    if isinstance(entry, str):
        return "apex-ray" in entry.lower()
    if not isinstance(entry, dict):
        return False
    return any(isinstance(entry.get(field), str) and "apex-ray" in entry[field].lower() for field in ("run", "runner"))


def _managed_gate_command_state(command: Any, *, expected: str) -> str:
    if command == expected:
        return "current"
    if command == "apex-ray gate pre-push":
        return "outdated"
    if not isinstance(command, str):
        return "unmanaged"
    try:
        parts = shlex.split(command)
    except ValueError:
        return "unmanaged"
    if (
        len(parts) == 6
        and parts[:3] == ["uvx", "--python", "3.14"]
        and parts[3].startswith("apex-ray@")
        and parts[4:] == ["gate", "pre-push"]
    ):
        return "outdated"
    return "unmanaged"


def _contains_non_lefthook_apex_ray_reference(body: str) -> bool:
    if "apex-ray" not in body.lower():
        return False
    lexer = shlex.shlex(body, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return True
    for token in tokens:
        if "apex-ray" not in token.lower():
            continue
        executable_name = token.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if executable_name in _LEFTHOOK_EXECUTABLE_NAMES:
            continue
        return True
    return False


def _replace_lefthook_run_scalar(path: Path, raw: str, command: str) -> str | None:
    try:
        document = yaml.compose(raw)
    except yaml.YAMLError:
        return None
    node = _managed_lefthook_run_node(document)
    if not isinstance(node, ScalarNode):
        return None
    if _yaml_node_reference_count(document, node) > 1:
        raise ConfigError(
            f"Lefthook managed command in {path} uses a shared YAML anchor/alias. "
            "Replace the alias with a standalone run value before refreshing."
        )
    rendered = yaml.safe_dump(command, default_style='"').splitlines()[0]
    return f"{raw[: node.start_mark.index]}{rendered}{raw[node.end_mark.index :]}"


def _assert_lefthook_run_replacement_safe(path: Path, raw: str) -> None:
    try:
        document = yaml.compose(raw)
    except yaml.YAMLError:
        return
    node = _managed_lefthook_run_node(document)
    if node is not None and _yaml_node_reference_count(document, node) > 1:
        raise ConfigError(
            f"Lefthook managed command in {path} uses a shared YAML anchor/alias. "
            "Replace the alias with a standalone run value before refreshing."
        )


def _managed_lefthook_run_node(document: Node | None) -> Node | None:
    node = document
    for key in ("pre-push", "commands", "apex-ray-review", "run"):
        node = _yaml_mapping_value(node, key)
        if node is None:
            return None
    return node


def _yaml_node_reference_count(document: Node | None, target: Node) -> int:
    count = 0
    expanded: set[int] = set()
    pending = [document] if document is not None else []
    while pending:
        node = pending.pop()
        if node is target:
            count += 1
        identity = id(node)
        if identity in expanded:
            continue
        expanded.add(identity)
        if isinstance(node, MappingNode):
            for key_node, value_node in node.value:
                pending.extend((key_node, value_node))
        elif isinstance(node, SequenceNode):
            pending.extend(node.value)
    return count


def _find_duplicate_yaml_mapping_key(document: Node | None) -> str | None:
    expanded: set[int] = set()
    pending = [document] if document is not None else []
    while pending:
        node = pending.pop()
        identity = id(node)
        if identity in expanded:
            continue
        expanded.add(identity)
        if isinstance(node, MappingNode):
            seen: set[tuple[str, str]] = set()
            for key_node, value_node in node.value:
                if isinstance(key_node, ScalarNode):
                    key_identity = (key_node.tag, key_node.value)
                    if key_identity in seen:
                        return key_node.value
                    seen.add(key_identity)
                pending.extend((key_node, value_node))
        elif isinstance(node, SequenceNode):
            pending.extend(node.value)
    return None


def _yaml_mapping_value(node: Node | None, key: str) -> Node | None:
    if not isinstance(node, MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return value_node
    return None


def _write_git_pre_push_hook(root: Path, *, overwrite: bool, command: str) -> Path | None:
    hook = _git_pre_push_hook_path(root)
    _validate_git_hook_write_path(root, hook)
    body = _managed_git_hook_body(command)
    hook.parent.mkdir(parents=True, exist_ok=True)
    if hook.exists():
        existing = hook.read_text(encoding="utf-8", errors="ignore")
        if existing == body:
            return None
        if not _is_managed_git_hook(existing) and existing != LEGACY_GIT_HOOK_BODY and not overwrite:
            raise ConfigError("Git pre-push hook already exists. Use --force to replace it or --hooks lefthook.")
    hook.write_text(body, encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _validate_git_hook_target(root: Path, *, overwrite: bool) -> None:
    hook = _git_pre_push_hook_path(root)
    _validate_git_hook_write_path(root, hook)
    if hook.exists():
        existing = hook.read_text(encoding="utf-8", errors="ignore")
        if not _is_managed_git_hook(existing) and existing != LEGACY_GIT_HOOK_BODY and not overwrite:
            raise ConfigError("Git pre-push hook already exists. Use --force to replace it or --hooks lefthook.")


def _managed_git_hook_body(command: str) -> str:
    return f"#!/bin/sh\nset -eu\n{MANAGED_GIT_HOOK_MARKER}\n{command}\n"


def _is_managed_git_hook(body: str) -> bool:
    lines = body.splitlines()
    return len(lines) == 4 and lines[:3] == ["#!/bin/sh", "set -eu", MANAGED_GIT_HOOK_MARKER]


def _git_pre_push_hook_path(root: Path) -> Path:
    if not git.is_git_repo(root):
        raise ConfigError("Direct git hook setup requires a git repository. Use --hooks lefthook or --hooks none.")
    hook_proc = git.run_git(["rev-parse", "--git-path", "hooks/pre-push"], cwd=root, check=False)
    if hook_proc.returncode != 0 or not hook_proc.stdout.strip():
        raise ConfigError("Unable to resolve git pre-push hook path. Use --hooks lefthook or --hooks none.")
    return (root / hook_proc.stdout.strip()).resolve()


def _validate_git_hook_write_path(root: Path, hook: Path) -> None:
    resolved_hook = hook.resolve(strict=False)
    resolved_root = root.resolve()
    common_dir = git.common_dir(root)
    if resolved_hook.is_relative_to(resolved_root):
        return
    if common_dir is not None and resolved_hook.is_relative_to(common_dir.resolve()):
        return
    raise ConfigError(
        f"Git pre-push hook path is outside the repository or its Git common directory: {hook}. "
        "Use --hooks lefthook or configure a repository-local core.hooksPath."
    )


def managed_hook_statuses(root: Path, *, runtime_version: str) -> list[ManagedHookStatus]:
    expected = render_uvx_command(runtime_version, "gate", "pre-push")
    statuses: list[ManagedHookStatus] = []
    lefthook_path = root / "lefthook.yml"
    if lefthook_path.exists():
        status = _lefthook_status(root, lefthook_path, expected=expected)
        if status is not None:
            statuses.append(status)
    if git.is_git_repo(root):
        try:
            git_hook = _git_pre_push_hook_path(root)
        except ConfigError:
            git_hook = None
        if git_hook is not None and git_hook.exists():
            status = _git_hook_status(root, git_hook, expected=expected)
            if status is not None:
                statuses.append(status)
    return statuses


def _lefthook_status(root: Path, path: Path, *, expected: str) -> ManagedHookStatus | None:
    try:
        _safe_repo_write_path(root, path)
        raw = _read_setup_text(path)
        data = _load_lefthook_data(path, raw)
        commands = _lefthook_commands(path, data)
        entries = _lefthook_pre_push_entries(path, data)
    except (ConfigError, OSError, UnicodeError) as exc:
        return ManagedHookStatus(path, "lefthook", "invalid", expected, reason=str(exc))
    conflicts = [
        label
        for label, entry in entries
        if label != "commands.apex-ray-review" and _looks_like_apex_ray_hook(label, entry)
    ]
    if conflicts:
        return ManagedHookStatus(
            path,
            "lefthook",
            "unmanaged",
            expected,
            reason=f"another Apex Ray hook is configured: {', '.join(conflicts)}",
        )
    entry = commands.get("apex-ray-review")
    if entry is None:
        return None
    actual = entry.get("run") if isinstance(entry, dict) else None
    state = _managed_gate_command_state(actual, expected=expected)
    return ManagedHookStatus(
        path,
        "lefthook",
        state,
        expected,
        actual_command=actual if isinstance(actual, str) else None,
        reason="managed command does not match the repository version lock" if state == "outdated" else "",
    )


def _git_hook_status(root: Path, path: Path, *, expected: str) -> ManagedHookStatus | None:
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return ManagedHookStatus(path, "git", "invalid", expected, reason=str(exc))
    expected_body = _managed_git_hook_body(expected)
    has_apex_ray_reference = _contains_non_lefthook_apex_ray_reference(body)
    if (
        body != expected_body
        and body != LEGACY_GIT_HOOK_BODY
        and not _is_managed_git_hook(body)
        and not has_apex_ray_reference
    ):
        return None
    try:
        _validate_git_hook_write_path(root, path)
    except ConfigError as exc:
        return ManagedHookStatus(path, "git", "invalid", expected, reason=str(exc))
    if body == expected_body:
        return ManagedHookStatus(path, "git", "current", expected, actual_command=expected)
    if body == LEGACY_GIT_HOOK_BODY:
        return ManagedHookStatus(
            path,
            "git",
            "outdated",
            expected,
            actual_command="apex-ray gate pre-push",
            reason="legacy generated hook is not version-pinned",
        )
    if _is_managed_git_hook(body):
        actual = body.splitlines()[-1]
        state = _managed_gate_command_state(actual, expected=expected)
        return ManagedHookStatus(
            path,
            "git",
            state,
            expected,
            actual_command=actual,
            reason="managed command does not match the repository version lock" if state == "outdated" else "",
        )
    if has_apex_ray_reference:
        return ManagedHookStatus(
            path,
            "git",
            "unmanaged",
            expected,
            reason="custom Git hook invokes Apex Ray and cannot be synchronized automatically",
        )
    return None


def _write_agent_files(
    root: Path,
    *,
    agent_files: str,
    agent_skill: bool,
    overwrite: bool,
    runtime_version: str | None,
) -> list[Path]:
    written: list[Path] = []
    agents_path = root / "AGENTS.md"
    if agent_files in {"codex", "both"}:
        written_path = _append_agent_block(
            root,
            agents_path,
            agent_skill=agent_skill,
            overwrite=overwrite,
            runtime_version=runtime_version,
        )
        if written_path is not None:
            written.append(written_path)
    if agent_files in {"claude", "both"}:
        root_claude_file = root / "CLAUDE.md"
        if root_claude_file.exists() or root_claude_file.is_symlink():
            written_path = _append_agent_block(
                root,
                root_claude_file,
                agent_skill=agent_skill,
                overwrite=overwrite,
                runtime_version=runtime_version,
            )
            if written_path is not None:
                written.append(written_path)
            return written
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        claude_file = claude_dir / "CLAUDE.md"
        if claude_file.exists() or claude_file.is_symlink():
            written_path = _append_agent_block(
                root,
                claude_file,
                agent_skill=agent_skill,
                overwrite=overwrite,
                runtime_version=runtime_version,
            )
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
        if _append_marked_block(
            claude_file,
            _agent_block(agent_skill=agent_skill, runtime_version=runtime_version),
            overwrite=overwrite,
        ):
            written.append(claude_file)
    return written


def _append_agent_block(
    root: Path,
    path: Path,
    *,
    agent_skill: bool,
    overwrite: bool,
    runtime_version: str | None,
) -> Path | None:
    block = _agent_block(agent_skill=agent_skill, runtime_version=runtime_version)
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


def _agent_block(*, agent_skill: bool, runtime_version: str | None) -> str:
    template = APEX_RAY_AGENT_BLOCK if agent_skill else APEX_RAY_AGENT_BLOCK_NO_SKILL
    return _render_agent_artifact_text(template, runtime_version)


def _agent_skill_templates(runtime_version: str | None) -> tuple[tuple[str, str], ...]:
    return (
        ("apex-ray", _render_agent_artifact_text(APEX_RAY_SKILL_TEXT, runtime_version)),
        ("apex-ray-improve", _render_agent_artifact_text(APEX_RAY_IMPROVE_SKILL_TEXT, runtime_version)),
    )


def _render_agent_artifact_text(template: str, runtime_version: str | None) -> str:
    runtime_version = _resolve_runtime_version(runtime_version)
    launcher = render_uvx_command(runtime_version)
    return APEX_RAY_CLI_COMMAND_RE.sub(launcher, template)


def _resolve_runtime_version(runtime_version: str | None) -> str:
    if runtime_version is not None:
        return runtime_version
    from apex_ray import __version__

    return __version__


def _write_agent_skill_files(
    root: Path,
    *,
    agent_files: str,
    overwrite: bool,
    runtime_version: str | None,
) -> list[Path]:
    if agent_files not in {"codex", "claude", "both"}:
        raise ConfigError("Unsupported agent-files value. Use none, codex, claude, or both.")
    written: list[Path] = []
    for skill_name, skill_text in _agent_skill_templates(runtime_version):
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
    if agent_files in {"codex", "both"} and _write_codex_skill_alias(
        root,
        _codex_skill_alias_path(root, skill_name),
        canonical.parent,
        skill_name,
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
    return root / CODEX_REPO_SKILL_DIR / "skills" / skill_name


def _preflight_codex_skill_aliases(root: Path) -> None:
    for skill_name in ("apex-ray", "apex-ray-improve"):
        path = _codex_skill_alias_path(root, skill_name)
        _safe_repo_write_path(root, path)
        if not path.exists() and not path.is_symlink():
            continue
        canonical_directory = root / ".apex-ray" / "skills" / skill_name
        if path.is_symlink():
            target = _safe_repo_symlink_target(root, path)
            if target.resolve(strict=False) != canonical_directory.resolve(strict=False):
                raise ConfigError(
                    f"Conflicting Codex skill directory at {path}: symlink does not point to {canonical_directory}."
                )
            continue
        if _is_git_materialized_codex_skill_symlink(root, path, canonical_directory):
            continue
        if not path.is_dir():
            raise ConfigError(f"Conflicting Codex skill directory at {path}: expected a directory or symlink.")
        skill_path = path / "SKILL.md"
        if skill_path.is_symlink():
            target = _safe_repo_symlink_target(root, skill_path)
            canonical_skill = canonical_directory / "SKILL.md"
            if target.resolve(strict=False) != canonical_skill.resolve(strict=False):
                raise ConfigError(
                    f"Conflicting Codex skill directory at {path}: SKILL.md symlink does not point to "
                    f"{canonical_skill}."
                )
            continue
        if not _is_managed_skill_copy(path, skill_name):
            raise ConfigError(
                f"Conflicting Codex skill directory at {path}. Move or rename it before refreshing Apex Ray "
                "agent artifacts."
            )


def _preflight_canonical_skills(root: Path) -> None:
    for skill_name in ("apex-ray", "apex-ray-improve"):
        canonical_directory = root / ".apex-ray" / "skills" / skill_name
        _safe_repo_write_path(root, canonical_directory)
        _safe_repo_write_path(root, canonical_directory / "SKILL.md")
        if canonical_directory.is_dir() and not canonical_directory.is_symlink():
            for child in canonical_directory.rglob("*"):
                if child.is_symlink():
                    raise ConfigError(f"Canonical Apex Ray skill directory contains a symlink: {child}.")
                _safe_repo_write_path(root, child)


def _write_codex_skill_alias(
    root: Path,
    path: Path,
    canonical_directory: Path,
    skill_name: str,
    *,
    overwrite: bool,
) -> bool:
    _safe_repo_write_path(root, path)
    if path.is_symlink():
        target = _safe_repo_symlink_target(root, path)
        if target.resolve(strict=False) != canonical_directory.resolve(strict=False):
            raise ConfigError(
                f"Conflicting Codex skill directory at {path}: symlink does not point to {canonical_directory}."
            )
        return False
    if _is_git_materialized_codex_skill_symlink(root, path, canonical_directory):
        if not overwrite:
            return False
        path.unlink()
        _create_codex_skill_alias(root, path, canonical_directory)
        return True
    if path.exists():
        if not path.is_dir():
            raise ConfigError(f"Conflicting Codex skill directory at {path}: expected a directory or symlink.")
        legacy_skill_link = path / "SKILL.md"
        if legacy_skill_link.is_symlink():
            target = _safe_repo_symlink_target(root, legacy_skill_link)
            if target.resolve(strict=False) != (canonical_directory / "SKILL.md").resolve(strict=False):
                raise ConfigError(
                    f"Conflicting Codex skill directory at {path}: SKILL.md symlink does not point to the "
                    "canonical skill."
                )
            if not overwrite:
                return False
            extra_entries = [entry for entry in path.iterdir() if entry.name != "SKILL.md"]
            legacy_skill_link.unlink()
            if extra_entries:
                _copy_skill_directory(root, canonical_directory, path)
            else:
                path.rmdir()
                _create_codex_skill_alias(root, path, canonical_directory)
            return True
        if _skill_directories_match(canonical_directory, path):
            return False
        if not _is_managed_skill_copy(path, skill_name):
            raise ConfigError(
                f"Conflicting Codex skill directory at {path}. Move or rename it before refreshing Apex Ray "
                "agent artifacts."
            )
        if not overwrite:
            return False
        _copy_skill_directory(root, canonical_directory, path)
        return True
    _create_codex_skill_alias(root, path, canonical_directory)
    return True


def _create_codex_skill_alias(root: Path, path: Path, canonical_directory: Path) -> None:
    _safe_repo_write_path(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(
            _relative_symlink_target(path, canonical_directory),
            target_is_directory=True,
        )
    except OSError:
        if path.exists() or path.is_symlink():
            raise
        _copy_skill_directory(root, canonical_directory, path)


def _is_git_materialized_codex_skill_symlink(root: Path, path: Path, canonical_directory: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    expected_target = _relative_symlink_target(path, canonical_directory)
    expected_targets = {expected_target.encode(), expected_target.replace("/", "\\").encode()}
    max_target_bytes = max(len(target) for target in expected_targets)
    try:
        with path.open("rb") as placeholder:
            materialized_target = placeholder.read(max_target_bytes + 1)
    except OSError:
        return False
    if materialized_target not in expected_targets:
        return False
    if not git.is_git_repo(root):
        return False
    relative_path = path.relative_to(root).as_posix()
    pathspec = f":(top,literal){relative_path}"
    index_entry = git.run_git(["ls-files", "--stage", "--", pathspec], cwd=root, check=False)
    if index_entry.returncode != 0:
        return False
    matching_entries: list[list[str]] = []
    for line in index_entry.stdout.splitlines():
        metadata, separator, tracked_path = line.partition("\t")
        if separator and tracked_path == relative_path:
            matching_entries.append(metadata.split())
    return (
        len(matching_entries) == 1
        and len(matching_entries[0]) == 3
        and matching_entries[0][0] == "120000"
        and matching_entries[0][2] == "0"
    )


def _copy_skill_directory(root: Path, source: Path, destination: Path) -> None:
    _safe_repo_write_path(root, source)
    _safe_repo_write_path(root, destination)
    for source_path in source.rglob("*"):
        if source_path.is_symlink():
            raise ConfigError(f"Canonical Apex Ray skill directory contains a symlink: {source_path}.")
        _safe_repo_write_path(root, source_path)
    if destination.exists():
        for source_path in source.rglob("*"):
            relative = source_path.relative_to(source)
            destination_path = destination / relative
            if destination_path.is_symlink():
                raise ConfigError(f"Conflicting Codex skill directory at {destination}: {relative} is a symlink.")
            if source_path.is_dir() and destination_path.exists() and not destination_path.is_dir():
                raise ConfigError(f"Conflicting Codex skill directory at {destination}: {relative} is not a directory.")
            if source_path.is_file() and destination_path.exists() and not destination_path.is_file():
                raise ConfigError(f"Conflicting Codex skill directory at {destination}: {relative} is not a file.")
    shutil.copytree(source, destination, dirs_exist_ok=True)


def _skill_directories_match(canonical: Path, copy: Path) -> bool:
    if not canonical.is_dir() or not copy.is_dir():
        return False
    for canonical_path in canonical.rglob("*"):
        if canonical_path.is_symlink():
            return False
        relative = canonical_path.relative_to(canonical)
        copy_path = copy / relative
        if canonical_path.is_dir():
            if not copy_path.is_dir() or copy_path.is_symlink():
                return False
            continue
        if not canonical_path.is_file() or not copy_path.is_file() or copy_path.is_symlink():
            return False
        if canonical_path.read_bytes() != copy_path.read_bytes():
            return False
    return True


def _is_managed_skill_copy(path: Path, skill_name: str) -> bool:
    skill_path = path / "SKILL.md"
    if not skill_path.is_file() or skill_path.is_symlink():
        return False
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        return False
    if not text.startswith("---\n"):
        return False
    try:
        frontmatter_text, _body = text[4:].split("\n---\n", 1)
        frontmatter = yaml.safe_load(frontmatter_text)
    except ValueError, yaml.YAMLError:
        return False
    return (
        isinstance(frontmatter, dict)
        and frontmatter.get("name") == skill_name
        and isinstance(frontmatter.get("apex_ray_template_version"), int)
    )


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


def _relative_symlink_target(link_path: PurePath, target: PurePath) -> str:
    return target.relative_to(link_path.parent, walk_up=True).as_posix()
