# Apex Ray Agent Guide

Repo-local rules for coding agents working on Apex Ray. Use `README.md` for user-facing usage and `docs/development.md` / `docs/architecture.md` for fuller project context.

## Purpose

Apex Ray is a local CLI-first AI code review engine for git diffs with analyzer-backed context for selected language families.

## Where Changes Go

- Python package APIs use thin `__init__.py` files that re-export from implementation modules.
- CLI commands live in `src/apex_ray/cli/`.
- Review orchestration, LLM context selection, and finding consolidation lives in `src/apex_ray/pipeline/`.
- Config, discovery, diff parsing, models, memory, rules, telemetry, and git helpers live in top-level `src/apex_ray/` modules.
- LLM provider, routing, prompt, cache, response, and usage logic lives in `src/apex_ray/llm/`.
- Context-pack construction and prompt budgeting lives in `src/apex_ray/context/`.
- Report rendering and LLM coverage summarization lives in `src/apex_ray/report/`.
- Benchmark capture/replay and comparison lives in `src/apex_ray/benchmark/`.
- Historical PR capture/replay, Greptile matching, state, storage, and telemetry lives in `src/apex_ray/pr_eval/`.
- The bundled TS/JS analyzer lives in `analyzer-runtimes/typescript/src/`, grouped by `contracts/`, `references/`, `workspace/`, `indexes/`, and `symbols/`.
- The built-in Python analyzer lives in `src/apex_ray/analyzers/python/`, grouped by symbols, bindings, calls, annotations, references, metadata, related tests, state, and workspace helpers.

Keep new modules inside the relevant package and keep package `__init__.py` files as public API re-export surfaces. Do not add new flat prefix files such as `cli_*.py`, `pipeline_*.py`, `llm_*.py`, `report_*.py`, `contract-*.ts`, or `workspace-*.ts`.

## Command Conventions

- Run repository commands from the repo root.
- Use `uv run ...` for Python tools and the local `apex-ray` console script.
- Use `npm --prefix analyzer-runtimes/typescript ...` for analyzer commands.
- Treat `.github/workflows/ci.yml` as CI parity source of truth before claiming a change is CI-ready.

## Worktrees

- For PR-sized, risky, or long-running changes, prefer `scripts/create-worktree.sh <branch-name>` from the primary checkout instead of creating worktrees manually.
- Use `scripts/create-worktree.sh --base <ref> --path <path> <branch-name>` only when a non-default base or location is needed.
- The companion `scripts/setup-worktree.sh` copies machine-local config only when the target path is git-ignored, then runs the normal source checkout setup. Do not manually copy `.apex-ray/config.local.yml`, provider settings, env files, or generated reports into tracked paths.
- Keep manual worktrees under `.worktrees/` unless there is a concrete reason to use a different ignored location.

## Verification

Run the smallest relevant check for the changed surface:

- Python code: `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyright`, and focused or full `uv run pytest -q`.
- TS analyzer code: `npm --prefix analyzer-runtimes/typescript run typecheck`, `npm --prefix analyzer-runtimes/typescript test`, and relevant Python context tests.
- CLI/config/report behavior: focused tests plus `uv run apex-ray doctor`.
- Packaging/release behavior: full CI-equivalent checks, `uv build --sdist --wheel`, `uv run twine check dist/*`, and installed-wheel smoke coverage from `.github/workflows/ci.yml`.

Before saying work is complete, report the verification that actually ran.

## Git Rules

- Commit messages and PR titles use Conventional Commits. PR titles must be suitable as squash-merge commit titles.
- Mark breaking changes with `!` and a `BREAKING CHANGE:` footer.

<!-- APEX_RAY_START -->
## Apex Ray

This project uses Apex Ray for local diff-aware review. Use the `$apex-ray` skill for review, gate, report, telemetry, and eval workflows. Apex Ray runs that use LLM analysis can be long-running and may appear idle; do not interrupt or kill the process just because it takes a long time. Wait for completion unless it exits, errors, or the user asks to stop. Do not bypass the configured pre-push gate by default; if bypassing is unavoidable, explain why and name the equivalent checks or review already run. Use `$apex-ray-improve` after merged PRs or review feedback to produce recommendation-only improvements for Apex Ray memory, rules, eval labels, telemetry, and config. Keep `.apex-ray/config.local.yml`, Apex Ray caches/telemetry/reports/eval runs, generated review artifacts, and local provider, model, API, or cost settings out of commits.
<!-- APEX_RAY_END -->
