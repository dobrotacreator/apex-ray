import json
import posixpath
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import ValidationError

from apex_ray.llm_cache import (
    VERIFIER_PROMPT_VERSION,
    cache_for_config,
    review_cache_key,
    review_prompt_version,
    verification_cache_key,
)
from apex_ray.memory import pack_prompt_payload
from apex_ray.models import (
    ContextPack,
    Finding,
    FindingConfidence,
    FindingResponse,
    FindingVerification,
    LLMConfig,
    LLMProviderName,
    LLMRun,
    RuleMatch,
    VerificationBatchResponse,
    VerificationResponse,
)


class LLMProviderError(RuntimeError):
    pass


def classify_llm_provider_error(error: Exception | str) -> str:
    text = str(error).lower()
    if "timed out" in text or "timeout" in text:
        return "failed_timeout"
    if "usage limit" in text or "rate limit" in text or "quota" in text or "insufficient_quota" in text:
        return "failed_quota"
    return "failed_provider"


class LLMProvider(Protocol):
    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]: ...

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification: ...


class FakeLLMProvider:
    def __init__(
        self,
        findings: list[Finding] | None = None,
        verification_approvals: list[bool] | None = None,
    ) -> None:
        self.findings = findings or []
        self.verification_approvals = verification_approvals or []
        self.reviewed_pack_ids: list[str] = []
        self.verified_batch_pack_ids: list[str] = []
        self.verified_batches: list[list[str]] = []
        self.verified_finding_titles: list[str] = []

    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
        self.reviewed_pack_ids.append(pack.id)
        return [finding.model_copy(update={"context_pack_id": pack.id}) for finding in self.findings]

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
        self.verified_finding_titles.append(finding.title)
        approval_index = len(self.verified_finding_titles) - 1
        approved = (
            self.verification_approvals[approval_index] if approval_index < len(self.verification_approvals) else True
        )
        return FindingVerification(
            finding=finding,
            approved=approved,
            confidence=FindingConfidence.HIGH,
            reason="Fake verifier approved the finding." if approved else "Fake verifier rejected the finding.",
        )

    def verify_findings(self, findings: list[Finding], pack: ContextPack, repo_root: Path) -> list[FindingVerification]:
        self.verified_batch_pack_ids.append(pack.id)
        self.verified_batches.append([finding.title for finding in findings])
        return [self.verify_finding(finding, pack, repo_root) for finding in findings]


class CodexCLIProvider:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
        codex_path = _resolve_codex_path(self.config.codex_path, repo_root)
        prompt = (
            build_shallow_review_prompt(pack) if self.config.review_depth == "shallow" else build_review_prompt(pack)
        )

        with tempfile.TemporaryDirectory(prefix="apex-ray-codex-") as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "finding_schema.json"
            output_path = tmp_path / "findings.json"
            schema_path.write_text(
                json.dumps(finding_response_schema(), indent=2),
                encoding="utf-8",
            )

            command = build_codex_command(
                codex_path=codex_path,
                schema_path=schema_path,
                output_path=output_path,
                model=self.config.model,
            )
            proc = subprocess.run(
                command,
                cwd=tmp_path,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )

            if proc.returncode != 0:
                details = proc.stderr.strip() or proc.stdout.strip()
                raise LLMProviderError(f"Codex CLI failed with exit {proc.returncode}: {details}")
            if not output_path.exists():
                raise LLMProviderError("Codex CLI did not write an output message.")

            return parse_finding_response(output_path.read_text(encoding="utf-8"), pack.id).findings

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
        return self.verify_findings([finding], pack, repo_root)[0]

    def verify_findings(self, findings: list[Finding], pack: ContextPack, repo_root: Path) -> list[FindingVerification]:
        if not findings:
            return []

        codex_path = _resolve_codex_path(self.config.codex_path, repo_root)
        prompt = build_verifier_batch_prompt(findings, pack)

        with tempfile.TemporaryDirectory(prefix="apex-ray-codex-verify-") as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "verification_schema.json"
            output_path = tmp_path / "verification.json"
            schema_path.write_text(
                json.dumps(verification_batch_response_schema(), indent=2),
                encoding="utf-8",
            )

            command = build_codex_command(
                codex_path=codex_path,
                schema_path=schema_path,
                output_path=output_path,
                model=self.config.model,
            )
            proc = subprocess.run(
                command,
                cwd=tmp_path,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )

            if proc.returncode != 0:
                details = proc.stderr.strip() or proc.stdout.strip()
                raise LLMProviderError(f"Codex CLI verifier failed with exit {proc.returncode}: {details}")
            if not output_path.exists():
                raise LLMProviderError("Codex CLI verifier did not write an output message.")

            return parse_verification_batch_response(output_path.read_text(encoding="utf-8"), findings)


