import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from apex_ray.llm.cache import (
    VERIFIER_PROMPT_VERSION,
    cache_for_config,
    review_cache_key,
    review_prompt_version,
    verification_cache_key,
)
from apex_ray.llm.errors import LLMProviderError, classify_llm_provider_error
from apex_ray.llm.findings import (
    dedupe_findings,
    filter_findings_for_context_pack,
)
from apex_ray.llm.findings import (
    verification_for_finding as _verification_for_finding,
)
from apex_ray.llm.providers import (
    LLMProvider,
    provider_from_config,
)
from apex_ray.llm.providers import (
    verify_findings_with_provider as _verify_findings_with_provider,
)
from apex_ray.llm.routing import (
    fallback_review_config_after_error as _fallback_review_config_after_error,
)
from apex_ray.llm.routing import (
    review_config_for_pack,
)
from apex_ray.llm.routing import (
    verification_groups_by_route as _verification_groups_by_route,
)
from apex_ray.llm.usage import (
    estimate_tokens as _estimate_tokens,
)
from apex_ray.llm.usage import (
    review_input_chars as _review_input_chars,
)
from apex_ray.llm.usage import (
    verification_batch_input_chars as _verification_batch_input_chars,
)
from apex_ray.models import (
    ContextPack,
    Finding,
    FindingConfidence,
    FindingVerification,
    LLMConfig,
    LLMRun,
)


def review_context_packs(
    packs: list[ContextPack],
    config: LLMConfig,
    repo_root: Path,
    provider: LLMProvider | None = None,
    *,
    review_depth: Literal["deep", "shallow"] = "deep",
) -> tuple[list[Finding], list[LLMRun]]:
    if not packs:
        return [], []

    base_config = config.model_copy(deep=True)
    base_config.review_depth = review_depth
    cache = cache_for_config(repo_root, config)
    findings: list[Finding] = []
    runs: list[LLMRun] = []

    def review_pack(pack: ContextPack) -> tuple[list[Finding], list[LLMRun]]:
        attempts = [review_config_for_pack(base_config, pack)]
        pack_findings: list[Finding] = []
        runs: list[LLMRun] = []
        attempted_fallback = False

        while attempts:
            pack_config, profile, route_reason = attempts.pop(0)
            llm_provider = provider or provider_from_config(pack_config)
            start = time.monotonic()
            cache_key = review_cache_key(pack, pack_config) if cache else None
            cache_hit = False
            provider_called = False
            try:
                cached_findings = None
                if cache and cache_key and not base_config.refresh_cache:
                    cached_findings = cache.read_review(cache_key)
                    cache_hit = cached_findings is not None
                if cached_findings is None:
                    provider_called = True
                    cached_findings = llm_provider.review_context_pack(pack, repo_root)
                    if cache and cache_key:
                        cache.write_review(cache_key, pack_config, cached_findings)
                pack_findings = filter_findings_for_context_pack(cached_findings, pack)
            except Exception as exc:  # keep one bad pack from failing the whole review
                status = classify_llm_provider_error(exc)
                input_chars = _review_input_chars(pack, review_depth=review_depth) if provider_called else 0
                runs.append(
                    LLMRun(
                        kind="review_shallow" if review_depth == "shallow" else "review",
                        provider=pack_config.provider,
                        model=pack_config.model,
                        profile=profile,
                        route_reason=route_reason,
                        prompt_version=review_prompt_version(pack_config),
                        context_pack_id=pack.id,
                        status=status,
                        duration_ms=_elapsed_ms(start),
                        input_chars=input_chars,
                        estimated_input_tokens=_estimate_tokens(input_chars),
                        cache_hit=False,
                        cache_hits=0,
                        cache_misses=1 if cache_key and provider_called else 0,
                        cache_key=cache_key,
                        error=str(exc),
                    )
                )
                fallback = _fallback_review_config_after_error(base_config, profile, status)
                if fallback is not None and not attempted_fallback:
                    attempts.append(fallback)
                    attempted_fallback = True
                    continue
                return [], runs

            input_chars = _review_input_chars(pack, review_depth=review_depth) if provider_called else 0
            runs.append(
                LLMRun(
                    kind="review_shallow" if review_depth == "shallow" else "review",
                    provider=pack_config.provider,
                    model=pack_config.model,
                    profile=profile,
                    route_reason=route_reason,
                    prompt_version=review_prompt_version(pack_config),
                    context_pack_id=pack.id,
                    status="ok",
                    duration_ms=_elapsed_ms(start),
                    input_chars=input_chars,
                    estimated_input_tokens=_estimate_tokens(input_chars),
                    findings_count=len(pack_findings),
                    cache_hit=cache_hit,
                    cache_hits=1 if cache_hit else 0,
                    cache_misses=1 if cache_key and not cache_hit else 0,
                    cache_key=cache_key,
                )
            )
            return pack_findings, runs

        return [], runs

    if provider is None and config.jobs > 1 and len(packs) > 1:
        with ThreadPoolExecutor(max_workers=config.jobs) as executor:
            results = [future.result() for future in [executor.submit(review_pack, pack) for pack in packs]]
    else:
        results = [review_pack(pack) for pack in packs]

    for pack_findings, pack_runs in results:
        findings = dedupe_findings([*findings, *pack_findings])
        runs.extend(pack_runs)

    return findings, runs


