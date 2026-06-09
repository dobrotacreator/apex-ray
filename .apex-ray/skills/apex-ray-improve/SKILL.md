---
name: apex-ray-improve
description: Use after merged PRs or review feedback to produce recommendation-only improvements for Apex Ray memory, rules, eval labels, telemetry, coverage, model routing, or config from PR comments, Greptile findings, Apex reports, and telemetry.
apex_ray_template_version: 2
---

# Apex Ray Improve

## Purpose

Run a post-merge learning pass. The goal is not to review the PR again; it is to decide whether Apex Ray should learn from what happened through repo memory, rules, eval labels, telemetry interpretation, coverage tuning, or config changes.

## Process

- Identify the PR number, repository root, base branch, merge commit, and whether the PR is merged. If the PR is not merged, label the output as a review-feedback learning pass instead of a post-merge pass.
- Collect PR signals with GitHub CLI when available: `gh pr view <number> --json number,title,state,mergedAt,mergeCommit,baseRefName,headRefName,author,comments,reviews,files,url` and review-thread comments from `gh api repos/<owner>/<repo>/pulls/<number>/comments --paginate`.
- Separate Greptile comments, human comments, CI/bot comments, and author follow-up commits. Treat comments as evidence, not ground truth.
- Inspect Apex Ray artifacts when present: `.apex-ray/reports/`, `.apex-ray/evals/cases/pr-<number>/`, `.apex-ray/evals/runs/*/pr-<number>/`, `.apex-ray/eval/labels/`, local review telemetry, and PR eval telemetry.
- If a comparable eval case is missing and the user asked for a fresh analysis, capture or replay narrowly with `apex-ray eval capture-prs --pr <number>` and `apex-ray eval run-prs` rather than running a broad historical benchmark.
- Compare external findings with Apex Ray findings. Call out missed issues, duplicate findings, false positives, findings outside scope, and true positives that Apex Ray found first.
- Look for durable learning candidates: recurring domain invariants, security or money-movement bug patterns, known false positives, severity calibration, rule gaps, coverage gaps, oversized packs, token budget pressure, timeout/provider failures, and poor model routing.
- Prefer small, reviewable suggestions. Draft memory/rule/config changes as proposals only; do not edit `.apex-ray/memory/`, `.apex-ray/rules/`, labels, or config unless the user explicitly asks to apply them.

## Output

Produce a concise recommendation report with these sections when relevant:

- `Summary`: whether Apex Ray needs tuning for this PR.
- `Missed Or Weak Signals`: external findings Apex Ray missed or under-ranked, with evidence.
- `False Positives Or Noise`: Apex Ray findings that appear wrong, duplicated, or not actionable.
- `Coverage And Cost`: partial severity, unreviewed P0/P1 packs, token estimates, duration, cache behavior, provider failures, and model route observations.
- `Recommended Memory`: draft card intent, paths/triggers, and why it is stable enough to consider.
- `Recommended Rules`: rule intent, matching scope, severity, and examples.
- `Recommended Config Or Eval Changes`: concrete tuning or label suggestions.
- `No Action`: items reviewed but intentionally not recommended.

## Boundaries

Keep this workflow recommendation-only by default. Do not commit raw comments, raw telemetry, eval run directories, reports, provider settings, or private identifiers. Do not turn one-off PR feedback into repo memory unless it generalizes beyond that PR. Do not use Apex Ray learning as a substitute for fixing the product code, tests, CI, or human review process.
