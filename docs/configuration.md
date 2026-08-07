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
  local_data:
    root: git_common
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
    jobs: 2
    coverage_mode: balanced
    max_packs: 48
    max_deep_packs: 16
    max_input_tokens: 180000
    max_consecutive_provider_failures: 3
    verify: true
    cache_dir: ${local_data}/cache/llm
  telemetry:
    enabled: true
    path: ${local_data}/telemetry/review-runs.jsonl
    path_mode: anonymized
  reports:
    archive: true
    archive_dir: ${local_data}/reports/runs
    retention: 20
    compression: auto
    compression_min_bytes: 65536
  triage:
    enabled: true
    state_path: ${local_data}/triage/suppressions.json
    events_path: ${local_data}/triage/events.jsonl
    default_expiry_days: 14
    max_active_suppressions: 200
    events_retention_days: 90
  gates:
    pre_push:
      enabled: true
      min_finding_severity: high
      require_verified_findings: true
      fail_on_quality_gate: true
      fail_on_partial_severity: critical
      max_stdout_findings: 10
      stdout_format: agent
      auto_followup: true
      auto_followup_max_pack_reviews: 16
      progress: auto
      progress_interval_seconds: 5
```

## Local Data

`review.local_data.root` defines where long-lived local artifacts are stored when a path starts with `${local_data}`. `apex-ray init` sets it to `git_common`, which resolves to an Apex Ray directory under the repository's shared git common directory. Linked worktrees from the same local clone then share telemetry, LLM cache entries, and archived report runs, even when individual worktree directories are deleted.

Latest report outputs still stay at their configured `--output`, `--json`, and `--html` paths, usually under the current worktree's `.apex-ray/reports/`, so parallel worktrees do not overwrite each other's latest snapshots.

For a `--worktree` target, in-repository report and writable cache paths must
be untracked and Git-ignored. This keeps checkpoint, final-report, LLM-cache,
and analyzer-cache writes outside the diff identity used by safe continuation.
The check also covers relative `APEX_RAY_CACHE_HOME` and `XDG_CACHE_HOME`
values. Commit the `.apex-ray/.gitignore` created by `apex-ray init`, then use
its ignored paths or paths outside the repository.

## Agent Artifact Refresh

`apex-ray init` writes `.apex-ray/version` as the repository's single Apex Ray version lock. Generated hooks and agent instructions embed that exact version with `uvx --python 3.14 apex-ray@<version> ...`; they never evaluate the lock as shell code. A matching lock is enforced before operational commands start. Legacy repositories without a lock continue to run for backward compatibility until they are migrated, while malformed and mismatched locks fail with an exact `uvx` remediation command.

`uvx` downloads a cold exact tool version from the package index, then reuses uv's tool cache on later runs. Install `uv` on developer and CI machines and pre-warm the pinned command where cold-start latency or offline pushes matter.

Managed agent guidance in `AGENTS.md`, Claude instruction files, and generated Apex Ray skills is also template-versioned. When a newer Apex Ray package has updated templates, `apex-ray doctor`, `apex-ray review`, and `apex-ray gate pre-push` emit a non-blocking local warning if existing managed agent artifacts are outdated.

Refresh only the managed agent artifacts without touching config or hooks:

```bash
apex-ray init --refresh-agent-artifacts --dry-run
apex-ray init --refresh-agent-artifacts
```

The refresh preserves user-authored text outside the `<!-- APEX_RAY_START -->` / `<!-- APEX_RAY_END -->` block and refreshes generated skills. For Codex, it also migrates legacy file-level `SKILL.md` symlinks to discoverable skill-directory symlinks, with a full directory-copy fallback on systems where symlinks are unavailable. Conflicting unmanaged skill directories are reported instead of overwritten. Refresh does not run automatically during review or pre-push, so Apex Ray never changes the working tree while evaluating a diff.

To migrate all managed launchers, or to upgrade a repository to the package version being run, use the explicit managed refresh:

```bash
# Adopt a lock without changing its existing version.
APEX_RAY_TARGET_VERSION=0.1.13
uvx --python 3.14 apex-ray@"${APEX_RAY_TARGET_VERSION}" init --refresh-managed-artifacts --dry-run
uvx --python 3.14 apex-ray@"${APEX_RAY_TARGET_VERSION}" init --refresh-managed-artifacts

