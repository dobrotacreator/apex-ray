from apex_ray.models import ContextPack, Finding, LLMConfig, RuleMatch


def verification_groups_by_route(
    findings_by_pack_id: dict[str, list[tuple[int, Finding]]],
    packs_by_id: dict[str, ContextPack],
    config: LLMConfig,
) -> list[tuple[str, list[tuple[int, Finding]], LLMConfig, str | None, str]]:
    groups: list[tuple[str, list[tuple[int, Finding]], LLMConfig, str | None, str]] = []
    for pack_id, indexed_findings in findings_by_pack_id.items():
        pack = packs_by_id[pack_id]
        route_groups: dict[
            tuple[str, str | None, str | None, str | None, str | None, str | None, int, str | None, str],
            tuple[LLMConfig, str | None, str, list[tuple[int, Finding]]],
        ] = {}
        for index, finding in indexed_findings:
            route_config, profile, route_reason = verification_config_for_finding(config, finding, pack)
            route_key = verification_route_key(route_config, profile, route_reason)
            if route_key not in route_groups:
                route_groups[route_key] = (route_config, profile, route_reason, [])
            route_groups[route_key][3].append((index, finding))
        groups.extend(
            (pack_id, route_findings, route_config, profile, route_reason)
            for route_config, profile, route_reason, route_findings in route_groups.values()
        )
    return groups


def verification_route_key(
    config: LLMConfig,
    profile: str | None,
    route_reason: str,
) -> tuple[str, str | None, str | None, str | None, str | None, str | None, int, str | None, str]:
    return (
        str(config.provider),
        config.model,
        str(config.effort) if config.effort else None,
        profile,
        config.codex_path,
        config.claude_path,
        config.timeout_seconds,
        config.cache_dir,
        route_reason,
    )


def review_config_for_pack(config: LLMConfig, pack: ContextPack) -> tuple[LLMConfig, str | None, str]:
    if config.review_depth == "shallow":
        if config.routing.review_profile:
            resolved, profile, reason = config_for_profile_or_model(
                config,
                config.routing.review_profile,
                f"profile:{config.routing.review_profile}",
            )
            return resolved, profile, f"shallow:{reason}"
        return config.model_copy(deep=True), None, "shallow:default"

    rule_override = rule_model_override(pack.rule_matches, field="model")
    if rule_override:
        return config_for_profile_or_model(config, rule_override, f"rule:{rule_override}")

    routing = config.routing
    escalate_reason = routing_condition_reason(routing.escalate_review_when, pack)
    if routing.escalated_review_profile and escalate_reason:
        return config_for_profile_or_model(
            config,
            routing.escalated_review_profile,
            f"escalated:{routing.escalated_review_profile}:{escalate_reason}",
        )
    if routing.review_profile:
        return config_for_profile_or_model(config, routing.review_profile, f"profile:{routing.review_profile}")
    return config.model_copy(deep=True), None, "default"


def fallback_review_config_after_error(
    config: LLMConfig,
    failed_profile: str | None,
    status: str,
) -> tuple[LLMConfig, str | None, str] | None:
    if status != "failed_quota":
        return None
    fallback_profile = config.routing.escalated_review_profile
    if not fallback_profile or fallback_profile == failed_profile:
        return None
    return config_for_profile_or_model(
        config,
        fallback_profile,
        f"fallback:{fallback_profile}:after_{status}",
    )


def verification_config_for_finding(
    config: LLMConfig,
    finding: Finding,
    pack: ContextPack,
) -> tuple[LLMConfig, str | None, str]:
    return verification_config_for_findings(config, [finding], pack)


def verification_config_for_findings(
    config: LLMConfig,
    findings: list[Finding],
    pack: ContextPack,
) -> tuple[LLMConfig, str | None, str]:
    rule_override = rule_model_override(pack.rule_matches, field="verify")
    if rule_override:
        return config_for_profile_or_model(config, rule_override, f"rule-verify:{rule_override}")

    routing = config.routing
    escalate_reason = next(
        (
            reason
            for finding in findings
            if (reason := routing_condition_reason(routing.escalate_verify_when, pack, finding))
        ),
        None,
    )
    if routing.escalated_verify_profile and escalate_reason:
        return config_for_profile_or_model(
            config,
            routing.escalated_verify_profile,
            f"escalated-verify:{routing.escalated_verify_profile}:{escalate_reason}",
        )
    if routing.verify_profile:
        return config_for_profile_or_model(config, routing.verify_profile, f"profile:{routing.verify_profile}")
    return config.model_copy(deep=True), None, "default"


