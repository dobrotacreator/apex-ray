# Rules And Memory

Apex Ray can inject project knowledge into review context without sending the whole repository. Use rules for stable review policy and memory cards for curated team learning.

## When To Use Each

| Need | Use |
| --- | --- |
| A stable invariant should be enforced whenever matching code changes | Rule |
| A finding was a false positive and reviewers should calibrate future verification | Memory |
| A domain term, ownership rule, or recurring bug pattern should survive across reviews | Memory |
| A high-severity constraint should apply to specific paths, symbols, or risk categories | Rule |
| A one-off false positive only matters for the current local gate run | Local finding triage |
| A one-off note only matters for the current PR | Do not commit it as rules or memory |

Rules and memory are selected by relevance before prompt construction. They are not blindly appended to every LLM request.

## Rules

Rules are Markdown files with YAML frontmatter under `.apex-ray/rules/`. They are matched to context packs and injected only when relevant.

Use rules for stable project constraints: API contracts, tenant isolation, auth boundaries, domain state-machine expectations, external provider payload rules, or repository-specific severity policy.

Example:

```md
---
id: payment-capture-idempotency
title: Preserve payment capture idempotency
severity: critical
paths:
  - src/payments/**
triggers:
  symbols:
    - capturePayment
---
Payment capture must remain idempotent. Repeated provider callbacks or retries must not double-charge, double-ship, or publish duplicate accounting events.
```

Keep rules short and actionable. A good rule tells the reviewer what invariant matters and where it applies; it does not restate general engineering advice.

## Memory

Memory cards live under `.apex-ray/memory/` by default and use Markdown with YAML frontmatter:

```md
---
id: cart-total-invariant
title: Preserve cart totals
kind: invariant
severity: high
paths:
  - src/cart.py
triggers:
  symbols:
    - calculate_total
---
Cart total changes must preserve quantity multiplication.
```

Supported `kind` values are `invariant`, `bug_pattern`, `false_positive`, `severity_calibration`, and `glossary`.

Use memory for facts that should survive across local review runs and across team members: domain invariants, recurring bug patterns, known false positives, severity calibration, and glossary terms.

## Prompt Budget

- Apex Ray retrieves memory per context pack instead of injecting all cards.
- `paths`, `context_paths`, `exclude_paths`, and `triggers` scope cards to relevant packs.
- `max_cards_per_pack`, `max_chars_per_pack`, `max_chars_per_card`, and `max_context_ratio` cap prompt growth.
- Memory can be dropped before contracts or metadata are removed from an over-budget context pack.
- `false_positive` and `severity_calibration` cards are verifier-only by default; they calibrate publication decisions without suppressing the review pass.
- For a one-off pre-push false positive, use `apex-ray findings suppress` instead of committing memory. Promote the lesson to memory only when the pattern repeats and generalizes.

Useful commands:

```bash
apex-ray memory lint
apex-ray memory suggest --from-report .apex-ray/reports/review.json --output memory-suggestions.md
```

`memory suggest` uses approved verifier findings by default. Add `--include-unverified` only when you are manually triaging an unverified report.

## Review Before Committing

Commit curated rules, memory cards, and benchmark fixtures. Do not commit raw run artifacts; `.apex-ray/eval/runs/` and `.apex-ray/evals/runs/` are ignored by default.

Before committing a new rule or memory card, check:

- It generalizes beyond one PR.
- Its path and symbol triggers are narrow enough to avoid prompt noise.
- It is written as a concrete review constraint, not a vague preference.
- It does not contain secrets, credentials, private customer data, or unnecessary source excerpts.
- It will still be understandable to a future reviewer without the original incident context.
