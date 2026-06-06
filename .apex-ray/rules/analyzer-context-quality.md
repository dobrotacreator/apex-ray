---
id: analyzer-context-quality
title: Preserve analyzer-backed review context quality
severity: high
mode: strict
paths:
  - "src/apex_ray/analyzers/**"
  - "analyzer-runtimes/typescript/src/**"
  - "analyzer-runtimes/typescript/test/**"
  - "tests/test_analyzers.py"
  - "tests/test_python_analyzer_contracts.py"
  - "tests/benchmarks/**"
triggers:
  text:
    - AnalyzerResult
    - AnalyzerSymbol
    - ContextPack
    - changed_symbols
    - references
    - metadata
    - related_tests
---
Analyzer changes must preserve complete, schema-valid context rather than returning partial-looking success.

Symbols and references must keep stable repository-relative file paths, `start_line` and `end_line` values, signatures, snippets, and metadata required by Python model validation. Do not emit symbols or context-pack entries with missing line ranges.

Timeouts, sharding, cache reads, parse failures, and partial analyzer failures must produce explicit warnings and must not make report coverage look complete.

New analyzer behavior should include focused fixtures or benchmark cases that exercise the changed context layer, especially for references, callees, contracts, metadata, framework boundaries, and related-test discovery.