# Upgrade the one lock and every recognized derived artifact together.
APEX_RAY_TARGET_VERSION=0.1.14
uvx --python 3.14 apex-ray@"${APEX_RAY_TARGET_VERSION}" init \
  --refresh-managed-artifacts --update-version-lock
```

The upgrade preflights the lock, hook, agent blocks, and skills before writing; it updates the lock last. It preserves the repository's existing Apex Ray Git or Lefthook mode when `--hooks` is omitted, and exact legacy generated hooks are migrated automatically. Custom, ambiguous, or duplicate Apex Ray hooks fail closed with manual-remediation guidance instead of being overwritten or causing two reviews. `doctor` checks lock/runtime agreement, exact managed hooks, `uvx` availability, and agent artifact freshness.

Apex Ray versions released before version-lock support cannot enforce a future lock themselves. The generated exact `uvx` hook is therefore the execution guarantee after migration. Publish the target Apex Ray version before merging downstream lock/hook updates that reference it.

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
    path: ${local_data}/telemetry/local-review-runs.jsonl
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

Keep telemetry in a different local file:

```yaml
review:
  telemetry:
    enabled: true
    path: ${local_data}/telemetry/local-review-runs.jsonl
```

Use `.apex-ray/config.yml` for shared policy and `.apex-ray/config.local.yml` for provider, model, cost, cache, timeout, and telemetry differences between contributors.

## Language Selection

By default Apex Ray reviews every reviewable diff file it can classify and builds analyzer-backed context where a backend exists. Today the enhanced analyzer backends cover TypeScript/JavaScript, Python, Go, and Dart/Flutter.

Use `review.languages` only when a project wants to restrict review scope:

```yaml
review:
  languages:
    - typescript
    - javascript
    - python
    - go
    - dart
```

Files in detected but disabled languages are reported as ignored. Rust can be discovered as a project language today, but enhanced analyzer support for it is planned rather than available.

## Dart And Flutter Analyzer

Dart review uses the Analysis Server from the SDK selected by the project. The
default configuration is backwards compatible and bounded:

```yaml
review:
  analyzer:
    timeout_seconds: 120
    index_cache_enabled: true
    dart:
      enabled: true
      command: []
      flutter: auto
      plugins: true
      max_changed_symbols: 80
      max_references_per_symbol: 24
      max_callees_per_symbol: 16
      max_related_tests_per_file: 12
      max_dependency_package_anchors: 16
```

`command` must be an argument list; it is never parsed by a shell. When it is
empty, Apex Ray selects the SDK in this order:

1. project-local `.fvm/flutter_sdk/bin/dart`;
2. `dart` on `PATH`;
3. `fvm dart` when FVM is on `PATH`;
4. the Dart executable next to an unambiguous `flutter` executable.

Use an explicit argument list such as `command: [/opt/flutter/bin/dart]` for a
custom SDK wrapper or fixed CI installation. `flutter` accepts `auto`,
`enabled`, or `disabled`. `plugins` defaults to `true` for compatibility with
trusted local project analysis; set it to `false` when analyzing an untrusted
checkout with an SDK that supports disabling analyzer plugins. The semantic
limits cap graph fan-out. `max_dependency_package_anchors` additionally bounds
the reverse-dependent local packages opened to discover cross-package
consumers. The global analyzer timeout bounds the server and all requests.

Apex Ray does not install an SDK, run `pub get`, or invoke code generation.
Resolve dependencies with the selected SDK before review. Common generated
Dart outputs such as `*.g.dart`, `*.freezed.dart`, `*.config.dart`,
`*.mocks.dart`, `*.gr.dart`, and `*.chopper.dart` remain available for symbol
resolution but are excluded from review targets and raw context snippets. Do
not broadly ignore them: keeping them in the analyzer inventory preserves
handwritten-to-generated relationships without spending prompt budget.

An unavailable SDK, server failure, timeout, or per-file error produces an
analyzer warning and diff-only context for the affected Dart files without
discarding successful results from other languages. Complete results use the
analyzer cache; use `--refresh-analyzer-cache` only while diagnosing stale SDK
or package state.

## Rules

Rules are Markdown files with YAML frontmatter under `.apex-ray/rules/`. Rules are injected only into matching context packs.

Use rules for stable project invariants: API contracts, tenant isolation, auth boundaries, domain state-machine expectations, or external provider payload rules.

## Memory

Memory cards are Markdown files with YAML frontmatter under `.apex-ray/memory/`. They provide lightweight team learning and calibration. Keep cards short and reviewable.

Use memory for known false positives, recurring review patterns, severity calibration, and project-specific vocabulary.

## Project Risk Policy

Built-in signals identify common auth, persistence, API, shell, I/O, schema,
migration, validation, and concurrency surfaces. `review.risk.rules` adds
project-specific, explainable risk without hard-coding domain knowledge into
Apex Ray:

```yaml
review:
  risk:
    built_in_enabled: true
    rules:
      - id: settlement-boundary
        title: Money movement and ledger settlement
        severity: critical
        score: 98
        paths:
          - "src/settlement/**"
          - "src/ledger/**"
        exclude_paths:
          - "**/*.test.ts"
        languages: [typescript]
        file_kinds: [source, migration]
        statuses: [added, modified, renamed]
        text:
          - transfer
          - rounding
          - idempotency
        risk:
          - persistence
          - external_io
        categories: [financial, money_movement]
        reviewer_tags: [finance]
        guidance: Preserve authorization, idempotency, currency precision, and ledger balance.