class ClaudeCodeCLIProvider:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
        claude_path = _resolve_claude_path(self.config.claude_path, repo_root)
        prompt = (
            build_shallow_review_prompt(pack) if self.config.review_depth == "shallow" else build_review_prompt(pack)
        )

        with tempfile.TemporaryDirectory(prefix="apex-ray-claude-") as tmp:
            tmp_path = Path(tmp)
            command = build_claude_command(
                claude_path=claude_path,
                schema=finding_response_schema(),
                model=self.config.model,
            )
            proc = subprocess.run(
                command,
                cwd=tmp_path,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )

            if proc.returncode != 0:
                details = proc.stderr.strip() or proc.stdout.strip()
                raise LLMProviderError(f"Claude Code CLI failed with exit {proc.returncode}: {details}")
            response_text = _claude_result_text(proc.stdout)
            return parse_finding_response(response_text, pack.id).findings

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
        return self.verify_findings([finding], pack, repo_root)[0]

    def verify_findings(self, findings: list[Finding], pack: ContextPack, repo_root: Path) -> list[FindingVerification]:
        if not findings:
            return []

        claude_path = _resolve_claude_path(self.config.claude_path, repo_root)
        prompt = build_verifier_batch_prompt(findings, pack)

        with tempfile.TemporaryDirectory(prefix="apex-ray-claude-verify-") as tmp:
            tmp_path = Path(tmp)
            command = build_claude_command(
                claude_path=claude_path,
                schema=verification_batch_response_schema(),
                model=self.config.model,
            )
            proc = subprocess.run(
                command,
                cwd=tmp_path,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )

            if proc.returncode != 0:
                details = proc.stderr.strip() or proc.stdout.strip()
                raise LLMProviderError(f"Claude Code CLI verifier failed with exit {proc.returncode}: {details}")
            response_text = _claude_result_text(proc.stdout)
            return parse_verification_batch_response(response_text, findings)


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


def filter_findings_for_context_pack(findings: list[Finding], pack: ContextPack) -> list[Finding]:
    context_files = _context_files(pack)
    filtered: list[Finding] = []
    for finding in findings:
        normalized_file = _normalize_context_file(finding.file)
        if finding.context_pack_id and finding.context_pack_id != pack.id:
            continue
        if normalized_file not in context_files:
            continue
        filtered.append(finding.model_copy(update={"context_pack_id": pack.id, "file": normalized_file}))
    return filtered


def _context_files(pack: ContextPack) -> set[str]:
    files = {_normalize_context_file(pack.file)}
    files.update(_normalize_context_file(snippet.file) for snippet in pack.changed_snippets)
    files.update(_normalize_context_file(snippet.file) for snippet in pack.reference_snippets)
    files.update(_normalize_context_file(snippet.file) for snippet in pack.callee_snippets)
    files.update(_normalize_context_file(snippet.file) for snippet in pack.contract_snippets)
    files.update(_normalize_context_file(snippet.file) for snippet in pack.metadata_snippets)
    files.update(_normalize_context_file(snippet.file) for snippet in pack.related_test_snippets)
    files.update(_normalize_context_file(reference.file) for reference in pack.references)
    files.update(_normalize_context_file(callee.file) for callee in pack.callees)
    files.update(_normalize_context_file(reference.file) for reference in pack.contracts)
    files.update(_normalize_context_file(reference.file) for reference in pack.metadata)
    files.update(_normalize_context_file(path) for path in pack.related_tests)
    return files