def verify_findings(
    findings: list[Finding],
    packs: list[ContextPack],
    config: LLMConfig,
    repo_root: Path,
    provider: LLMProvider | None = None,
) -> tuple[list[Finding], list[FindingVerification], list[LLMRun]]:
    if not findings:
        return [], [], []

    cache = cache_for_config(repo_root, config)
    packs_by_id = {pack.id: pack for pack in packs}
    verifications_by_index: dict[int, FindingVerification] = {}
    runs: list[LLMRun] = []
    findings_by_pack_id: dict[str, list[tuple[int, Finding]]] = {}

    for index, finding in enumerate(findings):
        pack = packs_by_id.get(finding.context_pack_id)
        if not pack:
            verifications_by_index[index] = FindingVerification(
                finding=finding,
                approved=False,
                confidence=FindingConfidence.HIGH,
                reason=f"Missing context pack: {finding.context_pack_id}",
            )
            continue
        findings_by_pack_id.setdefault(pack.id, []).append((index, finding))

    def verify_pack(
        pack_id: str,
        indexed_findings: list[tuple[int, Finding]],
        verification_config: LLMConfig,
        profile: str | None,
        route_reason: str,
    ) -> tuple[dict[int, FindingVerification], LLMRun]:
        pack = packs_by_id[pack_id]
        llm_provider = provider or provider_from_config(verification_config)
        start = time.monotonic()
        cache_keys = {
            index: verification_cache_key(finding, pack, verification_config) if cache else None
            for index, finding in indexed_findings
        }
        cache_hit = False
        cache_hits = 0
        cache_misses = 0
        pack_verifications: dict[int, FindingVerification] = {}
        misses: list[tuple[int, Finding]] = []
        status = "ok"
        error: str | None = None
        try:
            for index, finding in indexed_findings:
                verification = None
                cache_key = cache_keys[index]
                if cache and cache_key and not config.refresh_cache:
                    verification = cache.read_verification(cache_key, finding)
                if verification is None:
                    misses.append((index, finding))
                else:
                    pack_verifications[index] = verification

            cache_hit = not misses
            if cache:
                cache_hits = len(indexed_findings) - len(misses)
                cache_misses = len(misses)
            if misses:
                missed_findings = [finding for _, finding in misses]
                provider_verifications = _verify_findings_with_provider(llm_provider, missed_findings, pack, repo_root)
                if len(provider_verifications) != len(missed_findings):
                    raise LLMProviderError(
                        f"Verifier returned {len(provider_verifications)} decisions for {len(missed_findings)} findings."
                    )
                for (index, finding), verification in zip(misses, provider_verifications, strict=True):
                    normalized_verification = _verification_for_finding(verification, finding)
                    pack_verifications[index] = normalized_verification
                    cache_key = cache_keys[index]
                    if cache and cache_key:
                        cache.write_verification(cache_key, verification_config, normalized_verification)
        except Exception as exc:
            status = classify_llm_provider_error(exc)
            error = str(exc)
            for index, finding in misses:
                pack_verifications[index] = FindingVerification(
                    finding=finding,
                    approved=False,
                    confidence=FindingConfidence.LOW,
                    reason=f"Verifier failed for this finding: {error}",
                )

        input_chars = _verification_batch_input_chars([finding for _, finding in misses], pack) if misses else 0
        return (
            pack_verifications,
            LLMRun(
                kind="verify",
                provider=verification_config.provider,
                model=verification_config.model,
                profile=profile,
                route_reason=route_reason,
                prompt_version=VERIFIER_PROMPT_VERSION,
                context_pack_id=pack.id,
                status=status,
                duration_ms=_elapsed_ms(start),
                input_chars=input_chars,
                estimated_input_tokens=_estimate_tokens(input_chars),
                findings_count=sum(1 for verification in pack_verifications.values() if verification.approved),
                cache_hit=cache_hit,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
                cache_key=next(iter(cache_keys.values())) if len(cache_keys) == 1 else None,
                error=error,
            ),
        )

    verification_groups = _verification_groups_by_route(findings_by_pack_id, packs_by_id, config)
    if provider is None and config.jobs > 1 and len(verification_groups) > 1:
        with ThreadPoolExecutor(max_workers=config.jobs) as executor:
            results = [
                future.result()
                for future in [
                    executor.submit(verify_pack, pack_id, group, route_config, profile, route_reason)
                    for pack_id, group, route_config, profile, route_reason in verification_groups
                ]
            ]
    else:
        results = [
            verify_pack(pack_id, group, route_config, profile, route_reason)
            for pack_id, group, route_config, profile, route_reason in verification_groups
        ]

    for pack_verifications, run in results:
        verifications_by_index.update(pack_verifications)
        runs.append(run)

    verifications = [verifications_by_index[index] for index in range(len(findings))]
    approved_findings = [verification.finding for verification in verifications if verification.approved]

    return approved_findings, verifications, runs


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
