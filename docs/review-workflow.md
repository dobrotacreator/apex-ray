# Review Workflow

This page covers day-to-day Apex Ray usage after installation and project initialization.

## Choose A Review Target

Use one target per review run:

| Target | Command | Use when |
| --- | --- | --- |
| Unstaged worktree changes | `apex-ray review --worktree` | You are iterating locally before staging. |
| Staged changes | `apex-ray review --staged` | You want review to match the next commit. |
| Branch diff | `apex-ray review --base main` | You want PR-like review of `main...HEAD`. |
| Supplied diff file | `apex-ray review --diff change.diff` | You captured a patch outside the current worktree. |
| Partial report continuation | `apex-ray review --continue-from review.json` | You need to review skipped or residual context packs. |

For stable automation, write reports under `.apex-ray/reports/`:

```bash
apex-ray review \
  --base main \
  --llm \
  --output .apex-ray/reports/review.md \
  --json .apex-ray/reports/review.json \
  --html .apex-ray/reports/review.html
```

## Review Modes

No-LLM mode is deterministic and cheap:

```bash
apex-ray review --worktree --no-llm
```

It still parses the diff, runs available language analyzers, builds context packs, and reports review coverage surfaces.

LLM mode sends selected context packs to the configured local CLI provider:

```bash
apex-ray review --worktree --llm
```

By default, the configured verifier reviews candidate findings before they are published. Use `--no-verify` only for exploratory runs where speed matters more than publication quality.

## Reports

Apex Ray writes:

- Markdown for local reading.
- JSON for durable automation and continuation.
- Optional HTML for browser-based inspection.

Reports include findings, analyzer warnings, selected context packs, skipped packs, LLM routes, cache usage, token estimates, coverage status, and continuation commands.

Report paths are latest snapshots by default. Reusing the same paths overwrites the previous latest report. Enable report archives only when a team needs per-run artifacts for quality debugging:

```yaml
review:
  reports:
    archive: true
    archive_dir: .apex-ray/reports/runs
    retention: 20
```

Report artifacts can include source snippets, findings, file paths, provider metadata, and token estimates. Keep generated reports ignored unless a team intentionally curates a specific artifact.

## Coverage And Continuation

LLM coverage modes control how broadly Apex Ray reviews a diff:

- `fast`: capped deep review.
- `balanced`: deep review for high-value packs plus shallow breadth under token budget.
- `exhaustive`: review every reviewable pack when budget allows.

Large diffs can still be partial. The report makes partial coverage explicit with reviewed and unreviewed pack IDs, residual priorities, skipped reasons, and continuation commands.

Continue only unreviewed P0 packs:

```bash
apex-ray review \
  --continue-from .apex-ray/reports/review.json \
  --residual-priority p0 \
  --llm
```

Continue with automatic P0 follow-up after a first pass:

```bash
apex-ray review --base main --llm --auto-followup
```

## Pre-Push Gate

`apex-ray gate pre-push` runs the configured gate policy over `review.base...HEAD`.

Default gate behavior:

- writes `.apex-ray/reports/pre-push.md` and `.apex-ray/reports/pre-push.json`;
- blocks on verified `high` or `critical` findings;
- blocks on failed LLM coverage quality gate;
- blocks on `critical` partial coverage;
- prints live progress to stderr and a compact blocking summary to stdout.

Run it manually before relying on hook behavior:

```bash
apex-ray gate pre-push
```

If repeated push attempts review the same packs, Apex Ray uses the LLM response cache and analyzer caches where available to reduce repeated work.

## Caches

Apex Ray uses two local caches by default:

- `.apex-ray/cache/llm` for provider responses keyed by prompt context and routing.
- analyzer repo index caches where a backend supports them. Today this applies to TypeScript/JavaScript repository analysis; the Python analyzer is in-process and does not yet maintain a persistent repo index cache.

Refresh LLM cache entries:

```bash
apex-ray review --worktree --llm --refresh-cache
```

Refresh analyzer cache entries:

```bash
apex-ray review --worktree --refresh-analyzer-cache
```

Disable the LLM cache for one run:

```bash
apex-ray review --worktree --llm --no-cache
```

## Privacy Boundary

Without `--llm`, Apex Ray stays local and deterministic.

With `--llm`, Apex Ray sends selected diff and context-pack content to the configured local CLI provider. Review that provider's privacy and retention policy before using Apex Ray on private code. Caches, telemetry, and archived reports are local files, but they may contain repository paths, model names, finding counts, provider metadata, and source snippets.

## Common Troubleshooting

Run setup diagnostics:

```bash
apex-ray doctor
```

Typical issues:

- `Config: not found`: run `apex-ray init` in the target repository or pass `--config`.
- `Python analyzer available: false`: reinstall Apex Ray or run from a healthy source checkout. The Python analyzer is built in and should normally be available whenever the CLI imports successfully.
- `TypeScript analyzer built: false`: reinstall the published package, or in a source checkout run the TypeScript analyzer build from [Development](development.md).
- Provider command not found: install the configured Codex CLI or Claude Code CLI, or override the executable path in `.apex-ray/config.local.yml`.
- Hook cannot find `apex-ray`: install Apex Ray on the user `PATH` used by git hooks, or update the hook environment.