def _normalize_context_file(path: str) -> str:
    return posixpath.normpath(path.strip().replace("\\", "/")).removeprefix("./")


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    deduped: dict[tuple[str, str, int | None, str], Finding] = {}
    for finding in findings:
        key = (
            _normalize_for_dedupe(finding.title),
            _normalize_context_file(finding.file),
            finding.line,
            _normalize_for_dedupe(finding.failure_mode),
        )
        current = deduped.get(key)
        if current is None or _finding_rank(finding) > _finding_rank(current):
            deduped[key] = finding
    return list(deduped.values())


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


def _verification_groups_by_route(
    findings_by_pack_id: dict[str, list[tuple[int, Finding]]],
    packs_by_id: dict[str, ContextPack],
    config: LLMConfig,
) -> list[tuple[str, list[tuple[int, Finding]], LLMConfig, str | None, str]]:
    groups: list[tuple[str, list[tuple[int, Finding]], LLMConfig, str | None, str]] = []
    for pack_id, indexed_findings in findings_by_pack_id.items():
        pack = packs_by_id[pack_id]
        route_groups: dict[
            tuple[str, str | None, str | None, str | None, str | None, int, str | None, str],
            tuple[LLMConfig, str | None, str, list[tuple[int, Finding]]],
        ] = {}
        for index, finding in indexed_findings:
            route_config, profile, route_reason = verification_config_for_finding(config, finding, pack)
            route_key = _verification_route_key(route_config, profile, route_reason)
            if route_key not in route_groups:
                route_groups[route_key] = (route_config, profile, route_reason, [])
            route_groups[route_key][3].append((index, finding))
        groups.extend(
            (pack_id, route_findings, route_config, profile, route_reason)
            for route_config, profile, route_reason, route_findings in route_groups.values()
        )
    return groups


