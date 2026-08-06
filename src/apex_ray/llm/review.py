import json
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

from apex_ray.llm.api import validate_api_model_compatibility
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
from apex_ray.llm.prompts import ReviewPromptCache, render_review_prompt
from apex_ray.llm.providers import (
    LLMProvider,
    provider_from_config,
    review_context_pack_with_provider,
)
from apex_ray.llm.providers import (
    verify_findings_with_provider_result as _verify_findings_with_provider_result,
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
    estimate_provider_input_tokens as _estimate_provider_input_tokens,
)
from apex_ray.llm.usage import (
    llm_run_usage_fields as _llm_run_usage_fields,
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
    ReviewerConfig,
)
from apex_ray.progress import NoopProgress, ProgressSink
from apex_ray.reviewers import pack_for_reviewer

type _RouteKey = tuple[
    str,
    str | None,
    str | None,
    int,
    str | None,
    str | None,
]
_TERMINAL_PROVIDER_FAILURES = {"failed_auth", "failed_quota"}
_RETRYABLE_PROVIDER_FAILURES = {
    "failed_provider",
    "failed_rate_limit",
    "failed_timeout",
}


@dataclass
class _RouteCircuitState:
    consecutive_failures: int = 0
    open_reason: str | None = None
    trigger_status: str | None = None


class LLMRouteCircuitBreaker:
    """Run-scoped, thread-safe provider circuit state keyed by effective route."""

    def __init__(self) -> None:
        self._states: dict[_RouteKey, _RouteCircuitState] = {}
        self._lock = Lock()

    def open_failure(self, config: LLMConfig, profile: str | None) -> tuple[str, str] | None:
        key = _route_key(config, profile)
        with self._lock:
            state = self._states.get(key)
            if state is None or state.open_reason is None or state.trigger_status is None:
                return None
            return state.open_reason, state.trigger_status

    def record(
        self,
        config: LLMConfig,
        profile: str | None,
        status: str,
        *,
        max_consecutive_failures: int,
    ) -> None:
        key = _route_key(config, profile)
        with self._lock:
            state = self._states.setdefault(key, _RouteCircuitState())
            if state.open_reason is not None:
                return
            if status in _TERMINAL_PROVIDER_FAILURES:
                failure = status.removeprefix("failed_")
                state.open_reason = (
                    f"Provider circuit for {_route_label(config, profile)} opened after terminal {failure} failure."
                )
                state.trigger_status = status
                return
            if status in _RETRYABLE_PROVIDER_FAILURES:
                state.consecutive_failures += 1
                if state.consecutive_failures >= max_consecutive_failures:
                    state.open_reason = (
                        f"Provider circuit for {_route_label(config, profile)} opened after "
                        f"{state.consecutive_failures} consecutive provider failures."
                    )
                    state.trigger_status = status
                return
            state.consecutive_failures = 0


