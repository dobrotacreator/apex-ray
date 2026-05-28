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
cd analyzers/typescript
npm ci
npm run build
```

## Checks

Run from the repository root unless noted:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q
cd analyzers/typescript && npm run build
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

Release artifacts must be built from a clean tag. Before publishing, verify that the wheel and sdist include the expected source files, bundled analyzer files, metadata, and license.
