import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from apex_ray.llm import (
    APILLMProvider,
    ClaudeCodeCLIProvider,
    CodexCLIProvider,
    FakeLLMProvider,
    LLMProviderError,
    build_claude_command,
    build_codex_command,
    build_review_prompt,
    build_shallow_review_prompt,
    build_verifier_batch_prompt,
    build_verifier_prompt,
    dedupe_findings,
    estimate_provider_input_tokens,
    filter_findings_for_context_pack,
    finding_response_schema,
    parse_finding_response,
    parse_verification_batch_response,
    parse_verification_response,
    provider_from_config,
    review_cache_key,
    review_config_for_pack,
    review_context_packs,
    verification_batch_response_schema,
    verification_config_for_finding,
    verification_config_for_findings,
    verification_response_schema,
    verify_findings,
)
from apex_ray.llm.cache import (
    REVIEW_PROMPT_VERSION,
    REVIEW_SHALLOW_PROMPT_VERSION,
    VERIFIER_PROMPT_VERSION,
    LLMCache,
    verification_cache_key,
)
from apex_ray.llm.cli import claude_result_text
from apex_ray.llm.prompts import render_review_prompt
from apex_ray.llm.review import LLMRouteCircuitBreaker
from apex_ray.llm.usage import parse_claude_usage_from_json, parse_codex_usage_from_jsonl
from apex_ray.models import (
    AnalyzerReference,
    AnalyzerSymbol,
    CodeSnippet,
    ContextPack,
    FileKind,
    Finding,
    FindingConfidence,
    FindingSeverity,
    FindingVerification,
    LLMAPIConfig,
    LLMAPIProtocol,
    LLMConfig,
    LLMProfile,
    LLMProviderName,
    LLMReasoningEffort,
    LLMReviewResult,
    LLMRoutingCondition,
    LLMRoutingConfig,
    LLMStructuredOutput,
    LLMUsage,
    LLMVerificationResult,
    MemoryMatch,
    ReviewerConfig,
    RiskSeverity,
    RiskSignal,
    RuleMatch,
)


def make_pack() -> ContextPack:
    return ContextPack(
        id="src/cart.ts#calculateTotal:1",
        file="src/cart.ts",
        changed_lines=[(5, 6)],
        impact_notes=[
            "Changed symbols: exported function calculateTotal lines 5-8.",
            "Reference impact: 2 total references (call=2); 2 non-import usage references across 2 files.",
        ],
        symbol=AnalyzerSymbol(
            name="calculateTotal",
            kind="function",
            startLine=5,
            endLine=8,
            exported=True,
            signature="(items: CartItem[]): number",
        ),
    )


def make_python_pack() -> ContextPack:
    return ContextPack(
        id="src/routes.py#create_resource:1",
        file="src/routes.py",
        changed_lines=[(12, 14)],
        impact_notes=[
            "Changed symbols: exported function create_resource lines 12-18.",
            "Reference impact: 1 total reference (call=1); 1 non-import usage reference across 1 file.",
        ],
        symbol=AnalyzerSymbol(
            name="create_resource",
            kind="function",
            startLine=12,
            endLine=18,
            exported=True,
            signature="def create_resource(payload: ResourceCreate) -> ResourceRead",
        ),
    )


def make_unknown_language_pack(file: str = "src/types.pyi") -> ContextPack:
    return ContextPack(
        id=f"{file}#changed:1",
        file=file,
        changed_lines=[(3, 4)],
        impact_notes=["Changed symbols: changed lines 3-4."],
        symbol=AnalyzerSymbol(
            name="changed",
            kind="function",
            startLine=3,
            endLine=4,
            exported=True,
            signature="changed()",
        ),
    )


def test_fake_provider_returns_findings_with_pack_id() -> None:
    finding = Finding(
        title="Potential incorrect total",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=6,
        failure_mode="Totals can be wrong.",
        evidence="The changed symbol is calculateTotal.",
        suggested_fix="Preserve the existing multiplication.",
        suggested_test="Add a multi-item cart test.",
    )
    provider = FakeLLMProvider([finding])
    config = LLMConfig(provider=LLMProviderName.FAKE)

    findings, runs = review_context_packs([make_pack()], config, Path("."), provider=provider)

    assert provider.reviewed_pack_ids == ["src/cart.ts#calculateTotal:1"]
    assert findings[0].context_pack_id == "src/cart.ts#calculateTotal:1"
    assert findings[0].reviewer_context_pack_ids == {
        "general": ["src/cart.ts#calculateTotal:1"],
    }
    assert runs[0].status == "ok"
    assert runs[0].findings_count == 1
    assert runs[0].cache_key is None
    assert runs[0].prompt_version == REVIEW_PROMPT_VERSION


def test_review_context_packs_filters_findings_outside_context() -> None:
    valid = Finding(
        title="Potential incorrect total",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=6,
        failure_mode="Totals can be wrong.",
        evidence="The changed symbol is calculateTotal.",
        suggested_fix="Preserve the existing multiplication.",
        suggested_test="Add a multi-item cart test.",
    )
    invalid = valid.model_copy(update={"title": "Unrelated file issue", "file": "src/unrelated.ts"})
    provider = FakeLLMProvider([valid, invalid])
    config = LLMConfig(provider=LLMProviderName.FAKE)

    findings, runs = review_context_packs([make_pack()], config, Path("."), provider=provider)

    assert findings == [
        valid.model_copy(
            update={
                "context_pack_id": "src/cart.ts#calculateTotal:1",
                "reviewer_context_pack_ids": {
                    "general": ["src/cart.ts#calculateTotal:1"],
                },
            }
        )
    ]
    assert runs[0].findings_count == 1


def test_review_context_packs_records_routed_model_profile() -> None:
    provider = FakeLLMProvider([])
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        profiles={"cheap": LLMProfile(model="codex-cheap")},
        routing=LLMRoutingConfig(review_profile="cheap"),
    )

    _, runs = review_context_packs([make_pack()], config, Path("."), provider=provider)

    assert runs[0].model == "codex-cheap"
    assert runs[0].profile == "cheap"
    assert runs[0].route_reason == "profile:cheap"
    assert runs[0].input_chars > 0
    assert runs[0].estimated_input_tokens > 0


def test_llm_run_estimates_use_effective_provider_calibration() -> None:
    provider = FakeLLMProvider([])
    config = LLMConfig(
        provider=LLMProviderName.CODEX_CLI,
        model="review-model",
        cache_enabled=False,
    )

    _, review_runs = review_context_packs([make_pack()], config, Path("."), provider=provider)

    assert review_runs[0].estimated_input_tokens == estimate_provider_input_tokens(
        review_runs[0].input_chars,
        provider=LLMProviderName.CODEX_CLI,
    )


def test_review_context_packs_records_provider_failure_per_pack() -> None:
    class FailingProvider:
        def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
            raise LLMProviderError("ERROR: You've hit your usage limit for GPT-5.3-Codex-Spark.")

        def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
            raise AssertionError("not used")

    config = LLMConfig(provider=LLMProviderName.FAKE)

    findings, runs = review_context_packs([make_pack()], config, Path("."), provider=FailingProvider())

    assert findings == []
    assert runs[0].status == "failed_quota"
    assert "usage limit" in (runs[0].error or "")


def test_review_context_packs_opens_circuit_after_terminal_parallel_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingProvider:
        calls = 0

        def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
            self.calls += 1
            raise LLMProviderError("Invalid API key.", category="auth")

        def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
            raise AssertionError("not used")

    failing = FailingProvider()
    monkeypatch.setattr("apex_ray.llm.review.provider_from_config", lambda _config: failing)
    packs = [make_pack().model_copy(update={"id": f"src/cart.ts#pack-{index}"}) for index in range(6)]
    config = LLMConfig(provider=LLMProviderName.FAKE, jobs=2, max_consecutive_provider_failures=3)

    findings, runs = review_context_packs(packs, config, Path("."))

    assert findings == []
    assert failing.calls == 2
    assert [run.status for run in runs[:2]] == ["failed_auth", "failed_auth"]
    assert [run.status for run in runs[2:]] == ["skipped_circuit_open"] * 4
    assert all("circuit" in (run.error or "").lower() for run in runs[2:])


def test_review_context_packs_opens_circuit_only_for_failed_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HealthyProvider:
        calls = 0

        def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
            self.calls += 1
            return []

        def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
            raise AssertionError("not used")

    healthy = HealthyProvider()
    factory_calls = {"broken": 0, "healthy": 0}

    def provider_factory(config: LLMConfig) -> HealthyProvider:
        if config.model == "broken":
            factory_calls["broken"] += 1
            raise LLMProviderError("Invalid API key.", category="auth")
        factory_calls["healthy"] += 1
        return healthy

    monkeypatch.setattr("apex_ray.llm.review.provider_from_config", provider_factory)
    normal = make_pack()
    risky = normal.model_copy(
        update={
            "id": "src/cart.ts#risky-1",
            "risk_signals": [
                RiskSignal(
                    kind="auth",
                    severity=RiskSeverity.HIGH,
                    reason="Auth changed.",
                    file=normal.file,
                    line=5,
                )
            ],
        }
    )
    risky_again = risky.model_copy(update={"id": "src/cart.ts#risky-2"})
    normal_again = normal.model_copy(update={"id": "src/cart.ts#normal-2"})
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        model="healthy",
        cache_enabled=False,
        profiles={
            "broken": LLMProfile(
                provider=LLMProviderName.OPENAI_API,
                model="broken",
            )
        },
        routing=LLMRoutingConfig(
            escalated_review_profile="broken",
            escalate_review_when=LLMRoutingCondition(risk=["auth"]),
        ),
    )

    findings, runs = review_context_packs(
        [risky, normal, risky_again, normal_again],
        config,
        Path("."),
    )

    assert findings == []
    assert factory_calls == {"broken": 1, "healthy": 2}
    assert healthy.calls == 2
    assert [run.status for run in runs] == [
        "failed_auth",
        "ok",
        "skipped_circuit_open",
        "ok",
    ]
    assert [run.model for run in runs] == ["broken", "healthy", "broken", "healthy"]


