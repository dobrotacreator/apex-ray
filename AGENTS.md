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

## Verification

Run the smallest relevant check for the changed surface:

- Python code: `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyright`, and focused or full `uv run pytest -q`.
- TS analyzer code: `npm --prefix analyzer-runtimes/typescript run typecheck`, `npm --prefix analyzer-runtimes/typescript test`, and relevant Python context tests.
- CLI/config/report behavior: focused tests plus `uv run apex-ray doctor`.
- Packaging/release behavior: full CI-equivalent checks, `uv build --sdist --wheel`, `uv run twine check dist/*`, and installed-wheel smoke coverage from `.github/workflows/ci.yml`.

Before saying work is complete, report the verification that actually ran.

## Apex Ray Review Aid

Use Apex Ray itself only as an additional local review signal; it does not replace tests, linters, typecheck, security scanners, or human review.

```bash
uv run apex-ray review --worktree --no-llm --json review.json --output review.md
uv run apex-ray review --worktree --llm --json review.json --output review.md
uv run apex-ray review --continue-from review.json --llm
```

## Do Not Commit

- `.apex-ray/config.local.yml`
- `.apex-ray/cache/`
- `.apex-ray/telemetry/`
- `.apex-ray/reports/`
- `.apex-ray/eval*/runs/`
- generated `review.*` reports
- local provider, model, API, or cost settings

## Git Rules

- Do not commit unless explicitly asked.
- Commit messages use Conventional Commits.
- Mark breaking changes with `!` and a `BREAKING CHANGE:` footer.