```

Path/language/kind/status filters are combined with AND. `text` tokens match
changed lines and localize the signal to those lines. `risk` tokens match
built-in signals on the file. Within `text` and `risk`, any listed token can
trigger the rule. If neither trigger list is present, matching scope alone
creates one file-level signal.

`severity` drives residual P0/P1 coverage. `score` (0–100) refines pack
priority within that policy; an explicit zero is respected. Categories,
reviewer tags, and guidance are preserved in context packs and reports so an
agent can explain why a change was routed and what invariant to inspect.

Keep these rules about business impact, not coding style. Use project rules
and memory cards for detailed behavioral constraints; use risk policy to
decide attention, depth, and specialist routing.

## Focused Reviewers

`review.reviewers` runs independent passes with distinct focus, scope, model
profile, verification profile, depth, and budget. Findings retain reviewer
provenance and duplicate findings from multiple specialists are merged
without losing attribution.

```yaml
review:
  reviewers:
    - id: correctness
      name: General correctness
      focus: Diff-caused behavioral regressions and concrete failure modes.
      max_packs: 48
      max_deep_packs: 16

    - id: security
      name: Security reviewer
      focus: Authentication, authorization, injection, secrets, SSRF, and trust boundaries.
      instructions:
        - Report an exploit path and affected trust boundary.
        - Do not duplicate generic maintainability feedback.
      paths:
        - "src/**"
        - ".github/workflows/**"
      exclude_paths:
        - "**/*.test.ts"
      risk: [auth, shell, external_io]
      risk_tags: [security]
      profile: broad
      verify_profile: strong
      review_depth: balanced
      max_packs: 20
      max_deep_packs: 10
      max_input_tokens: 120000
      verify: true
      required: true

    - id: finance
      name: Financial risk reviewer
      focus: Money movement, precision, idempotency, reconciliation, and loss exposure.
      risk_tags: [finance]
      review_depth: deep
      max_packs: 12
