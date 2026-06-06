---
id: packaging-and-ci-contracts
title: Keep packaging, CI, and runtime discovery aligned
severity: high
mode: strict
paths:
  - "pyproject.toml"
  - "uv.lock"
  - ".github/workflows/**"
  - "release-please-config.json"
  - ".release-please-manifest.json"
  - "src/apex_ray/__init__.py"
  - "src/apex_ray/cli/**"
  - "src/apex_ray/discovery.py"
  - "analyzer-runtimes/typescript/package.json"
  - "analyzer-runtimes/typescript/package-lock.json"
  - "analyzer-runtimes/typescript/tsconfig*.json"
triggers:
  text:
    - package
    - console_scripts
    - apex-ray
    - analyzer
    - wheel
    - CI
---
Packaging, release, and CI changes must keep the installed wheel, local editable install, and repository checkout behavior aligned.

The `apex-ray` console entry point, bundled TypeScript analyzer runtime, Python analyzer availability, package data, supported Python and Node versions, docs build, and smoke tests must continue to agree.

Do not change build outputs, release metadata, dependency files, or workflow coverage without updating the corresponding smoke path or explaining why the existing CI coverage still proves the installed package works.
