# Historical PR Replay Evals

Apex Ray can capture merged GitHub PRs with Greptile comments and replay the same patch locally. This gives the team a repo-committed calibration set for recall and precision work without depending on a GitHub PR integration.

## Capture Cases

```bash
apex-ray eval capture-prs \
  --repo /path/to/project \
  --output /path/to/project/.apex-ray/evals/cases \
  --limit 10
```

Use `--pr <number>` one or more times to capture specific PRs. Capture requires `gh` auth for the repository. Each case contains:

- `manifest.yml`: PR metadata and extracted first-pass Greptile findings.
- `pr.diff`: clean unified diff for the first-pass review revision, suitable for Apex Ray replay.
- `greptile-comments.json`: raw captured Greptile issue, review, and inline comments.

Commit curated case directories when they represent useful review calibration fixtures. Do not commit run outputs.

## Run Replay

```bash
apex-ray eval run-prs \
  --repo /path/to/project \
  --cases /path/to/project/.apex-ray/evals/cases \
  --output /path/to/project/.apex-ray/evals/runs/$(date +%Y%m%d-%H%M%S) \
  --llm \
  --llm-jobs 4
```

For smoke tests, use `--no-llm`. The run command creates a temporary detached worktree at each captured replay head SHA, overlays the current project `.apex-ray` configuration, runs Apex Ray against the captured diff, and writes:

- `pr-eval-report.md`
- `pr-eval-report.json`
- per-PR `apex-report.md`, `apex-report.json`, `eval-result.json`, and `case-status.json`

Use `--analyzer-timeout <seconds>` for quick smoke runs on very large historical PRs. Full quality runs should normally use the repository configuration.

Replay worktrees are temporary, so LLM replay uses the source repository `.apex-ray/cache/llm` by default when no `--cache-dir` is supplied. This keeps repeated eval runs from paying again for unchanged context packs.

For large local replay suites, use case supervision:

```bash
apex-ray eval run-prs \
  --repo /path/to/project \
  --cases /path/to/project/.apex-ray/evals/cases \
  --output /path/to/project/.apex-ray/evals/runs/latest \
  --llm \
  --resume \
  --case-jobs 4 \
  --case-timeout 1800
```

`--case-jobs` runs independent PR cases in parallel. `--case-timeout` marks a stuck case as `timed_out`, terminates the worker process group, and keeps the suite moving. `--resume` trusts terminal per-case artifacts (`succeeded`, `partial`, `quarantined`, or `skipped`) only when their run fingerprint still matches the current replay options and label file; stale `running`/missing/mismatched cases are rerun. Partial cases exit non-zero by default; use `--allow-partial` only when explicitly accepting incomplete coverage.

The comparison matches Greptile findings to Apex Ray findings by file, nearby line, and issue text overlap. Missed Greptile findings indicate recall gaps. Extra Apex findings require manual triage: they can be false positives, true issues Greptile missed, or findings outside the first-pass Greptile baseline. Use `--allow-extra-findings` for a recall-only gate while still reporting extra findings.

Large PR replay can exercise the same local coverage controls as normal review:

```bash
apex-ray eval run-prs \
  --repo /path/to/project \
  --cases /path/to/project/.apex-ray/evals/cases \
  --output /path/to/project/.apex-ray/evals/runs/latest \
  --llm \
  --llm-coverage-mode balanced \
  --llm-max-deep-packs 32 \
  --llm-max-input-tokens 160000
```

`balanced` mode deep-reviews selected high-value packs and shallow-reviews remaining reviewable packs with compact prompts under the token budget. This improves large-PR breadth without sending every pack through the strongest model.

## Triage Labels And Telemetry

Generate repo-committable labels from a run:

```bash
apex-ray eval init-labels \
  --from-run /path/to/project/.apex-ray/evals/runs/latest \
  --output /path/to/project/.apex-ray/eval/labels
```

Each `pr-<number>.yml` file lets the team mark Greptile findings as `valid`, `not_issue`, or `out_of_scope`, and extra Apex findings as `true_positive`, `false_positive`, `duplicate`, `not_actionable`, or `unknown`. Re-run with labels:

```bash
apex-ray eval run-prs \
  --repo /path/to/project \
  --cases /path/to/project/.apex-ray/evals/cases \
  --output /path/to/project/.apex-ray/evals/runs/latest \
  --llm \
  --labels-dir .apex-ray/eval/labels
```

Labels affect only scoring. They are not injected into review prompts, so replay remains fair: Apex Ray still sees the same diff/config context it would see on a first run.

Set `case_status: quarantined` with `case_status_reason` in a label file when a historical case is known to be unreplayable or intentionally out of scope. Quarantined cases stay visible in reports but are excluded from aggregate scoring.

Append aggregate telemetry for trend tracking:

```bash
apex-ray eval run-prs \
  --repo /path/to/project \
  --cases /path/to/project/.apex-ray/evals/cases \
  --output /path/to/project/.apex-ray/evals/runs/latest \
  --llm \
  --labels-dir .apex-ray/eval/labels \
  --telemetry

apex-ray eval telemetry-summary \
  --telemetry-path /path/to/project/.apex-ray/eval/telemetry/pr-eval-runs.jsonl
```

Telemetry is append-only JSONL with run-level and per-PR counts: matched/missed Greptile findings, extra Apex findings, triage counts, context packs, reviewed/unreviewed pack counts, residual P0/P1 counts, coverage ratios, partial severity, LLM run failures, LLM duration, input-token estimates, and cache hits/misses. Keep detailed run directories ignored; commit curated cases, labels, and compact telemetry when the team wants a shared eval baseline.

Use these fields to tune a project-specific profile:

- high `unreviewed_context_packs_count` with low failure counts means `max_deep_packs`, `max_input_tokens`, or splitting strategy needs attention;
- high `failed_llm_review_runs_count` or `failed_llm_verify_runs_count` points to provider timeout/quota rather than review recall;
- repeated `coverage_partial_severity=critical` means local reports should be continued with `apex-ray review --continue-from ... --residual-priority p0` or rerun in exhaustive mode;
- cache hit/miss and duration trends show whether a config change is reducing cost or just moving work between cheap and strong models.

## Close Recall Gaps With Memory

After a replay run, draft memory cards from missed first-pass Greptile findings:

```bash
apex-ray eval suggest-memory \
  --from-run /path/to/project/.apex-ray/evals/runs/latest \
  --output /tmp/apex-ray-memory-suggestions.md
```

Review and edit the suggestions before committing them under `.apex-ray/memory/`. The generated cards are intentionally conservative: they preserve the historical finding, path, severity mapping, and code-span triggers, but a human should turn them into stable team knowledge.

## First-Pass Ground Truth

Inline Greptile review comments are treated as first-pass findings when they appear inside the configured window after the first Greptile comment. For inline comments, Apex Ray captures GitHub's `original_commit_id` and replays the diff at that first-pass review revision, not at the final PR head after author fixes. Summary comments are parsed only when they still look like the original created body. Edited summary comments are retained for audit but are not counted as first-pass findings because GitHub exposes the latest edited body, not the original first-pass text.
