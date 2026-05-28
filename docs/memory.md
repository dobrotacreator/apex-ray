# Apex Ray Repo Memory

Repo memory is curated project knowledge committed with the codebase. It is intended for facts that should survive across local review runs and across team members: domain invariants, recurring bug patterns, known false positives, severity calibration, and glossary terms.

Memory cards live under `.apex-ray/memory/` by default and use Markdown with YAML frontmatter:

```md
---
id: cart-total-invariant
title: Preserve cart totals
kind: invariant
severity: high
paths:
  - src/cart.ts
triggers:
  symbols:
    - calculateTotal
---
Cart total changes must preserve quantity multiplication.
```

Supported `kind` values are `invariant`, `bug_pattern`, `false_positive`, `severity_calibration`, and `glossary`.

Prompt budget is deliberately bounded:

- Apex Ray retrieves memory per context pack instead of injecting all cards.
- `paths`, `context_paths`, `exclude_paths`, and `triggers` scope cards to relevant packs.
- `max_cards_per_pack`, `max_chars_per_pack`, `max_chars_per_card`, and `max_context_ratio` cap prompt growth.
- Memory can be dropped before contracts or metadata are removed from an over-budget context pack.
- `false_positive` and `severity_calibration` cards are verifier-only by default; they calibrate publication decisions without suppressing the review pass.

Useful commands:

```bash
apex-ray memory lint
apex-ray memory suggest --from-report review.json --output memory-suggestions.md
```

Commit curated memory cards and benchmark fixtures. Do not commit raw run artifacts; `.apex-ray/eval/runs/` and `.apex-ray/evals/runs/` are ignored by default.
