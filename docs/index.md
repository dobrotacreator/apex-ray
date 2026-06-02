<p align="center">
  <img src="assets/apex-ray-logo-animated.svg" alt="Apex Ray logo animation" width="240">
</p>

# Apex Ray

Local CLI-first AI code review for TypeScript and JavaScript projects.

Apex Ray reads a git diff, builds compact context packs around changed code, runs optional LLM review through a local CLI provider, verifies findings, and writes Markdown, JSON, and HTML reports. It is designed for teams that want review intelligence locally, without depending on a hosted PR-review product.

!!! warning "Pre-1.0"
    Apex Ray is pre-1.0. Report schemas and configuration can change while the project is prepared for production use.

## What It Does

- Builds TS/JS context packs from changed files, symbols, callers, callees, contracts, metadata, and related tests.
- Supports project-specific rules and repo-committed review memory.
- Runs without LLM calls, or with Codex CLI / Claude Code CLI when configured.
- Routes cheap and strong models through profiles.
- Tracks LLM coverage, skipped packs, partial severity, provider failures, cache usage, and continuation commands.
- Replays historical GitHub PR review comments for local evals.
- Writes local telemetry so teams can tune cost, latency, and coverage over time.

## Requirements

- Python 3.14+
- Node.js 24+
- npm
- git
- uv for development
- Codex CLI or Claude Code CLI for LLM review
- GitHub CLI only for historical PR capture/eval commands

## Install

One-off run without a persistent install:

```bash
uvx apex-ray --help
uvx apex-ray doctor
```

User-level CLI install:

```bash
uv tool install apex-ray
apex-ray --version
apex-ray doctor
```

`pipx install apex-ray` is also supported if you use pipx for isolated Python CLI tools.

## Quickstart

In a project you want to review:

```bash
apex-ray init
apex-ray doctor
git status --short
```

Inspect and commit the setup files before using the first worktree review for application changes.

After the setup commit, run a deterministic local review:

```bash
apex-ray review --worktree --no-llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json
```

Run the configured LLM review explicitly:

```bash
apex-ray review --worktree --llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json --html .apex-ray/reports/review.html
```

Run the same gate that `apex-ray init` wires into pre-push:

```bash
apex-ray gate pre-push
```

## Learn More

- [Configuration](configuration.md)
- [LLM providers](providers.md)
- [Rules and memory](memory.md)
- [Architecture and workflow](architecture.md)
- [Telemetry](telemetry.md)
- [Historical PR replay evals](pr-eval.md)
- [Development](development.md)
