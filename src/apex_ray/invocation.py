from dataclasses import dataclass
from pathlib import Path

from apex_ray.models import (
    LLMCoverageMode,
    LLMProviderName,
    LLMRoutingConfig,
    ReviewConfig,
)


@dataclass(frozen=True, slots=True)
class ReviewOverrides:
    llm_enabled: bool | None = None
    provider: LLMProviderName | None = None
    model: str | None = None
    clear_routing_on_model: bool = True
    verify: bool | None = None
    cache_allowed: bool | None = None
    refresh_cache: bool = False
    cache_dir: Path | None = None
    default_cache_dir: Path | None = None
    llm_jobs: int | None = None
    coverage_mode: LLMCoverageMode | None = None
    max_deep_packs: int | None = None
    max_input_tokens: int | None = None
    analyzer_cache_allowed: bool | None = None
    refresh_analyzer_cache: bool = False
    analyzer_cache_dir: Path | None = None
    analyzer_timeout_seconds: int | None = None


def apply_review_overrides(config: ReviewConfig, overrides: ReviewOverrides) -> ReviewConfig:
    effective = config.model_copy(deep=True)

    if overrides.llm_enabled is not None:
        effective.llm.enabled = overrides.llm_enabled
    if overrides.provider is not None:
        effective.llm.provider = overrides.provider
    if overrides.model is not None:
        effective.llm.model = overrides.model
        if overrides.clear_routing_on_model:
            effective.llm.profiles = {}
            effective.llm.routing = LLMRoutingConfig()
    if overrides.verify is not None:
        effective.llm.verify = overrides.verify
    if overrides.cache_allowed is not None:
        effective.llm.cache_enabled = overrides.cache_allowed and effective.llm.cache_enabled
    if overrides.refresh_cache:
        effective.llm.refresh_cache = True
    if overrides.cache_dir is not None:
        effective.llm.cache_dir = str(overrides.cache_dir)
    elif overrides.default_cache_dir is not None and effective.llm.cache_enabled and not effective.llm.cache_dir:
        effective.llm.cache_dir = str(overrides.default_cache_dir)
    if overrides.llm_jobs is not None:
        effective.llm.jobs = overrides.llm_jobs
    if overrides.coverage_mode is not None:
        effective.llm.coverage_mode = overrides.coverage_mode
    if overrides.max_deep_packs is not None:
        effective.llm.max_deep_packs = overrides.max_deep_packs
    if overrides.max_input_tokens is not None:
        effective.llm.max_input_tokens = overrides.max_input_tokens
    if overrides.analyzer_cache_allowed is not None:
        effective.analyzer.index_cache_enabled = (
            overrides.analyzer_cache_allowed and effective.analyzer.index_cache_enabled
        )
    if overrides.refresh_analyzer_cache:
        effective.analyzer.refresh_index_cache = True
    if overrides.analyzer_cache_dir is not None:
        effective.analyzer.index_cache_dir = str(overrides.analyzer_cache_dir)
    if overrides.analyzer_timeout_seconds is not None:
        effective.analyzer.timeout_seconds = overrides.analyzer_timeout_seconds

    return effective
