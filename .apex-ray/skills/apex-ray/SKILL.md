---
name: apex-ray
description: Use when running or configuring Apex Ray local code reviews, interpreting reports, continuing partial reviews, tuning rules, memory, telemetry, or historical PR evals.
apex_ray_template_version: 2
---

# Apex Ray

## Purpose

Apex Ray is the project's local diff-aware AI review tool. Use it to create deterministic local review reports, run configured LLM review, continue partial coverage, tune repo rules/memory, inspect telemetry, and replay historical PR evals.

## Process

- Run `apex-ray doctor` when setup, config, provider, or analyzer state is uncertain.
- When Apex Ray is configured in a pre-push hook, do not proactively run `apex-ray review` or `apex-ray gate pre-push` as a routine final verification step; let `git push` invoke the hook so the pre-push incremental retry state remains the source of truth.
- For deterministic local review outside pre-push, run `apex-ray review --no-llm` only when the user asks or when diagnosing Apex Ray; default reports are written under `.apex-ray/reports/`.
- When the user asks, the hook is unavailable, or explicit pre-push gate parity is needed before pushing, run `apex-ray gate pre-push`; blocking findings and critical partial coverage are printed to stdout and the full report is written under `.apex-ray/reports/`.
- Do not bypass the configured pre-push gate by default. Use `apex-ray findings suppress` only for confirmed local false positives after checking the finding evidence, current code, and relevant tests or invariants. Provide a concrete objective reason; do not suppress uncertain findings, real defects, or findings merely to get a push through.
- If bypassing is unavoidable, explain why and name the equivalent checks or review already run.
- Use `--no-llm` or `.apex-ray/config.local.yml` when the configured local provider is unavailable or LLM cost is not appropriate.
- If a report has partial coverage, continue unreviewed work with `apex-ray review --continue-from .apex-ray/reports/review.json --residual-priority p0 --llm` or review a specific skipped pack with `--only-pack`.
- Use `.apex-ray/config.yml` for shared team policy and `.apex-ray/config.local.yml` for personal provider/model/cost overrides.
- Use `.apex-ray/rules/` for stable review rules and `.apex-ray/memory/` for curated team learning.
- Use `apex-ray telemetry-summary` when tuning cost, latency, coverage, or model routing.
- Treat `.apex-ray/reports/*.md/json/html` as latest snapshots. Archived run reports live under configured local data when `review.reports.archive: true`.
- Treat `.apex-ray/triage/` as local ephemeral finding state and audit events; do not commit raw suppressions.
- Use `apex-ray eval capture-prs` and `apex-ray eval run-prs` only for historical PR benchmark/eval work.

## Outputs

Prefer writing generated review artifacts under `.apex-ray/reports/`. Keep Markdown, JSON, and HTML reports together when possible so humans and automation can inspect the same run.

## Boundaries

Do not treat Apex Ray as a replacement for tests, linters, typecheck, CI, dependency scanners, SAST, or human review. Do not commit `.apex-ray/config.local.yml`, `.apex-ray/cache/`, `.apex-ray/telemetry/`, `.apex-ray/reports/`, eval run directories, generated review artifacts, or local provider, model, API, or cost settings unless the team intentionally curates a specific artifact.

### Local Finding Triage

When a pre-push finding is a confirmed local false positive, suppress the specific finding locally instead of bypassing the hook:

```bash
apex-ray findings list --from-report .apex-ray/reports/pre-push.json
apex-ray findings suppress apex-<id> \
  --from-report .apex-ray/reports/pre-push.json \
  --reason "The repository layer already enforces this invariant."
```

Use suppressions sparingly. Before suppressing, inspect the finding evidence, the current code, and relevant tests, invariants, or ownership assumptions. The reason must be concrete and objective enough for a later agent to audit. Do not suppress when the finding might be real, when you are unsure, or merely to get a push through.

Triage state is local and ignored by default. It is intended for frequent local review runs, not as shared team policy. A suppression applies only while the finding fingerprint and context-pack fingerprint still match; if relevant context changes, Apex Ray marks the suppression stale, prints the prior reason, and lets the finding block again. Re-check stale findings before suppressing again.

Useful cleanup commands:

```bash
apex-ray findings suppressions
apex-ray findings unsuppress sup-<id>
apex-ray findings prune
```
