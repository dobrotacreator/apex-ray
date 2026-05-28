# LLM Providers

Apex Ray can run without LLM review. In that mode it still builds context packs and reports coverage, risk signals, and review surfaces.

Apex Ray supports Codex CLI and Claude Code CLI as local providers:

```yaml
review:
  llm:
    enabled: true
    provider: codex_cli
    model: "<codex-model>"
    codex_path: codex
    verify: true
```

```yaml
review:
  llm:
    enabled: true
    provider: claude_code_cli
    model: "<claude-model-or-alias>"
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
      strong:
        provider: claude_code_cli
        model: "<strong-claude-model-or-alias>"
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

Do not use near-sunset model IDs in shared defaults. Team members can override provider choice, executable paths, jobs, timeout, token budget, and model cost locally in `.apex-ray/config.local.yml`.

Codex and Claude can be used in the same project by assigning different providers to routing profiles. Keep shared config focused on team review policy; put personal provider/model choices in local config when team members have different CLI subscriptions or credentials.

Both providers receive Apex Ray's generated context pack through stdin and must return JSON matching Apex Ray's schema. Claude Code runs with tools disabled for these provider calls; review context comes from Apex Ray, not from letting the provider inspect or edit the repository directly.
