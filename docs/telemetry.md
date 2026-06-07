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

Entries include a schema version, run duration, target mode, diff size, finding counts, context-pack counts, coverage ratios, partial severity, residual P0/P1 counts, LLM duration, cache hit/miss counts, failed LLM runs, pack statuses, model routes, and pre-push triage counters when a gate run suppresses or prunes findings.

Telemetry is intentionally metric-oriented. It does not store the full Markdown/JSON review artifact. If a team needs full per-run findings, evidence, skipped-pack details, and source-context snapshots for quality debugging, enable `review.reports.archive: true`.

## Local Triage Events

Finding suppressions append local lifecycle events to `review.triage.events_path`, including created, matched, stale, expired, pruned, and removed suppressions. These events are local audit/tuning data and should stay ignored. `review.triage.events_retention_days` bounds the local event log; set it to `null` only when a team intentionally wants longer local audit history.

Token fields are intentionally split:

- `llm_estimated_input_tokens` is Apex Ray's pre-run estimate from the generated prompt text. It is used for context budgeting and remains available even when a provider does not expose usage.
- `llm_actual_*` fields come from the local provider after the call when available. Claude Code JSON output can expose input/output/cache token usage and estimated cost. Codex CLI JSON events can expose token count events in supported versions.
- `llm_estimated_saved_input_tokens` estimates prompt tokens avoided by Apex Ray's local LLM cache.
- `llm_estimated_cost_usd` is a provider/client-side estimate, not authoritative billing.

## PR Eval Telemetry

Historical PR replay can append aggregate JSONL:

```bash
apex-ray eval run-prs --repo /path/to/project --cases .apex-ray/evals/cases --output .apex-ray/evals/runs/latest --llm --telemetry
apex-ray eval telemetry-summary --telemetry-path .apex-ray/eval/telemetry/pr-eval-runs.jsonl
```

Telemetry files can contain repository paths and model names. Keep them ignored by default unless a team explicitly curates and reviews a shared baseline.
