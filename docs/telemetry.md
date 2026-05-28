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

Entries include run duration, target mode, diff size, finding counts, context-pack counts, coverage ratios, partial severity, residual P0/P1 counts, token estimates, LLM duration, cache hit/miss counts, failed LLM runs, pack statuses, and model routes.

## PR Eval Telemetry

Historical PR replay can append aggregate JSONL:

```bash
apex-ray eval run-prs --repo /path/to/project --cases .apex-ray/evals/cases --output .apex-ray/evals/runs/latest --llm --telemetry
apex-ray eval telemetry-summary --telemetry-path .apex-ray/eval/telemetry/pr-eval-runs.jsonl
```

Telemetry files can contain repository paths and model names. Keep them ignored by default unless a team explicitly curates and reviews a shared baseline.
