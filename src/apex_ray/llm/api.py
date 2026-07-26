from __future__ import annotations

import os
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from apex_ray.llm.errors import LLMProviderError, LLMProviderErrorCategory
from apex_ray.llm.http import (
    JSONHTTPResponse,
    JSONTransport,
    JSONTransportError,
    UrllibJSONTransport,
    validate_api_endpoint_url,
)
from apex_ray.llm.prompts import (
    build_resolution_prompt,
    build_review_prompt,
    build_shallow_review_prompt,
    build_verifier_batch_prompt,
)
from apex_ray.llm.responses import (
    finding_response_schema,
    parse_finding_response,
    parse_resolution_response,
    parse_verification_batch_response,
    resolution_response_schema,
    verification_batch_response_schema,
)
from apex_ray.llm.usage import parse_api_usage
from apex_ray.models import (
    ContextPack,
    Finding,
    FindingResolution,
    FindingVerification,
    LLMAPIProtocol,
    LLMConfig,
    LLMProviderName,
    LLMReasoningEffort,
    LLMReviewResult,
    LLMStructuredOutput,
    LLMUsage,
    LLMVerificationResult,
    ReviewReport,
)


@dataclass(frozen=True)
class _ProviderPreset:
    protocol: LLMAPIProtocol
    structured_output: LLMStructuredOutput
    base_url: str
    api_key_env: str
    allowed_hosts: tuple[str, ...]
    allowed_host_suffixes: tuple[str, ...] = ()


_PRESETS = {
    LLMProviderName.OPENAI_API: _ProviderPreset(
        protocol=LLMAPIProtocol.OPENAI_RESPONSES,
        structured_output=LLMStructuredOutput.JSON_SCHEMA,
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        allowed_hosts=("api.openai.com",),
    ),
    LLMProviderName.ANTHROPIC_API: _ProviderPreset(
        protocol=LLMAPIProtocol.ANTHROPIC_MESSAGES,
        structured_output=LLMStructuredOutput.JSON_SCHEMA,
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        allowed_hosts=("api.anthropic.com",),
    ),
    LLMProviderName.DEEPSEEK_API: _ProviderPreset(
        protocol=LLMAPIProtocol.OPENAI_CHAT,
        structured_output=LLMStructuredOutput.JSON_OBJECT,
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        allowed_hosts=("api.deepseek.com",),
    ),
    LLMProviderName.QWEN_API: _ProviderPreset(
        protocol=LLMAPIProtocol.OPENAI_CHAT,
        structured_output=LLMStructuredOutput.JSON_OBJECT,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        allowed_hosts=(
            "dashscope.aliyuncs.com",
            "dashscope-intl.aliyuncs.com",
            "dashscope-us.aliyuncs.com",
        ),
        allowed_host_suffixes=(
            ".dashscope.aliyuncs.com",
            ".maas.aliyuncs.com",
        ),
    ),
    LLMProviderName.KIMI_API: _ProviderPreset(
        protocol=LLMAPIProtocol.OPENAI_CHAT,
        structured_output=LLMStructuredOutput.JSON_SCHEMA,
        base_url="https://api.moonshot.ai/v1",
        api_key_env="MOONSHOT_API_KEY",
        allowed_hosts=("api.moonshot.ai", "api.moonshot.cn"),
    ),
    LLMProviderName.ZAI_API: _ProviderPreset(
        protocol=LLMAPIProtocol.OPENAI_CHAT,
        structured_output=LLMStructuredOutput.JSON_OBJECT,
        base_url="https://api.z.ai/api/paas/v4",
        api_key_env="ZAI_API_KEY",
        allowed_hosts=("api.z.ai", "open.bigmodel.cn"),
    ),
}

_ENDPOINTS = {
    LLMAPIProtocol.OPENAI_RESPONSES: "/responses",
    LLMAPIProtocol.ANTHROPIC_MESSAGES: "/v1/messages",
    LLMAPIProtocol.OPENAI_CHAT: "/chat/completions",
}
_RESERVED_HEADERS = {
    "accept",
    "authorization",
    "content-length",
    "content-type",
    "host",
    "proxy-authorization",
    "x-api-key",
}
_TRUTHY_ENV_VALUES = {"1", "on", "true", "yes"}


