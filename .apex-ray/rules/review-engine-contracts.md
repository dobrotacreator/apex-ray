---
id: review-engine-contracts
title: Preserve review engine and report contracts
severity: high
mode: strict
paths:
  - "src/apex_ray/config.py"
  - "src/apex_ray/context/**"
  - "src/apex_ray/llm/**"
  - "src/apex_ray/memory.py"
  - "src/apex_ray/models.py"
  - "src/apex_ray/pipeline/**"
  - "src/apex_ray/report/**"
  - "src/apex_ray/rules.py"
  - "tests/test_config.py"
  - "tests/test_context.py"
  - "tests/test_llm.py"
  - "tests/test_memory.py"
  - "tests/test_pipeline.py"
  - "tests/test_report.py"
  - "tests/test_rules.py"
triggers:
  text:
    - ReviewReport
    - ContextPack
    - Finding
    - llm
    - coverage
    - prompt
    - rules
    - memory
---
Changes to review orchestration, context selection, prompts, routing, cache, rule/memory loading, report loading, or JSON models must preserve public report compatibility unless the change is explicitly versioned and documented.

Do not silently discard findings, context packs, analyzer warnings, provider errors, verification failures, partial coverage, or residual review work. Surface them through reports, coverage summaries, or gate decisions.

Budgeting and selection changes must preserve deterministic ordering and explainable residual work so `--continue-from`, `--only-pack`, and quality gate summaries remain trustworthy.

Prompt or verifier changes should keep findings constrained to concrete diff-caused issues and include tests or benchmark updates when they alter routing, coverage, schema parsing, severity, or confidence behavior.
