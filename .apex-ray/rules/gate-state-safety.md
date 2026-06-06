---
id: gate-state-safety
title: Keep pre-push gate state conservative
severity: high
mode: strict
paths:
  - "src/apex_ray/cli/gate.py"
  - "src/apex_ray/gate_retry.py"
  - "src/apex_ray/gates.py"
  - "src/apex_ray/git.py"
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
Gate and incremental retry changes must fail closed. Do not clear, downgrade, or hide a blocking finding unless the current reviewed diff/ref or a verifier provides concrete resolution evidence.

State resolution must use the reviewed git refs, not uncommitted working-tree contents, whenever the gate is reasoning about committed `HEAD` state.

Unavailable state files, unreadable refs, invalid reports, provider failures, or uncertain resolution should keep the finding active or mark it uncertain instead of silently passing.

Changes to carry, resolve, stale-state cleanup, quality gate debt, or stdout summaries need regression coverage for pass, block, uncertain, and stale/local-state cases.
