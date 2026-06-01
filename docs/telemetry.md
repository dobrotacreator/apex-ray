# Telemetry

Apex Ray telemetry is local, append-only JSONL intended for team tuning. It is not sent anywhere by Apex Ray.

## Local Review Telemetry

Enable telemetry in project config:

```yaml
review:
  telemetry:
    enabled: true
    path: .apex-ray/telemetry/review-runs.jsonl
```

Or enable it for one run:

```bash
apex-ray review --worktree --llm --telemetry
```

Summarize entries:

```bash
apex-ray telemetry-summary --telemetry-path .apex-ray/telemetry/review-runs.jsonl
```

Entries include a schema version, run duration, target mode, diff size, finding counts, context-pack counts, coverage ratios, partial severity, residual P0/P1 counts, LLM duration, cache hit/miss counts, failed LLM runs, pack statuses, and model routes.

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