def api_key_environment_name(config: LLMConfig) -> str | None:
    preset = _PRESETS.get(config.provider)
    return config.api.api_key_env or (preset.api_key_env if preset is not None else None)


class APILLMProvider:
    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: JSONTransport | None = None,
        environment: Mapping[str, str] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self.config = config
        self.transport = transport if transport is not None else UrllibJSONTransport()
        self.environment = dict(os.environ if environment is None else environment)
        self.sleep_fn = sleep_fn
        self.random_fn = random_fn
        self.preset = _PRESETS.get(config.provider)

        if config.provider != LLMProviderName.OPENAI_COMPATIBLE and self.preset is None:
            raise LLMProviderError(f"Unsupported API provider: {config.provider}")
        if not config.model or not config.model.strip():
            raise LLMProviderError(f"{config.provider} requires an explicit model.")
        self.model = config.model.strip()

        if self.preset is None:
            self.protocol = self._required_custom_protocol()
            self.structured_output = self._required_custom_structured_output()
        else:
            self.protocol = config.api.protocol or self.preset.protocol
            self.structured_output = config.api.structured_output or self.preset.structured_output

        self.base_url = self._resolve_base_url()
        self.url = _append_endpoint(self.base_url, _ENDPOINTS[self.protocol])
        self.api_key = self._resolve_api_key()
        self.headers, header_secrets = self._resolve_headers()
        self._secrets = [self.api_key, *header_secrets]

    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
        return self.review_context_pack_with_usage(pack, repo_root).findings

    def review_context_pack_with_usage(self, pack: ContextPack, repo_root: Path) -> LLMReviewResult:
        prompt = (
            build_shallow_review_prompt(pack) if self.config.review_depth == "shallow" else build_review_prompt(pack)
        )
        response_text, usage = self._complete(
            prompt=prompt,
            schema=finding_response_schema(),
            schema_name="apex_ray_finding_response",
        )
        try:
            response = parse_finding_response(response_text, pack.id)
        except LLMProviderError as exc:
            raise _malformed_response_error(exc, self._secrets) from exc
        return LLMReviewResult(findings=response.findings, usage=usage)

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
        return self.verify_findings([finding], pack, repo_root)[0]

    def verify_findings(self, findings: list[Finding], pack: ContextPack, repo_root: Path) -> list[FindingVerification]:
        return self.verify_findings_with_usage(findings, pack, repo_root).verifications

    def verify_findings_with_usage(
        self,
        findings: list[Finding],
        pack: ContextPack,
        repo_root: Path,
    ) -> LLMVerificationResult:
        if not findings:
            return LLMVerificationResult()
        response_text, usage = self._complete(
            prompt=build_verifier_batch_prompt(findings, pack),
            schema=verification_batch_response_schema(),
            schema_name="apex_ray_verification_batch_response",
        )
        try:
            verifications = parse_verification_batch_response(response_text, findings)
        except LLMProviderError as exc:
            raise _malformed_response_error(exc, self._secrets) from exc
        return LLMVerificationResult(verifications=verifications, usage=usage)

    def resolve_finding(
        self,
        finding: Finding,
        previous_pack: ContextPack | None,
        delta_report: ReviewReport,
        repo_root: Path,
    ) -> FindingResolution:
        response_text, _usage = self._complete(
            prompt=build_resolution_prompt(finding, previous_pack, delta_report),
            schema=resolution_response_schema(),
            schema_name="apex_ray_resolution_response",
        )
        try:
            return parse_resolution_response(response_text, finding)
        except LLMProviderError as exc:
            raise _malformed_response_error(exc, self._secrets) from exc

    def _complete(
        self,
        *,
        prompt: str,
        schema: dict[str, object],
        schema_name: str,
    ) -> tuple[str, LLMUsage | None]:
        payload = self._request_payload(prompt=prompt, schema=schema, schema_name=schema_name)
        response = self._request_with_retries(payload)
        data = _as_mapping(response.data)
        if data is None:
            raise LLMProviderError("API response must be a JSON object.", category="malformed")
        response_text = _extract_response_text(data, self.protocol)
        usage_mapping = _as_mapping(data.get("usage"))
        usage = (
            parse_api_usage(
                usage_mapping,
                source=str(self.config.provider),
                protocol=self.protocol,
            )
            if usage_mapping is not None
            else None
        )
        return response_text, usage

    def _request_payload(
        self,
        *,
        prompt: str,
        schema: dict[str, object],
        schema_name: str,
    ) -> dict[str, object]:
        if self.protocol == LLMAPIProtocol.OPENAI_RESPONSES:
            payload: dict[str, object] = {
                "model": self.model,
                "input": prompt,
                "max_output_tokens": self.config.api.max_output_tokens,
            }
            if self.config.provider == LLMProviderName.OPENAI_API:
                payload["store"] = False
            payload.update(self._openai_responses_reasoning())
            response_format = self._openai_responses_format(schema, schema_name)
            if response_format is not None:
                payload["text"] = {"format": response_format}
            return payload

        if self.protocol == LLMAPIProtocol.ANTHROPIC_MESSAGES:
            payload = {
                "model": self.model,
                "max_tokens": self.config.api.max_output_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            output_config: dict[str, object] = {}
            effort = self._anthropic_effort()
            if effort is not None:
                output_config["effort"] = effort
                payload["thinking"] = {"type": "adaptive"}
            if self.structured_output == LLMStructuredOutput.JSON_SCHEMA:
                output_config["format"] = {"type": "json_schema", "schema": schema}
            elif self.structured_output == LLMStructuredOutput.JSON_OBJECT:
                output_config["format"] = {
                    "type": "json_schema",
                    "schema": {"type": "object"},
                }
            if output_config:
                payload["output_config"] = output_config
            return payload

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config.api.max_output_tokens,
        }
        response_format = self._openai_chat_format(schema, schema_name)
        if response_format is not None:
            payload["response_format"] = response_format
        payload.update(self._openai_chat_reasoning())
        return payload

    def _request_with_retries(self, payload: Mapping[str, object]) -> JSONHTTPResponse:
        max_attempts = self.config.api.max_retries + 1
        for attempt in range(max_attempts):
            try:
                response = self.transport.request(
                    url=self.url,
                    headers=self.headers,
                    payload=payload,
                    timeout_seconds=self.config.timeout_seconds,
                    use_system_proxy=self.config.api.use_system_proxy,
                )
            except JSONTransportError as exc:
                provider_error = self._transport_error(exc)
            except (TimeoutError, OSError) as exc:
                provider_error = LLMProviderError(
                    "API request timed out." if isinstance(exc, TimeoutError) else "API network request failed.",
                    category="timeout" if isinstance(exc, TimeoutError) else "provider",
                    retryable=True,
                )
            else:
                response_data = _as_mapping(response.data)
                if 200 <= response.status_code < 300 and not (
                    response_data is not None and response_data.get("error") is not None
                ):
                    return response
                provider_error = self._http_error(response)

            if not provider_error.retryable or attempt + 1 >= max_attempts:
                raise provider_error
            self.sleep_fn(self._retry_delay(provider_error, attempt))

        raise AssertionError("unreachable")

    def _transport_error(self, error: JSONTransportError) -> LLMProviderError:
        if error.kind == "malformed":
            return LLMProviderError(str(error), category="malformed")
        if error.kind == "timeout":
            return LLMProviderError("API request timed out.", category="timeout", retryable=True)
        return LLMProviderError("API network request failed.", category="provider", retryable=True)

    def _http_error(self, response: JSONHTTPResponse) -> LLMProviderError:
        status = response.status_code
        error = _as_mapping(response.data)
        nested = _as_mapping(error.get("error")) if error is not None else None
        details = nested or error or {}
        code = " ".join(
            str(details.get(key, "")) for key in ("type", "code", "status", "message") if details.get(key) is not None
        ).lower()
        retry_after = _parse_retry_after(response.headers)

        category: LLMProviderErrorCategory
        if status in {401, 403} or any(
            token in code for token in ("authentication", "invalid_api_key", "unauthorized")
        ):
            category = "auth"
            retryable = False
        elif any(
            token in code
            for token in (
                "arrearage",
                "balance",
                "billing",
                "insufficient_quota",
                "quota",
            )
        ):
            category = "quota"
            retryable = False
        elif status == 429 or any(token in code for token in ("overloaded", "rate_limit", "rate limit")):
            category = "rate_limit"
            retryable = True
        elif status in {408, 504, 524}:
            category = "timeout"
            retryable = True
        else:
            category = "provider"
            retryable = status in {409, 425, 529} or status >= 500

        external_message = details.get("message")
        detail = str(external_message) if isinstance(external_message, str) else "The provider rejected the request."
        detail = _redact(detail, self._secrets)
        return LLMProviderError(
            f"API request failed with HTTP {status}: {detail}",
            category=category,
            retryable=retryable,
            status_code=status,
            retry_after=retry_after,
        )

    def _retry_delay(self, error: LLMProviderError, attempt: int) -> float:
        maximum = self.config.api.retry_max_seconds
        if error.retry_after is not None:
            return min(max(error.retry_after, 0.0), maximum)
        exponential = min(self.config.api.retry_backoff_seconds * (2**attempt), maximum)
        jitter = min(max(self.random_fn(), 0.0), 1.0)
        return exponential * jitter

    def _required_custom_protocol(self) -> LLMAPIProtocol:
        if self.config.api.protocol is None:
            raise LLMProviderError("openai_compatible requires api.protocol.")
        return self.config.api.protocol

    def _required_custom_structured_output(self) -> LLMStructuredOutput:
        if self.config.api.structured_output is None:
            raise LLMProviderError("openai_compatible requires api.structured_output.")
        return self.config.api.structured_output

    def _resolve_base_url(self) -> str:
        api = self.config.api
        if api.base_url_env:
            base_url = self.environment.get(api.base_url_env, "").strip()
            if not base_url:
                raise LLMProviderError(f"API endpoint environment variable {api.base_url_env} is not set.")
        elif api.base_url:
            base_url = api.base_url.strip()
        elif self.preset is not None:
            base_url = self.preset.base_url
        else:
            raise LLMProviderError("openai_compatible requires api.base_url or api.base_url_env.")

        host = _validated_endpoint_host(base_url)
        if self.preset is not None and not (
            host in self.preset.allowed_hosts
            or any(host.endswith(suffix) for suffix in self.preset.allowed_host_suffixes)
        ):
            raise LLMProviderError(f"API endpoint host {host!r} is not allowed for {self.config.provider}.")

        if self.preset is None and _is_ci(self.environment):
            if not api.base_url_env:
                raise LLMProviderError(
                    "CI custom API endpoints must be supplied through api.base_url_env, not literal api.base_url."
                )
            allowed_hosts = {
                item.lower().rstrip(".")
                for item in re.split(r"[\s,]+", self.environment.get(api.allowed_hosts_env, ""))
                if item
            }
            if host not in allowed_hosts:
                raise LLMProviderError(
                    f"Custom API endpoint host {host!r} is not present in the CI-controlled allowlist "
                    f"{api.allowed_hosts_env}."
                )
        return base_url.rstrip("/")

    def _resolve_api_key(self) -> str:
        key_env = api_key_environment_name(self.config)
        if key_env is None:
            raise LLMProviderError("openai_compatible requires api.api_key_env.")
        api_key = self.environment.get(key_env, "")
        if not api_key.strip():
            raise LLMProviderError(f"API key environment variable {key_env} is not set.")
        _validate_header_value(api_key, "API key")
        return api_key

    def _resolve_headers(self) -> tuple[dict[str, str], list[str]]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.protocol == LLMAPIProtocol.ANTHROPIC_MESSAGES:
            api_version = self.config.api.api_version or "2023-06-01"
            _validate_header_value(api_version, "Anthropic API version")
            headers["anthropic-version"] = api_version
            headers["x-api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"

        secrets: list[str] = []
        for header, env_name in self.config.api.headers_from_env.items():
            if header.lower() in _RESERVED_HEADERS:
                raise LLMProviderError(f"API header {header!r} is reserved and cannot be configured.")
            value = self.environment.get(env_name, "")
            if not value:
                raise LLMProviderError(f"API header environment variable {env_name} is not set.")
            _validate_header_value(value, f"API header {header!r}")
            headers[header] = value
            secrets.append(value)
        return headers, secrets

    def _openai_responses_format(
        self,
        schema: dict[str, object],
        schema_name: str,
    ) -> dict[str, object] | None:
        if self.structured_output == LLMStructuredOutput.PROMPT_ONLY:
            return None
        if self.structured_output == LLMStructuredOutput.JSON_OBJECT:
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "name": schema_name,
            "strict": True,
            "schema": schema,
        }

    def _openai_chat_format(
        self,
        schema: dict[str, object],
        schema_name: str,
    ) -> dict[str, object] | None:
        if self.structured_output == LLMStructuredOutput.PROMPT_ONLY:
            return None
        if self.structured_output == LLMStructuredOutput.JSON_OBJECT:
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        }

    def _openai_responses_reasoning(self) -> dict[str, object]:
        effort = self.config.effort
        if effort is None:
            return {}
        return {"reasoning": {"effort": str(effort)}}

    def _anthropic_effort(self) -> str | None:
        effort = self.config.effort
        if effort is None or effort == LLMReasoningEffort.NONE:
            return None
        if effort == LLMReasoningEffort.MINIMAL:
            return str(LLMReasoningEffort.LOW)
        return str(effort)

    def _openai_chat_reasoning(self) -> dict[str, object]:
        effort = self.config.effort
        if effort is None:
            return {}
        if self.config.provider == LLMProviderName.DEEPSEEK_API:
            if effort == LLMReasoningEffort.NONE:
                return {"thinking": {"type": "disabled"}}
            mapped = "max" if effort in {LLMReasoningEffort.XHIGH, LLMReasoningEffort.MAX} else "high"
            return {"thinking": {"type": "enabled"}, "reasoning_effort": mapped}
        if self.config.provider == LLMProviderName.QWEN_API:
            return {"enable_thinking": effort != LLMReasoningEffort.NONE}
        if self.config.provider == LLMProviderName.KIMI_API:
            if self.model.lower().startswith("kimi-k3"):
                return {"reasoning_effort": _map_kimi_reasoning_effort(effort)}
            state = "disabled" if effort == LLMReasoningEffort.NONE else "enabled"
            return {"thinking": {"type": state}}
        if self.config.provider == LLMProviderName.ZAI_API:
            if effort == LLMReasoningEffort.NONE:
                return {"thinking": {"type": "disabled"}}
            result: dict[str, object] = {"thinking": {"type": "enabled"}}
            if self.model.lower().startswith("glm-5.2"):
                result["reasoning_effort"] = str(effort)
            return result
        if effort == LLMReasoningEffort.NONE:
            return {}
        return {"reasoning_effort": str(effort)}