def test_profile_provider_change_does_not_inherit_another_api_credentials() -> None:
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="broad-model",
        api=LLMAPIConfig(
            api_key_env="BROAD_OPENAI_API_KEY",
            headers_from_env={"X-Tenant": "BROAD_TENANT"},
        ),
        profiles={
            "strong": LLMProfile(
                provider=LLMProviderName.ANTHROPIC_API,
                model="strong-model",
            )
        },
        routing=LLMRoutingConfig(review_profile="strong"),
    )

    resolved, profile, _reason = review_config_for_pack(config, make_pack())

    assert profile == "strong"
    assert resolved.provider == LLMProviderName.ANTHROPIC_API
    assert resolved.api == LLMAPIConfig()


@pytest.mark.parametrize(
    "base_url",
    [
        pytest.param(
            "http://127.0.0.2:11434/v1",
            id="ipv4-loopback-range",
        ),
        pytest.param(
            "http://2130706434:11434/v1",
            id="legacy-ipv4-loopback-range",
        ),
        pytest.param(
            "http://[::ffff:127.0.0.2]:11434/v1",
            id="ipv4-mapped-ipv6-loopback",
        ),
    ],
)
def test_literal_loopback_api_config_reaches_public_provider_factory(
    base_url: str,
) -> None:
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="local-review-model",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url=base_url,
            api_key_env="LOCAL_REVIEW_KEY",
            allow_insecure_loopback_http=True,
        ),
    )

    result = provider_from_config(
        config,
        environment={"LOCAL_REVIEW_KEY": "local-secret"},
    )

    assert isinstance(result, APILLMProvider)


@pytest.mark.parametrize(
    "base_url",
    [
        pytest.param("https://4294967296/v1", id="decimal-dword-overflow"),
        pytest.param("https://040000000000/v1", id="octal-dword-overflow"),
        pytest.param("https://0x100000000/v1", id="hex-dword-overflow"),
    ],
)
def test_literal_api_config_rejects_out_of_range_legacy_ipv4(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match="malformed host"):
        LLMAPIConfig(
            base_url=base_url,
            allow_insecure_loopback_http=True,
        )


def test_review_circuit_preserves_quota_fallback_for_later_packs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReviewProvider:
        def __init__(self, *, quota_failure: bool) -> None:
            self.quota_failure = quota_failure
            self.calls = 0

        def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
            self.calls += 1
            if self.quota_failure:
                raise LLMProviderError("Quota exhausted.", category="quota")
            return []

        def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
            raise AssertionError("not used")

    cheap = ReviewProvider(quota_failure=True)
    strong = ReviewProvider(quota_failure=False)

    def provider_factory(config: LLMConfig) -> ReviewProvider:
        return cheap if config.model == "cheap-model" else strong

    monkeypatch.setattr("apex_ray.llm.review.provider_from_config", provider_factory)
    first = make_pack()
    second = first.model_copy(update={"id": "src/cart.ts#second"})
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        cache_enabled=False,
        profiles={
            "cheap": LLMProfile(model="cheap-model"),
            "strong": LLMProfile(model="strong-model"),
        },
        routing=LLMRoutingConfig(
            review_profile="cheap",
            escalated_review_profile="strong",
        ),
    )

    findings, runs = review_context_packs([first, second], config, Path("."))

    assert findings == []
    assert cheap.calls == 1
    assert strong.calls == 2
    assert [run.status for run in runs] == [
        "failed_quota",
        "ok",
        "skipped_circuit_open",
        "ok",
    ]
    assert [run.route_reason for run in runs] == [
        "profile:cheap",
        "fallback:strong:after_failed_quota",
        "profile:cheap",
        "fallback:strong:after_failed_quota",
    ]


def test_shared_route_circuit_persists_across_review_and_verification_calls() -> None:
    class TerminalProvider(FakeLLMProvider):
        review_calls = 0
        verification_calls = 0

        def __init__(self) -> None:
            super().__init__()

        def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
            self.review_calls += 1
            raise LLMProviderError("Invalid API key.", category="auth")

        def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
            self.verification_calls += 1
            raise AssertionError("an open circuit must prevent verification")

    provider = TerminalProvider()
    circuit = LLMRouteCircuitBreaker()
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        model="shared-route",
        cache_enabled=False,
    )
    pack = make_pack()
    finding = Finding(
        title="Finding on the failed route",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=6,
        failure_mode="The route cannot be verified.",
        evidence="Provider authentication failed.",
        suggested_fix="Restore provider credentials.",
        suggested_test="Run verification with valid credentials.",
        context_pack_id=pack.id,
    )

    _, first_runs = review_context_packs(
        [pack],
        config,
        Path("."),
        provider=provider,
        circuit_breaker=circuit,
    )
    approved, verifications, verify_runs = verify_findings(
        [finding],
        [pack],
        config,
        Path("."),
        provider=provider,
        circuit_breaker=circuit,
    )
    _, independent_runs = review_context_packs(
        [pack],
        config,
        Path("."),
        provider=provider,
    )

    assert first_runs[0].status == "failed_auth"
    assert approved == []
    assert verifications[0].approved is False
    assert "circuit" in verifications[0].reason.lower()
    assert verify_runs[0].status == "skipped_circuit_open"
    assert provider.verification_calls == 0
    assert independent_runs[0].status == "failed_auth"
    assert provider.review_calls == 2


def test_route_circuit_treats_profile_aliases_as_the_same_transport() -> None:
    circuit = LLMRouteCircuitBreaker()
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="shared-model",
        api=LLMAPIConfig(
            base_url="https://gateway.example/v1",
            api_key_env="PRIVATE_LLM_API_KEY",
        ),
    )

    circuit.record(
        config,
        "security-alias",
        "failed_auth",
        max_consecutive_failures=3,
    )

    open_failure = circuit.open_failure(config, "finance-alias")
    assert open_failure is not None
    assert open_failure[1] == "failed_auth"


def test_route_circuit_isolates_effort_and_timeout_variants() -> None:
    circuit = LLMRouteCircuitBreaker()
    first = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="shared-model",
        effort=LLMReasoningEffort.LOW,
        timeout_seconds=30,
        api=LLMAPIConfig(
            base_url="https://gateway.example/v1",
            api_key_env="PRIVATE_LLM_API_KEY",
        ),
    )
    circuit.record(
        first,
        "security",
        "failed_timeout",
        max_consecutive_failures=1,
    )

    stronger = first.model_copy(update={"effort": LLMReasoningEffort.HIGH})
    longer = first.model_copy(update={"timeout_seconds": 120})

    assert circuit.open_failure(stronger, "security") is None
    assert circuit.open_failure(longer, "security") is None


def test_route_circuit_isolates_custom_api_tenant_headers() -> None:
    circuit = LLMRouteCircuitBreaker()
    first = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="shared-model",
        api=LLMAPIConfig(
            base_url="https://gateway.example/v1",
            api_key_env="PRIVATE_LLM_API_KEY",
            headers_from_env={"X-Tenant": "FIRST_TENANT"},
        ),
    )
    second = first.model_copy(deep=True)
    second.api.headers_from_env = {"X-Tenant": "SECOND_TENANT"}

    circuit.record(
        first,
        "first-reviewer",
        "failed_quota",
        max_consecutive_failures=3,
    )

    assert circuit.open_failure(second, "second-reviewer") is None


def test_review_context_packs_records_provider_reported_usage() -> None:
    class UsageProvider:
        def review_context_pack_with_usage(self, pack: ContextPack, repo_root: Path) -> LLMReviewResult:
            return LLMReviewResult(
                findings=[],
                usage=LLMUsage(
                    source="unit",
                    input_tokens=120,
                    cached_input_tokens=40,
                    output_tokens=20,
                    reasoning_output_tokens=5,
                    total_tokens=185,
                    estimated_cost_usd=0.0012,
                ),
            )

        def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
            raise AssertionError("usage-aware review path should be used")

        def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
            raise AssertionError("not used")

    config = LLMConfig(provider=LLMProviderName.FAKE)

    _, runs = review_context_packs([make_pack()], config, Path("."), provider=UsageProvider())

    assert runs[0].usage_source == "unit"
    assert runs[0].actual_input_tokens == 120
    assert runs[0].actual_cached_input_tokens == 40
    assert runs[0].actual_output_tokens == 20
    assert runs[0].actual_reasoning_output_tokens == 5
    assert runs[0].actual_total_tokens == 185
    assert runs[0].estimated_cost_usd == 0.0012


def test_review_context_packs_passes_pre_rendered_prompt_to_capable_provider() -> None:
    pack = make_pack()
    rendered_prompts = {}
    rendered = render_review_prompt(pack, cache=rendered_prompts)
    seen_prompts: list[str] = []

    class RenderedPromptProvider:
        def review_rendered_prompt_with_usage(
            self,
            reviewed_pack: ContextPack,
            repo_root: Path,
            prompt: str,
        ) -> LLMReviewResult:
            assert reviewed_pack is pack
            seen_prompts.append(prompt)
            return LLMReviewResult(findings=[])

        def review_context_pack(self, reviewed_pack: ContextPack, repo_root: Path) -> list[Finding]:
            raise AssertionError("pre-rendered review path should be used")

        def verify_finding(
            self,
            finding: Finding,
            reviewed_pack: ContextPack,
            repo_root: Path,
        ) -> FindingVerification:
            raise AssertionError("not used")

    _, runs = review_context_packs(
        [pack],
        LLMConfig(provider=LLMProviderName.FAKE),
        Path("."),
        provider=RenderedPromptProvider(),
        rendered_prompts=rendered_prompts,
    )

    assert seen_prompts == [rendered.text]
    assert runs[0].input_chars == len(rendered.text)


