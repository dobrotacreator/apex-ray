# Tuning Apex Ray

A good Apex Ray configuration spends deep-review budget on changes that can
cause the most damage, keeps broad coverage cheap, and makes residual risk
visible. Do not start by maximizing every cap. Start with a bounded baseline,
measure representative changes, then move one constraint at a time.

The repository includes four sanitized starting points:

| Preset | Use it when |
| --- | --- |
| [`typescript-balanced.yml`](https://github.com/dobrotacreator/apex-ray/blob/main/examples/configs/typescript-balanced.yml) | A TypeScript service needs a bounded general pass plus risk-scoped security review. |
| [`typescript-security.yml`](https://github.com/dobrotacreator/apex-ray/blob/main/examples/configs/typescript-security.yml) | Identity, untrusted input, CI, and supply-chain boundaries deserve a stronger specialist route. |
| [`typescript-fintech.yml`](https://github.com/dobrotacreator/apex-ray/blob/main/examples/configs/typescript-fintech.yml) | Financial invariants need independent correctness, finance, security, and UX perspectives. |
| [`github-actions-api.yml`](https://github.com/dobrotacreator/apex-ray/blob/main/examples/configs/github-actions-api.yml) | Pull-request review runs in GitHub Actions with an API key rather than a local CLI subscription. |

These are examples, not universal policy. Their paths and domain language are
generic on purpose. Copy one to `.apex-ray/config.yml`, replace its risk
boundaries with real project boundaries, and validate it with `apex-ray
doctor`.

## Establish A Baseline

Use a representative mix of small fixes, cross-module changes, migrations,
security-sensitive changes, and large pull requests. Preserve enough runs to
compare distributions rather than optimizing around one unusually easy or
pathological diff.

```yaml
review:
  local_data:
    root: git_common
  telemetry:
    enabled: true
    path: ${local_data}/telemetry/review-runs.jsonl
    path_mode: anonymized
  reports:
    archive: true
    archive_dir: ${local_data}/reports/runs
    retention: 20
    compression: auto
```

Use `apex-ray telemetry-summary` for aggregate behavior and archived reports
for finding-level investigation. Keep telemetry, reports, provider payloads,
and local config out of version control. `path_mode: anonymized` removes
absolute repository paths, but it does not anonymize reviewer IDs, rule IDs,
model names, or arbitrary external logs; inspect data before sharing it.

Track these outcomes together:

- source-line, high-risk, and deep/shallow pack coverage;
- residual P0/P1 packs and the reason each pack was skipped;
- verified useful findings, false positives, and missed historical findings;
- wall time, analyzer time, LLM time, and process/child peak RSS;
- actual input/output tokens, estimate ratio, cache savings, and estimated
  cost where the provider reports them;
- calls and usage per route and reviewer;
- provider failure types and whether the circuit breaker stopped futile work.

Cost or speed alone is not a quality metric. Pair telemetry with historical
PR replay or a curated set of known regressions before accepting a cheaper
configuration.

## Tune In This Order

### 1. Remove work that should never be reviewed

Ignore generated output, dependencies, coverage, build artifacts, and
minified bundles. For TypeScript projects this prevents noisy files from
consuming parsing memory and context budget:

```yaml
review:
  ignore:
    - "**/*.lock"
    - "**/generated/**"
    - "**/dist/**"
    - "**/build/**"
    - "**/coverage/**"
    - "**/node_modules/**"
```

Do not ignore migrations, schemas, deployment configuration, or generated
client/server contracts merely because they are large; those surfaces often
carry compatibility risk. Classify or scope them deliberately.

For Dart/Flutter, do not broadly ignore `*.g.dart`, `*.freezed.dart`, router,
DI, mock, or generated-client files. Apex Ray keeps recognized Dart outputs
index-only and removes their raw snippets automatically, preserving useful
handwritten-to-generated relationships without spending prompt budget. See
[Dart analyzer configuration](configuration.md#dart-and-flutter-analyzer).

Keep the TypeScript index cache and adaptive sharding enabled:

```yaml
review:
  analyzer:
    index_cache_enabled: true
    changed_file_shard_size: 40
    adaptive_sharding: true
    large_change_file_threshold: 20
    large_change_shard_size: 4
```

Use `${local_data}` with `root: git_common` for caches when developers use
linked worktrees. Refresh the analyzer cache only while diagnosing stale-index
behavior; routine refreshes discard most of its performance benefit.

### 2. Bound broad coverage before adding specialists

`max_packs` is the hard selection cap, `max_deep_packs` reserves the expensive
deep subset, and `max_input_tokens` is the aggregate review-input guard. A
practical TypeScript baseline is a balanced pass with a much smaller deep cap
than total cap:

```yaml
review:
  llm:
    coverage_mode: balanced
    max_packs: 48
    max_deep_packs: 16
    max_input_tokens: 180000
    jobs: 2
```

Treat those numbers as a starting scale, not a target. If ordinary changes
consistently stop at a cap and leave high-risk residual packs, first check
ignore rules and oversized/noisy context, then raise the constraint that
actually bound selection. If caps are rarely approached, lowering them does
not improve that workload.

Increase `jobs` only when the provider and runner have headroom. More
concurrency can reduce latency, but it does not reduce tokens and can increase
peak memory or trigger rate limits. API-backed CI usually tolerates modest
parallelism; subscription CLIs and memory-constrained runners often benefit
from `jobs: 1` or `2`.

### 3. Route by consequence, not by prompt size

Use a low-cost broad model and a stronger verifier/escalation profile. Escalate
on explicit trust boundaries, high/critical rules, and strict invariants:

```yaml
review:
  llm:
    profiles:
      broad:
        provider: codex_cli
        model: "<current-fast-model>"
        effort: low
      strong:
        provider: codex_cli
        model: "<current-strong-model>"
        effort: medium
    routing:
      review_profile: broad
      verify_profile: strong
      escalated_review_profile: strong
      escalated_verify_profile: strong
      escalate_review_when:
        risk: [auth, shell, migration]
        rule_severity: [high, critical]
        strict_rule: true
      escalate_verify_when:
        finding_severity: [high, critical]
```

Avoid escalating every truncated or merely large pack by default. If nearly
all packs take the strong route, routing is no longer selective: narrow
generic risk triggers, add project risk policy, and inspect which condition
fires. Keep model IDs explicit and review provider catalogs periodically;
model aliases and availability change faster than project policy.

Profiles may mix a local subscription CLI with direct APIs. API credentials
belong in environment variables, never YAML. See [LLM Providers](providers.md)
for endpoint and allowlist rules.

### 4. Describe project risk at stable boundaries

Risk policy should answer “where can this project lose money, data,
authorization, availability, or compatibility?” It should not reproduce a
style guide.

```yaml
review:
  risk:
    rules:
      - id: payment-state
        title: Payment state transition
        severity: critical
        score: 100
        paths: ["src/payments/**"]
        exclude_paths: ["**/*.test.ts", "**/*.spec.ts"]
        file_kinds: [source]
        categories: [financial, state-machine]
        reviewer_tags: [finance]
        guidance: Verify authorization, units, idempotency, atomicity, and reconciliation.
```

Prefer path/kind scope for stable architectural boundaries. Add `text` only
when the token is specific enough to localize a real hazard; broad words can
create many duplicate signals. Add `risk` when a project rule should require a
built-in signal such as `external_io` or `shell`.

Review the highest-scoring rules manually. A critical rule applied to an
entire generic directory can crowd out more precise signals and force strong
routes on routine changes.

### 5. Add reviewers with non-overlapping jobs

The general reviewer should remain bounded and cover every diff. Specialists
should normally receive only packs carrying relevant risk kinds or
`reviewer_tags`:

```yaml
review:
  reviewers:
    - id: correctness
      focus: Concrete behavioral regressions and broken contracts.
      profile: broad
      verify_profile: strong
      max_packs: 36
      max_deep_packs: 12
      max_input_tokens: 130000
      required: true

    - id: finance
      focus: Loss exposure from units, precision, limits, idempotency, settlement, and reconciliation.
      risk_tags: [finance]
      profile: strong
      verify_profile: strong
      review_depth: deep
      max_packs: 10
      max_deep_packs: 10
      max_input_tokens: 60000
```

Every reviewer has an independent budget. The possible total is therefore the
sum of reviewer budgets, not the top-level cap. Watch per-reviewer selected
packs and actual tokens. If two reviewers repeatedly inspect the same packs
and produce the same findings, tighten their focus/scope or combine them.

Use `required: true` only when failure of that reviewer must fail the LLM
quality gate. A required reviewer needs a scope that should match every run in
which it is selected; a narrowly risk-tagged specialist is usually optional
and enforced through residual critical-risk policy instead.

### 6. Separate local and CI budgets

CI has no interactive CLI subscription, so configure an API provider and read
the key from GitHub Secrets. A reviewer matrix gives specialists independent
timeouts, status checks, artifacts, and SARIF categories. It also multiplies
provider calls, so lower each reviewer cap rather than copying one large
single-review budget into every matrix job.

Fork and Dependabot pull requests do not receive normal repository secrets.
The bundled action falls back to deterministic no-LLM analysis for those
events. See [GitHub Actions](github-actions.md) for the secure workflow and
trusted-config boundary.

## Diagnose Common Signals

| Signal | Investigate | Typical adjustment |
| --- | --- | --- |
| Residual P0/P1 packs | Selection stage and skip reason | Fix ignores/noisy context, tag the boundary, or raise the binding pack/token cap. |
| Strong route on most packs | `route_reason` and project risk matches | Narrow escalation triggers and overly broad high-risk rules. |
| Specialist has no useful distinct findings | Reviewer pack overlap and provenance | Tighten `paths`, `risk`, or `risk_tags`; merge overlapping focus areas. |
| Actual input greatly exceeds estimate | Provider/model estimate ratio | Keep more token margin and compare like-for-like provider/model runs before reducing caps. |
| Verification dominates cost | Candidate finding volume and verify routes | Improve reviewer focus, avoid advisory noise, and reserve the strongest verifier for high-impact candidates. |
| Repeated provider failures | Failure type and circuit-open reason | Fix credentials/quota/endpoint first; do not raise the circuit threshold to retry deterministic failures. |
| Low cache hit rate on repeated diffs | Config, model, prompt, and cache location | Use shared `git_common` storage and avoid unnecessary profile/prompt churn. |
| TypeScript analyzer memory or latency spike | Included file classes and cache/shard metrics | Ignore build/dependency output, retain adaptive sharding, and test the specific large workspace. |
| Dart analyzer is partial or slow | SDK/package resolution, changed-symbol fan-out, analyzer warnings, and cache status | Resolve packages with the selected SDK, keep generated files index-only, lower Dart semantic caps, or increase the global timeout only after measuring. |

## Validate A Tuning Change

For each material change:

1. Run `apex-ray doctor`.
2. Run a no-LLM review to verify discovery, ignores, risk signals, reviewer
   scopes, and analyzer behavior without provider cost.
3. Replay a small representative set with LLM review and compare quality,
   residual risk, time, tokens, and failures.
4. Run the pre-push gate on the proposed shared config.
5. Keep the change only when its expected benefit appears without unacceptable
   missed findings or residual critical coverage.

Do not commit raw corporate code, full archived reports, provider payloads, or
third-party telemetry as tuning evidence. Record only aggregate,
non-identifying conclusions and generic configuration patterns.
