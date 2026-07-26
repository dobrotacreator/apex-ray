# GitHub Actions

Apex Ray ships a composite action at `.github/actions/apex-ray-review`. It checks
out the exact pull-request head into the isolated
`$GITHUB_WORKSPACE/.apex-ray/repository` analysis directory, reviews the
immutable base-SHA diff, writes Markdown/JSON/SARIF, adds a compact job summary,
uploads the reports as an artifact, and attempts a non-blocking code-scanning
upload.

The action runs Apex Ray from the immutable action source under
`GITHUB_ACTION_PATH`, never from the repository-under-review checkout. It
installs Python dependencies from the pinned action commit's `uv.lock` with
`uv sync --locked --no-install-project`, imports only that pinned Python source,
installs TypeScript analyzer dependencies from that action commit's
`package-lock.json` with `npm ci`, and builds only the pinned analyzer. It does
not run package-manager hooks, build scripts, tests, analyzer scripts, or Python
imports from the pull-request head. The reviewed checkout is parser input, not
executable action code. This separation avoids both pull-request runtime
replacement and an unrelated PyPI artifact or unlocked Python build-isolation
environment. The Python, Node.js, and uv tool versions are also exact.

## Recommended pull-request workflow

Replace `<full-release-commit-sha>` with the 40-character commit for the Apex
Ray release you have reviewed. Pinning the action itself and its transitive
actions prevents a mutable tag from changing the code that receives API
credentials. The action verifies that `github.action_ref` is a full
40-character commit SHA before checkout and rejects mutable tags and local
action paths.

Do not replace the pinned remote `uses:` line with
`uses: ./.github/actions/apex-ray-review` in a pull-request workflow that can
receive API credentials. A local action is loaded from the caller's checkout;
for `pull_request`, that can be the pull request merge commit, so the action
implementation itself is not an immutable trust boundary.

```yaml
name: Apex Ray

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  security-events: write

concurrency:
  group: apex-ray-${{ github.workflow }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    timeout-minutes: 45
    strategy:
      fail-fast: false
      matrix:
        reviewer: [correctness, security, ux]
    env:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
    steps:
      - name: Review as ${{ matrix.reviewer }}
        uses: dobrotacreator/apex-ray/.github/actions/apex-ray-review@<full-release-commit-sha>
        with:
          reviewers: ${{ matrix.reviewer }}
          llm: auto
          artifact-name: apex-ray-${{ matrix.reviewer }}
          sarif-category: apex-ray-${{ matrix.reviewer }}
```

`contents: read` is sufficient for checkout and deterministic review.
`security-events: write` is needed only for the optional SARIF upload. The
composite action cannot grant job permissions or configure concurrency, so
both belong in the caller workflow. If code scanning is unavailable for the
repository, SARIF upload is non-blocking and the artifact remains available.

The action intentionally does not post pull-request comments. That keeps the
default token read-only apart from the narrowly scoped code-scanning
permission. Teams can consume the JSON or SARIF in a separate, explicitly
privileged workflow after review.

