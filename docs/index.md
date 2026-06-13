<p align="center">
  <img src="assets/apex-ray-logo-animated.svg" alt="Apex Ray logo animation" width="240">
</p>

# Apex Ray

Local CLI-first AI code review for git diffs with analyzer-backed context.

Apex Ray reads a git diff, builds compact context packs around changed code, runs optional LLM review through a local CLI provider, verifies findings, and writes Markdown, JSON, and HTML reports. It is designed for teams that want review intelligence locally, without depending on a hosted PR-review product.

!!! warning "Pre-1.0"
    Apex Ray is pre-1.0. Report schemas and configuration can change while the project is prepared for production use.

## Start Here

| Goal | Read |
| --- | --- |
| Install Apex Ray and run a first review | [Quick Start](quickstart.md) |
| Choose review targets, understand reports, and continue partial coverage | [Review Workflow](review-workflow.md) |
| Configure shared policy, gates, reports, and coverage | [Configuration](configuration.md) |
| Set up Codex CLI or Claude Code CLI provider routing | [LLM Providers](providers.md) |
| Add project-specific review rules and team memory | [Rules And Memory](memory.md) |
| Understand internals and contribution workflow | [Architecture](architecture.md) and [Development](development.md) |

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
| Enhanced analyzer planned | Go, Rust | Repository-aware symbols, callers/callees, contracts, service boundaries, persistence/I/O surfaces, and related tests. |
| Generic fallback | Other reviewable diffs | Diff-hunk context, risk signals, project rules, memory, reports, and optional LLM review without a repository-aware symbol graph. |

## What It Does Not Do

Apex Ray does not replace tests, linters, typecheck, dependency scanners, SAST, CI, or human review. It focuses on diff-aware behavioral review and makes partial review coverage explicit instead of hiding it.

## Fast Path

```bash
uv tool install apex-ray
apex-ray doctor
apex-ray init
apex-ray review --worktree --no-llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json
```

After provider configuration is ready:

```bash
apex-ray review --worktree --llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json --html .apex-ray/reports/review.html
```

Run the pre-push gate manually:

```bash
apex-ray gate pre-push
```

See [Quick Start](quickstart.md) for the full first-run sequence.

## Core Concepts

- **Context packs** are the unit of review. A pack usually represents one changed symbol or file-level change plus nearby references, callees, contracts, rules, memory, metadata, and related tests.
- **Rules** are stable project constraints injected only when they match a context pack.
- **Memory** is curated team learning, false-positive calibration, and domain vocabulary.
- **Coverage** records which packs were reviewed deeply, reviewed shallowly, skipped, or left as residual work.
- **Reports** are local artifacts. Markdown and HTML are for humans; JSON is for automation and continuation.

## Documentation Map

- [Quick Start](quickstart.md): install, initialize a repo, run first no-LLM and LLM reviews.
- [Review Workflow](review-workflow.md): daily commands, targets, reports, continuation, cache behavior, and troubleshooting.
- [Configuration](configuration.md): shared config, local overrides, coverage, reports, and pre-push gate policy.
- [LLM Providers](providers.md): Codex CLI, Claude Code CLI, profiles, routing, effort, usage, and privacy boundary.
- [Rules And Memory](memory.md): project-specific review rules and curated repo memory.
- [Telemetry](telemetry.md): local JSONL metrics for cost, latency, routing, and coverage tuning.
- [Historical PR Replay Evals](pr-eval.md): capture and replay historical PR review comments for quality calibration.
- [Architecture](architecture.md): implementation map, review flow, init flow, gate flow, eval flow, and benchmark flow.
- [Development](development.md): contributor setup, checks, docs build, and release hygiene.
