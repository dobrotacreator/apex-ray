import json
from typing import Literal, TypedDict

from apex_ray.llm.prompts import build_review_prompt, build_shallow_review_prompt, build_verifier_batch_prompt
from apex_ray.models import ContextPack, Finding, LLMRun, LLMUsage


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
) -> int:
    return estimate_tokens(review_input_chars(pack, review_depth=review_depth))


def verification_batch_input_chars(findings: list[Finding], pack: ContextPack) -> int:
    return len(build_verifier_batch_prompt(findings, pack)) if findings else 0


def estimate_tokens(chars: int) -> int:
    return max(1, (chars + 3) // 4) if chars else 0


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


def _float_or_none(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
