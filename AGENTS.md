# Apex Ray Agent Guide

This repository contains Apex Ray, a local CLI-first AI code review engine focused on TypeScript and JavaScript projects.

## Core Commands

```bash
uv run pytest -q
cd analyzers/typescript && npm run build
git diff --check
uv run apex-ray doctor
```

Use `rg` for search. Keep generated review reports, caches, telemetry, and local config out of commits.

## Review Workflow

- Use `uv run apex-ray review --worktree --json review.json --output review.md` for local worktree changes.
- Add `--llm` only when the configured provider is available and the cost is appropriate.
- Use `--continue-from review.json` when a report has partial coverage.
- Do not treat Apex Ray as a replacement for project tests, linters, typecheck, or security scanners.

## Project Rules

- Commit messages use Conventional Commits.
- Mark breaking changes with `!` and a `BREAKING CHANGE:` footer.
- Prefer focused tests for behavior changes.
- Run the smallest relevant verification before reporting work as complete.