def test_review_prompt_cache_isolates_reviewer_prompt_contexts() -> None:
    pack = make_pack()
    seen_prompts: list[str] = []
    rendered_prompts = {}

    class RenderedPromptProvider:
        def review_rendered_prompt_with_usage(
            self,
            reviewed_pack: ContextPack,
            repo_root: Path,
            prompt: str,
        ) -> LLMReviewResult:
            seen_prompts.append(prompt)
            return LLMReviewResult(findings=[])

        def review_context_pack(self, reviewed_pack: ContextPack, repo_root: Path) -> list[Finding]:
            raise AssertionError("pre-rendered review path should be used")

        def verify_finding(
            self,
            finding: Finding,
            reviewed_pack: ContextPack,
            repo_root: Path,
        ) -> FindingVerification:
            raise AssertionError("not used")

    config = LLMConfig(provider=LLMProviderName.FAKE)
    provider = RenderedPromptProvider()
    review_context_packs(
        [pack],
        config,
        Path("."),
        provider=provider,
        reviewer=ReviewerConfig(id="security", focus="Authorization boundaries."),
        rendered_prompts=rendered_prompts,
    )
    review_context_packs(
        [pack],
        config,
        Path("."),
        provider=provider,
        reviewer=ReviewerConfig(id="finance", focus="Financial settlement risk."),
        rendered_prompts=rendered_prompts,
    )

    assert len(rendered_prompts) == 2
    assert "Authorization boundaries." in seen_prompts[0]
    assert "Financial settlement risk." not in seen_prompts[0]
    assert "Financial settlement risk." in seen_prompts[1]
    assert "Authorization boundaries." not in seen_prompts[1]


def test_review_prompt_cache_isolates_changed_pack_content_with_same_id() -> None:
    pack = make_pack()
    rendered_prompts = {}
    first = render_review_prompt(pack, cache=rendered_prompts)
    changed_pack = pack.model_copy(
        update={
            "impact_notes": [
                *pack.impact_notes,
                "Financial settlement behavior changed.",
            ]
        }
    )

    changed = render_review_prompt(changed_pack, cache=rendered_prompts)

    assert changed is not first
    assert len(rendered_prompts) == 2
    assert "Financial settlement behavior changed." not in first.text
    assert "Financial settlement behavior changed." in changed.text


def test_verify_findings_records_provider_reported_usage() -> None:
    finding = Finding(
        title="Potential incorrect total",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=6,
        failure_mode="Totals can be wrong.",
        evidence="The changed symbol is calculateTotal.",
        suggested_fix="Preserve the existing multiplication.",
        suggested_test="Add a multi-item cart test.",
        context_pack_id=make_pack().id,
    )

    class UsageVerifier:
        def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
            raise AssertionError("not used")

        def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
            raise AssertionError("usage-aware verifier path should be used")

        def verify_findings_with_usage(
            self, findings: list[Finding], pack: ContextPack, repo_root: Path
        ) -> LLMVerificationResult:
            return LLMVerificationResult(
                verifications=[
                    FindingVerification(
                        finding=findings[0],
                        approved=True,
                        confidence=FindingConfidence.HIGH,
                        reason="Concrete.",
                    )
                ],
                usage=LLMUsage(source="unit", input_tokens=40, output_tokens=8, total_tokens=48),
            )

    approved, verifications, runs = verify_findings(
        [finding],
        [make_pack()],
        LLMConfig(
            provider=LLMProviderName.CODEX_CLI,
            model="review-model",
            cache_enabled=False,
        ),
        Path("."),
        provider=UsageVerifier(),
    )

    assert approved == [finding]
    assert verifications[0].approved is True
    assert runs[0].usage_source == "unit"
    assert runs[0].actual_input_tokens == 40
    assert runs[0].actual_output_tokens == 8
    assert runs[0].actual_total_tokens == 48
    assert runs[0].estimated_input_tokens == estimate_provider_input_tokens(
        runs[0].input_chars,
        provider=LLMProviderName.CODEX_CLI,
    )


def test_shallow_review_uses_compact_prompt_and_cheap_route() -> None:
    provider = FakeLLMProvider([])
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        profiles={"cheap": LLMProfile(model="codex-cheap"), "strong": LLMProfile(model="codex-strong")},
        routing=LLMRoutingConfig(
            review_profile="cheap",
            escalated_review_profile="strong",
            escalate_review_when=LLMRoutingCondition(risk=["auth"]),
        ),
    )
    pack = make_pack().model_copy(
        update={
            "risk_signals": [
                RiskSignal(kind="auth", severity="high", reason="Auth changed.", file="src/cart.ts", line=5)
            ],
            "references": [
                AnalyzerReference(file="src/checkout.ts", line=12, text="calculateTotal(items)", kind="call")
            ],
        }
    )

    _, runs = review_context_packs([pack], config, Path("."), provider=provider, review_depth="shallow")
    shallow_prompt = build_shallow_review_prompt(pack)

    assert runs[0].kind == "review_shallow"
    assert runs[0].prompt_version == REVIEW_SHALLOW_PROMPT_VERSION
    assert runs[0].model == "codex-cheap"
    assert runs[0].route_reason == "shallow:profile:cheap"
    assert "Language hint: TypeScript/JavaScript." in shallow_prompt
    assert "NestJS/DTO/schema/DI/provider/route/cache changes" in shallow_prompt
    assert "src/checkout.ts" not in shallow_prompt


def test_review_context_packs_can_run_without_shared_provider_in_parallel() -> None:
    config = LLMConfig(provider=LLMProviderName.FAKE, jobs=2)
    first = make_pack()
    second = make_pack().model_copy(update={"id": "src/cart.ts#other:1"})

    findings, runs = review_context_packs([first, second], config, Path("."))

    assert findings == []
    assert [run.context_pack_id for run in runs] == [first.id, second.id]


def test_review_context_packs_reports_parallel_progress_without_reordering_runs() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def event(self, message: str, *, key: str | None = None, force: bool = False) -> None:
            self.messages.append(message)

    progress = Recorder()
    config = LLMConfig(provider=LLMProviderName.FAKE, jobs=2)
    first = make_pack()
    second = make_pack().model_copy(update={"id": "src/cart.ts#other:1"})

    findings, runs = review_context_packs([first, second], config, Path("."), progress=progress)

    assert findings == []
    assert [run.context_pack_id for run in runs] == [first.id, second.id]
    assert "review deep: 2 context pack(s), jobs=2" in progress.messages
    assert any(message.startswith("review deep 2/2 done") for message in progress.messages)


def test_filter_findings_for_context_pack_allows_reference_files() -> None:
    pack = make_pack().model_copy(
        update={
            "references": [
                AnalyzerReference(
                    file="src/checkout.ts",
                    line=12,
                    text="total: calculateTotal(items)",
                    kind="call",
                )
            ]
        }
    )
    finding = Finding(
        title="Consumer shape mismatch",
        severity=FindingSeverity.MEDIUM,
        confidence=FindingConfidence.HIGH,
        file="src/checkout.ts",
        line=12,
        failure_mode="Consumer may receive the wrong shape.",
        evidence="Reference file uses the changed symbol.",
        suggested_fix="Update the changed function or consumer.",
        suggested_test="Add checkout coverage.",
    )

    assert filter_findings_for_context_pack([finding], pack)[0].context_pack_id == pack.id


def test_filter_findings_for_context_pack_allows_contract_snippet_files() -> None:
    pack = make_pack().model_copy(
        update={
            "contracts": [
                AnalyzerReference(
                    file="src/quote-schemas.ts",
                    line=15,
                    endLine=19,
                    text="export const AddQuoteSchema = z.object({",
                    kind="contract",
                )
            ],
            "contract_snippets": [
                CodeSnippet(
                    file="src/quote-schemas.ts",
                    start_line=15,
                    end_line=19,
                    code="export const AddQuoteSchema = z.object({\n  fileIds: z.array(z.string()).min(1).max(10),\n});",
                )
            ],
        }
    )
    finding = Finding(
        title="Schema contract mismatch",
        severity=FindingSeverity.MEDIUM,
        confidence=FindingConfidence.HIGH,
        file="src/quote-schemas.ts",
        line=18,
        failure_mode="The changed handler violates the schema contract.",
        evidence="Contract snippet is part of the context pack.",
        suggested_fix="Preserve the schema-derived behavior.",
        suggested_test="Add schema-boundary coverage.",
    )

    assert filter_findings_for_context_pack([finding], pack)[0].context_pack_id == pack.id


def test_filter_findings_for_context_pack_normalizes_file_paths() -> None:
    finding = Finding(
        title="Potential incorrect total",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=".\\src\\cart.ts",
        line=6,
        failure_mode="Totals can be wrong.",
        evidence="The changed symbol is calculateTotal.",
        suggested_fix="Preserve the existing multiplication.",
        suggested_test="Add a multi-item cart test.",
    )

    filtered = filter_findings_for_context_pack([finding], make_pack())

    assert filtered[0].file == "src/cart.ts"
    assert filtered[0].context_pack_id == "src/cart.ts#calculateTotal:1"


def test_filter_findings_for_context_pack_rejects_wrong_pack_id() -> None:
    finding = Finding(
        title="Wrong pack finding",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=6,
        failure_mode="Finding came from a different context pack.",
        evidence="The finding declares another pack id.",
        suggested_fix="Ignore it for this pack.",
        suggested_test="No test needed.",
        context_pack_id="src/cart.ts#other:1",
    )

    assert filter_findings_for_context_pack([finding], make_pack()) == []


def test_prompts_filter_memory_by_review_stage() -> None:
    pack = make_pack().model_copy(
        update={
            "memory_matches": [
                MemoryMatch(
                    id="known-fp",
                    title="Known verifier false positive",
                    kind="false_positive",
                    severity="medium",
                    applies_to="verify",
                    rendered="[memory:known-fp] Known verifier false positive\nReject safe guards.",
                    prompt_chars=72,
                ),
                MemoryMatch(
                    id="cart-invariant",
                    title="Cart invariant",
                    kind="invariant",
                    severity="high",
                    applies_to="both",
                    rendered="[memory:cart-invariant] Cart invariant\nCart totals must include quantity.",
                    prompt_chars=76,
                ),
            ]
        }
    )
    finding = Finding(
        title="Potential incorrect total",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=6,
        failure_mode="Totals can be wrong.",
        evidence="The changed symbol is calculateTotal.",
        suggested_fix="Preserve the existing multiplication.",
        suggested_test="Add a multi-item cart test.",
        context_pack_id=pack.id,
    )

    review_prompt = build_review_prompt(pack)
    verifier_prompt = build_verifier_prompt(finding, pack)

    assert "cart-invariant" in review_prompt
    assert "known-fp" not in review_prompt
    assert "cart-invariant" in verifier_prompt
    assert "known-fp" in verifier_prompt


