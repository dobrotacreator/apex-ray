<p align="center">
  <img src="docs/assets/apex-ray-logo-animated.svg" alt="Apex Ray logo animation" width="240">
</p>

# Apex Ray

[![CI](https://github.com/dobrotacreator/apex-ray/actions/workflows/ci.yml/badge.svg)](https://github.com/dobrotacreator/apex-ray/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-0ea5e9.svg)](https://dobrotacreator.github.io/apex-ray/)
[![PyPI](https://img.shields.io/pypi/v/apex-ray.svg)](https://pypi.org/project/apex-ray/)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://github.com/dobrotacreator/apex-ray/blob/main/pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Local CLI-first AI code review for git diffs with analyzer-backed context.

Full documentation: [dobrotacreator.github.io/apex-ray](https://dobrotacreator.github.io/apex-ray/)

Apex Ray reads a git diff, builds compact context packs around changed code, runs optional LLM review through a local CLI provider, verifies findings, and writes Markdown, JSON, and HTML reports. It is designed for teams that want review intelligence locally, without depending on a hosted PR-review product.

> Apex Ray is pre-1.0. Report schemas and configuration can change while the project is prepared for production use.

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

## Quick Start

In a project you want to review:

```bash
apex-ray init
apex-ray doctor
git status --short
```

Inspect and commit the setup files before using the first worktree review for application changes.

If Apex Ray later warns that generated agent instructions are outdated, refresh only those managed artifacts with:

```bash
apex-ray init --refresh-agent-artifacts --dry-run
apex-ray init --refresh-agent-artifacts
```

Run a deterministic no-LLM review:

```bash
apex-ray review --worktree --no-llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json
```

Run the configured LLM review:

```bash
apex-ray review --worktree --llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json --html .apex-ray/reports/review.html
```

Review a branch against the configured base:

```bash
apex-ray review --base main --llm
```

Continue only unreviewed packs from a partial report:

```bash
apex-ray review --continue-from .apex-ray/reports/review.json --residual-priority p0 --llm
apex-ray review --continue-from .apex-ray/reports/review.json --only-pack '<pack-id>' --llm
```

Run the same gate that `apex-ray init` wires into pre-push:

```bash
apex-ray gate pre-push
```

The gate reviews `review.base...HEAD`, writes `.apex-ray/reports/pre-push.md` and `.apex-ray/reports/pre-push.json`, prints an agent-friendly blocking summary, and exits non-zero when the configured policy fails.

See the full [Quick Start](https://dobrotacreator.github.io/apex-ray/quickstart/) and [Review Workflow](https://dobrotacreator.github.io/apex-ray/review-workflow/) docs for installation requirements, provider setup, report interpretation, continuation commands, cache behavior, and troubleshooting.

## What It Does

- Builds context packs from changed files, symbols, callers, callees, contracts, metadata, and related tests.
- Runs a language-neutral diff -> context pack -> optional LLM review workflow.
- Uses enhanced analyzers for TypeScript/JavaScript, Python, and Go today, with Rust planned next.
- Supports project-specific rules and repo-committed review memory.
- Runs without LLM calls, or with Codex CLI / Claude Code CLI when configured.
- Routes cheap and strong models through profiles.
- Tracks LLM coverage, skipped packs, partial severity, provider failures, cache usage, and continuation commands.
- Replays historical GitHub PR review comments for local evals.
- Writes local telemetry so teams can tune cost, latency, and coverage over time.

## Analyzer Coverage

Apex Ray's review pipeline is language-neutral. It is strongest where an analyzer backend can build repository-aware context instead of relying only on diff hunks.

| Status | Language family | Strongest current surfaces |
| --- | --- | --- |
| Enhanced analyzer available | TypeScript, JavaScript | NestJS controllers/providers/modules/guards, DTO/schema validators, route and DI metadata, workspace imports/exports, enum/const fanout, cache and permission surfaces, related tests. |
| Enhanced analyzer available | Python | FastAPI routes/dependencies, Pydantic models/settings/validators, SQLAlchemy sessions/transactions, Alembic migrations, async worker/event flows, external HTTP/cloud/Redis adapters, dataclass/TypedDict/Protocol contracts, pytest/unittest tests and fixtures. |
| Enhanced analyzer available | Go | Type-aware package loading, repository-relative symbols, changed and deleted symbol ranges, callers/callees, interface contracts, context metadata, syntax-only fallback, and related tests. |
| Enhanced analyzer planned | Rust | Repository-aware symbols, callers/callees, contracts, service boundaries, persistence/I/O surfaces, and related tests. |
| Generic fallback | Other reviewable diffs | Diff-hunk context, risk signals, project rules, memory, reports, and optional LLM review without a repository-aware symbol graph. |

## What It Does Not Do

Apex Ray does not replace CI, tests, linters, typecheck, dependency scanners, SAST, or human review. It focuses on diff-aware behavioral review and makes partial coverage explicit.

## Documentation

- [Quick Start](https://dobrotacreator.github.io/apex-ray/quickstart/)
- [Review Workflow](https://dobrotacreator.github.io/apex-ray/review-workflow/)
- [Configuration](https://dobrotacreator.github.io/apex-ray/configuration/)
- [LLM Providers](https://dobrotacreator.github.io/apex-ray/providers/)
- [Rules And Memory](https://dobrotacreator.github.io/apex-ray/memory/)
- [Telemetry](https://dobrotacreator.github.io/apex-ray/telemetry/)
- [Historical PR Replay Evals](https://dobrotacreator.github.io/apex-ray/pr-eval/)
- [Architecture](https://dobrotacreator.github.io/apex-ray/architecture/)
- [Development](https://dobrotacreator.github.io/apex-ray/development/)

## Development

For local development from source:

```bash
git clone git@github.com:dobrotacreator/apex-ray.git
cd apex-ray
uv sync --all-groups
npm --prefix analyzer-runtimes/typescript ci
npm --prefix analyzer-runtimes/typescript run build
```

Useful checks:

```bash
uv run coverage run -m pytest -q
uv run coverage report -m
npm --prefix analyzer-runtimes/typescript run typecheck
npm --prefix analyzer-runtimes/typescript test
npm --prefix analyzer-runtimes/typescript run coverage
git diff --check
```

See [docs/development.md](docs/development.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

Maintainers: release automation is documented in [docs/development.md](docs/development.md#release-hygiene).

## License

Apache-2.0. See [LICENSE](LICENSE).