def _verification_route_key(
    config: LLMConfig,
    profile: str | None,
    route_reason: str,
) -> tuple[str, str | None, str | None, str | None, str | None, int, str | None, str]:
    return (
        str(config.provider),
        config.model,
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
            resolved, profile, reason = _config_for_profile_or_model(
                config,
                config.routing.review_profile,
                f"profile:{config.routing.review_profile}",
            )
            return resolved, profile, f"shallow:{reason}"
        return config.model_copy(deep=True), None, "shallow:default"

    rule_override = _rule_model_override(pack.rule_matches, field="model")
    if rule_override:
        return _config_for_profile_or_model(config, rule_override, f"rule:{rule_override}")

    routing = config.routing
    escalate_reason = _routing_condition_reason(routing.escalate_review_when, pack)
    if routing.escalated_review_profile and escalate_reason:
        return _config_for_profile_or_model(
            config,
            routing.escalated_review_profile,
            f"escalated:{routing.escalated_review_profile}:{escalate_reason}",
        )
    if routing.review_profile:
        return _config_for_profile_or_model(config, routing.review_profile, f"profile:{routing.review_profile}")
    return config.model_copy(deep=True), None, "default"


def _fallback_review_config_after_error(
    config: LLMConfig,
    failed_profile: str | None,
    status: str,
) -> tuple[LLMConfig, str | None, str] | None:
    if status != "failed_quota":
        return None
    fallback_profile = config.routing.escalated_review_profile
    if not fallback_profile or fallback_profile == failed_profile:
        return None
    return _config_for_profile_or_model(
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
    rule_override = _rule_model_override(pack.rule_matches, field="verify")
    if rule_override:
        return _config_for_profile_or_model(config, rule_override, f"rule-verify:{rule_override}")

    routing = config.routing
    escalate_reason = next(
        (
            reason
            for finding in findings
            if (reason := _routing_condition_reason(routing.escalate_verify_when, pack, finding))
        ),
        None,
    )
    if routing.escalated_verify_profile and escalate_reason:
        return _config_for_profile_or_model(
            config,
            routing.escalated_verify_profile,
            f"escalated-verify:{routing.escalated_verify_profile}:{escalate_reason}",
        )
    if routing.verify_profile:
        return _config_for_profile_or_model(config, routing.verify_profile, f"profile:{routing.verify_profile}")
    return config.model_copy(deep=True), None, "default"


def _config_for_profile_or_model(
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


def _rule_model_override(rule_matches: list[RuleMatch], field: str) -> str | None:
    ranked = sorted(
        [(index, rule) for index, rule in enumerate(rule_matches) if getattr(rule, field)],
        key=lambda item: (_severity_rank(str(item[1].severity)), item[1].mode == "strict", -item[0]),
        reverse=True,
    )
    if not ranked:
        return None
    value = getattr(ranked[0][1], field)
    return str(value) if value else None


def _routing_condition_matches(condition: object, pack: ContextPack, finding: Finding | None = None) -> bool:
    return _routing_condition_reason(condition, pack, finding) is not None


def _routing_condition_reason(condition: object, pack: ContextPack, finding: Finding | None = None) -> str | None:
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


def _review_input_chars(pack: ContextPack, *, review_depth: Literal["deep", "shallow"] = "deep") -> int:
    if review_depth == "shallow":
        return len(build_shallow_review_prompt(pack))
    return len(build_review_prompt(pack))


def estimate_review_input_tokens(
    pack: ContextPack,
    *,
    review_depth: Literal["deep", "shallow"] = "deep",
) -> int:
    return _estimate_tokens(_review_input_chars(pack, review_depth=review_depth))


def _verification_input_chars(finding: Finding, pack: ContextPack) -> int:
    return len(build_verifier_prompt(finding, pack))


def _verification_batch_input_chars(findings: list[Finding], pack: ContextPack) -> int:
    return len(build_verifier_batch_prompt(findings, pack)) if findings else 0


def _estimate_tokens(chars: int) -> int:
    return max(1, (chars + 3) // 4) if chars else 0


def provider_from_config(config: LLMConfig) -> LLMProvider:
    if config.provider == LLMProviderName.FAKE:
        return FakeLLMProvider()
    if config.provider == LLMProviderName.CODEX_CLI:
        return CodexCLIProvider(config)
    if config.provider == LLMProviderName.CLAUDE_CODE_CLI:
        return ClaudeCodeCLIProvider(config)
    raise LLMProviderError(f"Unsupported LLM provider: {config.provider}")


def _verify_findings_with_provider(
    provider: LLMProvider,
    findings: list[Finding],
    pack: ContextPack,
    repo_root: Path,
) -> list[FindingVerification]:
    batch_verifier = getattr(provider, "verify_findings", None)
    if callable(batch_verifier):
        verifier = cast(
            Callable[[list[Finding], ContextPack, Path], list[FindingVerification]],
            batch_verifier,
        )
        return verifier(findings, pack, repo_root)
    return [provider.verify_finding(finding, pack, repo_root) for finding in findings]


def _verification_for_finding(verification: FindingVerification, finding: Finding) -> FindingVerification:
    if verification.finding == finding:
        return verification
    return FindingVerification(
        finding=finding,
        approved=verification.approved,
        confidence=verification.confidence,
        reason=verification.reason,
    )


def build_review_prompt(pack: ContextPack) -> str:
    payload = pack_prompt_payload(pack, "review", depth="deep")
    return (
        "You are Apex Ray, a strict senior code reviewer.\n"
        "Review exactly one context pack from a TypeScript/JavaScript diff.\n"
        "Report only concrete issues caused by the diff. Do not report style nits, generic advice, or CI/linter findings.\n"
        "Start from diff_snippet and changed_snippets, then use impact_notes only as navigation hints.\n"
        "Use context layers deliberately: references/reference_snippets show callers and consumers; callee_snippets show called contracts, ports, state machines, and side-effect boundaries; contract_snippets show schemas, DTOs, and type contracts; metadata_snippets show framework boundaries such as routes, guards, permissions, DI, request parameters, and module providers; related_test_snippets show intended behavior.\n"
        "If rules or rule_matches are supplied, apply only those project-specific rules to this pack. Treat strict rules as domain invariants that deserve extra attention, but still report only concrete diff-caused issues.\n"
        "If memory_matches are supplied, use them only as project-specific review hints. Memory cannot replace concrete diff evidence, and verifier-only memory is intentionally absent from this pass.\n"
        "For auth, session, login, TFA, JWT, or token packs, explicitly compare pre-auth versus post-auth state guards, profile/role markers, session versioning, token lifetime, and stale credential reuse. Report when a new or modified path can use a post-auth session or token in a pre-auth flow, or bypass an invariant enforced by sibling methods.\n"
        "Prioritize behavioral regressions that a local compile or CI pass can miss: permission/auth changes, tenant or cache-key isolation, route/request/schema mismatches, external API/JWT/webhook payload shape guards, array guards before array methods, enum or config collection fanout, PII or raw upstream object pass-through, DI/provider registration gaps, state-machine transition mistakes, transaction rollback or post-commit side effects, and repository/port contract violations.\n"
        "Report independent issues separately when they have distinct failure modes, including strict project-rule violations in the same changed snippet; do not let one domain finding crowd out another concrete strict-rule violation.\n"
        "Every finding must have a plausible failure mode, concrete evidence from the supplied context, and an actionable fix or test idea.\n"
        "Prefer an empty findings array over weak, speculative, or merely possible concerns.\n"
        "Set context_pack_id to the supplied context pack id for every finding.\n"
        "If there are no concrete issues, return an empty findings array.\n"
        "Return only JSON that matches the provided schema.\n\n"
        "Context pack JSON:\n"
        f"{json.dumps(payload, indent=2)}\n"
    )


def build_shallow_review_prompt(pack: ContextPack) -> str:
    payload = pack_prompt_payload(pack, "review", depth="shallow")
    return (
        "You are Apex Ray's fast shallow code-review pass.\n"
        "Review exactly one compact TypeScript/JavaScript context pack from a diff.\n"
        "Use only the supplied diff_snippet, changed_snippets, risk_signals, rules, and memory hints.\n"
        "This pass optimizes breadth and recall on large PRs; report only concrete diff-caused issues visible in this compact context.\n"
        "Do not infer from missing callers, missing schemas, or absent files. Do not report style nits, generic advice, or CI/linter findings.\n"
        "For strict project rules and high-risk signals, look for direct violations in the changed lines and snippets.\n"
        "Every finding must include a plausible failure mode, concrete evidence, and an actionable fix or test idea.\n"
        "Prefer an empty findings array over weak or speculative concerns.\n"
        "Set context_pack_id to the supplied context pack id for every finding.\n"
        "Return only JSON that matches the provided schema.\n\n"
        "Compact context pack JSON:\n"
        f"{json.dumps(payload, indent=2)}\n"
    )


def build_verifier_prompt(finding: Finding, pack: ContextPack) -> str:
    finding_payload = finding.model_dump(mode="json")
    pack_payload = pack_prompt_payload(pack, "verify")
    return (
        "You are Apex Ray's verification pass for AI code review findings.\n"
        "Decide whether the finding should be published.\n"
        "Approve only if the issue is caused by the diff, has concrete evidence in the context pack, is actionable, and is not a style nit or generic advice.\n"
        "Approve concrete diff-caused violations of supplied strict project rules when the changed code clearly violates the rule; a strict safety, boundary, or project-policy violation is actionable even when the immediate failure mode is policy drift or future boundary risk rather than a current runtime exception.\n"
        "Still reject generic style preferences that are not tied to a supplied strict rule or concrete behavioral risk.\n"
        "Treat impact_notes as navigation hints only; reject if the concrete diff/snippet evidence does not support the finding.\n"
        "Reject if context_pack_id differs from the supplied context pack id, or if finding.file is not present in any supplied context layer: changed snippets, references, callees, contracts, metadata, or related tests.\n"
        "Use context layers deliberately: references show consumers, callees show called contracts and side-effect boundaries, contracts show schemas/DTO/type requirements, metadata shows framework/route/permission/DI boundaries, and related tests show intended behavior.\n"
        "If rules or rule_matches are supplied, use them as project-specific review criteria. A strict rule can establish review significance when the diff and snippets concretely show a violation, but it cannot replace missing evidence that the changed code exists or that an external behavior assumption is true.\n"
        "If memory_matches are supplied, use them as project-specific calibration, including known false-positive and severity-calibration entries. Reject findings that match known false positives unless the diff evidence materially differs.\n"
        "When consumers, contracts, metadata, or related tests are supplied, approve only when the failure mode is connected to at least one concrete supplied layer and the changed code can realistically trigger it.\n"
        "Reject the finding if it is speculative, contradicted by context, already handled by the changed code, lacks a plausible failure mode, or depends on missing assumptions.\n"
        "Return only JSON that matches the provided schema.\n\n"
        "Candidate finding JSON:\n"
        f"{json.dumps(finding_payload, indent=2)}\n\n"
        "Context pack JSON:\n"
        f"{json.dumps(pack_payload, indent=2)}\n"
    )


def build_verifier_batch_prompt(findings: list[Finding], pack: ContextPack) -> str:
    findings_payload = [finding.model_dump(mode="json") for finding in findings]
    pack_payload = pack_prompt_payload(pack, "verify")
    return (
        "You are Apex Ray's batched verification pass for AI code review findings.\n"
        "Decide whether each candidate finding should be published.\n"
        "Return one decision per input finding in decisions[], using finding_index to point at the zero-based index in candidate_findings.\n"
        "Approve only if the issue is caused by the diff, has concrete evidence in the context pack, is actionable, and is not a style nit or generic advice.\n"
        "Approve concrete diff-caused violations of supplied strict project rules when the changed code clearly violates the rule; a strict safety, boundary, or project-policy violation is actionable even when the immediate failure mode is policy drift or future boundary risk rather than a current runtime exception.\n"
        "Still reject generic style preferences that are not tied to a supplied strict rule or concrete behavioral risk.\n"
        "Treat impact_notes as navigation hints only; reject if the concrete diff/snippet evidence does not support the finding.\n"
        "Reject if context_pack_id differs from the supplied context pack id, or if finding.file is not present in any supplied context layer: changed snippets, references, callees, contracts, metadata, or related tests.\n"
        "Use context layers deliberately: references show consumers, callees show called contracts and side-effect boundaries, contracts show schemas/DTO/type requirements, metadata shows framework/route/permission/DI boundaries, and related tests show intended behavior.\n"
        "If rules or rule_matches are supplied, use them as project-specific review criteria. A strict rule can establish review significance when the diff and snippets concretely show a violation, but it cannot replace missing evidence that the changed code exists or that an external behavior assumption is true.\n"
        "If memory_matches are supplied, use them as project-specific calibration, including known false-positive and severity-calibration entries. Reject findings that match known false positives unless the diff evidence materially differs.\n"
        "When consumers, contracts, metadata, or related tests are supplied, approve only when the failure mode is connected to at least one concrete supplied layer and the changed code can realistically trigger it.\n"
        "Reject a finding if it is speculative, contradicted by context, already handled by the changed code, lacks a plausible failure mode, or depends on missing assumptions.\n"
        "Evaluate each candidate independently; approving one finding must not make a weaker sibling finding pass.\n"
        "Return only JSON that matches the provided schema.\n\n"
        "Candidate findings JSON:\n"
        f"{json.dumps({'candidate_findings': findings_payload}, indent=2)}\n\n"
        "Context pack JSON:\n"
        f"{json.dumps(pack_payload, indent=2)}\n"
    )


def build_codex_command(
    codex_path: str,
    schema_path: Path,
    output_path: Path,
    model: str | None = None,
) -> list[str]:
    command = [
        codex_path,
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
    ]
    if model:
        command.extend(["--model", model])
    command.append("-")
    return command


def build_claude_command(
    claude_path: str,
    schema: dict[str, object],
    model: str | None = None,
) -> list[str]:
    command = [
        claude_path,
        "--print",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--tools",
        "",
    ]
    if model:
        command.extend(["--model", model])
    return command


def finding_response_schema() -> dict[str, object]:
    finding_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "file": {"type": "string"},
            "line": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "failure_mode": {"type": "string"},
            "evidence": {"type": "string"},
            "suggested_fix": {"type": "string"},
            "suggested_test": {"type": "string"},
            "context_pack_id": {"type": "string"},
        },
        "required": [
            "title",
            "severity",
            "confidence",
            "file",
            "line",
            "failure_mode",
            "evidence",
            "suggested_fix",
            "suggested_test",
            "context_pack_id",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "findings": {
                "type": "array",
                "items": finding_schema,
            }
        },
        "required": ["findings"],
    }


def verification_response_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "approved": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["approved", "confidence", "reason"],
    }


def verification_batch_response_schema() -> dict[str, object]:
    decision_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "finding_index": {"type": "integer", "minimum": 0},
            "approved": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["finding_index", "approved", "confidence", "reason"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decisions": {
                "type": "array",
                "items": decision_schema,
            }
        },
        "required": ["decisions"],
    }


