# LLM Providers

Apex Ray can run without LLM review. In that mode it still builds context packs and reports coverage, risk signals, and review surfaces.

Apex Ray supports Codex CLI and Claude Code CLI as local providers:

```yaml
review:
  llm:
    enabled: true
    provider: codex_cli
    model: "<codex-model>"
    effort: medium
    codex_path: codex
    verify: true
```

Put shared provider policy in `.apex-ray/config.yml` only when the whole team can use it. Put personal model IDs, executable paths, job counts, timeouts, and token budgets in `.apex-ray/config.local.yml`.

## Provider Setup Checklist

1. Install and authenticate the local provider CLI.
2. Run the provider once outside Apex Ray to verify credentials.
3. Run `apex-ray doctor` from the target repository.
4. Add shared provider policy or local overrides.
5. Run a small `--llm` review and inspect the Markdown and JSON reports.

Local override example:

```yaml
review:
  llm:
    provider: codex_cli
    model: "<personal-codex-model>"
    effort: medium
    codex_path: codex
    jobs: 2
    timeout_seconds: 900
    max_input_tokens: 120000
```

```yaml
review:
  llm:
    enabled: true
    provider: claude_code_cli
    model: "<claude-model-or-alias>"
    effort: medium
    claude_path: claude
    verify: true
```

Model routing is configured through profiles:

```yaml
review:
  llm:
    profiles:
      cheap:
        provider: codex_cli
        model: "<cheap-codex-model>"
        effort: low
      strong:
        provider: claude_code_cli
        model: "<strong-claude-model-or-alias>"
        effort: medium
    routing:
      review_profile: cheap
      verify_profile: strong
      escalated_review_profile: strong
      escalate_review_when:
        risk: [auth, external_io, persistence]
        rule_severity: [high, critical]
        strict_rule: true
        pack_truncated: true
```

Do not use near-sunset model IDs in shared defaults. Team members can override provider choice, executable paths, jobs, timeout, token budget, reasoning effort, and model cost locally in `.apex-ray/config.local.yml`.

Codex and Claude can be used in the same project by assigning different providers to routing profiles. Keep shared config focused on team review policy; put personal provider/model choices in local config when team members have different CLI subscriptions or credentials.

`effort` maps to Codex CLI `model_reasoning_effort` (`low`, `medium`, `high`, `xhigh`) and Claude Code CLI `--effort` (`low`, `medium`, `high`, `xhigh`, `max`). Configure it at `review.llm.effort` for the default route or inside each profile for routed review/verify calls.

Both providers receive Apex Ray's generated context pack through stdin and must return JSON matching Apex Ray's schema. Claude Code runs with tools disabled for these provider calls; review context comes from Apex Ray, not from letting the provider inspect or edit the repository directly.

Apex Ray records provider-reported usage when the CLI exposes it. Claude Code JSON output can include token usage and estimated cost metadata. Codex CLI JSON events can include token count events in supported versions. If provider usage is absent, reports and telemetry still include Apex Ray's estimated input-token counts.

## Disable LLM Locally

When a machine should avoid LLM cost or does not have provider credentials, use local config:

```yaml
review:
  llm:
    enabled: false
```

You can still run deterministic analyzer/context reports with:

```bash
apex-ray review --worktree --no-llm
```

## Privacy And Cost

With `--llm`, Apex Ray sends selected diff and context-pack content to the configured local CLI provider. Review that provider's privacy and retention policy before using Apex Ray on private code.

Use routing profiles when you want cheaper broad review and stronger verification:

- `review_profile`: broad first-pass review.
- `verify_profile`: verifier pass for candidate findings.
- `escalated_review_profile`: stronger review for high-risk packs.

Use telemetry to tune cost, latency, cache hit rates, and coverage after real runs.
