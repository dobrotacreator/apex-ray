# Development

## Prerequisites

- Python 3.14+
- Node.js 24+
- npm
- uv
- git

## Setup

```bash
uv sync --all-groups
npm --prefix analyzers/typescript ci
npm --prefix analyzers/typescript run build
```

## Architecture Map

See `docs/architecture.md` for the high-level product flow, init artifacts, telemetry/eval flow, and test fixture map.

Python code lives under `src/apex_ray/`. Keep domain code in the existing packages:

- `cli/`: Typer app wiring and command modules.
- `pipeline/`: review orchestration, continuation, context-pack selection, and finding consolidation.
- `llm/`: provider calls, model routing, prompts, response parsing, cache, and usage accounting.
- `context/`: context-pack construction, snippets, and prompt budget reduction.
- `report/`: Markdown/HTML rendering and coverage breakdowns.
- `benchmark/`: benchmark capture, matching, models, and reports.
- `pr_eval/`: historical PR capture/replay, Greptile matching, run state, storage, and telemetry.

The bundled TypeScript analyzer lives under `analyzers/typescript/src/`. Keep analyzer internals grouped by responsibility:

- `contracts/`: contracts, DTOs, decorator metadata, schemas, and dependency expansion.
- `references/`: reference collection, merging, and target matching.
- `workspace/`: workspace package import/export/member references.
- `indexes/`: repository, source-file, semantic-file, import/export, and DI indexes.
- `symbols/`: symbol collection, export metadata, implemented members, and synthetic symbols.

Avoid adding new flat prefix modules like `cli_*.py`, `pipeline_*.py`, `llm_*.py`, `report_*.py`, `contract-*.ts`, or `workspace-*.ts`; use package-local names inside the relevant directory. Keep Python package `__init__.py` files thin and focused on public re-exports.

## Checks

Run from the repository root unless noted:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run coverage run -m pytest -q
uv run coverage report -m
npm --prefix analyzers/typescript run typecheck
npm --prefix analyzers/typescript test
npm --prefix analyzers/typescript run coverage
uv build --sdist --wheel
uv run twine check dist/*
git diff --check
```

Install Lefthook if you want local git hooks:

```bash
lefthook install
```

## Generated Files

Do not commit:

- `.apex-ray/config.local.yml`
- `.apex-ray/cache/`
- `.apex-ray/telemetry/`
- `.apex-ray/reports/`
- `.apex-ray/eval*/runs/`
- generated `review.*` reports
- local provider settings

## Release Hygiene

Release automation and PyPI publishing are documented in `docs/releasing.md`.

Release artifacts must be built from a clean tag. Before publishing, verify that the wheel and sdist include the expected source files, bundled analyzer files, metadata, and license.
