# Contributing

Thanks for improving Apex Ray. This project is still pre-1.0, so breaking changes are allowed when they materially improve review quality, local usability, or release safety.

## Development Setup

Prerequisites:

- Python 3.14+
- Node.js 24+ and npm for the bundled TypeScript/JavaScript analyzer
- uv
- git

Install dependencies and build the bundled TypeScript/JavaScript analyzer. The Python analyzer is part of the Python package and does not need a separate build step.

```bash
uv sync --all-groups
npm --prefix analyzer-runtimes/typescript ci
npm --prefix analyzer-runtimes/typescript run build
```

Run the main checks from the repository root:

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

## Git Flow

- Work on feature branches.
- Keep `main` releasable.
- Merge through pull requests after CI passes.
- Do not publish or tag from a dirty worktree.
- Do not commit generated review reports, local telemetry, caches, or `.apex-ray/config.local.yml`.
- Keep shared `.apex-ray/config.yml` focused on team policy; use `.apex-ray/config.local.yml` for personal provider/model/cost settings.

## Commit Messages

Use Conventional Commits:

```text
feat(llm): add Claude Code CLI provider
fix(report): preserve failed pack status in coverage
docs: rewrite quickstart
chore(ci): add wheel install smoke
feat!: change review report schema
```

Allowed commit types are `feat`, `fix`, `refactor`, `test`, `docs`, `infra`, `adr`, `migration`, and `chore`.

Use `!` and a `BREAKING CHANGE:` footer for behavior or schema changes that can affect users, automation, or stored reports.

## Pull Requests

Good pull requests include:

- A clear summary of user-visible behavior.
- Focused tests for the changed behavior.
- Fresh verification commands and results.
- Notes for breaking changes, migration steps, or follow-up work.

For review-quality changes, prefer executable fixtures or historical PR replay evidence over anecdotal prompt tuning.

## Release Notes

Do not manually bump `pyproject.toml` or edit release sections in `CHANGELOG.md` for normal releases. Release Please reads Conventional Commits on `main`, opens a release PR, updates `pyproject.toml`, updates `CHANGELOG.md`, and creates the release tag after that PR is merged.

Use clear commit types and scopes so generated release notes are useful. See [docs/development.md](docs/development.md#release-hygiene) for maintainer release flow details.
