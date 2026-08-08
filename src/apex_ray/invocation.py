import os
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

from apex_ray.models import (
    LLMAPIConfig,
    LLMCoverageMode,
    LLMProviderName,
    LLMRoutingConfig,
    ReviewConfig,
)
from apex_ray.version_lock import render_uvx_argv


class ApexRayLauncherKind(StrEnum):
    """Supported repository command launchers."""

    BARE = "bare"
    UVX = "uvx"
    SOURCE = "source"


@dataclass(frozen=True, slots=True)
class ApexRayLauncher:
    """Validated argv prefix for invoking Apex Ray in a repository."""

    kind: ApexRayLauncherKind
    version: str | None = None

    def __post_init__(self) -> None:
        if self.kind == ApexRayLauncherKind.UVX:
            if self.version is None:
                raise ValueError("The uvx Apex Ray launcher requires an exact version.")
            render_uvx_argv(self.version)
            return
        if self.version is not None:
            raise ValueError(f"The {self.kind.value} Apex Ray launcher does not accept a version.")

    @classmethod
    def bare(cls) -> Self:
        return cls(ApexRayLauncherKind.BARE)

    @classmethod
    def locked(cls, version: str) -> Self:
        return cls(ApexRayLauncherKind.UVX, version=version)

    @classmethod
    def source(cls) -> Self:
        return cls(ApexRayLauncherKind.SOURCE)

    def argv(self, *arguments: str) -> list[str]:
        if self.kind == ApexRayLauncherKind.UVX:
            return render_uvx_argv(self.version or "", *arguments)
        if self.kind == ApexRayLauncherKind.SOURCE:
            return ["uv", "run", "--locked", "apex-ray", *arguments]
        return ["apex-ray", *arguments]


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
    max_packs: int | None = None


def render_shell_command(
    args: Sequence[str],
    *,
    platform_name: str | None = None,
) -> str:
    """Render argv for the platform shell used by continuation guidance."""

    if (platform_name or os.name) == "nt":
        return "& " + " ".join(_quote_powershell_argument(arg) for arg in args)
    return shlex.join(args)


def render_apex_ray_command(
    args: Sequence[str],
    *,
    launcher: ApexRayLauncher | None = None,
    launcher_version: str | None = None,
    platform_name: str | None = None,
) -> str:
    """Render an Apex Ray command through its repository-selected launcher."""

    if launcher is not None and launcher_version is not None:
        raise ValueError("Pass either launcher or launcher_version, not both.")
    effective_launcher = launcher
    if effective_launcher is None:
        effective_launcher = (
            ApexRayLauncher.locked(launcher_version) if launcher_version is not None else ApexRayLauncher.bare()
        )
    return render_shell_command(effective_launcher.argv(*args), platform_name=platform_name)


def _quote_powershell_argument(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def apply_review_overrides(config: ReviewConfig, overrides: ReviewOverrides) -> ReviewConfig:
    effective = config.model_copy(deep=True)

    if overrides.llm_enabled is not None:
        effective.llm.enabled = overrides.llm_enabled
    if overrides.provider is not None:
        inherited_provider = effective.llm.provider
        effective.llm.provider = overrides.provider
        if inherited_provider != overrides.provider:
            effective.llm.api = LLMAPIConfig()
        for profile in effective.llm.profiles.values():
            profile_provider = profile.provider or inherited_provider
            profile.provider = overrides.provider
            if profile_provider != overrides.provider:
                profile.api = None
    if overrides.model is not None:
        effective.llm.model = overrides.model
        if overrides.clear_routing_on_model:
            effective.llm.profiles = {}
            effective.llm.routing = LLMRoutingConfig()
            for reviewer in effective.reviewers:
                reviewer.profile = None
                reviewer.verify_profile = None
            for rule in effective.rule_definitions:
                rule.model = None
                rule.verify = None
    if overrides.verify is not None:
        effective.llm.verify = overrides.verify
        for reviewer in effective.reviewers:
            reviewer.verify = overrides.verify
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
        for reviewer in effective.reviewers:
            reviewer.coverage_mode = overrides.coverage_mode
    if overrides.max_packs is not None:
        effective.llm.max_packs = overrides.max_packs
        for reviewer in effective.reviewers:
            reviewer.max_packs = overrides.max_packs
    if overrides.max_deep_packs is not None:
        effective.llm.max_deep_packs = overrides.max_deep_packs
        for reviewer in effective.reviewers:
            reviewer.max_deep_packs = overrides.max_deep_packs
    if overrides.max_input_tokens is not None:
        effective.llm.max_input_tokens = overrides.max_input_tokens
        for reviewer in effective.reviewers:
            reviewer.max_input_tokens = overrides.max_input_tokens
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
