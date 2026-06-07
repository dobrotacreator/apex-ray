---
name: apex-ray
description: Use when running or configuring Apex Ray local code reviews, interpreting reports, continuing partial reviews, tuning rules, memory, telemetry, or historical PR evals.
---

# Apex Ray

## Purpose

Apex Ray is the project's local diff-aware AI review tool. Use it to create deterministic local review reports, run configured LLM review, continue partial coverage, tune repo rules/memory, inspect telemetry, and replay historical PR evals.

## Process

- Run `apex-ray doctor` when setup, config, provider, or analyzer state is uncertain.
- When Apex Ray is configured in a pre-push hook, do not proactively run `apex-ray review` or `apex-ray gate pre-push` as a routine final verification step; let `git push` invoke the hook so the pre-push incremental retry state remains the source of truth.
- For deterministic local review outside pre-push, run `apex-ray review --no-llm` only when the user asks or when diagnosing Apex Ray; default reports are written under `.apex-ray/reports/`.
- When the user asks, the hook is unavailable, or explicit pre-push gate parity is needed before pushing, run `apex-ray gate pre-push`; blocking findings and critical partial coverage are printed to stdout and the full report is written under `.apex-ray/reports/`.
- Do not bypass the configured pre-push gate by default. If bypassing is unavoidable, explain why and name the equivalent checks or review already run.
- Use `--no-llm` or `.apex-ray/config.local.yml` when the configured local provider is unavailable or LLM cost is not appropriate.
- If a report has partial coverage, continue unreviewed work with `apex-ray review --continue-from .apex-ray/reports/review.json --residual-priority p0 --llm` or review a specific skipped pack with `--only-pack`.
- Use `.apex-ray/config.yml` for shared team policy and `.apex-ray/config.local.yml` for personal provider/model/cost overrides.
- Use `.apex-ray/rules/` for stable review rules and `.apex-ray/memory/` for curated team learning.
- Use `apex-ray telemetry-summary --telemetry-path .apex-ray/telemetry/review-runs.jsonl` when tuning cost, latency, coverage, or model routing.
- Treat `.apex-ray/reports/*.md/json/html` as latest snapshots. Use `review.reports.archive: true` only when full per-run report history is needed for quality debugging.
- Use `apex-ray eval capture-prs` and `apex-ray eval run-prs` only for historical PR benchmark/eval work.

## Outputs

Prefer writing generated review artifacts under `.apex-ray/reports/`. Keep Markdown, JSON, and HTML reports together when possible so humans and automation can inspect the same run.

## Boundaries

Do not treat Apex Ray as a replacement for tests, linters, typecheck, CI, dependency scanners, SAST, or human review. Do not commit `.apex-ray/config.local.yml`, `.apex-ray/cache/`, `.apex-ray/telemetry/`, `.apex-ray/reports/`, eval run directories, generated review artifacts, or local provider, model, API, or cost settings unless the team intentionally curates a specific artifact.