```

`paths`, `exclude_paths`, `file_kinds`, `risk`, and `risk_tags` scope which
context packs a reviewer receives. A reviewer with no scope filters sees all
packs. `review_depth` is:

- `balanced`: selected high-value packs are deep-reviewed and remaining
  selected packs receive the compact shallow pass;
- `deep`: only the deep selection is run;
- `shallow`: all selected packs receive the compact pass.

Set `required: true` when that specialist must finish every selected
reviewer-pack assignment for the coverage quality gate to pass. Matching packs
deferred by selection limits remain visible in reviewer coverage and
continuation todos for both required and optional reviewers, but do not turn a
bounded, risk-based run into exhaustive review. Use `--strict-coverage`,
`--until-complete`, or the Action's `coverage-policy: complete` when every
matching assignment must finish.

An applicable required reviewer that matches packs but selects none still
fails closed: the gate has no successful reviewer assignment to trust. Once at
least one pack is selected, additional matching packs deferred by bounded
selection remain explicit completion debt rather than silently expanding that
ordinary run.

Run every enabled reviewer by default, or select one or more explicitly:

```bash
apex-ray review --base main --reviewer security --reviewer finance
apex-ray gate pre-push --reviewer security
```

Repeated `--reviewer` is useful for local investigation and CI matrices.
Unknown and disabled reviewer IDs fail before provider calls.

The top-level `review.llm` budget is the default for each reviewer, not one
aggregate pool shared by all reviewers. Reviewer fields override that default
for their own pass, and every enabled reviewer consumes an independent budget;
the possible aggregate is therefore the sum of their effective budgets.
Markdown and JSON coverage summaries record each reviewer's effective mode,
depth, pack caps, and input-token cap.

Per-invocation CLI budget overrides apply uniformly to every selected reviewer. When provided,
`--llm-coverage-mode`, `--llm-max-packs`, `--llm-max-deep-packs`, and
`--llm-max-input-tokens` replace the corresponding root value **and** every
configured reviewer override for that run. Use reviewer YAML fields when
specialists need different persistent budgets; use CLI overrides when every
reviewer pass should use the same limits. The possible aggregate remains the
sum of the selected reviewers' effective budgets; these flags do not create a
shared invocation-wide envelope.

## Coverage

`review.llm.coverage_mode` controls how much of a diff receives LLM review:

- `fast`: capped deep review.
- `balanced`: deep review for high-value packs plus shallow breadth under token budget.
- `exhaustive`: prioritize every reviewable pack, while still honoring all
  configured pack, deep-review, input-token, and provider limits.

Reports show partial severity, reviewed/unreviewed packs, residual P0/P1 work,
reviewer assignments, and continuation commands. Their completion status is:

- `complete` when every scoped pack and matching reviewer assignment is
  reviewed and no partial debt remains;
- `partial` when work remains without a hard execution/budget failure;
- `incomplete` when a pack is over budget, a reviewer execution or
  verification fails, or required-reviewer policy debt prevents completion;
- `disabled` when LLM review did not run.

Findings and gate messages refer to the reviewed scope. A clean partial report
is not evidence that unreviewed packs are clean.

For ordinary runs, the completion status summarizes the report's global LLM
scope. With `--until-complete --reviewer <id>`, the completion contract instead
covers only packs matching that reviewer and its assignments. Its
`coverage_completion` result may be `complete` while global coverage remains
partial outside that matching scope. If no reviewer is named and exactly one
configured reviewer is `required`, a fresh completion run executes only that
baseline reviewer. Keep the baseline unfiltered when it is meant to represent
the entire reviewable diff.

Tune coverage with:

- `max_packs`: hard total cap across deep and shallow passes.
- `max_deep_packs`: cap for full deep review.
- `max_input_tokens`: approximate total LLM review input-token budget.
- `coverage_mode`: breadth/depth strategy.
- `max_consecutive_provider_failures`: open the provider circuit after this
  many consecutive infrastructure failures; auth and quota failures open it
  immediately.

Prefer `balanced` for normal team use. Use `fast` for cheap smoke review and
`exhaustive` for high-risk changes when provider cost and latency are
acceptable. Because `exhaustive` still obeys caps, use
`apex-ray review --until-complete` (and `--strict-coverage` when a non-complete
result must fail) for an explicit completion contract.

Token estimates include provider-specific prompt/scaffold overhead. They are
deliberately conservative for CLI providers because observed CLI usage
includes a fixed agent scaffold that a simple characters/4 estimate misses.
Provider-reported usage remains authoritative in reports and telemetry.

See [Tuning](tuning.md) for TypeScript-oriented starting presets and a
measurement loop for pack caps, route escalation, risk policy, specialist
reviewers, concurrency, and CI cost.

## Reports

Review and gate commands always write latest report files to the configured `--output`, `--json`, and optional `--html` paths. Reusing the same paths overwrites those latest files.

Set `review.reports.archive: true` to also copy each generated report into a run directory under `review.reports.archive_dir`. This preserves full Markdown/JSON/HTML artifacts for review-quality debugging while keeping the latest paths stable for agents and hooks.

```yaml
review:
  reports:
    archive: true
    archive_dir: ${local_data}/reports/runs
    retention: 20
    compression: auto
    compression_min_bytes: 65536