def test_review_context_pack_uses_cache_on_second_run(tmp_path: Path) -> None:
    finding = Finding(
        title="Potential incorrect total",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=6,
        failure_mode="Totals can be wrong.",
        evidence="The changed symbol is calculateTotal.",
        suggested_fix="Preserve the existing multiplication.",
        suggested_test="Add a multi-item cart test.",
    )
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        cache_dir=str(tmp_path / "llm-cache"),
    )
    first_provider = FakeLLMProvider([finding])

    first_findings, first_runs = review_context_packs([make_pack()], config, tmp_path, provider=first_provider)

    second_provider = FakeLLMProvider([])
    second_findings, second_runs = review_context_packs([make_pack()], config, tmp_path, provider=second_provider)

    assert first_provider.reviewed_pack_ids == ["src/cart.ts#calculateTotal:1"]
    assert first_runs[0].cache_hit is False
    assert first_runs[0].cache_key
    assert second_provider.reviewed_pack_ids == []
    assert second_runs[0].cache_hit is True
    assert second_runs[0].input_chars == 0
    assert second_runs[0].estimated_input_tokens == 0
    assert second_runs[0].actual_total_tokens == 0
    assert second_runs[0].estimated_saved_input_tokens > 0
    assert second_runs[0].cache_hits == 1
    assert second_runs[0].cache_misses == 0
    assert second_findings == first_findings


def test_llm_cache_parallel_writes_to_same_key_use_distinct_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = LLMCache(tmp_path)
    config = LLMConfig(provider=LLMProviderName.FAKE)
    finding = Finding(
        title="Potential incorrect total",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=6,
        failure_mode="Totals can be wrong.",
        evidence="The changed symbol is calculateTotal.",
        suggested_fix="Preserve the existing multiplication.",
        suggested_test="Add a multi-item cart test.",
    )
    original_write_text = Path.write_text
    barrier = threading.Barrier(2)

    def delayed_write_text(self: Path, data: str, encoding: str) -> int:
        result = original_write_text(self, data, encoding=encoding)
        if self.parent == tmp_path and self.suffix == ".tmp":
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(Path, "write_text", delayed_write_text)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(cache.write_review, "same-key", config, [finding]) for _ in range(2)]
        for future in futures:
            future.result()

    assert cache.read_review("same-key") == [finding]


def test_review_cache_key_changes_when_context_pack_changes() -> None:
    config = LLMConfig(provider=LLMProviderName.FAKE)
    pack = make_pack()
    changed_pack = pack.model_copy(update={"diff_snippet": ["+return 1;"]})

    assert review_cache_key(pack, config) != review_cache_key(changed_pack, config)


def test_review_cache_key_reuses_rendered_prompt_payload_without_changing_identity() -> None:
    config = LLMConfig(provider=LLMProviderName.FAKE)
    pack = make_pack()
    rendered = render_review_prompt(pack)

    assert review_cache_key(
        pack,
        config,
        prompt_payload=rendered.prompt_payload,
    ) == review_cache_key(pack, config)


def test_review_cache_key_changes_when_model_changes() -> None:
    pack = make_pack()
    cheap = LLMConfig(provider=LLMProviderName.FAKE, model="cheap")
    strong = LLMConfig(provider=LLMProviderName.FAKE, model="strong")

    assert review_cache_key(pack, cheap) != review_cache_key(pack, strong)


def test_review_cache_key_changes_when_review_depth_changes() -> None:
    pack = make_pack()
    deep = LLMConfig(provider=LLMProviderName.FAKE, review_depth="deep")
    shallow = LLMConfig(provider=LLMProviderName.FAKE, review_depth="shallow")

    assert review_cache_key(pack, deep) != review_cache_key(pack, shallow)


def test_review_cache_key_changes_when_effort_changes() -> None:
    pack = make_pack()
    low = LLMConfig(provider=LLMProviderName.FAKE, model="codex-model", effort="low")
    high = LLMConfig(provider=LLMProviderName.FAKE, model="codex-model", effort="high")

    assert review_cache_key(pack, low) != review_cache_key(pack, high)


def test_api_cache_keys_change_when_endpoint_contract_changes() -> None:
    pack = make_pack()
    finding = Finding(
        title="Potential incorrect total",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=6,
        failure_mode="Totals can be wrong.",
        evidence="The changed symbol is calculateTotal.",
        suggested_fix="Preserve multiplication.",
        suggested_test="Add a multi-item cart test.",
        context_pack_id=pack.id,
    )
    first = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="review-model",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_SCHEMA,
            base_url="https://first.example/v1",
            api_key_env="FIRST_API_KEY",
        ),
    )
    second = first.model_copy(
        deep=True,
        update={
            "api": first.api.model_copy(
                update={
                    "base_url": "https://second.example/v1",
                    "api_key_env": "SECOND_API_KEY",
                }
            )
        },
    )

    assert review_cache_key(pack, first) != review_cache_key(pack, second)
    assert verification_cache_key(finding, pack, first) != verification_cache_key(
        finding,
        pack,
        second,
    )


def test_api_cache_key_changes_when_endpoint_environment_value_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="review-model",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_SCHEMA,
            base_url_env="CUSTOM_REVIEW_URL",
            api_key_env="CUSTOM_REVIEW_KEY",
        ),
    )
    monkeypatch.setenv("CUSTOM_REVIEW_URL", "https://first.example/v1")
    first_key = review_cache_key(make_pack(), config)

    monkeypatch.setenv("CUSTOM_REVIEW_URL", "https://second.example/v1")
    second_key = review_cache_key(make_pack(), config)

    assert first_key != second_key
    assert "first.example" not in first_key
    assert "second.example" not in second_key


def test_api_cache_keys_change_when_auth_or_tenant_identity_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack = make_pack()
    finding = Finding(
        title="Potential incorrect total",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=6,
        failure_mode="Totals can be wrong.",
        evidence="The changed symbol is calculateTotal.",
        suggested_fix="Preserve multiplication.",
        suggested_test="Add a multi-item cart test.",
        context_pack_id=pack.id,
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="review-model",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_SCHEMA,
            base_url="https://gateway.example/v1",
            api_key_env="CUSTOM_REVIEW_KEY",
            headers_from_env={"X-Tenant-ID": "CUSTOM_REVIEW_TENANT"},
        ),
    )
    monkeypatch.setenv("CUSTOM_REVIEW_KEY", "first-secret")
    monkeypatch.setenv("CUSTOM_REVIEW_TENANT", "tenant-a")
    first = (
        review_cache_key(pack, config),
        verification_cache_key(finding, pack, config),
    )

    monkeypatch.setenv("CUSTOM_REVIEW_KEY", "second-secret")
    second = (
        review_cache_key(pack, config),
        verification_cache_key(finding, pack, config),
    )
    monkeypatch.setenv("CUSTOM_REVIEW_TENANT", "tenant-b")
    third = (
        review_cache_key(pack, config),
        verification_cache_key(finding, pack, config),
    )

    assert len({*first, *second, *third}) == 6
    assert all("secret" not in key and "tenant-" not in key for key in (*first, *second, *third))


def test_built_in_api_cache_key_uses_default_key_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = LLMConfig(provider=LLMProviderName.OPENAI_API, model="gpt-5.6")
    monkeypatch.setenv("OPENAI_API_KEY", "first-secret")
    first = review_cache_key(make_pack(), config)

    monkeypatch.setenv("OPENAI_API_KEY", "second-secret")
    second = review_cache_key(make_pack(), config)

    assert first != second
    assert "first-secret" not in first
    assert "second-secret" not in second


def test_review_config_uses_default_profile() -> None:
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        profiles={"cheap": LLMProfile(model="codex-cheap")},
        routing=LLMRoutingConfig(review_profile="cheap"),
    )

    resolved, profile, reason = review_config_for_pack(config, make_pack())

    assert resolved.model == "codex-cheap"
    assert profile == "cheap"
    assert reason == "profile:cheap"


def test_review_config_profile_can_switch_provider_and_cli_path() -> None:
    config = LLMConfig(
        provider=LLMProviderName.CODEX_CLI,
        codex_path="codex",
        claude_path="claude",
        profiles={
            "cheap": LLMProfile(provider=LLMProviderName.CODEX_CLI, model="codex-cheap", codex_path="tools/codex"),
            "strong": LLMProfile(
                provider=LLMProviderName.CLAUDE_CODE_CLI,
                model="sonnet",
                effort="xhigh",
                claude_path="tools/claude",
            ),
        },
        routing=LLMRoutingConfig(review_profile="strong"),
    )

    resolved, profile, reason = review_config_for_pack(config, make_pack())

    assert resolved.provider == "claude_code_cli"
    assert resolved.model == "sonnet"
    assert resolved.effort == "xhigh"
    assert resolved.claude_path == "tools/claude"
    assert resolved.codex_path == "codex"
    assert profile == "strong"
    assert reason == "profile:strong"


def test_review_config_escalates_on_risk() -> None:
    pack = make_pack().model_copy(
        update={
            "risk_signals": [
                RiskSignal(kind="auth", severity="high", reason="Auth changed.", file="src/cart.ts", line=5)
            ]
        }
    )
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        profiles={"cheap": LLMProfile(model="codex-cheap"), "strong": LLMProfile(model="codex-strong")},
        routing=LLMRoutingConfig(
            review_profile="cheap",
            escalated_review_profile="strong",
            escalate_review_when=LLMRoutingCondition(risk=["auth"]),
        ),
    )

    resolved, profile, reason = review_config_for_pack(config, pack)

    assert resolved.model == "codex-strong"
    assert profile == "strong"
    assert reason == "escalated:strong:risk:auth"


