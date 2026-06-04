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
    effort: medium
    coverage_mode: balanced
    max_packs: 64
    max_deep_packs: 48
    max_input_tokens: 300000
    verify: true
  telemetry:
    enabled: false
    path: .apex-ray/telemetry/review-runs.jsonl
  reports:
    archive: false
    archive_dir: .apex-ray/reports/runs
    retention: 20
  gates:
    pre_push:
      enabled: true
      min_finding_severity: high
      require_verified_findings: true
      fail_on_quality_gate: true
      fail_on_partial_severity: critical
      max_stdout_findings: 10
      stdout_format: agent
      auto_followup_p0: true
      progress: auto
      progress_interval_seconds: 5
```

## Local Override Example

```yaml
review:
  llm:
    jobs: 2
    provider: claude_code_cli
    model: "<personal-model-or-alias>"
    effort: medium
    claude_path: claude
    timeout_seconds: 900
    max_input_tokens: 80000
  telemetry:
    path: .apex-ray/telemetry/local-review-runs.jsonl
```

## Common Local Overrides

Disable LLM review on a machine that should stay deterministic or offline:

```yaml
review:
  llm:
    enabled: false
```

Use a personal provider and smaller budget without changing team policy:

```yaml
review:
  llm:
    provider: claude_code_cli
    model: "<personal-model-or-alias>"
    jobs: 2
    max_input_tokens: 80000
```

Keep telemetry local to your machine:

```yaml
review:
  telemetry:
    enabled: true
    path: .apex-ray/telemetry/local-review-runs.jsonl
```

Use `.apex-ray/config.yml` for shared policy and `.apex-ray/config.local.yml` for provider, model, cost, cache, timeout, and telemetry differences between contributors.

## Language Selection

By default Apex Ray reviews every reviewable diff file it can classify and builds analyzer-backed context where a backend exists. Today the enhanced analyzer backends cover TypeScript/JavaScript and Python.

Use `review.languages` only when a project wants to restrict review scope:

```yaml
review:
  languages:
    - typescript
    - javascript
    - python
```

Files in detected but disabled languages are reported as ignored. Go and Rust can be discovered as project languages today, but enhanced analyzer support for them is planned rather than available.

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

Tune coverage with:

- `max_packs`: total LLM-reviewable pack cap.
- `max_deep_packs`: cap for full deep review.
- `max_input_tokens`: approximate total LLM review input-token budget.
- `coverage_mode`: breadth/depth strategy.

Prefer `balanced` for normal team use. Use `fast` for cheap smoke review and `exhaustive` for high-risk changes when provider cost and latency are acceptable.

## Reports

Review and gate commands always write latest report files to the configured `--output`, `--json`, and optional `--html` paths. Reusing the same paths overwrites those latest files.

Set `review.reports.archive: true` to also copy each generated report into a run directory under `review.reports.archive_dir`. This preserves full Markdown/JSON/HTML artifacts for review-quality debugging while keeping the latest paths stable for agents and hooks.

```yaml
review:
  reports:
    archive: true
    archive_dir: .apex-ray/reports/runs
    retention: 20
```

`retention` keeps the newest run directories and prunes older ones. Set `retention: null` to disable pruning. Report archives may contain source snippets, findings, file paths, and provider metadata; keep `.apex-ray/reports/` ignored unless the team intentionally curates a specific artifact.

## Pre-Push Gate

`apex-ray gate pre-push` runs a base-branch review and applies `review.gates.pre_push`.

Default behavior:

- compare `review.base...HEAD`;
- write `.apex-ray/reports/pre-push.md` and `.apex-ray/reports/pre-push.json`;
- block on verified `high` or `critical` findings;
- block on failed LLM coverage quality gate;
- block on `critical` partial coverage;
- print live progress to stderr and a compact, agent-readable summary to stdout.

Set `review.gates.pre_push.enabled: false` in local config to skip the hook gate. Prefer local config for personal cost/model/provider differences instead of editing the shared hook command.

Set `review.llm.enabled: false` in local config when a machine should keep normal review and pre-push gate runs deterministic and offline.

`review.gates.pre_push.progress` controls live hook output:

- `auto`: show progress for local runs and suppress it when `CI` is set.
- `always`: always print progress to stderr.
- `never`: suppress progress.

`progress_interval_seconds` throttles repeated per-pack counters while still forcing major stage messages and final counters.

### Incremental Retry

Set `review.gates.pre_push.incremental_retry.enabled: true` to speed up repeated pre-push attempts after a previous gate run.

```yaml
review:
  gates:
    pre_push:
      incremental_retry:
        enabled: true
        state_path: .apex-ray/reports/pre-push-state.json
```

The first run still reviews `review.base...HEAD`. Later eligible retry runs review only `previous_gate_head..HEAD`, carry forward unresolved blocking findings and coverage debt, and write combined gate state to `state_path`.

Incremental retry is fail-closed:

- previous verified blocking findings keep blocking until the resolution verifier returns `resolved`;
- `still_present` and `uncertain` resolution results keep blocking;
- critical carried coverage debt is not cleared by a delta-only run;
- missing state, missing previous HEAD, merge-base changes, or config/rule/memory/model/prompt/gate-policy changes fall back to a full `review.base...HEAD` review.

## Config Validation

Run diagnostics after changing configuration:

```bash
apex-ray doctor
```

Run a no-LLM review to verify discovery, ignores, analyzer coverage, and report paths without provider cost:

```bash
apex-ray review --worktree --no-llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json
```
