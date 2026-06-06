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
npm --prefix analyzer-runtimes/typescript ci
npm --prefix analyzer-runtimes/typescript run build
```

## Architecture Map

See `architecture.md` for the high-level product flow, init artifacts, telemetry/eval flow, and test fixture map.

Python code lives under `src/apex_ray/`. Keep domain code in the existing packages:

- `cli/`: Typer app wiring and command modules.
- `pipeline/`: review orchestration, continuation, context-pack selection, and finding consolidation.
- `llm/`: provider calls, model routing, prompts, response parsing, cache, and usage accounting.
- `context/`: context-pack construction, snippets, and prompt budget reduction.
- `report/`: Markdown/HTML rendering and coverage breakdowns.
- `benchmark/`: benchmark capture, matching, models, and reports.
- `pr_eval/`: historical PR capture/replay, Greptile matching, run state, storage, and telemetry.

The bundled TypeScript analyzer lives under `analyzer-runtimes/typescript/src/`. Keep analyzer internals grouped by responsibility:

- `contracts/`: contracts, DTOs, decorator metadata, schemas, and dependency expansion.
- `references/`: reference collection, merging, and target matching.
- `workspace/`: workspace package import/export/member references.
- `indexes/`: repository, source-file, semantic-file, import/export, and DI indexes.
- `symbols/`: symbol collection, export metadata, implemented members, and synthetic symbols.

The Python analyzer lives under `src/apex_ray/analyzers/python/`. Keep it grouped by responsibility:

- `runner.py`: backend entry point and per-file fallback behavior.
- `workspace.py`: repository file discovery, safe path resolution, reading, and parsing.
- `symbols.py`: Python symbol collection, imports, exports, deleted symbols, decorators, and base/annotation references.
- `bindings.py` and `calls.py`: import binding, receiver, call-site, and simple instance tracking.
- `annotations.py`, `references.py`, and `metadata.py`: annotation contracts, workspace references/callees, and framework-agnostic boundary metadata.
- `related_tests.py`: related test discovery and ranking.
- `state.py`, `utils.py`, and `constants.py`: shared data structures, helpers, and patchable analyzer limits.

Avoid adding new flat prefix modules like `cli_*.py`, `pipeline_*.py`, `llm_*.py`, `report_*.py`, `contract-*.ts`, or `workspace-*.ts`; use package-local names inside the relevant directory. Keep Python package `__init__.py` files thin and focused on public re-exports.

## Checks

Run from the repository root unless noted:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run coverage run -m pytest -q
uv run coverage report -m
npm --prefix analyzer-runtimes/typescript run typecheck
npm --prefix analyzer-runtimes/typescript test
npm --prefix analyzer-runtimes/typescript run coverage
uv build --sdist --wheel
uv run twine check dist/*
git diff --check
```

Install Lefthook if you want local git hooks:

```bash
lefthook install
```

## Worktrees

For PR-sized or risky local changes, prefer an isolated worktree created from the primary checkout:

```bash
scripts/create-worktree.sh <branch-name>
```

By default this creates `.worktrees/<branch-name>`, checks out the requested branch from `origin/main`, and then runs `scripts/setup-worktree.sh`. Use `--base <ref>`, `--path <path>`, or `--no-setup` when you need a different base, location, or manual setup.

`scripts/setup-worktree.sh` copies ignored machine-local files from the primary checkout when they exist and are missing in the new worktree, including `.apex-ray/config.local.yml`, `.mcp.json`, and local env files. The script verifies each copied path is ignored in the target worktree before copying, so private provider settings and local review artifacts do not become tracked by accident. It then runs the normal project setup commands (`uv sync --all-groups`, TypeScript analyzer install/build, and Lefthook install when available).

## Docs Site

Build the GitHub Pages documentation site locally:

```bash
uv run --locked --only-group docs mkdocs build --strict
```

Preview it locally:

```bash
uv run --locked --only-group docs mkdocs serve
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

Release Please owns version bumps, changelog updates, tags, and GitHub Releases. It reads Conventional Commits on `main` and opens a release PR that updates `pyproject.toml`, `CHANGELOG.md`, and `.release-please-manifest.json`.

Do not manually bump the package version for normal releases. Review and merge the Release Please PR when the generated version and changelog are correct.

Merging a Release Please PR creates the GitHub Release and tag. The `Publish PyPI` workflow then builds from that release tag and publishes with PyPI Trusted Publishing through the `pypi` environment.

Pre-1.0 versioning is conservative: breaking changes bump the minor version; features and fixes bump the patch version.

PyPI publishing uses Trusted Publishing from `.github/workflows/publish-pypi.yml` and the GitHub environment `pypi`. Configure those once before the first release; do not add a long-lived PyPI API token unless Trusted Publishing is unavailable.

Release artifacts must be built from a clean tag. Before publishing, verify that the wheel and sdist include the expected source files, bundled analyzer files, metadata, and license.
