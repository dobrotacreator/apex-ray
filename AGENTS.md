# Apex Ray Agent Guide

This repository contains Apex Ray, a local CLI-first AI code review engine focused on TypeScript and JavaScript projects.

## Project Shape

- `src/apex_ray/`: Python CLI, config loading, review pipeline, telemetry, and shared models.
- `src/apex_ray/llm/`: provider orchestration, routing, prompts, response parsing, cache, and usage accounting.
- `src/apex_ray/context/`: context-pack construction, snippet rendering, and prompt budget logic.
- `src/apex_ray/report/`: Markdown/HTML report rendering and LLM coverage breakdowns.
- `src/apex_ray/benchmark/`: benchmark capture, matching, models, and reporting.
- `src/apex_ray/pr_eval/`: historical PR capture/replay, Greptile matching, state, storage, and telemetry.
- `analyzers/typescript/src/`: bundled TypeScript/JavaScript analyzer entrypoints plus shared compiler utilities.
- `analyzers/typescript/src/contracts/`: contract, DTO, decorator, schema, and metadata context collection.
- `analyzers/typescript/src/references/`: symbol reference, callee, merge, and target-matching logic.
- `analyzers/typescript/src/workspace/`: workspace package import/export/member reference resolution.
- `analyzers/typescript/src/indexes/`: repo, source-file, semantic-file, import/export, and DI indexes.
- `analyzers/typescript/src/symbols/`: symbol collection, export metadata, implemented members, and synthetic symbols.
- `tests/`: Python tests plus TS/JS fixtures, benchmark specs, and historical PR eval coverage.
- `docs/`: user-facing configuration, provider, memory, telemetry, eval, and development docs.
- `.github/workflows/ci.yml`: source of truth for full CI parity.

Keep new modules inside the relevant package instead of adding flat prefix files such as `llm_*.py`, `report_*.py`, `contract-*.ts`, or `workspace-*.ts`.

## Setup

Requires Python 3.14+, Node.js 24+, npm, uv, and git.

```bash
uv sync --all-groups
npm --prefix analyzers/typescript ci
npm --prefix analyzers/typescript run build
uv run apex-ray doctor
```

## Verification

Run the smallest relevant checks for the change, and match CI when touching shared behavior.

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q
npm --prefix analyzers/typescript run typecheck
npm --prefix analyzers/typescript test
uv build --sdist --wheel
uv run twine check dist/*
git diff --check
```

Typical scope:

- Python code: `ruff format --check`, `ruff check`, `pyright`, and focused or full `pytest`.
- TypeScript analyzer: `npm run typecheck`, `npm test`, and relevant Python context tests.
- CLI/config/report behavior: focused tests plus `uv run apex-ray doctor`.
- Packaging/release changes: full CI-equivalent checks, `uv build --sdist --wheel`, and `twine check`.

## Review Workflow

- Use `uv run apex-ray review --worktree --no-llm --json review.json --output review.md` for deterministic local worktree review.
- Add `--llm` only when the configured provider is available and the cost is appropriate.
- Use `--continue-from review.json` when a report has partial coverage.
- Do not treat Apex Ray as a replacement for project tests, linters, typecheck, or security scanners.

## Do Not Commit

- `.apex-ray/config.local.yml`
- `.apex-ray/cache/`
- `.apex-ray/telemetry/`
- `.apex-ray/reports/`
- `.apex-ray/eval*/runs/`
- generated `review.*` reports
- local provider, model, API, or cost settings

## Project Rules

- Commit messages use Conventional Commits.
- Mark breaking changes with `!` and a `BREAKING CHANGE:` footer.
- Run the smallest relevant verification before reporting work as complete.
