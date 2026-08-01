---
id: gate-state-test-coverage
title: Preserve conservative gate-state regression coverage
severity: medium
mode: advisory
paths:
  - "tests/test_cli.py"
  - "tests/test_gates.py"
  - "tests/test_git.py"
triggers:
  text:
    - pre-push
    - carried
    - incremental
    - gate
    - state
    - HEAD
---
Gate and incremental retry tests must keep pass, block, uncertain, stale-state,
and local-state behavior explicit.

Prefer public behavior over internal call-order assertions. Cover both the
reviewed git refs and uncommitted worktree boundary whenever a regression could
otherwise clear, downgrade, or hide blocking debt.
