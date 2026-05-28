# Configuration

Apex Ray reads project configuration from `.apex-ray/config.yml`.

The file is intended to be committed when it describes shared review behavior: base branch, ignored paths, rule paths, memory paths, LLM routing, coverage mode, and telemetry policy.

Machine-specific settings should live in `.apex-ray/config.local.yml`. Local config is ignored by `apex-ray init` and is loaded automatically unless a command uses an explicit `--config` path.

Merge order:

```text
built-in defaults < .apex-ray/config.yml < .apex-ray/config.local.yml < CLI flags
```

Maps are merged deeply. Lists are replaced. This lets shared config own policy while each contributor can override provider, model, CLI executable paths, timeout, jobs, cache path, telemetry path, or coverage budget locally.

## Minimal Example

```yaml
review:
  base: main
  ignore:
    - "**/*.lock"
    - "**/generated/**"
  rule_paths:
    - .apex-ray/rules
  memory:
    enabled: true
    paths:
      - .apex-ray/memory
  llm:
    enabled: true
    provider: codex_cli
    coverage_mode: balanced
    max_input_tokens: 120000
    verify: true
  telemetry:
    enabled: false
    path: .apex-ray/telemetry/review-runs.jsonl
```

## Local Override Example

```yaml
review:
  llm:
    jobs: 2
    provider: claude_code_cli
    model: "<personal-model-or-alias>"
    claude_path: claude
    timeout_seconds: 900
    max_input_tokens: 80000
  telemetry:
    path: .apex-ray/telemetry/local-review-runs.jsonl
```

## Rules

Rules are Markdown files with YAML frontmatter under `.apex-ray/rules/`. Rules are injected only into matching context packs.

Use rules for stable project invariants: API contracts, tenant isolation, auth boundaries, domain state-machine expectations, or external provider payload rules.

## Memory

Memory cards are Markdown files with YAML frontmatter under `.apex-ray/memory/`. They provide lightweight team learning and calibration. Keep cards short and reviewable.

Use memory for known false positives, recurring review patterns, severity calibration, and project-specific vocabulary.

## Coverage

`review.llm.coverage_mode` controls how much of a diff receives LLM review:

- `fast`: capped deep review.
- `balanced`: deep review for high-value packs plus shallow breadth under token budget.
- `exhaustive`: review every reviewable pack when budget allows.

Reports show partial severity, reviewed/unreviewed packs, residual P0/P1 work, and continuation commands.