def parse_finding_response(text: str, context_pack_id: str) -> FindingResponse:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        raw = json.loads(_extract_json_object(text))

    try:
        response = FindingResponse.model_validate(raw)
    except ValidationError as exc:
        raise LLMProviderError(f"Invalid finding response: {exc}") from exc

    return FindingResponse(
        findings=[
            finding.model_copy(update={"context_pack_id": finding.context_pack_id or context_pack_id})
            for finding in response.findings
        ]
    )


def parse_verification_response(text: str, finding: Finding) -> FindingVerification:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        raw = json.loads(_extract_json_object(text))

    try:
        response = VerificationResponse.model_validate(raw)
    except ValidationError as exc:
        raise LLMProviderError(f"Invalid verification response: {exc}") from exc

    return FindingVerification(
        finding=finding,
        approved=response.approved,
        confidence=response.confidence,
        reason=response.reason,
    )


def parse_verification_batch_response(text: str, findings: list[Finding]) -> list[FindingVerification]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        raw = json.loads(_extract_json_object(text))

    try:
        response = VerificationBatchResponse.model_validate(raw)
    except ValidationError as exc:
        raise LLMProviderError(f"Invalid verification response: {exc}") from exc

    decisions_by_index = {}
    for decision in response.decisions:
        if decision.finding_index >= len(findings):
            raise LLMProviderError(f"Verifier returned out-of-range finding_index: {decision.finding_index}")
        if decision.finding_index in decisions_by_index:
            raise LLMProviderError(f"Verifier returned duplicate finding_index: {decision.finding_index}")
        decisions_by_index[decision.finding_index] = decision

    expected_indexes = set(range(len(findings)))
    missing_indexes = expected_indexes - set(decisions_by_index)
    if missing_indexes:
        missing = ", ".join(str(index) for index in sorted(missing_indexes))
        raise LLMProviderError(f"Verifier omitted decisions for finding indexes: {missing}")

    return [
        FindingVerification(
            finding=finding,
            approved=decisions_by_index[index].approved,
            confidence=decisions_by_index[index].confidence,
            reason=decisions_by_index[index].reason,
        )
        for index, finding in enumerate(findings)
    ]


