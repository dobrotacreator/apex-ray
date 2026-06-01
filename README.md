<p align="center">
  <img src="docs/assets/apex-ray-logo-animated.svg" alt="Apex Ray logo animation" width="240">
</p>

# Apex Ray

[![CI](https://github.com/dobrotacreator/apex-ray/actions/workflows/ci.yml/badge.svg)](https://github.com/dobrotacreator/apex-ray/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/apex-ray.svg)](https://pypi.org/project/apex-ray/)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://github.com/dobrotacreator/apex-ray/blob/main/pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Local CLI-first AI code review for TypeScript and JavaScript projects.

Apex Ray reads a git diff, builds compact context packs around changed code, runs optional LLM review through a local CLI provider, verifies findings, and writes Markdown/JSON/HTML reports. It is designed for teams that want review intelligence locally, without depending on a hosted PR-review product.

> Apex Ray is pre-1.0. Report schemas and configuration can change while the project is prepared for production use.

## What It Does

- Builds TS/JS context packs from changed files, symbols, callers, callees, contracts, metadata, and related tests.
- Supports project-specific rules and repo-committed review memory.
- Runs without LLM calls, or with Codex CLI / Claude Code CLI when configured.
- Routes cheap and strong models through profiles.
- Tracks LLM coverage, skipped packs, partial severity, provider failures, cache usage, and continuation commands.
- Replays historical GitHub PR review comments for local evals.
- Writes local telemetry so teams can tune cost, latency, and coverage over time.

## What It Does Not Do

Apex Ray does not replace CI, tests, linters, typecheck, dependency scanners, SAST, or human review. It focuses on diff-aware behavioral review.

## Requirements

- Python 3.14+
- Node.js 24+
- npm
- git
- uv for development
- Codex CLI or Claude Code CLI for LLM review
- GitHub CLI only for historical PR capture/eval commands

## Install

For published releases:

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

For local development from source:

```bash
git clone git@github.com:dobrotacreator/apex-ray.git
cd apex-ray
uv sync --all-groups
npm --prefix analyzers/typescript ci
npm --prefix analyzers/typescript run build
```

Run from the repository root:

```bash
uv run apex-ray --version
uv run apex-ray doctor
```

The shorter `apex-ray ...` commands below assume the console script is installed on your `PATH`. When working from a source checkout, either prefix commands with `uv run` or install the local checkout as an editable user tool:

```bash
uv tool install --editable .
apex-ray doctor
```

## Quickstart

In a project you want to review:

```bash
apex-ray init
apex-ray doctor
git status --short
```

Inspect and commit the setup files before using the first worktree review for application changes.

`apex-ray init` creates `.apex-ray/config.yml`, rules/memory/report directories, gitignore entries, brief agent instruction pointers, project-local Apex Ray skills (`$apex-ray` and `$apex-ray-improve`), and a Lefthook pre-push gate command that follows shared and local config. Use `--hooks none`, `--agent-files none`, or `--no-agent-skill` for exceptional repositories.

After the setup commit, run a deterministic local review:

```bash
apex-ray review --worktree --no-llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json
```

Run the configured LLM review explicitly:

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
apex-ray review --continue-from .apex-ray/reports/review.json --only-pack 'apps/api/src/payments.ts#capture:1' --llm
```

Run the same gate that `apex-ray init` wires into pre-push:

```bash
apex-ray gate pre-push
```

The gate reviews `review.base...HEAD`, writes `.apex-ray/reports/pre-push.md` and `.apex-ray/reports/pre-push.json`, prints an agent-friendly blocking summary, and exits non-zero when the configured policy fails.

## Configuration

Project configuration lives in `.apex-ray/config.yml`:

```yaml
review:
  base: main
  ignore:
    - "**/*.lock"
    - "**/generated/**"
  rule_paths:
    - .apex-ray/rules
  memory:
    enabled: true
    paths:
      - .apex-ray/memory
  llm:
    enabled: true
    provider: codex_cli
    effort: medium
    coverage_mode: balanced
    max_packs: 64
    max_deep_packs: 48
    max_input_tokens: 300000
    verify: true
  telemetry:
    enabled: false
    path: .apex-ray/telemetry/review-runs.jsonl
  gates:
    pre_push:
      enabled: true
      min_finding_severity: high
      require_verified_findings: true
      fail_on_quality_gate: true
      fail_on_partial_severity: critical
```

Machine-specific overrides can live in `.apex-ray/config.local.yml`. Apex Ray merges built-in defaults, shared config, local config, and CLI flags in that order. Local config is gitignored by default and is intended for provider/model/cost differences between contributors.

See [docs/configuration.md](docs/configuration.md) for configuration details.

## Rules And Memory

Rules are Markdown files with YAML frontmatter under `.apex-ray/rules/`. They are matched to context packs and injected only when relevant.

Memory cards are Markdown files under `.apex-ray/memory/`. They keep concise team learning, false-positive calibration, and domain review hints close to the codebase.

See [docs/memory.md](docs/memory.md) for memory-card details.

## LLM Providers

Apex Ray supports Codex CLI and Claude Code CLI. Profiles let a project combine cheaper broad review with stronger verification/escalation, including mixed providers:

```yaml
review:
  llm:
    profiles:
      cheap:
        provider: codex_cli
        model: "<cheap-codex-model>"
        effort: low
      strong:
        provider: claude_code_cli
        model: "<strong-claude-model-or-alias>"
        effort: medium
    routing:
      review_profile: cheap
      verify_profile: strong
      escalated_review_profile: strong
      escalate_review_when:
        risk: [auth, external_io, persistence]
        rule_severity: [high, critical]
        strict_rule: true
        pack_truncated: true
```

Avoid near-sunset model IDs in shared defaults. `effort` maps to Codex CLI `model_reasoning_effort` and Claude Code CLI `--effort`; use `.apex-ray/config.local.yml` for personal provider/model/path/cost overrides.

See [docs/providers.md](docs/providers.md).

## Architecture

For a high-level implementation map, review flow, init artifacts, telemetry/eval flow, and test fixture explanation, see [docs/architecture.md](docs/architecture.md).

## Coverage And Continuation

LLM coverage modes:

- `fast`: capped deep review.
- `balanced`: deep review for high-value packs plus shallow breadth under token budget.
- `exhaustive`: review every reviewable pack when budget allows.

Reports include reviewed/unreviewed pack IDs, partial severity, residual P0/P1 packs, skipped reasons, provider failures, cache metrics, provider-reported token usage when available, and suggested continuation commands.

Report paths are latest snapshots by default. Enable `review.reports.archive: true` to also keep full per-run report copies under `.apex-ray/reports/runs/` with configurable retention.

## Telemetry

Local review telemetry is append-only JSONL. It is intended for tuning cost, latency, model routing, and coverage. Apex Ray records estimated input tokens before provider calls and provider-reported actual token/cost metadata after calls when the configured CLI exposes it:

```bash
apex-ray review --worktree --llm --telemetry
apex-ray telemetry-summary --telemetry-path .apex-ray/telemetry/review-runs.jsonl
```

Telemetry is measurement-only and is not injected into review prompts automatically. See [docs/telemetry.md](docs/telemetry.md).

## Historical PR Evals

Apex Ray can capture prior GitHub PR comments and replay local review on historical diffs:

```bash
apex-ray eval capture-prs --repo /path/to/project --output /path/to/project/.apex-ray/evals/cases --limit 10
apex-ray eval run-prs --repo /path/to/project --cases /path/to/project/.apex-ray/evals/cases --output /path/to/project/.apex-ray/evals/runs/latest --llm
```

See [docs/pr-eval.md](docs/pr-eval.md).

## Privacy

When LLM review is enabled, Apex Ray sends selected diff and context-pack content to the configured local CLI provider. Review that provider's privacy and retention policy before using Apex Ray on private code.

Caches, telemetry, and archived reports are local files. They may include repository paths, model names, finding counts, coverage metadata, token estimates, findings, and source snippets. Keep them ignored unless a team intentionally curates a shared artifact.

## Development

```bash
uv run coverage run -m pytest -q
uv run coverage report -m
npm --prefix analyzers/typescript run typecheck
npm --prefix analyzers/typescript test
npm --prefix analyzers/typescript run coverage
git diff --check
```

See [docs/development.md](docs/development.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

Maintainers: release automation is documented in [docs/development.md](docs/development.md#release-hygiene).

## License

Apache-2.0. See [LICENSE](LICENSE).
