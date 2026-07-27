# LLM Providers

Apex Ray can run without LLM review. In that mode it still builds context packs and reports coverage, risk signals, and review surfaces.

Apex Ray supports subscription-backed local CLIs and direct API providers:

- `codex_cli` and `claude_code_cli`;
- `openai_api` and `anthropic_api`;
- `deepseek_api`, `qwen_api`, `kimi_api`, and `zai_api`;
- `openai_compatible` for an explicitly configured OpenAI Responses, Anthropic
  Messages, or OpenAI-compatible Chat Completions endpoint.

Local CLI example:

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

## Direct API Providers

API keys are read only from environment variables. Apex Ray does not accept a
key value in YAML or on the command line.
LLM cache keys include one-way identities for the resolved endpoint,
credential, and custom header values, so rotating credentials or switching
tenants cannot reuse another API identity's cached review. Raw values are
never written to cache metadata.

OpenAI Responses API:

```yaml
review:
  llm:
    enabled: true
    provider: openai_api
    model: gpt-5.6-sol
    effort: medium
    api:
      api_key_env: OPENAI_API_KEY
      max_output_tokens: 4096
      max_retries: 2
```

The native OpenAI provider always sends `store: false`, so review source is
not retained as Responses application state. This does not override the
account's abuse-monitoring policy; OpenAI documents the applicable retention
and Zero Data Retention controls in its
[data controls guide](https://developers.openai.com/api/docs/guides/your-data).

Native Anthropic Messages API:

```yaml
review:
  llm:
    enabled: true
    provider: anthropic_api
    model: claude-sonnet-5
    effort: medium
    api:
      api_key_env: ANTHROPIC_API_KEY
      max_output_tokens: 4096
```

Anthropic API effort levels are passed through as
`low`/`medium`/`high`/`xhigh`/`max`; Apex Ray maps its portable `minimal`
level to Anthropic's `low`.

The built-in API providers pin their documented service hosts, use native
structured output where the provider supports it, normalize usage metadata,
honor `Retry-After`, and retry only transient network/rate-limit/server
failures. Authentication, quota, refusal, truncation, and malformed-output
failures remain distinct in reports and gates. Presets are limited to
protocols implemented by their official service: OpenAI supports both the
Responses and Chat Completions protocols, while the other presets use their
native protocol. Use `openai_compatible` when a gateway for the same model
requires a different wire protocol.

The Chinese flagship presets use OpenAI-compatible transports with their
provider-specific endpoint, key variable, structured-output capability, and
reasoning controls:

| Provider | Config value | Default key variable | Current flagship model example (July 2026) |
|---|---|---|---|
| DeepSeek | `deepseek_api` | `DEEPSEEK_API_KEY` | `deepseek-v4-pro` |
| Alibaba Qwen/DashScope | `qwen_api` | `DASHSCOPE_API_KEY` | `qwen3.7-max` |
| Moonshot Kimi | `kimi_api` | `MOONSHOT_API_KEY` | `kimi-k3` |
| Z.ai GLM | `zai_api` | `ZAI_API_KEY` | `glm-5.2` |

Model catalogs change faster than Apex Ray releases. Treat these as examples,
confirm the model ID in the provider's current documentation, and set it
explicitly. A provider preset never silently substitutes a model.
See the official [DeepSeek](https://api-docs.deepseek.com/quick_start/pricing),
[Qwen](https://help.aliyun.com/en/model-studio/text-generation),
[Kimi](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart), and
[Z.ai](https://docs.z.ai/guides/llm/glm-5.2) catalogs.

The Qwen preset defaults to the shared Beijing DashScope endpoint. For another
region or Alibaba's recommended workspace-dedicated production endpoint, set
`review.llm.api.base_url_env` to an environment variable containing the
official `dashscope-*.aliyuncs.com` or `*.maas.aliyuncs.com` base URL. The
preset still enforces that the resolved host belongs to Alibaba Cloud.

`effort` is translated conservatively to each Chat Completions dialect:
DeepSeek uses its documented `high`/`max` levels, Qwen uses the thinking
toggle, Kimi K3 maps to `low`/`high`/`max` while K2 models use their thinking
toggle, and GLM 5.2 maps to its supported `high`/`max` reasoning levels while
older GLM models receive only the supported toggle.

```yaml
review:
  llm:
    enabled: true
    provider: deepseek_api
    model: deepseek-v4-pro
    effort: medium
    api:
      api_key_env: DEEPSEEK_API_KEY
```

## Custom Compatible Endpoints

Custom endpoints require an explicit protocol and structured-output mode:

```yaml
review:
  llm:
    enabled: true
    provider: openai_compatible
    model: company-review-model
    api:
      protocol: openai_chat
      structured_output: json_schema
      base_url_env: COMPANY_LLM_BASE_URL
      api_key_env: COMPANY_LLM_API_KEY
      headers_from_env:
        X-Tenant-ID: COMPANY_LLM_TENANT
```

Supported protocols are `openai_responses`, `anthropic_messages`, and
`openai_chat`. Supported output modes are `json_schema`, `json_object`, and
`prompt_only`; prefer `json_schema` when the endpoint implements it.

HTTPS is mandatory. Loopback HTTP is available only when
`api.allow_insecure_loopback_http: true` is set explicitly. Redirects are not
followed, credentials cannot appear in the URL, reserved authentication
headers cannot be overridden, and response bodies are size-limited.

In CI, Apex Ray uses two fixed, trusted policy variables whose names cannot be
changed by repository configuration:

- `APEX_RAY_API_ALLOWED_HOSTS` lists normalized custom endpoint hostnames;
- `APEX_RAY_API_ALLOWED_ENV_VARS` lists the environment-variable names that
  repository configuration may select for `base_url_env`, `api_key_env`, and
  `headers_from_env`.

For example:

```yaml
env:
  COMPANY_LLM_BASE_URL: https://llm.example.internal/v1
  COMPANY_LLM_API_KEY: ${{ secrets.COMPANY_LLM_API_KEY }}
  COMPANY_LLM_TENANT: apex-ray
  APEX_RAY_API_ALLOWED_HOSTS: llm.example.internal
  APEX_RAY_API_ALLOWED_ENV_VARS: >-
    COMPANY_LLM_BASE_URL,COMPANY_LLM_API_KEY,COMPANY_LLM_TENANT
```

Define both policy variables in trusted CI or a protected environment, not
from checked-in Apex Ray configuration or pull-request input. Built-in presets
always use their preset API-key variable in CI. Preset endpoint/header
selectors and all custom-provider selectors must appear in
`APEX_RAY_API_ALLOWED_ENV_VARS`. Outside CI, custom endpoints, selector names,
and explicitly opted-in loopback HTTP remain configurable for local gateways
and integration tests.

## Provider Setup Checklist

1. Choose a provider boundary: install and authenticate a local CLI, or create
   an API key and expose it through the configured environment variable.
2. Verify the CLI or API credential outside Apex Ray with a minimal request.
3. Run `apex-ray doctor` from the target repository.
4. Add shared provider policy or local overrides. In CI, keep credentials in
   protected secrets and define the trusted endpoint/environment allowlists.
5. Run a small `--llm` review and inspect the Markdown, JSON, and optional
   SARIF reports.

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
        provider: openai_api
        model: gpt-5.6-sol
        api:
          api_key_env: OPENAI_API_KEY
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

CLI subscriptions and APIs can be mixed in the same project by assigning
different providers to routing profiles. A profile may override its `api`
configuration. When a profile changes the provider, unspecified API settings
start from that provider's safe defaults instead of inheriting another
provider's endpoint or credential environment variables. Keep shared config
focused on team review policy; put personal provider/model choices in local
config when team members have different subscriptions or credentials.

`effort` maps to Codex CLI `model_reasoning_effort` (`low`, `medium`, `high`, `xhigh`) and Claude Code CLI `--effort` (`low`, `medium`, `high`, `xhigh`, `max`). Configure it at `review.llm.effort` for the default route or inside each profile for routed review/verify calls.

CLI providers receive Apex Ray's generated context pack through stdin; API
providers receive the same bounded prompt in the request body. Claude Code
runs with tools disabled for these calls. Review context comes from Apex Ray,
not from letting a provider inspect or edit the repository directly.

Apex Ray records provider-reported usage when available. Direct APIs normalize
input, cached-input, output, reasoning, cache-read, and cache-creation tokens.
If provider usage is absent, reports and telemetry retain a conservative,
provider-aware input estimate. The configured token budget is a preflight
guard; provider-reported usage remains the accounting source of truth.

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

With `--llm`, Apex Ray sends selected diff and context-pack content to the
configured CLI or API provider. Review that provider's privacy, data
residency, training, and retention policy before using Apex Ray on private
code. Never commit keys, tenant headers, literal private endpoints, or local
provider overrides.

Use routing profiles when you want cheaper broad review and stronger verification:

- `review_profile`: broad first-pass review.
- `verify_profile`: verifier pass for candidate findings.
- `escalated_review_profile`: stronger review for high-risk packs.

Use telemetry to tune cost, latency, cache hit rates, and coverage after real runs.
