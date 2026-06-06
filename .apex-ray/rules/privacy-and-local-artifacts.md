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
Do not commit local provider settings, model names chosen for a private machine, API keys, credentials, `.apex-ray/config.local.yml`, `.apex-ray/cache/`, `.apex-ray/telemetry/`, `.apex-ray/reports/`, raw eval runs, or generated review reports.

Fixtures, benchmark cases, docs, telemetry samples, and regression tests derived from private or corporate projects must be anonymized before they enter this repository. Strip company names, product names, customer identifiers, account numbers, real endpoints, tokens, private domain vocabulary, and proprietary object names unless the artifact is already intentionally public.

Prefer minimal synthetic examples that preserve the bug shape without preserving private business data.