def test_review_config_does_not_escalate_excluded_file_kind() -> None:
    pack = make_pack().model_copy(
        update={
            "file": "src/cart.test.ts",
            "file_kind": FileKind.TEST,
            "risk_signals": [
                RiskSignal(kind="auth", severity="high", reason="Auth changed.", file="src/cart.test.ts", line=5)
            ],
        }
    )
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        profiles={"cheap": LLMProfile(model="codex-cheap"), "strong": LLMProfile(model="codex-strong")},
        routing=LLMRoutingConfig(
            review_profile="cheap",
            escalated_review_profile="strong",
            escalate_review_when=LLMRoutingCondition(risk=["auth"], exclude_file_kind=[FileKind.TEST]),
        ),
    )

    resolved, profile, reason = review_config_for_pack(config, pack)

    assert resolved.model == "codex-cheap"
    assert profile == "cheap"
    assert reason == "profile:cheap"


def test_review_config_uses_rule_model_override() -> None:
    pack = make_pack().model_copy(
        update={
            "rule_matches": [
                RuleMatch(
                    id="cart-total",
                    title="Preserve totals",
                    severity=FindingSeverity.HIGH,
                    mode="strict",
                    model="strong",
                )
            ]
        }
    )
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        profiles={"cheap": LLMProfile(model="codex-cheap"), "strong": LLMProfile(model="codex-strong")},
        routing=LLMRoutingConfig(review_profile="cheap"),
    )

    resolved, profile, reason = review_config_for_pack(config, pack)

    assert resolved.model == "codex-strong"
    assert profile == "strong"
    assert reason == "rule:strong"


def test_verification_config_uses_verify_profile() -> None:
    finding = Finding(
        title="Cart totals ignore item quantity",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        profiles={"strong": LLMProfile(model="codex-strong")},
        routing=LLMRoutingConfig(verify_profile="strong"),
    )

    resolved, profile, reason = verification_config_for_finding(config, finding, make_pack())

    assert resolved.model == "codex-strong"
    assert profile == "strong"
    assert reason == "profile:strong"


def test_verification_config_escalates_on_finding_severity_and_confidence() -> None:
    finding = Finding(
        title="Low confidence critical issue",
        severity=FindingSeverity.CRITICAL,
        confidence=FindingConfidence.LOW,
        file="src/cart.ts",
        line=7,
        failure_mode="Critical totals regression.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        profiles={"cheap": LLMProfile(model="codex-cheap"), "strong": LLMProfile(model="codex-strong")},
        routing=LLMRoutingConfig(
            verify_profile="cheap",
            escalated_verify_profile="strong",
            escalate_verify_when=LLMRoutingCondition(
                finding_severity=[FindingSeverity.CRITICAL],
                finding_confidence=[FindingConfidence.LOW],
            ),
        ),
    )

    resolved, profile, reason = verification_config_for_finding(config, finding, make_pack())

    assert resolved.model == "codex-strong"
    assert profile == "strong"
    assert reason == "escalated-verify:strong:finding_severity:critical"


def test_verification_config_for_batch_escalates_when_any_finding_matches() -> None:
    low = Finding(
        title="Low severity issue",
        severity=FindingSeverity.LOW,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Minor issue.",
        evidence="Weak evidence.",
        suggested_fix="Adjust code.",
        suggested_test="Add coverage.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    high = low.model_copy(update={"title": "High severity issue", "severity": FindingSeverity.HIGH})
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        profiles={"cheap": LLMProfile(model="codex-cheap"), "strong": LLMProfile(model="codex-strong")},
        routing=LLMRoutingConfig(
            verify_profile="cheap",
            escalated_verify_profile="strong",
            escalate_verify_when=LLMRoutingCondition(finding_severity=[FindingSeverity.HIGH]),
        ),
    )

    resolved, profile, reason = verification_config_for_findings(config, [low, high], make_pack())

    assert resolved.model == "codex-strong"
    assert profile == "strong"
    assert reason == "escalated-verify:strong:finding_severity:high"


def test_verification_config_escalates_on_pack_risk_and_strict_rule() -> None:
    finding = Finding(
        title="Permission bypass",
        severity=FindingSeverity.MEDIUM,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Unauthorized access.",
        evidence="Guard changed.",
        suggested_fix="Restore guard.",
        suggested_test="Add denied role test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    pack = make_pack().model_copy(
        update={
            "risk_signals": [
                RiskSignal(kind="auth", severity="high", reason="Auth changed.", file="src/cart.ts", line=5)
            ],
            "rule_matches": [
                RuleMatch(
                    id="auth-guard",
                    title="Preserve guards",
                    severity=FindingSeverity.HIGH,
                    mode="strict",
                )
            ],
        }
    )
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        profiles={"cheap": LLMProfile(model="codex-cheap"), "strong": LLMProfile(model="codex-strong")},
        routing=LLMRoutingConfig(
            verify_profile="cheap",
            escalated_verify_profile="strong",
            escalate_verify_when=LLMRoutingCondition(risk=["auth"], strict_rule=True),
        ),
    )

    resolved, profile, reason = verification_config_for_finding(config, finding, pack)

    assert resolved.model == "codex-strong"
    assert profile == "strong"
    assert reason == "escalated-verify:strong:risk:auth"


def test_refresh_cache_calls_provider_again(tmp_path: Path) -> None:
    first = Finding(
        title="First finding",
        severity=FindingSeverity.LOW,
        confidence=FindingConfidence.MEDIUM,
        file="src/cart.ts",
        line=6,
        failure_mode="First failure.",
        evidence="First evidence.",
        suggested_fix="First fix.",
        suggested_test="First test.",
    )
    second = first.model_copy(update={"title": "Second finding"})
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        cache_dir=str(tmp_path / "llm-cache"),
    )
    review_context_packs([make_pack()], config, tmp_path, provider=FakeLLMProvider([first]))

    refresh_config = config.model_copy(update={"refresh_cache": True})
    provider = FakeLLMProvider([second])
    findings, runs = review_context_packs([make_pack()], refresh_config, tmp_path, provider=provider)

    assert provider.reviewed_pack_ids == ["src/cart.ts#calculateTotal:1"]
    assert runs[0].cache_hit is False
    assert findings[0].title == "Second finding"


