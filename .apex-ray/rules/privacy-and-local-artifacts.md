---
id: privacy-and-local-artifacts
title: Keep local, provider, and private artifacts out of commits
severity: high
mode: strict
paths:
  - "**"
triggers:
  text:
    - .apex-ray
    - telemetry
    - reports
    - config.local
    - provider
    - model
    - api_key
    - token
    - benchmark
    - fixture
---
Committed `.apex-ray/config.yml` may define shared, non-secret team policy: provider family such as `codex_cli`, LLM coverage/budget defaults, gate policy, and repository-relative `.apex-ray/...` paths. Do not flag those shared settings by themselves.

Do not commit personal provider overrides, private model aliases, API keys, credentials, `.apex-ray/config.local.yml`, `.apex-ray/cache/`, `.apex-ray/telemetry/`, `.apex-ray/reports/`, raw eval runs, or generated review reports.

Fixtures, benchmark cases, docs, telemetry samples, and regression tests derived from private or corporate projects must be anonymized before they enter this repository. Strip company names, product names, customer identifiers, account numbers, real endpoints, tokens, private domain vocabulary, and proprietary object names unless the artifact is already intentionally public.

Prefer minimal synthetic examples that preserve the bug shape without preserving private business data.