```

`retention` keeps the newest run directories and prunes older ones. Set
`retention: null` to disable pruning. Newly generated configurations explicitly
use `compression: auto`, which stores artifacts at
or above the threshold as deterministic `.gz` files and records encoding and
sizes in the versioned `manifest.json`; `none` and `gzip` force either behavior. Older
configurations that omit `compression` retain the legacy uncompressed behavior. Small
archives remain directly readable.

Report archives may contain source snippets, findings, file paths, and
provider metadata; keep generated reports ignored unless the team
intentionally curates a specific artifact. Manifest source paths are stored
relative to the repository where possible. External sources use opaque IDs,
and same-named artifacts are stored under distinct filenames.

## Pre-Push Gate

`apex-ray gate pre-push` runs a base-branch review and applies `review.gates.pre_push`.

Default behavior:

- compare `review.base...HEAD`;
- write `.apex-ray/reports/pre-push.md` and `.apex-ray/reports/pre-push.json`;
- block on verified `high` or `critical` findings;
- block on failed LLM coverage quality gate;
- block on `critical` partial coverage;
- print live progress to stderr and a compact, agent-readable summary to stdout.

The base-ref precedence is explicit `--base`, then `APEX_RAY_BASE`, then
`TURBO_SCM_BASE`, then `review.base`. The environment fallbacks let generated
hooks share the same temporary base override as stacked-branch and monorepo
tooling without replacing Apex Ray's version-pinned command.

Use the canonical automatic follow-up keys for new configurations:

```yaml
review:
  gates:
    pre_push:
      fail_on_partial_severity: critical
      auto_followup: true
      auto_followup_max_pack_reviews: 16
```

The gate makes at most one deep continuation pass over the concrete packs that
caused the current blocking coverage decision. The partial threshold supplies
the baseline residual scope (`critical` P0, `major` P0/P1, `minor` P0/P1/P2),
while failed review/verification calls, required-reviewer debt, configured
source/high-risk thresholds, and high-risk depth debt add their exact blocking
pack IDs even when their pack priority is lower. `none` (or `null`) disables
threshold-only residual selection. `auto_followup_max_pack_reviews` limits
primary reviewer-pack assignments. Provider retries, fallbacks, and finding
verification can add requests beyond this cap.

Existing configurations remain compatible. When `auto_followup` is omitted,
the legacy `auto_followup_p0` and `auto_followup_p0_max_pack_reviews` keys keep
their original P0-only behavior, regardless of the partial-severity threshold.
Set the canonical key explicitly when adopting the generalized policy.

Globally unreviewed work at the blocking threshold and unfinished selected
assignments for reviewers marked `required: true` remain blocking coverage
debt. Matching assignments deferred by reviewer limits remain visible as
warnings and continuation todos; explicit strict/complete coverage policies
promote them to required work. This bounds API or subscription use without
hiding unfinished work.

Set `review.gates.pre_push.enabled: false` in local config to skip the hook gate. Prefer local config for personal cost/model/provider differences instead of editing the shared hook command.

Set `review.llm.enabled: false` in local config when a machine should keep normal review and pre-push gate runs deterministic and offline.

By default the gate never contacts a Git remote. Repositories that require a
fresh remote base before every push can opt in explicitly:

```yaml
review:
  base: origin/main
  gates:
    pre_push:
      fetch_base: true