def _resolve_codex_path(codex_path: str, repo_root: Path | None = None) -> str:
    return _resolve_cli_path(codex_path, repo_root, display_name="Codex CLI")


def _resolve_claude_path(claude_path: str, repo_root: Path | None = None) -> str:
    return _resolve_cli_path(claude_path, repo_root, display_name="Claude Code CLI")


def _resolve_cli_path(cli_path: str, repo_root: Path | None, *, display_name: str) -> str:
    configured_path = Path(cli_path).expanduser()
    if "/" in cli_path or "\\" in cli_path or configured_path.is_absolute():
        if not configured_path.is_absolute() and repo_root is not None:
            configured_path = repo_root / configured_path
        resolved_path = configured_path.resolve()
        if not resolved_path.exists():
            raise LLMProviderError(f"{display_name} not found: {cli_path}")
        return str(resolved_path)

    resolved = shutil.which(cli_path)
    if not resolved:
        raise LLMProviderError(f"{display_name} not found: {cli_path}")
    return resolved


def _claude_result_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise LLMProviderError("Claude Code CLI did not write an output message.")
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(raw, dict):
        if raw.get("is_error"):
            message = raw.get("result") or raw.get("error") or stripped
            raise LLMProviderError(f"Claude Code CLI returned an error: {message}")
        result = raw.get("result")
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return json.dumps(result)
        if "findings" in raw or "decisions" in raw:
            return json.dumps(raw)
    return stripped


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMProviderError("LLM response did not contain a JSON object.")
    return text[start : end + 1]


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _normalize_for_dedupe(value: str) -> str:
    return " ".join(value.lower().strip().split())


def _finding_rank(finding: Finding) -> tuple[int, int]:
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    return (
        severity_rank.get(str(finding.severity), 0),
        confidence_rank.get(str(finding.confidence), 0),
    )
