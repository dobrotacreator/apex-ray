from apex_ray.models import ContextPack, LLMConfig, ReviewerConfig, ReviewerPromptContext
from apex_ray.path_matching import path_matches_any


class ReviewerConfigError(ValueError):
    pass


def effective_reviewers(
    configured: list[ReviewerConfig],
    requested_ids: list[str] | None = None,
) -> list[ReviewerConfig]:
    if not configured:
        configured = [
            ReviewerConfig(
                id="general",
                name="General review",
            )
        ]
    enabled_by_id = {reviewer.id: reviewer for reviewer in configured if reviewer.enabled}
    if requested_ids is None:
        return list(enabled_by_id.values())

    selected: list[ReviewerConfig] = []
    seen: set[str] = set()
    for reviewer_id in requested_ids:
        if reviewer_id in seen:
            continue
        reviewer = enabled_by_id.get(reviewer_id)
        if reviewer is None:
            raise ReviewerConfigError(f"Unknown or disabled reviewer: {reviewer_id}")
        selected.append(reviewer)
        seen.add(reviewer_id)
    if not selected:
        raise ReviewerConfigError("At least one reviewer must be selected")
    return selected


def reviewer_matches_pack(reviewer: ReviewerConfig, pack: ContextPack) -> bool:
    if reviewer.paths and not _path_matches(pack.file, reviewer.paths):
        return False
    if reviewer.exclude_paths and _path_matches(pack.file, reviewer.exclude_paths):
        return False
    if reviewer.file_kinds and pack.file_kind not in reviewer.file_kinds:
        return False
    if not reviewer.risk and not reviewer.risk_tags:
        return True

    risk_kinds = {signal.kind for signal in pack.risk_signals}
    risk_tags = {tag for signal in pack.risk_signals for tag in signal.reviewer_tags}
    return bool(risk_kinds.intersection(reviewer.risk) or risk_tags.intersection(reviewer.risk_tags))


def llm_config_for_reviewer(config: LLMConfig, reviewer: ReviewerConfig) -> LLMConfig:
    resolved = config.model_copy(deep=True)
    if reviewer.profile is not None:
        resolved.routing.review_profile = reviewer.profile
    if reviewer.verify_profile is not None:
        resolved.routing.verify_profile = reviewer.verify_profile
    if reviewer.coverage_mode is not None:
        resolved.coverage_mode = reviewer.coverage_mode
    if reviewer.max_packs is not None:
        resolved.max_packs = reviewer.max_packs
    if reviewer.max_deep_packs is not None:
        resolved.max_deep_packs = reviewer.max_deep_packs
    if reviewer.max_input_tokens is not None:
        resolved.max_input_tokens = reviewer.max_input_tokens
    if reviewer.verify is not None:
        resolved.verify = reviewer.verify
    return resolved


def pack_for_reviewer(pack: ContextPack, reviewer: ReviewerConfig) -> ContextPack:
    if reviewer.id == "general" and not reviewer.focus and not reviewer.instructions:
        return pack
    return pack.model_copy(
        update={
            "reviewer": ReviewerPromptContext(
                id=reviewer.id,
                name=reviewer.name or reviewer.id,
                focus=reviewer.focus,
                instructions=reviewer.instructions,
            )
        },
    )


def _path_matches(path: str, patterns: list[str]) -> bool:
    return path_matches_any(path, patterns)