def test_fake_verifier_can_reject_findings() -> None:
    finding = Finding(
        title="Speculative issue",
        severity=FindingSeverity.MEDIUM,
        confidence=FindingConfidence.LOW,
        file="src/cart.ts",
        line=6,
        failure_mode="Might be wrong.",
        evidence="Weak evidence.",
        suggested_fix="Investigate.",
        suggested_test="Add a test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    provider = FakeLLMProvider(verification_approvals=[False])
    config = LLMConfig(provider=LLMProviderName.FAKE)

    approved, verifications, runs = verify_findings([finding], [make_pack()], config, Path("."), provider=provider)

    assert approved == []
    assert verifications[0].approved is False
    assert runs[0].kind == "verify"
    assert runs[0].findings_count == 0
    assert runs[0].prompt_version == VERIFIER_PROMPT_VERSION


def test_verify_findings_batches_by_context_pack() -> None:
    first = Finding(
        title="Cart totals ignore item quantity",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    second = first.model_copy(
        update={
            "title": "Speculative duplicate",
            "severity": FindingSeverity.LOW,
            "confidence": FindingConfidence.LOW,
        }
    )
    provider = FakeLLMProvider(verification_approvals=[True, False])
    config = LLMConfig(provider=LLMProviderName.FAKE)

    approved, verifications, runs = verify_findings(
        [first, second], [make_pack()], config, Path("."), provider=provider
    )

    assert provider.verified_batch_pack_ids == ["src/cart.ts#calculateTotal:1"]
    assert provider.verified_batches == [["Cart totals ignore item quantity", "Speculative duplicate"]]
    assert provider.verified_finding_titles == ["Cart totals ignore item quantity", "Speculative duplicate"]
    assert approved == [first]
    assert [verification.approved for verification in verifications] == [True, False]
    assert len(runs) == 1
    assert runs[0].findings_count == 1
    assert runs[0].input_chars > 0


def test_verify_findings_scopes_only_referenced_packs_for_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    referenced_pack = make_pack()
    unused_pack = referenced_pack.model_copy(
        update={"id": "src/cart.ts#unused:1"},
    )
    finding = Finding(
        title="Cart totals ignore item quantity",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id=referenced_pack.id,
    )
    scoped_pack_ids: list[str] = []

    from apex_ray.llm import review as review_module

    original_pack_for_reviewer = review_module.pack_for_reviewer

    def tracked_pack_for_reviewer(pack: ContextPack, reviewer: ReviewerConfig) -> ContextPack:
        scoped_pack_ids.append(pack.id)
        return original_pack_for_reviewer(pack, reviewer)

    monkeypatch.setattr(review_module, "pack_for_reviewer", tracked_pack_for_reviewer)

    approved, verifications, runs = verify_findings(
        [finding],
        [referenced_pack, unused_pack],
        LLMConfig(provider=LLMProviderName.FAKE, cache_enabled=False),
        Path("."),
        provider=FakeLLMProvider(verification_approvals=[True]),
        reviewer=ReviewerConfig(id="security", focus="Authorization boundaries."),
    )

    assert scoped_pack_ids == [referenced_pack.id]
    assert approved == [finding]
    assert verifications[0].reviewer_id == "security"
    assert [run.context_pack_id for run in runs] == [referenced_pack.id]


def test_verify_findings_falls_back_to_legacy_provider_protocol() -> None:
    finding = Finding(
        title="Legacy verifier finding",
        severity=FindingSeverity.MEDIUM,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=6,
        failure_mode="Totals are wrong.",
        evidence="Changed line removes quantity.",
        suggested_fix="Restore quantity multiplication.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )

    class LegacyProvider:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
            return []

        def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
            self.calls.append(finding.title)
            return FindingVerification(
                finding=finding,
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Legacy verifier approved.",
            )

    provider = LegacyProvider()

    approved, verifications, runs = verify_findings(
        [finding],
        [make_pack()],
        LLMConfig(provider=LLMProviderName.FAKE),
        Path("."),
        provider=provider,
    )

    assert provider.calls == ["Legacy verifier finding"]
    assert approved == [finding]
    assert verifications[0].approved is True
    assert len(runs) == 1


def test_verify_findings_uses_cache_on_second_run(tmp_path: Path) -> None:
    finding = Finding(
        title="Cart totals ignore item quantity",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        cache_dir=str(tmp_path / "llm-cache"),
    )
    first_provider = FakeLLMProvider(verification_approvals=[True])

    approved, _, first_runs = verify_findings([finding], [make_pack()], config, tmp_path, provider=first_provider)

    second_provider = FakeLLMProvider(verification_approvals=[False])
    cached_approved, _, second_runs = verify_findings(
        [finding], [make_pack()], config, tmp_path, provider=second_provider
    )

    assert approved == [finding]
    assert first_runs[0].cache_hit is False
    assert first_runs[0].cache_hits == 0
    assert first_runs[0].cache_misses == 1
    assert second_provider.verified_finding_titles == []
    assert second_provider.verified_batches == []
    assert second_runs[0].cache_hit is True
    assert second_runs[0].cache_hits == 1
    assert second_runs[0].cache_misses == 0
    assert second_runs[0].input_chars == 0
    assert cached_approved == [finding]


def test_verify_findings_records_partial_batch_cache_hits(tmp_path: Path) -> None:
    cached = Finding(
        title="Cached issue",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    missed = cached.model_copy(update={"title": "New issue", "line": 8})
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        cache_dir=str(tmp_path / "llm-cache"),
    )
    verify_findings([cached], [make_pack()], config, tmp_path, provider=FakeLLMProvider(verification_approvals=[True]))

    provider = FakeLLMProvider(verification_approvals=[True])
    approved, _, runs = verify_findings([cached, missed], [make_pack()], config, tmp_path, provider=provider)

    assert provider.verified_batches == [["New issue"]]
    assert approved == [cached, missed]
    assert runs[0].cache_hit is False
    assert runs[0].cache_hits == 1
    assert runs[0].cache_misses == 1
    assert runs[0].input_chars > 0


def test_verify_findings_preserves_cache_hits_when_a_batch_miss_fails(
    tmp_path: Path,
) -> None:
    class FailingVerifier(FakeLLMProvider):
        def verify_findings(
            self,
            findings: list[Finding],
            pack: ContextPack,
            repo_root: Path,
        ) -> list[FindingVerification]:
            raise LLMProviderError("temporary verifier outage")

    cached = Finding(
        title="Cached authorization issue",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="An authorization guard is bypassed.",
        evidence="The guard no longer precedes the write.",
        suggested_fix="Restore the guard.",
        suggested_test="Reject an unauthorized write.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    missed = cached.model_copy(update={"title": "Uncached validation issue", "line": 8})
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        cache_dir=str(tmp_path / "llm-cache"),
    )
    verify_findings(
        [cached],
        [make_pack()],
        config,
        tmp_path,
        provider=FakeLLMProvider(verification_approvals=[True]),
    )

    approved, verifications, runs = verify_findings(
        [cached, missed],
        [make_pack()],
        config,
        tmp_path,
        provider=FailingVerifier(),
    )

    assert approved == [cached]
    assert [(decision.approved, decision.superseded) for decision in verifications] == [
        (True, False),
        (False, True),
    ]
    assert runs[0].status == "failed_provider"
    assert runs[0].cache_hits == 1
    assert runs[0].cache_misses == 1


def test_verify_findings_groups_cache_by_per_finding_route(tmp_path: Path) -> None:
    low = Finding(
        title="Low severity cached issue",
        severity=FindingSeverity.LOW,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Minor issue.",
        evidence="Weak evidence.",
        suggested_fix="Adjust code.",
        suggested_test="Add coverage.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    high = low.model_copy(
        update={
            "title": "High severity issue",
            "severity": FindingSeverity.HIGH,
            "failure_mode": "Important issue.",
        }
    )
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        cache_dir=str(tmp_path / "llm-cache"),
        profiles={"cheap": LLMProfile(model="codex-cheap"), "strong": LLMProfile(model="codex-strong")},
        routing=LLMRoutingConfig(
            verify_profile="cheap",
            escalated_verify_profile="strong",
            escalate_verify_when=LLMRoutingCondition(finding_severity=[FindingSeverity.HIGH]),
        ),
    )
    verify_findings([low], [make_pack()], config, tmp_path, provider=FakeLLMProvider(verification_approvals=[True]))

    provider = FakeLLMProvider(verification_approvals=[True])
    approved, _, runs = verify_findings([low, high], [make_pack()], config, tmp_path, provider=provider)

    assert provider.verified_batches == [["High severity issue"]]
    assert approved == [low, high]
    cheap_run = next(run for run in runs if run.profile == "cheap")
    strong_run = next(run for run in runs if run.profile == "strong")
    assert cheap_run.cache_hit is True
    assert cheap_run.cache_hits == 1
    assert cheap_run.cache_misses == 0
    assert cheap_run.input_chars == 0
    assert strong_run.cache_hit is False
    assert strong_run.cache_hits == 0
    assert strong_run.cache_misses == 1
    assert strong_run.input_chars > 0


def test_verify_findings_opens_circuit_only_for_failed_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RouteVerifier:
        def __init__(self, *, fails: bool) -> None:
            self.fails = fails
            self.calls = 0

        def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
            raise AssertionError("not used")

        def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
            self.calls += 1
            if self.fails:
                raise LLMProviderError("Invalid API key.", category="auth")
            return FindingVerification(
                finding=finding,
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Healthy route verified the finding.",
            )

    broken = RouteVerifier(fails=True)
    healthy = RouteVerifier(fails=False)

    def provider_factory(config: LLMConfig) -> RouteVerifier:
        return broken if config.model == "broken" else healthy

    monkeypatch.setattr("apex_ray.llm.review.provider_from_config", provider_factory)
    packs = [make_pack().model_copy(update={"id": f"src/cart.ts#verify-{index}"}) for index in range(4)]
    findings = [
        Finding(
            title=f"Finding {index}",
            severity=severity,
            confidence=FindingConfidence.HIGH,
            file=pack.file,
            line=6,
            failure_mode="The changed code can fail.",
            evidence="The diff removes a required check.",
            suggested_fix="Restore the check.",
            suggested_test="Add a regression test.",
            context_pack_id=pack.id,
        )
        for index, (pack, severity) in enumerate(
            zip(
                packs,
                [
                    FindingSeverity.HIGH,
                    FindingSeverity.LOW,
                    FindingSeverity.HIGH,
                    FindingSeverity.LOW,
                ],
                strict=True,
            )
        )
    ]
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        model="healthy",
        cache_enabled=False,
        profiles={
            "cheap": LLMProfile(provider=LLMProviderName.FAKE, model="healthy"),
            "strong": LLMProfile(provider=LLMProviderName.OPENAI_API, model="broken"),
        },
        routing=LLMRoutingConfig(
            verify_profile="cheap",
            escalated_verify_profile="strong",
            escalate_verify_when=LLMRoutingCondition(finding_severity=[FindingSeverity.HIGH]),
        ),
    )

    approved, verifications, runs = verify_findings(findings, packs, config, Path("."))

    assert approved == [findings[1], findings[3]]
    assert broken.calls == 1
    assert healthy.calls == 2
    assert [run.status for run in runs] == [
        "failed_auth",
        "ok",
        "skipped_circuit_open",
        "ok",
    ]
    assert [verification.approved for verification in verifications] == [False, True, False, True]
    assert "circuit" in verifications[2].reason.lower()


def test_verify_findings_opens_retryable_circuit_after_parallel_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutVerifier:
        calls = 0

        def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
            raise AssertionError("not used")

        def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
            self.calls += 1
            raise LLMProviderError("Provider timed out.", category="timeout", retryable=True)

    timeout_verifier = TimeoutVerifier()
    monkeypatch.setattr("apex_ray.llm.review.provider_from_config", lambda _config: timeout_verifier)
    packs = [make_pack().model_copy(update={"id": f"src/cart.ts#timeout-{index}"}) for index in range(5)]
    findings = [
        Finding(
            title=f"Timeout finding {index}",
            severity=FindingSeverity.MEDIUM,
            confidence=FindingConfidence.HIGH,
            file=pack.file,
            line=6,
            failure_mode="The changed code can fail.",
            evidence="The diff removes a required check.",
            suggested_fix="Restore the check.",
            suggested_test="Add a regression test.",
            context_pack_id=pack.id,
        )
        for index, pack in enumerate(packs)
    ]
    config = LLMConfig(
        provider=LLMProviderName.FAKE,
        model="timeout-route",
        cache_enabled=False,
        jobs=2,
        max_consecutive_provider_failures=2,
    )

    approved, verifications, runs = verify_findings(findings, packs, config, Path("."))

    assert approved == []
    assert timeout_verifier.calls == 2
    assert [run.status for run in runs] == [
        "failed_timeout",
        "failed_timeout",
        "skipped_circuit_open",
        "skipped_circuit_open",
        "skipped_circuit_open",
    ]
    assert all(not verification.approved for verification in verifications)
    assert all("circuit" in verification.reason.lower() for verification in verifications[2:])


def test_dedupe_findings_keeps_highest_ranked_duplicate() -> None:
    low = Finding(
        title="Payment webhook CORS handling was removed",
        severity=FindingSeverity.LOW,
        confidence=FindingConfidence.MEDIUM,
        file="src/middleware.ts",
        line=20,
        failure_mode="May break CORS.",
        evidence="Middleware changed.",
        suggested_fix="Restore route.",
        suggested_test="Add middleware test.",
        context_pack_id="pack-1",
    )
    high = low.model_copy(
        update={
            "severity": FindingSeverity.MEDIUM,
            "confidence": FindingConfidence.HIGH,
            "context_pack_id": "pack-2",
        }
    )

    findings = dedupe_findings([low, high])

    assert len(findings) == 1
    assert findings[0].context_pack_id == "pack-2"


def test_dedupe_findings_keeps_same_title_on_different_lines() -> None:
    first = Finding(
        title="Missing permission check",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/admin.ts",
        line=20,
        failure_mode="The list endpoint is public.",
        evidence="No guard on list.",
        suggested_fix="Add guard.",
        suggested_test="Add list auth test.",
        context_pack_id="pack-1",
    )
    second = first.model_copy(
        update={
            "line": 45,
            "failure_mode": "The delete endpoint is public.",
            "evidence": "No guard on delete.",
            "context_pack_id": "pack-2",
        }
    )

    findings = dedupe_findings([first, second])

    assert findings == [first, second]


def test_build_codex_command_is_read_only_and_schema_bound(tmp_path: Path) -> None:
    command = build_codex_command(
        codex_path="/usr/local/bin/codex",
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "out.json",
        model="gpt-5-codex",
    )

    assert command[:4] == ["/usr/local/bin/codex", "--ask-for-approval", "never", "exec"]
    assert "--json" in command
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert "--output-schema" in command
    assert "--output-last-message" in command
    assert command[-3:] == ["--model", "gpt-5-codex", "-"]


def test_build_codex_command_can_set_reasoning_effort(tmp_path: Path) -> None:
    command = build_codex_command(
        codex_path="/usr/local/bin/codex",
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "out.json",
        model="gpt-5.4",
        effort="low",
    )

    assert command[command.index("--config") + 1] == 'model_reasoning_effort="low"'
    assert command.index("--config") < command.index("exec")


def test_build_codex_command_rejects_claude_only_max_effort(tmp_path: Path) -> None:
    with pytest.raises(LLMProviderError, match="does not support effort 'max'"):
        build_codex_command(
            codex_path="/usr/local/bin/codex",
            schema_path=tmp_path / "schema.json",
            output_path=tmp_path / "out.json",
            effort="max",
        )


def test_parse_codex_usage_from_jsonl_uses_latest_token_count_event() -> None:
    usage = parse_codex_usage_from_jsonl(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 25,
                                    "output_tokens": 10,
                                    "reasoning_output_tokens": 3,
                                    "total_tokens": 113,
                                }
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 120,
                                    "cached_input_tokens": 30,
                                    "output_tokens": 15,
                                    "reasoning_output_tokens": 5,
                                    "total_tokens": 140,
                                }
                            },
                        },
                    }
                ),
            ]
        )
    )

    assert usage is not None
    assert usage.source == "codex_cli_json"
    assert usage.input_tokens == 120
    assert usage.cached_input_tokens == 30
    assert usage.output_tokens == 15
    assert usage.reasoning_output_tokens == 5
    assert usage.total_tokens == 140


