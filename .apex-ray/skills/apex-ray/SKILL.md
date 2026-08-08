---
name: apex-ray
description: Use when running or configuring Apex Ray local code reviews, interpreting reports, continuing partial reviews, tuning rules, memory, telemetry, or historical PR evals.
apex_ray_template_version: 5
---

# Apex Ray

## Purpose

Apex Ray is the project's local diff-aware AI review tool. Use it to create deterministic local review reports, run configured LLM review, continue partial coverage, tune repo rules/memory, inspect telemetry, and replay historical PR evals.

## Process

- Run `uv run --locked apex-ray doctor` when setup, config, provider, or analyzer state is uncertain.
- When Apex Ray is configured in a pre-push hook, do not proactively run `uv run --locked apex-ray review` or `uv run --locked apex-ray gate pre-push` as a routine final verification step; let `git push` invoke the hook so the pre-push incremental retry state remains the source of truth.
- For deterministic local review outside pre-push, run `uv run --locked apex-ray review --no-llm` only when the user asks or when diagnosing Apex Ray; default reports are written under `.apex-ray/reports/`.
- When the user asks, the hook is unavailable, or explicit pre-push gate parity is needed before pushing, run `uv run --locked apex-ray gate pre-push`; blocking findings and critical partial coverage are printed to stdout and the full report is written under `.apex-ray/reports/`.
- Do not bypass the configured pre-push gate by default. Use `uv run --locked apex-ray findings suppress` only for confirmed local false positives after checking the finding evidence, current code, and relevant tests or invariants. Provide a concrete objective reason; do not suppress uncertain findings, real defects, or findings merely to get a push through.
- If bypassing is unavoidable, explain why and name the equivalent checks or review already run.
- Use `--no-llm` or `.apex-ray/config.local.yml` when the configured local provider is unavailable or LLM cost is not appropriate.
- Use `.apex-ray/config.yml` for shared team policy and `.apex-ray/config.local.yml` for personal provider/model/cost overrides.
- Use `.apex-ray/rules/` for stable review rules and `.apex-ray/memory/` for curated team learning.
- Use `uv run --locked apex-ray telemetry-summary` when tuning cost, latency, coverage, or model routing.
- Treat `.apex-ray/reports/*.md/json/html` as latest snapshots. Archived run reports live under configured local data when `review.reports.archive: true`.
- Treat `.apex-ray/triage/` as local ephemeral finding state and audit events; do not commit raw suppressions.
- Use `uv run --locked apex-ray eval capture-prs` and `uv run --locked apex-ray eval run-prs` only for historical PR benchmark/eval work.

### Coverage And Continuation

- Findings apply only to the reviewed scope. Before describing a diff as clean, inspect `llm_coverage.completion_status`, unique context-pack coverage, reviewer-assignment coverage, and `llm_coverage.coverage_todos`. For `partial` or `incomplete`, state the material remaining debt; zero findings does not mean the whole diff was reviewed cleanly.
- Read the full JSON `coverage_todos`: each item identifies the pack, reason, `suggested_command`, and reviewer when applicable. Prefer that targeted command, or deliberately select unreviewed debt with `uv run --locked apex-ray review --continue-from .apex-ray/reports/review.json --residual-priority p0 --llm`, repeatable `--only-pack`, or `--only-slice`. An ordinary continuation is one budgeted pass and may need to be repeated.
- Continue when the user asks, the gate or team policy requires it, or material uncovered risk remains. Prefer P0, high-risk, and exact-pack work, respect configured budgets, and do not raise caps or drain optional reviewer scopes solely to turn the status green.
- When one baseline reviewer scope must finish, run `uv run --locked apex-ray review --continue-from .apex-ray/reports/review.json --reviewer '<baseline-reviewer-id>' --until-complete --strict-coverage --llm --json .apex-ray/reports/review.json`. `--strict-coverage` exits non-zero if bounded completion cannot finish. Completion covers only the selected reviewer's matching assignments; a completed specialist scope does not imply global completion. Use an unfiltered baseline reviewer when the whole reviewable diff must be covered, then re-check report-level coverage.
- Continuation validates the saved review target. If Apex Ray rejects a stale or changed target, run a fresh review instead of reusing archived packs.

## Outputs

Prefer writing generated review artifacts under `.apex-ray/reports/`. Keep Markdown, JSON, and HTML reports together when possible so humans and automation can inspect the same run.

## Boundaries

Do not treat Apex Ray as a replacement for tests, linters, typecheck, CI, dependency scanners, SAST, or human review. Do not commit `.apex-ray/config.local.yml`, `.apex-ray/cache/`, `.apex-ray/telemetry/`, `.apex-ray/reports/`, eval run directories, generated review artifacts, or local provider, model, API, or cost settings unless the team intentionally curates a specific artifact.

### Local Finding Triage

When a pre-push finding is a confirmed local false positive, suppress the specific finding locally instead of bypassing the hook:

```bash
uv run --locked apex-ray findings list --from-report .apex-ray/reports/pre-push.json
uv run --locked apex-ray findings suppress apex-ID \
  --from-report .apex-ray/reports/pre-push.json \
  --reason "The repository layer already enforces this invariant."
```

Use suppressions sparingly. Before suppressing, inspect the finding evidence, the current code, and relevant tests, invariants, or ownership assumptions. The reason must be concrete and objective enough for a later agent to audit. Do not suppress when the finding might be real, when you are unsure, or merely to get a push through.

Triage state is local and ignored by default. It is intended for frequent local review runs, not as shared team policy. A suppression applies only while the finding fingerprint and context-pack fingerprint still match; if relevant context changes, Apex Ray marks the suppression stale, prints the prior reason, and lets the finding block again. Re-check stale findings before suppressing again.

Useful cleanup commands:

```bash
uv run --locked apex-ray findings suppressions
uv run --locked apex-ray findings unsuppress sup-ID
uv run --locked apex-ray findings prune
```