def review_context_packs(
    packs: list[ContextPack],
    config: LLMConfig,
    repo_root: Path,
    provider: LLMProvider | None = None,
    *,
    review_depth: Literal["deep", "shallow"] = "deep",
    progress: ProgressSink | None = None,
    reviewer: ReviewerConfig | None = None,
    circuit_breaker: LLMRouteCircuitBreaker | None = None,
    rendered_prompts: ReviewPromptCache | None = None,
) -> tuple[list[Finding], list[LLMRun]]:
    if not packs:
        return [], []

    progress = progress or NoopProgress()
    base_config = config.model_copy(deep=True)
    base_config.review_depth = review_depth
    cache = cache_for_config(repo_root, config)
    findings: list[Finding] = []
    runs: list[LLMRun] = []
    reviewer_id = reviewer.id if reviewer is not None else "general"
    review_packs = [pack_for_reviewer(pack, reviewer) for pack in packs] if reviewer is not None else packs
    route_circuit = circuit_breaker if circuit_breaker is not None else LLMRouteCircuitBreaker()

    def review_pack(pack: ContextPack) -> tuple[list[Finding], list[LLMRun]]:
        rendered_prompt = render_review_prompt(
            pack,
            review_depth=review_depth,
            cache=rendered_prompts,
        )
        rendered_input_chars = len(rendered_prompt.text)
        attempts = [review_config_for_pack(base_config, pack)]
        pack_findings: list[Finding] = []
        runs: list[LLMRun] = []
        attempted_fallback = False

        while attempts:
            pack_config, profile, route_reason = attempts.pop(0)
            start = time.monotonic()
            cache_key = (
                review_cache_key(
                    pack,
                    pack_config,
                    prompt_payload=rendered_prompt.prompt_payload,
                )
                if cache
                else None
            )
            cache_hit = False
            provider_called = False
            usage = None
            try:
                validate_api_model_compatibility(pack_config)
                cached_findings = None
                if cache and cache_key and not base_config.refresh_cache:
                    cached_findings = cache.read_review(cache_key)
                    cache_hit = cached_findings is not None
                if cached_findings is None:
                    # Worker-side admission keeps cache hits usable while
                    # preventing queued work from carrying a stale permit.
                    open_failure = route_circuit.open_failure(pack_config, profile)
                    if open_failure is not None:
                        reason, trigger_status = open_failure
                        runs.append(
                            _skipped_review_run(
                                pack,
                                pack_config,
                                profile,
                                route_reason,
                                review_depth,
                                reviewer_id,
                                reason,
                                cache_key,
                            )
                        )
                        fallback = _fallback_review_config_after_error(
                            base_config,
                            profile,
                            trigger_status,
                        )
                        if fallback is not None and not attempted_fallback:
                            attempts.append(fallback)
                            attempted_fallback = True
                            continue
                        return [], runs
                    provider_called = True
                    llm_provider = provider or provider_from_config(pack_config)
                    result = review_context_pack_with_provider(
                        llm_provider,
                        pack,
                        repo_root,
                        prompt=rendered_prompt.text,
                    )
                    cached_findings = result.findings
                    usage = result.usage
                    if cache and cache_key:
                        cache.write_review(cache_key, pack_config, cached_findings)
                filtered_findings = filter_findings_for_context_pack(cached_findings, pack)
                pack_findings = [
                    finding.model_copy(
                        update={
                            # Reviewer provenance is an execution fact, not
                            # model-authored output. Persist it explicitly so
                            # current reports remain distinguishable from
                            # legacy reports that predate durable origins.
                            "reviewer_ids": [] if reviewer is None else [reviewer_id],
                            "reviewer_context_pack_ids": {reviewer_id: [pack.id]},
                        }
                    )
                    for finding in filtered_findings
                ]
            except Exception as exc:  # keep one bad pack from failing the whole review
                status = classify_llm_provider_error(exc)
                if provider_called:
                    route_circuit.record(
                        pack_config,
                        profile,
                        status,
                        max_consecutive_failures=config.max_consecutive_provider_failures,
                    )
                input_chars = rendered_input_chars if provider_called else 0
                runs.append(
                    LLMRun(
                        kind="review_shallow" if review_depth == "shallow" else "review",
                        provider=pack_config.provider,
                        model=pack_config.model,
                        effort=pack_config.effort,
                        profile=profile,
                        route_reason=route_reason,
                        prompt_version=review_prompt_version(pack_config),
                        reviewer_id=reviewer_id,
                        context_pack_id=pack.id,
                        status=status,
                        duration_ms=_elapsed_ms(start),
                        input_chars=input_chars,
                        estimated_input_tokens=_estimate_provider_input_tokens(
                            input_chars,
                            provider=pack_config.provider,
                        ),
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

            if provider_called:
                route_circuit.record(
                    pack_config,
                    profile,
                    "ok",
                    max_consecutive_failures=config.max_consecutive_provider_failures,
                )
            input_chars = rendered_input_chars if provider_called else 0
            runs.append(
                LLMRun(
                    kind="review_shallow" if review_depth == "shallow" else "review",
                    provider=pack_config.provider,
                    model=pack_config.model,
                    effort=pack_config.effort,
                    profile=profile,
                    route_reason=route_reason,
                    prompt_version=review_prompt_version(pack_config),
                    reviewer_id=reviewer_id,
                    context_pack_id=pack.id,
                    status="ok",
                    duration_ms=_elapsed_ms(start),
                    input_chars=input_chars,
                    estimated_input_tokens=_estimate_provider_input_tokens(
                        input_chars,
                        provider=pack_config.provider,
                    ),
                    findings_count=len(pack_findings),
                    cache_hit=cache_hit,
                    cache_hits=1 if cache_hit else 0,
                    cache_misses=1 if cache_key and not cache_hit else 0,
                    cache_key=cache_key,
                    estimated_saved_input_tokens=(
                        _estimate_provider_input_tokens(
                            rendered_input_chars,
                            provider=pack_config.provider,
                        )
                        if cache_hit
                        else 0
                    ),
                    **_llm_run_usage_fields(usage),
                )
            )
            return pack_findings, runs

        return [], runs

    progress.event(
        f"review {review_depth}: {len(review_packs)} context pack(s), "
        f"jobs={_effective_jobs(config.jobs, len(review_packs))}",
        force=True,
    )
    if provider is None and config.jobs > 1 and len(review_packs) > 1:
        results: list[tuple[list[Finding], list[LLMRun]] | None] = [None] * len(review_packs)

        def submit_review_pack(
            executor: ThreadPoolExecutor,
            index: int,
        ) -> Future[tuple[list[Finding], list[LLMRun]]]:
            pack = review_packs[index]
            return executor.submit(review_pack, pack)

        completed = 0
        for index, result in _rolling_thread_pool_results(
            len(review_packs),
            max_workers=config.jobs,
            submit=submit_review_pack,
        ):
            results[index] = result
            completed += 1
            progress.event(
                _review_progress_message(
                    review_depth,
                    completed,
                    len(review_packs),
                    review_packs[index],
                    result[1],
                ),
                key=f"review:{review_depth}",
                force=completed == len(review_packs),
            )
        completed_results = [result for result in results if result is not None]
    else:
        completed_results = []
        for index, pack in enumerate(review_packs, start=1):
            progress.event(
                f"review {review_depth} {index}/{len(review_packs)} starting ({_short_context_id(pack.id)})",
                key=f"review:{review_depth}:start",
                force=index == 1,
            )
            result = review_pack(pack)
            completed_results.append(result)
            progress.event(
                _review_progress_message(review_depth, index, len(review_packs), pack, result[1]),
                key=f"review:{review_depth}",
                force=index == len(review_packs),
            )

    for pack_findings, pack_runs in completed_results:
        findings = dedupe_findings([*findings, *pack_findings])
        runs.extend(pack_runs)

    return findings, runs


def _skipped_review_run(
    pack: ContextPack,
    pack_config: LLMConfig,
    profile: str | None,
    route_reason: str,
    review_depth: Literal["deep", "shallow"],
    reviewer_id: str,
    reason: str,
    cache_key: str | None,
) -> LLMRun:
    return LLMRun(
        kind="review_shallow" if review_depth == "shallow" else "review",
        provider=pack_config.provider,
        model=pack_config.model,
        effort=pack_config.effort,
        profile=profile,
        route_reason=route_reason,
        prompt_version=review_prompt_version(pack_config),
        reviewer_id=reviewer_id,
        context_pack_id=pack.id,
        status="skipped_circuit_open",
        duration_ms=0,
        cache_hit=False,
        cache_hits=0,
        cache_misses=1 if cache_key else 0,
        cache_key=cache_key,
        error=reason,
    )


def verify_findings(
    findings: list[Finding],
    packs: list[ContextPack],
    config: LLMConfig,
    repo_root: Path,
    provider: LLMProvider | None = None,
    progress: ProgressSink | None = None,
    *,
    reviewer: ReviewerConfig | None = None,
    circuit_breaker: LLMRouteCircuitBreaker | None = None,
) -> tuple[list[Finding], list[FindingVerification], list[LLMRun]]:
    if not findings:
        return [], [], []

    progress = progress or NoopProgress()
    cache = cache_for_config(repo_root, config)
    reviewer_id = reviewer.id if reviewer is not None else "general"
    referenced_pack_ids = {finding.context_pack_id for finding in findings}
    referenced_packs = [pack for pack in packs if pack.id in referenced_pack_ids]
    verification_packs = (
        [pack_for_reviewer(pack, reviewer) for pack in referenced_packs] if reviewer is not None else referenced_packs
    )
    packs_by_id = {pack.id: pack for pack in verification_packs}
    verifications_by_index: dict[int, FindingVerification] = {}
    runs: list[LLMRun] = []
    findings_by_pack_id: dict[str, list[tuple[int, Finding]]] = {}
    route_circuit = circuit_breaker if circuit_breaker is not None else LLMRouteCircuitBreaker()

    for index, finding in enumerate(findings):
        pack = packs_by_id.get(finding.context_pack_id)
        if not pack:
            verifications_by_index[index] = FindingVerification(
                finding=finding,
                reviewer_id=reviewer_id,
                approved=False,
                confidence=FindingConfidence.HIGH,
                reason=f"Missing context pack: {finding.context_pack_id}",
                superseded=True,
                superseded_reason="Verification did not run because the context pack was unavailable.",
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
        start = time.monotonic()
        cache_keys = {
            index: verification_cache_key(finding, pack, verification_config) if cache else None
            for index, finding in indexed_findings
        }
        cache_hit = False
        cache_hits = 0
        cache_misses = 0
        usage = None
        pack_verifications: dict[int, FindingVerification] = {}
        misses = list(indexed_findings)
        status = "ok"
        error: str | None = None
        provider_called = False
        try:
            validate_api_model_compatibility(verification_config)
            misses = []
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
                # Recheck after cache reads: this task may have waited in the
                # executor while another in-flight call opened the circuit.
                open_failure = route_circuit.open_failure(verification_config, profile)
                if open_failure is not None:
                    reason, _trigger_status = open_failure
                    status = "skipped_circuit_open"
                    error = reason
                    for index, finding in misses:
                        pack_verifications[index] = FindingVerification(
                            finding=finding,
                            reviewer_id=reviewer_id,
                            approved=False,
                            confidence=FindingConfidence.LOW,
                            reason=f"Verifier skipped because {reason}",
                            superseded=True,
                            superseded_reason=(
                                "Verification run did not complete successfully (skipped_circuit_open)."
                            ),
                        )
                else:
                    provider_called = True
                    llm_provider = provider or provider_from_config(verification_config)
                    missed_findings = [finding for _, finding in misses]
                    result = _verify_findings_with_provider_result(llm_provider, missed_findings, pack, repo_root)
                    provider_verifications = result.verifications
                    usage = result.usage
                    if len(provider_verifications) != len(missed_findings):
                        raise LLMProviderError(
                            f"Verifier returned {len(provider_verifications)} decisions "
                            f"for {len(missed_findings)} findings."
                        )
                    for (index, finding), verification in zip(misses, provider_verifications, strict=True):
                        normalized_verification = _verification_for_finding(
                            verification,
                            finding,
                        ).model_copy(update={"reviewer_id": reviewer_id})
                        pack_verifications[index] = normalized_verification
                        cache_key = cache_keys[index]
                        if cache and cache_key:
                            cache.write_verification(cache_key, verification_config, normalized_verification)
        except Exception as exc:
            status = classify_llm_provider_error(exc)
            error = str(exc)
            if provider_called:
                route_circuit.record(
                    verification_config,
                    profile,
                    status,
                    max_consecutive_failures=config.max_consecutive_provider_failures,
                )
            for index, finding in misses:
                pack_verifications[index] = FindingVerification(
                    finding=finding,
                    reviewer_id=reviewer_id,
                    approved=False,
                    confidence=FindingConfidence.LOW,
                    reason=f"Verifier failed for this finding: {error}",
                    superseded=True,
                    superseded_reason=(f"Verification run did not complete successfully ({status})."),
                )
        else:
            if provider_called:
                route_circuit.record(
                    verification_config,
                    profile,
                    "ok",
                    max_consecutive_failures=config.max_consecutive_provider_failures,
                )

        input_chars = (
            _verification_batch_input_chars([finding for _, finding in misses], pack)
            if misses and provider_called
            else 0
        )
        pack_verifications = {
            index: verification.model_copy(update={"reviewer_id": reviewer_id})
            for index, verification in pack_verifications.items()
        }
        return (
            pack_verifications,
            LLMRun(
                kind="verify",
                provider=verification_config.provider,
                model=verification_config.model,
                effort=verification_config.effort,
                profile=profile,
                route_reason=route_reason,
                prompt_version=VERIFIER_PROMPT_VERSION,
                reviewer_id=reviewer_id,
                context_pack_id=pack.id,
                status=status,
                duration_ms=_elapsed_ms(start),
                input_chars=input_chars,
                estimated_input_tokens=_estimate_provider_input_tokens(
                    input_chars,
                    provider=verification_config.provider,
                ),
                findings_count=sum(1 for verification in pack_verifications.values() if verification.approved),
                cache_hit=cache_hit,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
                cache_key=next(iter(cache_keys.values())) if len(cache_keys) == 1 else None,
                error=error,
                estimated_saved_input_tokens=_estimated_verification_saved_tokens(
                    indexed_findings,
                    misses,
                    pack,
                    verification_config.provider,
                ),
                **_llm_run_usage_fields(usage),
            ),
        )

    verification_groups = _verification_groups_by_route(findings_by_pack_id, packs_by_id, config)

    if provider is None and config.jobs > 1 and len(verification_groups) > 1:
        results: list[tuple[dict[int, FindingVerification], LLMRun] | None] = [None] * len(verification_groups)
        progress.event(
            f"verify: {len(findings)} finding(s) across {len(verification_groups)} context pack(s), jobs={config.jobs}",
            force=True,
        )

        def submit_verification_group(
            executor: ThreadPoolExecutor,
            index: int,
        ) -> Future[tuple[dict[int, FindingVerification], LLMRun]]:
            pack_id, group, route_config, profile, route_reason = verification_groups[index]
            return executor.submit(
                verify_pack,
                pack_id,
                group,
                route_config,
                profile,
                route_reason,
            )

        completed = 0
        for index, result in _rolling_thread_pool_results(
            len(verification_groups),
            max_workers=config.jobs,
            submit=submit_verification_group,
        ):
            results[index] = result
            completed += 1
            pack_id = verification_groups[index][0]
            progress.event(
                _verify_progress_message(completed, len(verification_groups), pack_id, result[1]),
                key="verify",
                force=completed == len(verification_groups),
            )
        completed_results = [result for result in results if result is not None]
    else:
        progress.event(
            f"verify: {len(findings)} finding(s) across {len(verification_groups)} context pack(s), jobs=1",
            force=True,
        )
        completed_results = []
        for index, (pack_id, group, route_config, profile, route_reason) in enumerate(verification_groups, start=1):
            progress.event(
                f"verify {index}/{len(verification_groups)} starting ({_short_context_id(pack_id)})",
                key="verify:start",
                force=index == 1,
            )
            result = verify_pack(
                pack_id,
                group,
                route_config,
                profile,
                route_reason,
            )
            completed_results.append(result)
            progress.event(
                _verify_progress_message(index, len(verification_groups), pack_id, result[1]),
                key="verify",
                force=index == len(verification_groups),
            )

    for pack_verifications, run in completed_results:
        verifications_by_index.update(pack_verifications)
        runs.append(run)

    verifications = [verifications_by_index[index] for index in range(len(findings))]
    approved_findings = [
        verification.finding for verification in verifications if verification.approved and not verification.superseded
    ]

    return approved_findings, verifications, runs


def _route_key(config: LLMConfig, profile: str | None) -> _RouteKey:
    executable = None
    provider = str(config.provider)
    if provider == "codex_cli":
        executable = config.codex_path
    elif provider == "claude_code_cli":
        executable = config.claude_path
    api_route = None
    if provider not in {"codex_cli", "claude_code_cli", "fake"}:
        api = config.api
        api_route = json.dumps(
            {
                "protocol": str(api.protocol) if api.protocol is not None else None,
                "base_url": api.base_url,
                "base_url_env": api.base_url_env,
                "api_key_env": api.api_key_env,
                "allowed_hosts_env": api.allowed_hosts_env,
                "headers_from_env": dict(sorted(api.headers_from_env.items())),
                "api_version": api.api_version,
                "use_system_proxy": api.use_system_proxy,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    return (
        provider,
        config.model,
        str(config.effort) if config.effort is not None else None,
        config.timeout_seconds,
        executable,
        api_route,
    )


def _route_label(config: LLMConfig, profile: str | None) -> str:
    return f"provider={config.provider}, model={config.model or '<default>'}, profile={profile or '<default>'}"


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _rolling_thread_pool_results[T](
    total: int,
    *,
    max_workers: int,
    submit: Callable[[ThreadPoolExecutor, int], Future[T]],
) -> Iterator[tuple[int, T]]:
    """Yield completed tasks while keeping at most max_workers in flight.

    Workers recheck route admission after cache lookup and immediately before
    provider creation. Retryable failures can permit bounded overshoot while
    sibling calls are still in flight, but queued work cannot call a provider
    after the open circuit is observable.
    """
    next_index = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        in_flight: dict[Future[T], int] = {}
        while next_index < total and len(in_flight) < max_workers:
            in_flight[submit(executor, next_index)] = next_index
            next_index += 1

        while in_flight:
            completed_futures, _pending = wait(in_flight, return_when=FIRST_COMPLETED)
            completed_results: list[tuple[int, T]] = []
            for future in sorted(completed_futures, key=in_flight.__getitem__):
                index = in_flight.pop(future)
                result = future.result()
                if next_index < total:
                    in_flight[submit(executor, next_index)] = next_index
                    next_index += 1
                completed_results.append((index, result))
            yield from completed_results


def _effective_jobs(jobs: int, total: int) -> int:
    return min(jobs, total)


def _review_progress_message(
    review_depth: str,
    completed: int,
    total: int,
    pack: ContextPack,
    runs: list[LLMRun],
) -> str:
    status = runs[-1].status if runs else "skipped"
    findings_count = sum(run.findings_count for run in runs)
    return (
        f"review {review_depth} {completed}/{total} done "
        f"({_short_context_id(pack.id)}, {status}, {findings_count} finding(s))"
    )


def _verify_progress_message(completed: int, total: int, pack_id: str, run: LLMRun) -> str:
    return (
        f"verify {completed}/{total} done ({_short_context_id(pack_id)}, {run.status}, {run.findings_count} approved)"
    )


def _short_context_id(value: str, max_chars: int = 96) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 1]}..."


def _estimated_verification_saved_tokens(
    indexed_findings: list[tuple[int, Finding]],
    misses: list[tuple[int, Finding]],
    pack: ContextPack,
    provider: str,
) -> int:
    missed_indexes = {index for index, _ in misses}
    cached_findings = [finding for index, finding in indexed_findings if index not in missed_indexes]
    return (
        _estimate_provider_input_tokens(
            _verification_batch_input_chars(cached_findings, pack),
            provider=provider,
        )
        if cached_findings
        else 0
    )
