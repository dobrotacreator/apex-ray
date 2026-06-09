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
| Partial report continuation | `apex-ray review --continue-from .apex-ray/reports/review.json` | You need to review skipped or residual context packs. |

Reports are written under `.apex-ray/reports/` by default. For stable automation, pass explicit paths:

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

Report paths are latest snapshots by default. Reusing the same paths overwrites the previous latest report. `apex-ray init` enables report archives so per-run artifacts survive short-lived worktrees:

```yaml
review:
  reports:
    archive: true
    archive_dir: ${local_data}/reports/runs
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

### Local False Positives

For a confirmed one-off local false positive, create an expiring local suppression instead of bypassing the gate:

```bash
apex-ray findings list --from-report .apex-ray/reports/pre-push.json
apex-ray findings suppress apex-<id> \
  --from-report .apex-ray/reports/pre-push.json \
  --reason "This path is guarded before the reviewed helper is called."
```

Use suppressions sparingly. Inspect the finding evidence, current code, and relevant tests or invariants before suppressing. The reason should be concrete enough for a later agent to audit; do not suppress uncertain findings or real defects just to get a push through.

The next `apex-ray gate pre-push` run still writes the raw finding in the report, but the gate decision lists it under suppressed findings and does not block on it. Suppressions are local, expire automatically, and become stale when the matching context pack changes. When that happens, the gate output/report prints the stale suppression and prior reason, and the finding blocks again until it is re-checked. Use `apex-ray findings suppressions`, `apex-ray findings unsuppress sup-<id>`, or `apex-ray findings prune` for cleanup.

Commit a `kind: false_positive` memory card only for repeated, generalizable calibration. Do not commit raw local suppressions.

## Caches

Apex Ray uses two local caches by default:

- `${local_data}/cache/llm` for provider responses keyed by prompt context and routing. With the default `review.local_data.root: git_common`, linked worktrees from the same local clone share this cache.
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
