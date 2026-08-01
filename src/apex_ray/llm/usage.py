import json
from collections.abc import Mapping
from typing import Literal, TypedDict

from apex_ray.llm.prompts import build_review_prompt, build_shallow_review_prompt, build_verifier_batch_prompt
from apex_ray.models import ContextPack, Finding, LLMAPIProtocol, LLMProviderName, LLMRun, LLMUsage


class LLMRunUsageFields(TypedDict, total=False):
    actual_input_tokens: int
    actual_cached_input_tokens: int
    actual_output_tokens: int
    actual_reasoning_output_tokens: int
    actual_total_tokens: int
    actual_cache_read_input_tokens: int
    actual_cache_creation_input_tokens: int
    estimated_cost_usd: float | None
    usage_source: str


class LLMUsageTotals(TypedDict):
    actual_input_tokens: int
    actual_cached_input_tokens: int
    actual_output_tokens: int
    actual_reasoning_output_tokens: int
    actual_total_tokens: int
    actual_cache_read_input_tokens: int
    actual_cache_creation_input_tokens: int
    estimated_saved_input_tokens: int
    estimated_cost_usd: float | None
    usage_sources: list[str]


def review_input_chars(pack: ContextPack, *, review_depth: Literal["deep", "shallow"] = "deep") -> int:
    if review_depth == "shallow":
        return len(build_shallow_review_prompt(pack))
    return len(build_review_prompt(pack))


def estimate_review_input_tokens(
    pack: ContextPack,
    *,
    review_depth: Literal["deep", "shallow"] = "deep",
    provider: LLMProviderName | str | None = None,
) -> int:
    return estimate_provider_input_tokens(
        review_input_chars(pack, review_depth=review_depth),
        provider=provider,
    )


def verification_batch_input_chars(findings: list[Finding], pack: ContextPack) -> int:
    return len(build_verifier_batch_prompt(findings, pack)) if findings else 0


def estimate_tokens(chars: int) -> int:
    return max(1, (chars + 3) // 4) if chars else 0


def estimate_provider_input_tokens(
    chars: int,
    *,
    provider: LLMProviderName | str | None = None,
) -> int:
    """Estimate request input for budgeting, not post-run accounting.

    CLI providers include a sizeable system/tool scaffold that is absent from
    the prompt string. Direct APIs stay closer to prompt tokenization. Actual
    provider usage, when returned, remains the accounting source of truth.
    """
    if chars <= 0:
        return 0

    provider_name = str(provider) if provider is not None else None
    if provider_name == LLMProviderName.CODEX_CLI:
        # 4.25x the legacy chars/4 estimate.
        return _ceil_div(chars * 17, 16)
    if provider_name == LLMProviderName.CLAUDE_CODE_CLI:
        # 5x the legacy chars/4 estimate.
        return _ceil_div(chars * 5, 4)
    if provider_name in {
        LLMProviderName.DEEPSEEK_API,
        LLMProviderName.QWEN_API,
        LLMProviderName.KIMI_API,
        LLMProviderName.ZAI_API,
    }:
        return _ceil_div(chars, 2) + 768
    if provider_name == LLMProviderName.OPENAI_COMPATIBLE:
        return _ceil_div(chars, 2) + 1024
    if provider_name in {
        LLMProviderName.OPENAI_API,
        LLMProviderName.ANTHROPIC_API,
    }:
        return _ceil_div(chars, 3) + 512
    if provider_name == LLMProviderName.FAKE:
        return estimate_tokens(chars)
    return _ceil_div(chars, 3) + 1024


def llm_run_usage_fields(usage: LLMUsage | None) -> LLMRunUsageFields:
    if usage is None:
        return {}
    return {
        "actual_input_tokens": usage.input_tokens,
        "actual_cached_input_tokens": usage.cached_input_tokens,
        "actual_output_tokens": usage.output_tokens,
        "actual_reasoning_output_tokens": usage.reasoning_output_tokens,
        "actual_total_tokens": usage.total_tokens,
        "actual_cache_read_input_tokens": usage.cache_read_input_tokens,
        "actual_cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "estimated_cost_usd": usage.estimated_cost_usd,
        "usage_source": usage.source,
    }


def parse_codex_usage_from_jsonl(text: str) -> LLMUsage | None:
    last_usage: dict[str, object] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                last_usage = usage
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        total = info.get("total_token_usage")
        if isinstance(total, dict):
            last_usage = total
        else:
            last_usage = info
    if last_usage is None:
        return None
    return _usage_from_mapping(last_usage, source="codex_cli_json", cached_tokens_in_input=True)


def parse_claude_usage_from_json(text: str) -> LLMUsage | None:
    try:
        raw = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None
    cost = _float_or_none(raw.get("total_cost_usd") or raw.get("cost_usd") or raw.get("costUSD"))
    return _usage_from_mapping(
        usage,
        source="claude_json",
        estimated_cost_usd=cost,
        cached_tokens_in_input=False,
    )


def parse_api_usage(
    usage: Mapping[str, object],
    *,
    source: str,
    protocol: LLMAPIProtocol,
) -> LLMUsage | None:
    input_details = _mapping(usage.get("input_tokens_details"))
    prompt_details = _mapping(usage.get("prompt_tokens_details"))
    output_details = _mapping(usage.get("output_tokens_details"))
    completion_details = _mapping(usage.get("completion_tokens_details"))

    input_tokens = _first_int(usage, "input_tokens", "prompt_tokens")
    output_tokens = _first_int(usage, "output_tokens", "completion_tokens")
    cache_read_input_tokens = max(
        _first_int(
            usage,
            "cache_read_input_tokens",
            "prompt_cache_hit_tokens",
            "cached_input_tokens",
            "cached_tokens",
        ),
        _first_int(input_details, "cached_tokens"),
        _first_int(prompt_details, "cached_tokens"),
    )
    cache_creation_input_tokens = _first_int(
        usage,
        "cache_creation_input_tokens",
        "cache_creation_tokens",
        "cache_write_input_tokens",
        "cache_write_tokens",
    )
    reasoning_output_tokens = max(
        _first_int(usage, "reasoning_output_tokens", "reasoning_tokens"),
        _first_int(output_details, "reasoning_tokens"),
        _first_int(completion_details, "reasoning_tokens"),
    )
    normalized: dict[str, object] = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cache_read_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": _first_int(usage, "total_tokens"),
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
    }
    return _usage_from_mapping(
        normalized,
        source=source,
        estimated_cost_usd=_float_or_none(
            usage.get("estimated_cost_usd") or usage.get("cost_usd") or usage.get("cost")
        ),
        cached_tokens_in_input=protocol != LLMAPIProtocol.ANTHROPIC_MESSAGES,
    )