def _extract_response_text(data: Mapping[str, object], protocol: LLMAPIProtocol) -> str:
    if protocol == LLMAPIProtocol.OPENAI_RESPONSES:
        return _extract_openai_responses_text(data)
    if protocol == LLMAPIProtocol.ANTHROPIC_MESSAGES:
        return _extract_anthropic_text(data)
    return _extract_openai_chat_text(data)


def _extract_openai_responses_text(data: Mapping[str, object]) -> str:
    status = data.get("status")
    if status == "incomplete":
        incomplete_details = _as_mapping(data.get("incomplete_details"))
        reason = str(incomplete_details.get("reason", "")) if incomplete_details is not None else ""
        if reason in {"content_filter", "safety"}:
            raise LLMProviderError("API provider refused the request.", category="refusal")
        raise LLMProviderError("API response was truncated before completion.", category="truncated")
    if status == "failed":
        raise LLMProviderError("API provider reported a failed response.")
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    text_parts: list[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            item_mapping = _as_mapping(item)
            if item_mapping is None:
                continue
            if item_mapping.get("type") == "refusal":
                raise LLMProviderError("API provider refused the request.", category="refusal")
            content = item_mapping.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                block_mapping = _as_mapping(block)
                if block_mapping is None:
                    continue
                if block_mapping.get("type") == "refusal" or block_mapping.get("refusal"):
                    raise LLMProviderError("API provider refused the request.", category="refusal")
                text = block_mapping.get("text")
                if block_mapping.get("type") in {"output_text", "text"} and isinstance(text, str):
                    text_parts.append(text)
    if not text_parts:
        raise LLMProviderError("API response did not contain output text.", category="malformed")
    return "".join(text_parts)


def _extract_anthropic_text(data: Mapping[str, object]) -> str:
    stop_reason = data.get("stop_reason")
    if stop_reason == "max_tokens":
        raise LLMProviderError("API response was truncated at max_tokens.", category="truncated")
    if stop_reason == "refusal":
        raise LLMProviderError("API provider refused the request.", category="refusal")

    text_parts: list[str] = []
    content = data.get("content")
    if isinstance(content, list):
        for block in content:
            block_mapping = _as_mapping(block)
            if block_mapping is None:
                continue
            if block_mapping.get("type") == "refusal":
                raise LLMProviderError("API provider refused the request.", category="refusal")
            text = block_mapping.get("text")
            if block_mapping.get("type") == "text" and isinstance(text, str):
                text_parts.append(text)
    if not text_parts:
        raise LLMProviderError("API response did not contain text content.", category="malformed")
    return "".join(text_parts)


def _extract_openai_chat_text(data: Mapping[str, object]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMProviderError("API response did not contain a choice.", category="malformed")
    choice = _as_mapping(choices[0])
    if choice is None:
        raise LLMProviderError("API response choice was malformed.", category="malformed")
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise LLMProviderError("API response was truncated at max_tokens.", category="truncated")
    if finish_reason == "content_filter":
        raise LLMProviderError("API provider refused the request.", category="refusal")

    message = _as_mapping(choice.get("message"))
    if message is None:
        raise LLMProviderError("API response did not contain a message.", category="malformed")
    if message.get("refusal"):
        raise LLMProviderError("API provider refused the request.", category="refusal")
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        text_parts = []
        for block in content:
            block_mapping = _as_mapping(block)
            if block_mapping is None:
                continue
            text = block_mapping.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        if text_parts:
            return "".join(text_parts)
    raise LLMProviderError("API response did not contain message content.", category="malformed")


def _malformed_response_error(error: LLMProviderError, secrets: list[str]) -> LLMProviderError:
    return LLMProviderError(_redact(str(error), secrets), category="malformed")


def _map_kimi_reasoning_effort(effort: LLMReasoningEffort) -> str:
    return {
        LLMReasoningEffort.NONE: str(LLMReasoningEffort.LOW),
        LLMReasoningEffort.MINIMAL: str(LLMReasoningEffort.LOW),
        LLMReasoningEffort.LOW: str(LLMReasoningEffort.LOW),
        LLMReasoningEffort.MEDIUM: str(LLMReasoningEffort.HIGH),
        LLMReasoningEffort.HIGH: str(LLMReasoningEffort.HIGH),
        LLMReasoningEffort.XHIGH: str(LLMReasoningEffort.MAX),
        LLMReasoningEffort.MAX: str(LLMReasoningEffort.MAX),
    }[effort]


def _append_endpoint(base_url: str, endpoint: str) -> str:
    parsed = urlsplit(base_url)
    base_parts = [part for part in parsed.path.split("/") if part]
    endpoint_parts = [part for part in endpoint.split("/") if part]
    overlap = 0
    for candidate in range(min(len(base_parts), len(endpoint_parts)), 0, -1):
        if base_parts[-candidate:] == endpoint_parts[:candidate]:
            overlap = candidate
            break
    path = "/" + "/".join([*base_parts, *endpoint_parts[overlap:]])
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _validated_endpoint_host(url: str) -> str:
    try:
        return validate_api_endpoint_url(url)
    except ValueError as exc:
        raise LLMProviderError(str(exc), category="malformed") from exc


def _is_ci(environment: Mapping[str, str]) -> bool:
    return any(environment.get(name, "").strip().lower() in _TRUTHY_ENV_VALUES for name in ("CI", "GITHUB_ACTIONS"))


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    value = next((value for name, value in headers.items() if name.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(seconds, 0.0) if isfinite(seconds) else None
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except TypeError, ValueError:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)


def _redact(message: str, secrets: list[str]) -> str:
    redacted = message
    for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"\bsk-[A-Za-z0-9._-]+\b", "[REDACTED]", redacted)
    redacted = "".join(character if character >= " " else " " for character in redacted)
    return redacted[:500]


def _validate_header_value(value: str, label: str) -> None:
    if "\r" in value or "\n" in value:
        raise LLMProviderError(f"{label} must not contain newline characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LLMProviderError(f"{label} must not contain control characters.")


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, dict):
        return value
    return None