The repository's
[`github-actions-api.yml`](https://github.com/dobrotacreator/apex-ray/blob/main/examples/configs/github-actions-api.yml)
is a bounded TypeScript/API starting point. See [Tuning](tuning.md) before
expanding matrix reviewers or token budgets, because each reviewer has an
independent cap.

## Configure focused reviewers

Reviewer ids in the workflow must exist in the trusted repository config. A
matrix gives every focus an independent timeout, artifact, SARIF category, and
status check. A single action invocation may instead accept a comma- or
newline-separated list; the action converts it into repeated `--reviewer`
flags.

```yaml
review:
  llm:
    enabled: true
    provider: openai_api
    model: "<approved-openai-model-id>"
  reviewers:
    - id: security
      name: Security
      focus: Authentication, authorization, injection, secrets, and trust boundaries.
      risk_tags: [security]
    - id: finance
      name: Financial risk
      focus: Money movement, rounding, reconciliation, limits, and idempotency.
      risk_tags: [financial]
    - id: ux
      name: UX and UI
      focus: User-visible regressions, accessibility, state transitions, and error recovery.
      paths: ["src/ui/**", "apps/web/**"]
  risk:
    rules:
      - id: money-movement
        title: Money movement boundary
        severity: critical
        paths: ["src/payments/**", "src/ledger/**"]
        categories: [financial, correctness]
        reviewer_tags: [finance]
        guidance: Verify units, rounding, idempotency, authorization, and reconciliation.
```

Keep focus text specific and testable. Prefer separate reviewers when a domain
needs a different model profile, token budget, timeout, artifact, or ownership
boundary; use one invocation with several reviewer ids when a combined report
is more useful than independent checks.

The action fails when review execution or report rendering fails. By default it
also fails when `llm_coverage.quality_gate_status` is `fail`; this includes a
configured required reviewer that did not complete successfully. A finding,
even a critical one, does not by itself fail the action. Use branch protection
or repository rules to make the resulting matrix job checks required for
merge.

For an advisory rollout, set `fail-on-quality-gate: "false"`. Reports, the job
summary, and the `quality-gate-status` action output still expose the failed
coverage gate, but it does not change the step's exit status. Keep the default
for reviewers whose coverage is a merge requirement.

## API credentials

Do not put API keys, secret values, or secret names in action inputs. Configure
the provider in `.apex-ray/config.yml`, name its environment variables there,
and pass values through the job `env` mapping from GitHub Secrets. Preset
providers use these default key names:

| Provider | Config value | Default secret environment variable |
| --- | --- | --- |
| OpenAI | `openai_api` | `OPENAI_API_KEY` |
| Anthropic | `anthropic_api` | `ANTHROPIC_API_KEY` |
| DeepSeek | `deepseek_api` | `DEEPSEEK_API_KEY` |
| Qwen | `qwen_api` | `DASHSCOPE_API_KEY` |
| Kimi | `kimi_api` | `MOONSHOT_API_KEY` |
| Z.AI | `zai_api` | `ZAI_API_KEY` |

For a custom OpenAI-compatible gateway, keep the URL and host allowlist under
CI control as repository or environment variables:

```yaml
review:
  llm:
    enabled: true
    provider: openai_compatible
    model: internal-review-model
    api:
      protocol: openai_chat
      structured_output: json_object
      base_url_env: APEX_RAY_LLM_BASE_URL
      api_key_env: APEX_RAY_LLM_API_KEY
      allowed_hosts_env: APEX_RAY_API_ALLOWED_HOSTS
```

```yaml
jobs:
  review:
    runs-on: ubuntu-latest
    environment: apex-ray-review
    env:
      APEX_RAY_LLM_BASE_URL: ${{ vars.APEX_RAY_LLM_BASE_URL }}
      APEX_RAY_API_ALLOWED_HOSTS: ${{ vars.APEX_RAY_API_ALLOWED_HOSTS }}
      APEX_RAY_LLM_API_KEY: ${{ secrets.APEX_RAY_LLM_API_KEY }}
    steps:
      - uses: dobrotacreator/apex-ray/.github/actions/apex-ray-review@<full-release-commit-sha>
```

The allowlist is a comma- or whitespace-separated list of hostnames, without
schemes or paths. In CI, a custom endpoint must come from `base_url_env`, and
its normalized host must appear in the environment variable named by
`allowed_hosts_env`. Use an environment with required reviewers for
high-value credentials, set provider spending limits, and avoid forwarding
unrelated repository secrets to the job.

## Fork and configuration safety

The workflow uses the ordinary, unprivileged pull-request event. GitHub does
not pass Actions secrets to workflows triggered from forks (apart from a
read-only repository token), so the action explicitly forces `--no-llm` for
fork and Dependabot pull requests. Those runs still perform deterministic
classification and analyzer-backed review, produce artifacts, and skip the
code-scanning upload that requires a write token.

Do not switch this workflow to `pull_request_target`. That event runs with the
base repository's privileged token and secrets, and combining it with a
pull-request head checkout is unsafe if any later step starts executing code
from that checkout. Apex Ray keeps its own runtime separate and treats the head
as analysis input, but the ordinary `pull_request` event is still the intended
and least-privileged integration. Use a separate privileged workflow that
consumes validated report artifacts if a later operation needs write access.

For every pull request, the default `trust-pr-config: false` loads
`.apex-ray/config.yml` from the base commit and writes a restricted temporary
copy. The restricted copy disables custom analyzer scripts, external
rule/memory files, caches, telemetry, report archives, and triage writes.
Inline risk rules and reviewer definitions from the base branch remain
available.

When LLM review can run from that restricted copy, every effective provider
route must use an API provider. The action rejects `codex_cli` and
`claude_code_cli` both at `review.llm.provider` and in any profile, including a
profile selected by global routing or a focused reviewer. Fork and Dependabot
runs are forced to `--no-llm`, so CLI routes present in the trusted base config
are never executed there. Configure `openai_api`, `anthropic_api`, another
preset API, or `openai_compatible` for CI.

Set `trust-pr-config: true` only for same-repository pull requests whose authors
are allowed to choose the declarative API endpoint, reviewer, and risk
configuration. The option is ignored for forks. Head configuration still goes
through the restricted-copy sanitizer: custom analyzer scripts, external
rule/memory files, CLI LLM providers, caches, telemetry, report archives, and
triage writes cannot execute from the pull-request checkout. Review changes to
shared config, reviewer prompts, API environment-variable names, and endpoint
allowlists as security changes before merging them to the base branch.

If another step performs checkout, use `checkout: "false"` only after checking
out the exact head commit at `GITHUB_WORKSPACE` with full history and without
persisted credentials. Keep the action itself remotely pinned and do not run
dependency hooks, builds, tests, or repository-provided scripts before review
when the job has secrets. The action validates all report paths relative to the
repository under review and exposes both repository-relative outputs
(`markdown-output`, `json-output`, `sarif-output`) and absolute outputs
(`repository-path`, `markdown-path`, `json-path`, `sarif-path`). Local absolute
paths remain omitted from SARIF locations and messages.

GitHub references:

- [Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)
- [Contexts reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)
- [Events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [Assigning permissions to jobs](https://docs.github.com/en/actions/how-tos/security-for-github-actions/security-guides/automatic-token-authentication)
- [Uploading SARIF to GitHub](https://docs.github.com/en/code-security/code-scanning/integrating-with-code-scanning/uploading-a-sarif-file-to-github)
