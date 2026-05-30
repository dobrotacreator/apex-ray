from pathlib import Path

from apex_ray.invocation import ReviewOverrides, apply_review_overrides
from apex_ray.models import (
    LLMCoverageMode,
    LLMProfile,
    LLMProviderName,
    LLMRoutingConfig,
    ReviewConfig,
)


def test_apply_review_overrides_sets_review_options(tmp_path: Path) -> None:
    config = ReviewConfig()

    effective = apply_review_overrides(
        config,
        ReviewOverrides(
            llm_enabled=True,
            provider=LLMProviderName.FAKE,
            model="strong-local",
            verify=False,
            cache_allowed=False,
            refresh_cache=True,
            cache_dir=tmp_path / "llm-cache",
            llm_jobs=3,
            coverage_mode=LLMCoverageMode.EXHAUSTIVE,
            max_deep_packs=7,
            max_input_tokens=50_000,
        ),
    )

    assert config.llm.enabled is False
    assert effective.llm.enabled is True
    assert effective.llm.provider == LLMProviderName.FAKE
    assert effective.llm.model == "strong-local"
    assert effective.llm.verify is False
    assert effective.llm.cache_enabled is False
    assert effective.llm.refresh_cache is True
    assert effective.llm.cache_dir == str(tmp_path / "llm-cache")
    assert effective.llm.jobs == 3
    assert effective.llm.coverage_mode == LLMCoverageMode.EXHAUSTIVE
    assert effective.llm.max_deep_packs == 7
    assert effective.llm.max_input_tokens == 50_000


def test_model_override_clears_profiles_and_routing_by_default() -> None:
    config = ReviewConfig()
    config.llm.profiles = {"cheap": LLMProfile(model="cheap-local")}
    config.llm.routing = LLMRoutingConfig(review_profile="cheap")

    effective = apply_review_overrides(config, ReviewOverrides(model="strong-local"))

    assert effective.llm.model == "strong-local"
    assert effective.llm.profiles == {}
    assert effective.llm.routing == LLMRoutingConfig()
    assert config.llm.profiles == {"cheap": LLMProfile(model="cheap-local")}
    assert config.llm.routing.review_profile == "cheap"


def test_model_override_can_preserve_profiles_and_routing() -> None:
    config = ReviewConfig()
    config.llm.profiles = {"cheap": LLMProfile(model="cheap-local")}
    config.llm.routing = LLMRoutingConfig(review_profile="cheap")

    effective = apply_review_overrides(
        config,
        ReviewOverrides(model="strong-local", clear_routing_on_model=False),
    )

    assert effective.llm.model == "strong-local"
    assert effective.llm.profiles == config.llm.profiles
    assert effective.llm.routing == config.llm.routing


def test_default_cache_dir_only_fills_enabled_empty_cache_dir(tmp_path: Path) -> None:
    config = ReviewConfig()

    effective = apply_review_overrides(
        config,
        ReviewOverrides(default_cache_dir=tmp_path / "default-cache"),
    )
    disabled = apply_review_overrides(
        config,
        ReviewOverrides(cache_allowed=False, default_cache_dir=tmp_path / "disabled-cache"),
    )
    configured = config.model_copy(deep=True)
    configured.llm.cache_dir = "/already/configured"
    preserved = apply_review_overrides(
        configured,
        ReviewOverrides(default_cache_dir=tmp_path / "ignored-cache"),
    )

    assert effective.llm.cache_dir == str(tmp_path / "default-cache")
    assert disabled.llm.cache_enabled is False
    assert disabled.llm.cache_dir is None
    assert preserved.llm.cache_dir == "/already/configured"


def test_apply_review_overrides_sets_analyzer_options(tmp_path: Path) -> None:
    config = ReviewConfig()

    effective = apply_review_overrides(
        config,
        ReviewOverrides(
            analyzer_cache_allowed=False,
            refresh_analyzer_cache=True,
            analyzer_cache_dir=tmp_path / "index-cache",
            analyzer_timeout_seconds=45,
        ),
    )

    assert effective.analyzer.index_cache_enabled is False
    assert effective.analyzer.refresh_index_cache is True
    assert effective.analyzer.index_cache_dir == str(tmp_path / "index-cache")
    assert effective.analyzer.timeout_seconds == 45
