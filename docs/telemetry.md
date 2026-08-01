# Telemetry

Apex Ray telemetry is local, append-only JSONL intended for team tuning. It is not sent anywhere by Apex Ray.

## Local Review Telemetry

Enable telemetry in project config:

```yaml
review:
  local_data:
    root: git_common
  telemetry:
    enabled: true
    path: ${local_data}/telemetry/review-runs.jsonl
    path_mode: anonymized
```

`git_common` keeps telemetry under the shared git common directory for the local clone, so linked worktrees append to the same JSONL file instead of losing metrics when a temporary worktree is removed.

Or enable it for one run:

```bash
apex-ray review --worktree --llm --telemetry
```

Summarize entries:

```bash
apex-ray telemetry-summary
```

Review telemetry v2 includes a schema version, stable hashed repository ID,
run duration, stage durations, process/child peak RSS when the platform
exposes it, target mode, diff size, risk counts, finding counts, reviewer
outcomes, context-pack counts, coverage ratios, partial severity, residual
P0/P1 counts, LLM duration, cache hit/miss counts, failed LLM runs, pack
statuses, model routes, and pre-push triage counters when a gate run
suppresses or prunes findings.

Verification metrics distinguish active approved/rejected decisions,
unresolved attempts, and superseded historical decisions so a provider
failure is not reported as an effective rejection. The v2 compatibility
fields `verification_decisions_count` and
`approved_verification_decisions_count` continue to count full history; use
`active_verification_decisions_count`,
`active_approved_verification_decisions_count`, and
`active_rejected_verification_decisions_count` for current outcomes.

Newly generated configurations explicitly use `path_mode: anonymized`. It omits the repository's absolute
path, stores artifact paths relative to the repository (or only a basename
when outside it), and writes a one-way 16-character repository ID so multiple
runs can still be grouped. For backward compatibility, an older configuration
that omits `path_mode` retains the legacy `full` behavior; add
`path_mode: anonymized` when upgrading it. Set `path_mode: full` only for a deliberately local
dataset that needs absolute paths. This does not anonymize model names,
reviewer IDs, rule IDs, or arbitrary text in other logs; inspect any dataset
before sharing it.

Telemetry is intentionally metric-oriented. It does not store the full Markdown/JSON review artifact. If a team needs full per-run findings, evidence, skipped-pack details, and source-context snapshots for quality debugging, enable `review.reports.archive: true`.

## Local Triage Events

Finding suppressions append local lifecycle events to `review.triage.events_path`, including created, matched, stale, expired, pruned, and removed suppressions. Stale events keep the prior suppression reason so a later agent can re-check the finding instead of blindly renewing it. These events are local audit/tuning data and should stay ignored. `review.triage.events_retention_days` bounds the local event log; set it to `null` only when a team intentionally wants longer local audit history.

Token fields are intentionally split:

- `llm_estimated_input_tokens` is Apex Ray's conservative provider-aware
  pre-run estimate from the generated prompt text and provider scaffold. It
  is used for context budgeting and remains available when a provider does
  not expose usage.
- `llm_actual_*` fields come from the local provider after the call when available. Claude Code JSON output can expose input/output/cache token usage and estimated cost. Codex CLI JSON events can expose token count events in supported versions.
- `llm_input_estimate_ratio` is actual provider-reported input divided by the
  pre-run estimate for that run. Track it by provider/model before tightening
  a budget.
- `llm_estimated_saved_input_tokens` estimates prompt tokens avoided by Apex Ray's local LLM cache.
- `llm_estimated_cost_usd` is a provider/client-side estimate, not authoritative billing.

## PR Eval Telemetry

Historical PR replay can append aggregate JSONL:

```bash
apex-ray eval run-prs --repo /path/to/project --cases .apex-ray/evals/cases --output .apex-ray/evals/runs/latest --llm --telemetry
apex-ray eval telemetry-summary --telemetry-path .apex-ray/eval/telemetry/pr-eval-runs.jsonl
```

PR-eval telemetry has its own schema and may still contain repository paths.
All telemetry can contain model names, reviewer/rule IDs, and operational
metadata. Keep it ignored by default unless a team explicitly anonymizes,
curates, and reviews a shared baseline.

See [Tuning](tuning.md) for the metrics to compare and the order in which to
adjust ignores, analyzer settings, coverage caps, routing, risk policy, and
reviewer budgets.