def config_for_profile_or_model(
    config: LLMConfig,
    profile_or_model: str,
    reason: str,
) -> tuple[LLMConfig, str | None, str]:
    if profile_or_model in config.profiles:
        profile = config.profiles[profile_or_model]
        resolved = config.model_copy(deep=True)
        if profile.provider:
            resolved.provider = profile.provider
        if profile.model is not None:
            resolved.model = profile.model
        if profile.effort is not None:
            resolved.effort = profile.effort
        if profile.timeout_seconds is not None:
            resolved.timeout_seconds = profile.timeout_seconds
        if profile.codex_path is not None:
            resolved.codex_path = profile.codex_path
        if profile.claude_path is not None:
            resolved.claude_path = profile.claude_path
        return resolved, profile_or_model, reason

    resolved = config.model_copy(deep=True)
    resolved.model = profile_or_model
    return resolved, None, reason


def rule_model_override(rule_matches: list[RuleMatch], field: str) -> str | None:
    ranked = sorted(
        [(index, rule) for index, rule in enumerate(rule_matches) if getattr(rule, field)],
        key=lambda item: (_severity_rank(str(item[1].severity)), item[1].mode == "strict", -item[0]),
        reverse=True,
    )
    if not ranked:
        return None
    value = getattr(ranked[0][1], field)
    return str(value) if value else None


def routing_condition_matches(condition: object, pack: ContextPack, finding: Finding | None = None) -> bool:
    return routing_condition_reason(condition, pack, finding) is not None


def routing_condition_reason(condition: object, pack: ContextPack, finding: Finding | None = None) -> str | None:
    excluded_file_kinds = getattr(condition, "exclude_file_kind", [])
    if excluded_file_kinds and str(pack.file_kind) in {str(file_kind) for file_kind in excluded_file_kinds}:
        return None

    file_kinds = getattr(condition, "file_kind", [])
    if file_kinds and str(pack.file_kind) in {str(file_kind) for file_kind in file_kinds}:
        return f"file_kind:{pack.file_kind}"

    finding_severities = getattr(condition, "finding_severity", [])
    if (
        finding is not None
        and finding_severities
        and str(finding.severity) in {str(severity) for severity in finding_severities}
    ):
        return f"finding_severity:{finding.severity}"

    finding_confidences = getattr(condition, "finding_confidence", [])
    if (
        finding is not None
        and finding_confidences
        and str(finding.confidence) in {str(confidence) for confidence in finding_confidences}
    ):
        return f"finding_confidence:{finding.confidence}"

    risk = getattr(condition, "risk", [])
    if risk:
        risk_kinds = {signal.kind for signal in pack.risk_signals}
        matched = sorted(str(kind) for kind in risk if kind in risk_kinds)
        if matched:
            return f"risk:{matched[0]}"

    rule_severity = getattr(condition, "rule_severity", [])
    if rule_severity:
        severities = {str(rule.severity) for rule in pack.rule_matches}
        matched = sorted(str(severity) for severity in rule_severity if str(severity) in severities)
        if matched:
            return f"rule_severity:{matched[0]}"

    if getattr(condition, "strict_rule", False) and any(rule.mode == "strict" for rule in pack.rule_matches):
        return "strict_rule"

    if getattr(condition, "pack_truncated", False) and pack.stats.truncated:
        return "pack_truncated"

    min_pack_chars = getattr(condition, "min_pack_chars", None)
    if min_pack_chars is not None and pack.stats.estimated_chars >= min_pack_chars:
        return f"min_pack_chars:{min_pack_chars}"

    return None


def _severity_rank(severity: str) -> int:
    return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(severity, 0)