```

For an exact remote-tracking base such as `origin/main` or
`refs/remotes/origin/main`, `fetch_base` resolves the configured remote and
fetches only that branch into its matching remote-tracking ref, without tags or
submodules. The latter spelling is canonicalized to `origin/main` before
merge-base, retry-state, and diff calculations.

Hook overrides may also use a short branch name such as `feature/stack`. When
`origin` is configured and contains that exact branch, Apex Ray refreshes it and
uses `origin/feature/stack`. A same-named local branch or tag is used unchanged
only when `origin` reports that the exact short branch is absent.

Unambiguous local commit-ish values are validated without a remote lookup. Use
a full SHA-1/SHA-256 object ID, a revision expression such as `HEAD~1`, or an
explicit ref such as `refs/heads/feature/stack` for an offline base.
Authentication, transport, and other failures while resolving an ambiguous
short name remain blocking, even when a same-named local ref exists. Unknown
refs always stop the gate before reading the diff or starting review. Leave
`fetch_base` disabled for fully offline operation or when another hook already
resolves and refreshes the base.

### Local Finding Triage

When a pre-push finding is a confirmed local false positive, do not bypass the hook. Suppress the specific finding locally:

```bash
apex-ray findings list --from-report .apex-ray/reports/pre-push.json
apex-ray findings suppress apex-<id> \
  --from-report .apex-ray/reports/pre-push.json \
  --reason "The repository layer already enforces this invariant."
```

Use suppressions sparingly. Before suppressing, inspect the finding evidence, the current code, and relevant tests, invariants, or ownership assumptions. The reason must be concrete and objective enough for a later agent to audit. Do not suppress when the finding might be real, when you are unsure, or merely to get a push through.

Triage state is local and ignored by default. It is intended for frequent local review runs, not as shared team policy. A suppression applies only while the finding fingerprint and context-pack fingerprint still match; if the relevant context changes, Apex Ray marks the suppression stale, prints the prior reason in the gate output/report, and lets the finding block again. Re-check stale findings before suppressing again. Suppressions expire after `review.triage.default_expiry_days` unless `--expires` is provided.

Useful cleanup commands:

```bash
apex-ray findings suppressions
apex-ray findings unsuppress sup-<id>
apex-ray findings prune
```

Use committed memory/rules/eval/config only when a repeated false-positive pattern generalizes beyond one local run. Raw suppressions should stay local.

```yaml
review:
  triage:
    enabled: true
    state_path: ${local_data}/triage/suppressions.json
    events_path: ${local_data}/triage/events.jsonl
    default_expiry_days: 14
    max_active_suppressions: 200
    events_retention_days: 90
```

When report archiving is enabled, pre-push archives include `pre-push-triage.json` with the suppressed-finding snapshot and lifecycle counters for that gate run.

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
        max_resolution_calls_per_retry: 8
```

The first run still reviews `review.base...HEAD`. Later eligible retry runs review only `previous_gate_head..HEAD`, carry forward unresolved blocking findings and coverage debt, and write combined gate state to `state_path`.

`max_resolution_calls_per_retry` bounds sequential LLM resolution calls when a
new file could affect several carried findings. Apex Ray resolves critical
findings first and keeps deferred findings blocking for the next retry. At an
unchanged HEAD, deferred calls reuse only the exact matching evidence report;
after every deferred finding has been attempted, the same evidence is not sent
to the provider again.

Incremental retry is fail-closed:

- previous verified blocking findings keep blocking until the resolution verifier returns `resolved`;
- `still_present` and `uncertain` resolution results keep blocking;
- critical carried coverage debt is not cleared by a delta-only run;
- a continuation command emitted by the gate updates the same pre-push JSON
  report; the next eligible retry validates that report and resumes bounded P0
  coverage instead of permanently OR-ing stale debt;
- when commits were added while carried coverage was being resumed, the gate
  preserves the earlier retry HEAD and asks for one more run so the new delta
  cannot bypass review;
- a missing or mismatched coverage-debt report falls back to a full
  `review.base...HEAD` review while retaining prior blocking findings;
- missing state, missing previous HEAD, a previous HEAD outside the current
  HEAD's ancestry, merge-base changes, or config/rule/memory/model/prompt/gate-policy
  changes fall back to a full `review.base...HEAD` review.

The ancestry check makes the state file safe to reuse across normal worktree
branch switches and rebases. A divergent state is not deleted before review;
the full fallback must finish first, then its current HEAD becomes the new
incremental baseline.

Except for the coverage-debt recovery case above, a full fallback is a new
authoritative review for its target and configuration and replaces the prior
incremental state.

## Config Validation

Run diagnostics after changing configuration:

```bash
apex-ray doctor
```

Run a no-LLM review to verify discovery, ignores, analyzer coverage, and report paths without provider cost:

```bash
apex-ray review --worktree --no-llm --output .apex-ray/reports/review.md --json .apex-ray/reports/review.json
```