def aggregate_actual_usage(runs: list[LLMRun]) -> LLMUsageTotals:
    cost_values = [run.estimated_cost_usd for run in runs if run.estimated_cost_usd is not None]
    sources = sorted({run.usage_source for run in runs if run.usage_source})
    return {
        "actual_input_tokens": sum(run.actual_input_tokens for run in runs),
        "actual_cached_input_tokens": sum(run.actual_cached_input_tokens for run in runs),
        "actual_output_tokens": sum(run.actual_output_tokens for run in runs),
        "actual_reasoning_output_tokens": sum(run.actual_reasoning_output_tokens for run in runs),
        "actual_total_tokens": sum(run.actual_total_tokens for run in runs),
        "actual_cache_read_input_tokens": sum(run.actual_cache_read_input_tokens for run in runs),
        "actual_cache_creation_input_tokens": sum(run.actual_cache_creation_input_tokens for run in runs),
        "estimated_saved_input_tokens": sum(run.estimated_saved_input_tokens for run in runs),
        "estimated_cost_usd": round(sum(cost_values), 6) if cost_values else None,
        "usage_sources": sources,
    }


def _usage_from_mapping(
    mapping: dict[str, object],
    *,
    source: str,
    estimated_cost_usd: float | None = None,
    cached_tokens_in_input: bool,
) -> LLMUsage | None:
    input_tokens = _int(mapping.get("input_tokens"))
    cached_input_tokens = _int(mapping.get("cached_input_tokens"))
    output_tokens = _int(mapping.get("output_tokens"))
    reasoning_output_tokens = _int(mapping.get("reasoning_output_tokens"))
    cache_read_input_tokens = _int(mapping.get("cache_read_input_tokens"))
    cache_creation_input_tokens = _int(mapping.get("cache_creation_input_tokens"))
    if not cached_input_tokens:
        cached_input_tokens = cache_read_input_tokens
    total_tokens = _int(mapping.get("total_tokens"))
    if not total_tokens:
        cache_tokens = 0 if cached_tokens_in_input else cached_input_tokens + cache_creation_input_tokens
        total_tokens = input_tokens + cache_tokens + output_tokens
    if not any(
        [
            input_tokens,
            cached_input_tokens,
            output_tokens,
            reasoning_output_tokens,
            cache_read_input_tokens,
            cache_creation_input_tokens,
            total_tokens,
            estimated_cost_usd,
        ]
    ):
        return None
    return LLMUsage(
        source=source,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        total_tokens=total_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _first_int(mapping: Mapping[str, object], *keys: str) -> int:
    for key in keys:
        value = _int(mapping.get(key))
        if value:
            return value
    return 0


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {}


def _float_or_none(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator
