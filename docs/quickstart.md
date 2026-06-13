# Quick Start

This guide gets Apex Ray installed, initialized in a target repository, and running a first local review.

## Install

For a one-off run:

```bash
uvx apex-ray --help
uvx apex-ray doctor
```

For a user-level CLI install:

```bash
uv tool install apex-ray
apex-ray --version
apex-ray doctor
```

`pipx install apex-ray` is also supported if you prefer pipx for isolated Python CLI tools.

## Requirements

Apex Ray can review git diffs through a language-neutral pipeline. Enhanced analyzers currently cover TypeScript/JavaScript and Python:

- Python 3.14+
- git
- Node.js 24+ and npm only when reviewing TypeScript or JavaScript with the bundled analyzer
- Codex CLI or Claude Code CLI only when using `--llm`
- GitHub CLI only for historical PR replay commands

Run `apex-ray doctor` from the repository you want to review. It checks git discovery, detected languages, the built-in Python analyzer, Node.js, and the bundled TypeScript analyzer.

## Initialize A Project

From a repository you want Apex Ray to review:

```bash
apex-ray init
apex-ray doctor
git status --short
```

Inspect and commit the generated setup before reviewing application changes. `apex-ray init` creates shared config and review-learning directories under `.apex-ray/`, writes `.apex-ray/.gitignore` for Apex Ray generated artifacts, and can install a pre-push gate through Lefthook or a git hook.

Generated shared files are meant to be reviewed like normal project configuration. Machine-specific settings belong in `.apex-ray/config.local.yml`, which is ignored by default.

The generated config stores telemetry, LLM cache entries, and archived report runs under a shared local-data directory for the current git clone, so linked worktrees can be deleted without losing those accumulated metrics.

When Apex Ray's generated agent instructions change in a newer package version, `apex-ray doctor`, `apex-ray review`, and `apex-ray gate pre-push` warn locally if the repository's managed `AGENTS.md` block or generated skills are outdated. Refresh only those managed artifacts with:

```bash
apex-ray init --refresh-agent-artifacts --dry-run
apex-ray init --refresh-agent-artifacts
```

## First Deterministic Review

Run a no-LLM review first. This verifies diff parsing, project discovery, analyzer coverage, context pack construction, and report rendering without provider cost:

```bash
apex-ray review \
  --worktree \
  --no-llm
```

Use `--staged` to review only staged changes, `--base main` to review `main...HEAD`, or `--diff path/to/change.diff` for a supplied unified diff.

## First LLM Review

After provider configuration is ready, run:

```bash
apex-ray review \
  --worktree \
  --llm \
  --output .apex-ray/reports/review.md \
  --json .apex-ray/reports/review.json \
  --html .apex-ray/reports/review.html
```

The Markdown report is easiest to read locally, the JSON report is the durable machine-readable artifact, and the optional HTML report is useful when sharing a local run with teammates.

If your shared config enables an LLM provider but a machine should stay offline or avoid provider cost, put this in `.apex-ray/config.local.yml`:

```yaml
review:
  llm:
    enabled: false
```

## Pre-Push Gate

If `apex-ray init` configured a hook, make sure the installed `apex-ray` command is available on `PATH` for git hooks. With Lefthook, activate local hooks with:

```bash
lefthook install
```

Run the hook-equivalent gate manually:

```bash
apex-ray gate pre-push
```

The gate reviews `review.base...HEAD`, writes `.apex-ray/reports/pre-push.md` and `.apex-ray/reports/pre-push.json`, and exits non-zero when the configured policy blocks the push.

## Continue Partial Coverage

Large diffs can exceed the configured LLM coverage budget. Reports include reviewed and unreviewed pack IDs plus continuation commands.

Continue the highest-priority unreviewed work:

```bash
apex-ray review \
  --continue-from .apex-ray/reports/review.json \
  --residual-priority p0 \
  --llm
```

Review one specific skipped pack:

```bash
apex-ray review \
  --continue-from .apex-ray/reports/review.json \
  --only-pack "<pack-id>" \
  --llm
```

## Next Steps

- Configure shared review policy in [Configuration](configuration.md).
- Configure Codex CLI or Claude Code CLI in [LLM Providers](providers.md).
- Learn how to read reports and choose review targets in [Review Workflow](review-workflow.md).
- Add project-specific rules and memory in [Rules And Memory](memory.md).