def test_parse_codex_usage_from_jsonl_supports_turn_completed_events() -> None:
    usage = parse_codex_usage_from_jsonl(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 15134,
                    "cached_input_tokens": 3456,
                    "output_tokens": 24,
                    "reasoning_output_tokens": 17,
                },
            }
        )
    )

    assert usage is not None
    assert usage.source == "codex_cli_json"
    assert usage.input_tokens == 15134
    assert usage.cached_input_tokens == 3456
    assert usage.output_tokens == 24
    assert usage.reasoning_output_tokens == 17
    assert usage.total_tokens == 15158


def test_codex_provider_runs_outside_reviewed_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    seen_cwd: Path | None = None

    def fake_run(
        command: list[str],
        cwd: Path,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_cwd
        seen_cwd = cwd
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({"findings": []}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("apex_ray.llm.providers.subprocess.run", fake_run)

    provider = CodexCLIProvider(LLMConfig(provider=LLMProviderName.CODEX_CLI, codex_path=str(codex)))
    findings = provider.review_context_pack(make_pack(), repo)

    assert findings == []
    assert seen_cwd is not None
    assert seen_cwd != repo
    assert repo not in seen_cwd.parents


def test_codex_provider_resolves_relative_codex_path_before_changing_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    codex = repo / "tools" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    seen_command: list[str] | None = None

    def fake_run(
        command: list[str],
        cwd: Path,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_command
        seen_command = command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({"findings": []}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("apex_ray.llm.providers.subprocess.run", fake_run)

    provider = CodexCLIProvider(LLMConfig(provider=LLMProviderName.CODEX_CLI, codex_path="tools/codex"))
    findings = provider.review_context_pack(make_pack(), repo)

    assert findings == []
    assert seen_command is not None
    assert seen_command[0] == str(codex.resolve())


def test_codex_provider_verifies_findings_in_one_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    codex = repo / "tools" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    first = Finding(
        title="Cart totals ignore item quantity",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    second = first.model_copy(update={"title": "Speculative duplicate"})
    seen_inputs: list[str] = []

    def fake_run(
        command: list[str],
        cwd: Path,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        seen_inputs.append(input)
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "decisions": [
                        {"finding_index": 0, "approved": True, "confidence": "high", "reason": "Concrete."},
                        {"finding_index": 1, "approved": False, "confidence": "high", "reason": "Too weak."},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("apex_ray.llm.providers.subprocess.run", fake_run)

    provider = CodexCLIProvider(LLMConfig(provider=LLMProviderName.CODEX_CLI, codex_path="tools/codex"))
    verifications = provider.verify_findings([first, second], make_pack(), repo)

    assert len(seen_inputs) == 1
    assert "Candidate findings JSON" in seen_inputs[0]
    assert [verification.approved for verification in verifications] == [True, False]


def test_build_claude_command_is_noninteractive_schema_bound_and_toolless() -> None:
    command = build_claude_command(
        claude_path="/usr/local/bin/claude",
        schema=finding_response_schema(),
        model="sonnet",
    )

    assert command[:2] == ["/usr/local/bin/claude", "--print"]
    assert "--no-session-persistence" in command
    assert command[command.index("--output-format") + 1] == "json"
    assert "--json-schema" in command
    assert json.loads(command[command.index("--json-schema") + 1])["required"] == ["findings"]
    assert command[command.index("--tools") + 1] == ""
    assert command[-2:] == ["--model", "sonnet"]


def test_build_claude_command_can_set_effort() -> None:
    command = build_claude_command(
        claude_path="/usr/local/bin/claude",
        schema=finding_response_schema(),
        model="sonnet",
        effort="max",
    )

    assert command[-4:] == ["--model", "sonnet", "--effort", "max"]


def test_parse_claude_usage_from_json_reads_cache_tokens_and_cost() -> None:
    usage = parse_claude_usage_from_json(
        json.dumps(
            {
                "type": "result",
                "result": json.dumps({"findings": []}),
                "total_cost_usd": 0.0025,
                "usage": {
                    "input_tokens": 50,
                    "cache_read_input_tokens": 200,
                    "cache_creation_input_tokens": 30,
                    "output_tokens": 12,
                },
            }
        )
    )

    assert usage is not None
    assert usage.source == "claude_json"
    assert usage.input_tokens == 50
    assert usage.cached_input_tokens == 200
    assert usage.cache_read_input_tokens == 200
    assert usage.cache_creation_input_tokens == 30
    assert usage.output_tokens == 12
    assert usage.total_tokens == 292
    assert usage.estimated_cost_usd == 0.0025


def test_claude_provider_runs_outside_reviewed_repo_and_parses_json_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    seen_cwd: Path | None = None

    def fake_run(
        command: list[str],
        cwd: Path,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_cwd
        seen_cwd = cwd
        assert "--json-schema" in command
        assert "Context pack JSON" in input
        stdout = json.dumps({"type": "result", "result": json.dumps({"findings": []})})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("apex_ray.llm.providers.subprocess.run", fake_run)

    provider = ClaudeCodeCLIProvider(LLMConfig(provider=LLMProviderName.CLAUDE_CODE_CLI, claude_path=str(claude)))
    findings = provider.review_context_pack(make_pack(), repo)

    assert findings == []
    assert seen_cwd is not None
    assert seen_cwd != repo
    assert repo not in seen_cwd.parents


def test_claude_provider_resolves_relative_claude_path_before_changing_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    claude = repo / "tools" / "claude"
    claude.parent.mkdir(parents=True)
    claude.write_text("#!/bin/sh\n", encoding="utf-8")
    seen_command: list[str] | None = None

    def fake_run(
        command: list[str],
        cwd: Path,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_command
        seen_command = command
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"result": {"findings": []}}), stderr="")

    monkeypatch.setattr("apex_ray.llm.providers.subprocess.run", fake_run)

    provider = ClaudeCodeCLIProvider(LLMConfig(provider=LLMProviderName.CLAUDE_CODE_CLI, claude_path="tools/claude"))
    findings = provider.review_context_pack(make_pack(), repo)

    assert findings == []
    assert seen_command is not None
    assert seen_command[0] == str(claude.resolve())


def test_claude_provider_prefers_structured_output_over_text_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    claude = repo / "tools" / "claude"
    claude.parent.mkdir(parents=True)
    claude.write_text("#!/bin/sh\n", encoding="utf-8")

    def fake_run(
        command: list[str],
        cwd: Path,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "{findings: []}",
                "structured_output": {"findings": []},
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("apex_ray.llm.providers.subprocess.run", fake_run)

    provider = ClaudeCodeCLIProvider(LLMConfig(provider=LLMProviderName.CLAUDE_CODE_CLI, claude_path="tools/claude"))

    assert provider.review_context_pack(make_pack(), repo) == []


def test_claude_result_text_reports_structured_output_retry_failure() -> None:
    stdout = json.dumps(
        {
            "type": "result",
            "subtype": "error_max_structured_output_retries",
            "result": "Could not produce valid output.",
        }
    )

    with pytest.raises(LLMProviderError, match="error_max_structured_output_retries"):
        claude_result_text(stdout)


def test_claude_provider_verifies_findings_in_one_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    claude = repo / "tools" / "claude"
    claude.parent.mkdir(parents=True)
    claude.write_text("#!/bin/sh\n", encoding="utf-8")
    first = Finding(
        title="Cart totals ignore item quantity",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )
    second = first.model_copy(update={"title": "Speculative duplicate"})
    seen_inputs: list[str] = []

    def fake_run(
        command: list[str],
        cwd: Path,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        seen_inputs.append(input)
        stdout = json.dumps(
            {
                "result": {
                    "decisions": [
                        {"finding_index": 0, "approved": True, "confidence": "high", "reason": "Concrete."},
                        {"finding_index": 1, "approved": False, "confidence": "high", "reason": "Too weak."},
                    ]
                }
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("apex_ray.llm.providers.subprocess.run", fake_run)

    provider = ClaudeCodeCLIProvider(LLMConfig(provider=LLMProviderName.CLAUDE_CODE_CLI, claude_path="tools/claude"))
    verifications = provider.verify_findings([first, second], make_pack(), repo)

    assert len(seen_inputs) == 1
    assert "Candidate findings JSON" in seen_inputs[0]
    assert [verification.approved for verification in verifications] == [True, False]


def test_parse_finding_response_fills_context_pack_id() -> None:
    response = parse_finding_response(
        """
        {
          "findings": [
            {
              "title": "Missing deny case",
              "severity": "medium",
              "confidence": "high",
              "file": "src/auth.ts",
              "line": 12,
              "failure_mode": "Unauthorized users may be allowed.",
              "evidence": "The changed branch returns true for admin-like roles.",
              "suggested_fix": "Check explicit permissions.",
              "suggested_test": "Add a denied role test."
            }
          ]
        }
        """,
        "pack-1",
    )

    assert response.findings[0].context_pack_id == "pack-1"


def test_parse_finding_response_wraps_invalid_json_as_provider_error() -> None:
    with pytest.raises(LLMProviderError, match="LLM finding response contained invalid JSON"):
        parse_finding_response("{findings: []}", "pack-1")


def test_finding_response_schema_is_strict() -> None:
    schema = finding_response_schema()
    item_schema = schema["properties"]["findings"]["items"]

    assert schema["additionalProperties"] is False
    assert item_schema["additionalProperties"] is False
    assert "context_pack_id" in item_schema["required"]


def test_verification_response_schema_is_strict() -> None:
    schema = verification_response_schema()

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["approved", "confidence", "reason"]


def test_verification_batch_response_schema_is_strict() -> None:
    schema = verification_batch_response_schema()
    item_schema = schema["properties"]["decisions"]["items"]

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["decisions"]
    assert item_schema["additionalProperties"] is False
    assert item_schema["required"] == ["finding_index", "approved", "confidence", "reason"]


def test_parse_verification_response() -> None:
    finding = Finding(
        title="Cart totals ignore item quantity",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="pack-1",
    )

    verification = parse_verification_response(
        '{"approved": true, "confidence": "high", "reason": "Diff removes quantity multiplication."}',
        finding,
    )

    assert verification.approved is True
    assert verification.finding.title == finding.title


def test_parse_verification_batch_response_maps_decisions_by_index() -> None:
    first = Finding(
        title="First issue",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="pack-1",
    )
    second = first.model_copy(update={"title": "Second issue"})

    verifications = parse_verification_batch_response(
        json.dumps(
            {
                "decisions": [
                    {"finding_index": 1, "approved": False, "confidence": "high", "reason": "Too weak."},
                    {"finding_index": 0, "approved": True, "confidence": "high", "reason": "Concrete."},
                ]
            }
        ),
        [first, second],
    )

    assert [verification.finding.title for verification in verifications] == ["First issue", "Second issue"]
    assert [verification.approved for verification in verifications] == [True, False]


def test_review_prompt_contains_context_pack() -> None:
    pack = make_pack().model_copy(
        update={
            "rules": ["[custom-rule:cart-total] Preserve cart totals\nCart total changes must preserve quantity."],
            "rule_matches": [
                RuleMatch(
                    id="cart-total",
                    title="Preserve cart totals",
                    severity=FindingSeverity.HIGH,
                    mode="strict",
                )
            ],
        }
    )
    prompt = build_review_prompt(pack)

    assert "calculateTotal" in prompt
    assert "Language hint: TypeScript/JavaScript." in prompt
    assert "NestJS decorators/modules/providers/guards" in prompt
    assert "impact_notes only as navigation hints" in prompt
    assert "contract_snippets show schemas" in prompt
    assert "metadata_snippets show framework boundaries" in prompt
    assert "pre-auth versus post-auth state guards" in prompt
    assert "stale credential reuse" in prompt
    assert "external API/JWT/webhook payload shape guards" in prompt
    assert "array guards before array methods" in prompt
    assert "PII or raw upstream object pass-through" in prompt
    assert "Report independent issues separately" in prompt
    assert "strict rules as domain invariants" in prompt
    assert "Preserve cart totals" in prompt
    assert "Prefer an empty findings array over weak" in prompt
    assert "Reference impact" in prompt
    assert "Return only JSON" in prompt


def test_review_prompt_is_language_neutral_and_python_aware() -> None:
    prompt = build_review_prompt(make_python_pack())

    assert "Review exactly one context pack from a code diff." in prompt
    assert "TypeScript/JavaScript diff" not in prompt
    assert "Language hint: Python." in prompt
    assert "FastAPI route/dependency/auth boundaries" in prompt
    assert "Pydantic model/settings/validator/schema changes" in prompt
    assert "SQLAlchemy session/transaction/commit/rollback boundaries" in prompt
    assert "Alembic migrations" in prompt
    assert "pytest/unittest fixture" in prompt


def test_shallow_review_prompt_is_language_neutral_and_python_aware() -> None:
    prompt = build_shallow_review_prompt(make_python_pack())

    assert "compact code context pack from a diff" in prompt
    assert "TypeScript/JavaScript context pack" not in prompt
    assert "Language hint: Python." in prompt
    assert "Python boundary risks" in prompt
    assert "FastAPI/Pydantic/SQLAlchemy/Alembic" in prompt


@pytest.mark.parametrize("file", ["src/module.mts", "src/module.cts"])
def test_review_prompt_uses_typescript_guidance_for_modern_module_suffixes(file: str) -> None:
    prompt = build_review_prompt(make_unknown_language_pack(file))

    assert "Language hint: TypeScript/JavaScript." in prompt
    assert "Language hint: unknown." not in prompt


@pytest.mark.parametrize("file", ["src/types.pyi", "src/module.lua"])
def test_review_prompt_uses_unknown_guidance_for_unclassified_suffixes(file: str) -> None:
    prompt = build_review_prompt(make_unknown_language_pack(file))

    assert "Language hint: unknown." in prompt
    assert "fallback or unknown-language packs" in prompt
    assert "Language hint: Python." not in prompt
    assert "Language hint: TypeScript/JavaScript." not in prompt


def test_verifier_prompt_contains_finding_and_context() -> None:
    finding = Finding(
        title="Cart totals ignore item quantity",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )

    prompt = build_verifier_prompt(finding, make_pack())

    assert "Cart totals ignore item quantity" in prompt
    assert "calculateTotal" in prompt
    assert "Approve only if" in prompt
    assert "violations of supplied strict project rules" in prompt
    assert "not tied to a supplied strict rule" in prompt
    assert "Treat impact_notes as navigation hints only" in prompt
    assert "context_pack_id differs" in prompt
    assert "finding.file is not present in any supplied context layer" in prompt
    assert "contracts show schemas" in prompt
    assert "metadata shows framework" in prompt
    assert "Language hint: TypeScript/JavaScript." in prompt
    assert "TypeScript/JavaScript-specific findings" in prompt
    assert "NestJS/DTO/schema/DI/provider/route/cache boundaries" in prompt
    assert "project-specific review criteria" in prompt
    assert "concretely show a violation" in prompt


def test_verifier_prompt_is_language_neutral_and_python_aware() -> None:
    finding = Finding(
        title="Resource validation bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/routes.py",
        line=14,
        failure_mode="The route can persist invalid resource data.",
        evidence="The changed code bypasses ResourceCreate validation.",
        suggested_fix="Validate through the request model before persistence.",
        suggested_test="Add a route test for invalid payloads.",
        context_pack_id="src/routes.py#create_resource:1",
    )

    prompt = build_verifier_prompt(finding, make_python_pack())

    assert "Language hint: Python." in prompt
    assert "TypeScript/JavaScript diff" not in prompt
    assert "Python-specific findings" in prompt
    assert "FastAPI/Pydantic/SQLAlchemy/Alembic/pytest" in prompt


def test_verifier_batch_prompt_contains_findings_and_context() -> None:
    finding = Finding(
        title="Cart totals ignore item quantity",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=7,
        failure_mode="Totals are undercounted.",
        evidence="Quantity is removed.",
        suggested_fix="Multiply by quantity.",
        suggested_test="Add quantity > 1 test.",
        context_pack_id="src/cart.ts#calculateTotal:1",
    )

    prompt = build_verifier_batch_prompt([finding], make_pack())

    assert "batched verification pass" in prompt
    assert "finding_index" in prompt
    assert "Candidate findings JSON" in prompt
    assert "Cart totals ignore item quantity" in prompt
    assert "calculateTotal" in prompt
    assert "Language hint: TypeScript/JavaScript." in prompt
    assert "TypeScript/JavaScript-specific findings" in prompt
    assert "NestJS/DTO/schema/DI/provider/route/cache boundaries" in prompt
    assert "Evaluate each candidate independently" in prompt


def test_verifier_batch_prompt_is_language_neutral_and_python_aware() -> None:
    finding = Finding(
        title="Resource validation bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/routes.py",
        line=14,
        failure_mode="The route can persist invalid resource data.",
        evidence="The changed code bypasses ResourceCreate validation.",
        suggested_fix="Validate through the request model before persistence.",
        suggested_test="Add a route test for invalid payloads.",
        context_pack_id="src/routes.py#create_resource:1",
    )

    prompt = build_verifier_batch_prompt([finding], make_python_pack())

    assert "Language hint: Python." in prompt
    assert "Python-specific findings" in prompt
    assert "FastAPI/Pydantic/SQLAlchemy/Alembic/pytest" in prompt
