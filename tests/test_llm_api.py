from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPException, HTTPMessage, IncompleteRead
from io import BytesIO
from pathlib import Path
from typing import Never, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

import apex_ray.llm.http as http_module
from apex_ray.llm.api import APILLMProvider
from apex_ray.llm.errors import LLMProviderError, classify_llm_provider_error
from apex_ray.llm.http import JSONHTTPResponse, JSONTransportError, UrllibJSONTransport
from apex_ray.llm.providers import provider_from_config
from apex_ray.llm.responses import (
    finding_response_schema,
    resolution_response_schema,
    verification_batch_response_schema,
)
from apex_ray.llm.routing import review_config_for_pack
from apex_ray.llm.usage import estimate_provider_input_tokens, estimate_tokens, parse_api_usage
from apex_ray.models import (
    ContextPack,
    DiffSummary,
    Finding,
    FindingConfidence,
    FindingSeverity,
    LLMAPIConfig,
    LLMAPIProtocol,
    LLMConfig,
    LLMProfile,
    LLMProviderName,
    LLMReasoningEffort,
    LLMStructuredOutput,
    ReviewReport,
    TargetMode,
)


@dataclass(frozen=True)
class TransportCall:
    url: str
    headers: Mapping[str, str]
    payload: Mapping[str, object]
    timeout_seconds: float
    use_system_proxy: bool


class StubTransport:
    def __init__(self, *results: JSONHTTPResponse | Exception) -> None:
        self.results = list(results)
        self.calls: list[TransportCall] = []

    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
        use_system_proxy: bool,
    ) -> JSONHTTPResponse:
        self.calls.append(
            TransportCall(
                url=url,
                headers=dict(headers),
                payload=dict(payload),
                timeout_seconds=timeout_seconds,
                use_system_proxy=use_system_proxy,
            )
        )
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class StubHTTPHandle:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.headers: dict[str, str] = {}

    def __enter__(self) -> StubHTTPHandle:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        return self.body[:n] if n >= 0 else self.body


class StubOpener:
    def __init__(self, response: StubHTTPHandle) -> None:
        self.response = response
        self.request: Request | None = None
        self.timeout: float | None = None

    def open(self, request: Request, timeout: float) -> StubHTTPHandle:
        self.request = request
        self.timeout = timeout
        return self.response


class StubHTTPErrorBody(BytesIO):
    def __init__(
        self,
        body: bytes = b'{"error":{"message":"unavailable"}}',
        *,
        read_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        super().__init__(body)
        self.read_error = read_error
        self.close_error = close_error

    def read(self, n: int | None = -1) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return super().read(n)

    def close(self) -> None:
        if self.close_error is not None:
            error = self.close_error
            self.close_error = None
            super().close()
            raise error
        super().close()


class StubHTTPErrorOpener:
    def __init__(self, body: StubHTTPErrorBody) -> None:
        self.body = body

    def open(self, request: Request, timeout: float) -> StubHTTPHandle:
        raise HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            HTTPMessage(),
            self.body,
        )


def make_pack() -> ContextPack:
    return ContextPack(id="src/cart.ts#calculate:1", file="src/cart.ts", changed_lines=[(4, 5)])


def make_finding() -> Finding:
    return Finding(
        title="Quantity is ignored",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=5,
        failure_mode="Cart totals are undercounted.",
        evidence="The changed expression no longer multiplies by quantity.",
        suggested_fix="Restore the multiplication.",
        suggested_test="Cover a quantity greater than one.",
        context_pack_id="src/cart.ts#calculate:1",
    )


def success_response(
    content: str = '{"findings":[]}',
    *,
    usage: Mapping[str, object] | None = None,
) -> JSONHTTPResponse:
    return JSONHTTPResponse(
        status_code=200,
        headers={},
        data={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": dict(usage or {}),
        },
    )


def responses_success_response(content: str = '{"findings":[]}') -> JSONHTTPResponse:
    return JSONHTTPResponse(
        status_code=200,
        headers={},
        data={"status": "completed", "output_text": content, "usage": {}},
    )


def provider(
    config: LLMConfig,
    transport: StubTransport,
    environment: Mapping[str, str],
    *,
    sleeps: list[float] | None = None,
) -> APILLMProvider:
    sleep_calls = sleeps if sleeps is not None else []
    return APILLMProvider(
        config,
        transport=transport,
        environment=environment,
        sleep_fn=sleep_calls.append,
        random_fn=lambda: 1.0,
    )


def test_stdlib_transport_serializes_json_and_disables_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = StubOpener(StubHTTPHandle(b'{"ok":true}'))
    installed_handlers: list[object] = []

    def fake_build_opener(*handlers: object) -> StubOpener:
        installed_handlers.extend(handlers)
        return opener

    monkeypatch.setattr(http_module, "build_opener", fake_build_opener)

    response = UrllibJSONTransport().request(
        url="https://api.example/v1/review",
        headers={
            "Authorization": "Bearer secret",
            "Accept-Encoding": "gzip",
        },
        payload={"model": "model", "input": "review"},
        timeout_seconds=7,
        use_system_proxy=False,
    )

    assert response.data == {"ok": True}
    assert opener.request is not None
    assert json.loads(cast(bytes, opener.request.data)) == {"model": "model", "input": "review"}
    assert opener.request.get_header("Accept-encoding") == "identity"
    assert opener.timeout == 7
    assert {type(handler).__name__ for handler in installed_handlers} == {
        "ProxyHandler",
        "_NoRedirectHandler",
    }


@pytest.mark.parametrize(
    ("url", "message"),
    [
        pytest.param("http://models.example.test/v1/review", "HTTPS", id="remote-http"),
        pytest.param("https://user:password@api.example/v1/review", "credentials", id="userinfo"),
        pytest.param("https:///v1/review", "host", id="missing-host"),
        pytest.param("https://api.example:invalid/v1/review", "port", id="invalid-port"),
        pytest.param("https://[not-an-ipv6]/v1/review", "host", id="invalid-ipv6"),
        pytest.param("https://api%2f.example/v1/review", "host", id="encoded-host-separator"),
        pytest.param("https://api.example/v1/review?key=value", "query or fragment", id="query"),
        pytest.param("https://api.example\\@attacker.example/v1/review", "credentials", id="ambiguous-host"),
        pytest.param(" https://api.example/v1/review", "path", id="leading-space"),
        pytest.param("https://api.example/v1 bad", "path", id="raw-path-space"),
        pytest.param("https://api.example/v1\\bad", "path", id="raw-path-backslash"),
        pytest.param("https://api.example/v1/%zz", "path", id="invalid-percent-escape"),
    ],
)
def test_stdlib_transport_rejects_unsafe_url_before_building_opener(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    message: str,
) -> None:
    opener_calls = 0

    def fail_if_built(*handlers: object) -> StubOpener:
        nonlocal opener_calls
        opener_calls += 1
        raise AssertionError("unsafe URL reached the network opener")

    monkeypatch.setattr(http_module, "build_opener", fail_if_built)

    with pytest.raises(JSONTransportError, match=message) as caught:
        UrllibJSONTransport().request(
            url=url,
            headers={"Authorization": "Bearer secret"},
            payload={},
            timeout_seconds=7,
            use_system_proxy=False,
        )

    assert caught.value.kind == "malformed"
    assert opener_calls == 0


def test_stdlib_transport_rejects_remote_http_before_reading_headers() -> None:
    class UnreadableHeaders(dict[str, str]):
        def items(self) -> Never:
            raise AssertionError("unsafe endpoint reached secret headers")

    with pytest.raises(JSONTransportError, match="HTTPS") as caught:
        UrllibJSONTransport().request(
            url="http://models.example.test/v1/review",
            headers=UnreadableHeaders(Authorization="Bearer secret"),
            payload={},
            timeout_seconds=7,
            use_system_proxy=False,
        )

    assert caught.value.kind == "malformed"


def test_stdlib_transport_normalizes_idn_host_before_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = StubOpener(StubHTTPHandle(b'{"ok":true}'))
    monkeypatch.setattr(http_module, "build_opener", lambda *handlers: opener)

    response = UrllibJSONTransport().request(
        url="https://例え.テスト/v1/review",
        headers={"Authorization": "Bearer secret"},
        payload={},
        timeout_seconds=7,
        use_system_proxy=False,
    )

    assert response.data == {"ok": True}
    assert opener.request is not None
    assert opener.request.full_url == "https://xn--r8jz45g.xn--zckzah/v1/review"


@pytest.mark.parametrize(
    "request_error",
    [
        pytest.param(ValueError("invalid request target"), id="value-error"),
        pytest.param(
            UnicodeEncodeError("ascii", "例", 0, 1, "invalid request target"),
            id="unicode-error",
        ),
    ],
)
def test_stdlib_transport_normalizes_request_construction_failures(
    monkeypatch: pytest.MonkeyPatch,
    request_error: Exception,
) -> None:
    opener_calls = 0

    def fail_request(*args: object, **kwargs: object) -> Request:
        raise request_error

    def fail_if_built(*handlers: object) -> StubOpener:
        nonlocal opener_calls
        opener_calls += 1
        raise AssertionError("malformed request reached the network opener")

    monkeypatch.setattr(http_module, "Request", fail_request)
    monkeypatch.setattr(http_module, "build_opener", fail_if_built)

    with pytest.raises(JSONTransportError, match="valid HTTP request") as caught:
        UrllibJSONTransport().request(
            url="https://api.example/v1/review",
            headers={"Authorization": "Bearer secret"},
            payload={},
            timeout_seconds=7,
            use_system_proxy=False,
        )

    assert caught.value.kind == "malformed"
    assert opener_calls == 0


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://localhost:11434/v1/review", id="localhost"),
        pytest.param("http://127.0.0.1:11434/v1/review", id="ipv4"),
        pytest.param("http://[::1]:11434/v1/review", id="ipv6"),
    ],
)
def test_stdlib_transport_allows_intentional_loopback_http(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    opener = StubOpener(StubHTTPHandle(b'{"ok":true}'))
    installed_handlers: list[object] = []

    def fake_build_opener(*handlers: object) -> StubOpener:
        installed_handlers.extend(handlers)
        return opener

    monkeypatch.setattr(http_module, "build_opener", fake_build_opener)

    response = UrllibJSONTransport().request(
        url=url,
        headers={"Authorization": "Bearer local-secret"},
        payload={},
        timeout_seconds=7,
        use_system_proxy=True,
    )

    assert response.data == {"ok": True}
    assert opener.request is not None
    assert opener.request.full_url == url
    proxy_handler = next(handler for handler in installed_handlers if isinstance(handler, http_module.ProxyHandler))
    assert getattr(proxy_handler, "proxies", None) == {}


def test_stdlib_transport_keeps_http_status_for_non_json_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = StubHTTPErrorOpener(StubHTTPErrorBody(b"upstream unavailable"))
    monkeypatch.setattr(http_module, "build_opener", lambda *handlers: opener)

    response = UrllibJSONTransport().request(
        url="https://api.example/v1/review",
        headers={},
        payload={},
        timeout_seconds=7,
        use_system_proxy=True,
    )

    assert response.status_code == 503
    assert response.data == {}


@pytest.mark.parametrize(
    ("read_error", "expected_kind"),
    [
        pytest.param(TimeoutError("body stalled"), "timeout", id="timeout-error"),
        pytest.param(
            URLError(TimeoutError("socket stalled")),
            "timeout",
            id="wrapped-socket-timeout",
        ),
        pytest.param(URLError("TLS failure"), "network", id="url-error"),
        pytest.param(OSError("connection reset"), "network", id="os-error"),
        pytest.param(HTTPException("invalid HTTP framing"), "network", id="http-error"),
        pytest.param(IncompleteRead(b'{"partial":'), "network", id="incomplete-read"),
    ],
)
def test_stdlib_transport_normalizes_http_error_body_read_failures(
    monkeypatch: pytest.MonkeyPatch,
    read_error: Exception,
    expected_kind: str,
) -> None:
    opener = StubHTTPErrorOpener(StubHTTPErrorBody(read_error=read_error))
    monkeypatch.setattr(http_module, "build_opener", lambda *handlers: opener)

    with pytest.raises(JSONTransportError) as caught:
        UrllibJSONTransport().request(
            url="https://api.example/v1/review",
            headers={},
            payload={},
            timeout_seconds=7,
            use_system_proxy=False,
        )

    assert caught.value.kind == expected_kind


def test_stdlib_transport_normalizes_http_error_body_close_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = StubHTTPErrorOpener(StubHTTPErrorBody(close_error=OSError("close failed")))
    monkeypatch.setattr(http_module, "build_opener", lambda *handlers: opener)

    with pytest.raises(JSONTransportError) as caught:
        UrllibJSONTransport().request(
            url="https://api.example/v1/review",
            headers={},
            payload={},
            timeout_seconds=7,
            use_system_proxy=False,
        )

    assert caught.value.kind == "network"


def test_api_provider_retries_oversized_http_503_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_module, "_MAX_RESPONSE_BYTES", 128)
    first_opener = StubHTTPErrorOpener(StubHTTPErrorBody(b"x" * 129))
    second_opener = StubOpener(
        StubHTTPHandle(
            json.dumps(responses_success_response().data).encode("utf-8"),
        )
    )
    openers = iter([first_opener, second_opener])
    monkeypatch.setattr(http_module, "build_opener", lambda *handlers: next(openers))
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(max_retries=1, retry_backoff_seconds=0.001),
    )
    api_provider = APILLMProvider(
        config,
        transport=UrllibJSONTransport(),
        environment={"OPENAI_API_KEY": "secret"},
        sleep_fn=lambda _seconds: None,
        random_fn=lambda: 1.0,
    )

    result = api_provider.review_context_pack(make_pack(), Path("."))

    assert result == []
    assert second_opener.request is not None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"1" * 10_000, id="integer-digit-limit"),
        pytest.param(b"[" * 500_000 + b"0" + b"]" * 500_000, id="recursion-limit"),
    ],
)
def test_stdlib_transport_normalizes_pathological_success_json(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    monkeypatch.setattr(
        http_module,
        "build_opener",
        lambda *handlers: StubOpener(StubHTTPHandle(body)),
    )

    with pytest.raises(JSONTransportError) as caught:
        UrllibJSONTransport().request(
            url="https://api.example/v1/review",
            headers={},
            payload={},
            timeout_seconds=7,
            use_system_proxy=False,
        )

    assert caught.value.kind == "malformed"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"1" * 10_000, id="integer-digit-limit"),
        pytest.param(b"[" * 500_000 + b"0" + b"]" * 500_000, id="recursion-limit"),
    ],
)
def test_api_provider_retries_pathological_http_503_json(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    first_opener = StubHTTPErrorOpener(StubHTTPErrorBody(body))
    second_opener = StubOpener(
        StubHTTPHandle(
            json.dumps(responses_success_response().data).encode("utf-8"),
        )
    )
    openers = iter([first_opener, second_opener])
    monkeypatch.setattr(http_module, "build_opener", lambda *handlers: next(openers))
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(max_retries=1, retry_backoff_seconds=0.001),
    )
    api_provider = APILLMProvider(
        config,
        transport=UrllibJSONTransport(),
        environment={"OPENAI_API_KEY": "secret"},
        sleep_fn=lambda _seconds: None,
        random_fn=lambda: 1.0,
    )

    result = api_provider.review_context_pack(make_pack(), Path("."))

    assert result == []
    assert second_opener.request is not None


def test_openai_responses_request_and_usage_are_exact() -> None:
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={"x-request-id": "req-1"},
            data={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"findings":[]}'}],
                    }
                ],
                "usage": {
                    "input_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 40},
                    "cache_write_tokens": 7,
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 5},
                    "total_tokens": 140,
                },
            },
        )
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        effort=LLMReasoningEffort.HIGH,
        timeout_seconds=41,
        api=LLMAPIConfig(max_output_tokens=2048, use_system_proxy=False),
    )

    result = provider(config, transport, {"OPENAI_API_KEY": "openai-secret"}).review_context_pack_with_usage(
        make_pack(), Path(".")
    )

    assert result.findings == []
    assert result.usage is not None
    assert result.usage.source == "openai_api"
    assert result.usage.input_tokens == 120
    assert result.usage.cached_input_tokens == 40
    assert result.usage.cache_read_input_tokens == 40
    assert result.usage.cache_creation_input_tokens == 7
    assert result.usage.reasoning_output_tokens == 5
    call = transport.calls[0]
    prompt = call.payload["input"]
    assert transport.calls == [
        TransportCall(
            url="https://api.openai.com/v1/responses",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer openai-secret",
                "Content-Type": "application/json",
            },
            payload={
                "model": "gpt-5.6",
                "input": prompt,
                "max_output_tokens": 2048,
                "store": False,
                "reasoning": {"effort": "high"},
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "apex_ray_finding_response",
                        "strict": True,
                        "schema": finding_response_schema(),
                    }
                },
            },
            timeout_seconds=41,
            use_system_proxy=False,
        )
    ]
    assert str(prompt).startswith("You are Apex Ray")


def test_openai_responses_preserves_explicit_none_reasoning_effort() -> None:
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "status": "completed",
                "output_text": '{"findings":[]}',
            },
        )
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6-sol",
        effort=LLMReasoningEffort.NONE,
    )

    provider(config, transport, {"OPENAI_API_KEY": "secret"}).review_context_pack(make_pack(), Path("."))

    assert transport.calls[0].payload["reasoning"] == {"effort": "none"}


def test_anthropic_messages_request_is_native_and_strict() -> None:
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"findings":[]}'}],
                "usage": {
                    "input_tokens": 90,
                    "cache_creation_input_tokens": 12,
                    "cache_read_input_tokens": 30,
                    "output_tokens": 10,
                },
            },
        )
    )
    config = LLMConfig(
        provider=LLMProviderName.ANTHROPIC_API,
        model="claude-opus-5",
        effort=LLMReasoningEffort.HIGH,
        api=LLMAPIConfig(api_version="2023-06-01", max_output_tokens=1024),
    )

    result = provider(config, transport, {"ANTHROPIC_API_KEY": "anthropic-secret"}).review_context_pack_with_usage(
        make_pack(), Path(".")
    )

    assert result.usage is not None
    assert result.usage.input_tokens == 90
    assert result.usage.cached_input_tokens == 30
    assert result.usage.cache_creation_input_tokens == 12
    assert result.usage.total_tokens == 142
    call = transport.calls[0]
    assert call.url == "https://api.anthropic.com/v1/messages"
    assert call.headers == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": "anthropic-secret",
    }
    messages = cast(list[Mapping[str, object]], call.payload["messages"])
    assert call.payload == {
        "model": "claude-opus-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": messages[0]["content"]}],
        "output_config": {
            "effort": "high",
            "format": {
                "type": "json_schema",
                "schema": finding_response_schema(),
            },
        },
        "thinking": {"type": "adaptive"},
    }


def test_anthropic_api_preserves_documented_xhigh_effort() -> None:
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"findings":[]}'}],
            },
        )
    )
    config = LLMConfig(
        provider=LLMProviderName.ANTHROPIC_API,
        model="claude-sonnet-5",
        effort=LLMReasoningEffort.XHIGH,
    )

    provider(config, transport, {"ANTHROPIC_API_KEY": "secret"}).review_context_pack(make_pack(), Path("."))

    output_config = cast(Mapping[str, object], transport.calls[0].payload["output_config"])
    assert output_config["effort"] == "xhigh"


@pytest.mark.parametrize(
    (
        "provider_name",
        "key_env",
        "url",
        "model",
        "token_field",
        "response_format",
        "reasoning_fields",
    ),
    [
        (
            LLMProviderName.DEEPSEEK_API,
            "DEEPSEEK_API_KEY",
            "https://api.deepseek.com/chat/completions",
            "deepseek-v4-pro",
            "max_tokens",
            {"type": "json_object"},
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        ),
        (
            LLMProviderName.QWEN_API,
            "DASHSCOPE_API_KEY",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "qwen3.7-max",
            "max_tokens",
            {"type": "json_object"},
            {"enable_thinking": True},
        ),
        (
            LLMProviderName.KIMI_API,
            "MOONSHOT_API_KEY",
            "https://api.moonshot.ai/v1/chat/completions",
            "kimi-k3",
            "max_completion_tokens",
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "apex_ray_finding_response",
                    "strict": True,
                    "schema": finding_response_schema(),
                },
            },
            {"reasoning_effort": "high"},
        ),
        (
            LLMProviderName.ZAI_API,
            "ZAI_API_KEY",
            "https://api.z.ai/api/paas/v4/chat/completions",
            "glm-5.2",
            "max_tokens",
            {"type": "json_object"},
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        ),
    ],
)
def test_openai_chat_presets_use_documented_capabilities(
    provider_name: LLMProviderName,
    key_env: str,
    url: str,
    model: str,
    token_field: str,
    response_format: Mapping[str, object],
    reasoning_fields: Mapping[str, object],
) -> None:
    transport = StubTransport(
        success_response(
            usage={
                "prompt_tokens": 80,
                "prompt_tokens_details": {"cached_tokens": 25},
                "completion_tokens": 12,
                "completion_tokens_details": {"reasoning_tokens": 4},
                "total_tokens": 92,
            }
        )
    )
    config = LLMConfig(
        provider=provider_name,
        model=model,
        effort=LLMReasoningEffort.HIGH,
        api=LLMAPIConfig(max_output_tokens=1536),
    )

    result = provider(config, transport, {key_env: "provider-secret"}).review_context_pack_with_usage(
        make_pack(), Path(".")
    )

    assert result.usage is not None
    assert result.usage.cached_input_tokens == 25
    assert result.usage.reasoning_output_tokens == 4
    call = transport.calls[0]
    assert call.url == url
    assert call.headers["Authorization"] == "Bearer provider-secret"
    messages = cast(list[Mapping[str, object]], call.payload["messages"])
    assert call.payload == {
        "model": model,
        "messages": [{"role": "user", "content": messages[0]["content"]}],
        token_field: 1536,
        "response_format": response_format,
        **reasoning_fields,
    }


def test_qwen_preset_allows_official_workspace_dedicated_endpoint() -> None:
    transport = StubTransport(success_response())
    config = LLMConfig(
        provider=LLMProviderName.QWEN_API,
        model="qwen3.7-max",
        api=LLMAPIConfig(base_url=("https://llm-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")),
    )

    provider(
        config,
        transport,
        {"DASHSCOPE_API_KEY": "provider-secret"},
    ).review_context_pack(make_pack(), Path("."))

    assert (
        transport.calls[0].url == "https://llm-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
    )


@pytest.mark.parametrize(
    ("provider_name", "key_env", "model", "effort", "reasoning_fields"),
    [
        (
            LLMProviderName.DEEPSEEK_API,
            "DEEPSEEK_API_KEY",
            "deepseek-v4-pro",
            LLMReasoningEffort.LOW,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        ),
        (
            LLMProviderName.DEEPSEEK_API,
            "DEEPSEEK_API_KEY",
            "deepseek-v4-pro",
            LLMReasoningEffort.NONE,
            {"thinking": {"type": "disabled"}},
        ),
        (
            LLMProviderName.QWEN_API,
            "DASHSCOPE_API_KEY",
            "qwen3.7-max",
            LLMReasoningEffort.MAX,
            {"enable_thinking": True},
        ),
        (
            LLMProviderName.QWEN_API,
            "DASHSCOPE_API_KEY",
            "qwen3.7-max",
            LLMReasoningEffort.NONE,
            {"enable_thinking": False},
        ),
        (
            LLMProviderName.KIMI_API,
            "MOONSHOT_API_KEY",
            "kimi-k3",
            LLMReasoningEffort.MEDIUM,
            {"reasoning_effort": "high"},
        ),
        (
            LLMProviderName.KIMI_API,
            "MOONSHOT_API_KEY",
            "kimi-k2.6",
            LLMReasoningEffort.MEDIUM,
            {"thinking": {"type": "enabled"}},
        ),
        (
            LLMProviderName.ZAI_API,
            "ZAI_API_KEY",
            "glm-5.2",
            LLMReasoningEffort.MEDIUM,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "medium"},
        ),
        (
            LLMProviderName.ZAI_API,
            "ZAI_API_KEY",
            "glm-5.1",
            LLMReasoningEffort.MEDIUM,
            {"thinking": {"type": "enabled"}},
        ),
    ],
)
def test_openai_chat_presets_use_model_compatible_reasoning_controls(
    provider_name: LLMProviderName,
    key_env: str,
    model: str,
    effort: LLMReasoningEffort,
    reasoning_fields: Mapping[str, object],
) -> None:
    transport = StubTransport(success_response())
    config = LLMConfig(provider=provider_name, model=model, effort=effort)

    provider(config, transport, {key_env: "provider-secret"}).review_context_pack(make_pack(), Path("."))

    payload = transport.calls[0].payload
    assert {
        key: payload[key] for key in ("thinking", "reasoning_effort", "enable_thinking") if key in payload
    } == reasoning_fields


def test_api_provider_supports_batch_verification_and_resolution_schemas() -> None:
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "status": "completed",
                "output_text": (
                    '{"decisions":[{"finding_index":0,"approved":true,"confidence":"high","reason":"Supported."}]}'
                ),
                "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            },
        ),
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "status": "completed",
                "output_text": (
                    '{"status":"resolved","confidence":"high","reason":"Fixed.",'
                    '"evidence":"Multiplication restored.","suggested_next_action":""}'
                ),
            },
        ),
    )
    config = LLMConfig(provider=LLMProviderName.OPENAI_API, model="gpt-5.6")
    api_provider = provider(config, transport, {"OPENAI_API_KEY": "secret"})
    finding = make_finding()

    verification = api_provider.verify_findings_with_usage([finding], make_pack(), Path("."))
    delta = ReviewReport.model_construct(
        diff=DiffSummary(target_mode=TargetMode.WORKTREE),
        context_packs=[],
    )
    resolution = api_provider.resolve_finding(finding, make_pack(), delta, Path("."))

    assert verification.verifications[0].approved is True
    assert resolution.status == "resolved"
    first_text = cast(Mapping[str, object], transport.calls[0].payload["text"])
    second_text = cast(Mapping[str, object], transport.calls[1].payload["text"])
    first_format = cast(Mapping[str, object], first_text["format"])
    second_format = cast(Mapping[str, object], second_text["format"])
    assert first_format["schema"] == verification_batch_response_schema()
    assert first_format["name"] == "apex_ray_verification_batch_response"
    assert second_format["schema"] == resolution_response_schema()
    assert second_format["name"] == "apex_ray_resolution_response"


def test_generic_compatible_provider_uses_env_endpoint_headers_and_allowlist_in_ci() -> None:
    transport = StubTransport(success_response())
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="company-reviewer",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url_env="COMPANY_LLM_URL",
            api_key_env="COMPANY_LLM_KEY",
            headers_from_env={"X-Project": "COMPANY_LLM_PROJECT"},
        ),
    )
    environment = {
        "CI": "true",
        "COMPANY_LLM_URL": "https://llm-gateway.example/v1",
        "COMPANY_LLM_KEY": "company-secret",
        "COMPANY_LLM_PROJECT": "apex-ray",
        "APEX_RAY_API_ALLOWED_HOSTS": "llm-gateway.example",
        "APEX_RAY_API_ALLOWED_ENV_VARS": "COMPANY_LLM_URL,COMPANY_LLM_KEY,COMPANY_LLM_PROJECT",
    }

    provider(config, transport, environment).review_context_pack(make_pack(), Path("."))

    call = transport.calls[0]
    assert call.url == "https://llm-gateway.example/v1/chat/completions"
    assert call.headers["Authorization"] == "Bearer company-secret"
    assert call.headers["X-Project"] == "apex-ray"


def test_generic_compatible_provider_uses_normalized_idn_host_for_ci_allowlist() -> None:
    transport = StubTransport(success_response())
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="company-reviewer",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url_env="COMPANY_LLM_URL",
            api_key_env="COMPANY_LLM_KEY",
        ),
    )

    provider(
        config,
        transport,
        {
            "CI": "true",
            "COMPANY_LLM_URL": "https://例え.テスト/v1",
            "COMPANY_LLM_KEY": "company-secret",
            "APEX_RAY_API_ALLOWED_HOSTS": "xn--r8jz45g.xn--zckzah",
            "APEX_RAY_API_ALLOWED_ENV_VARS": "COMPANY_LLM_URL,COMPANY_LLM_KEY",
        },
    ).review_context_pack(make_pack(), Path("."))

    assert len(transport.calls) == 1


def test_generic_compatible_provider_preserves_local_loopback_endpoint() -> None:
    transport = StubTransport(success_response())
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="local-model",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url_env="LOCAL_LLM_URL",
            api_key_env="LOCAL_LLM_KEY",
        ),
    )

    provider(
        config,
        transport,
        {
            "LOCAL_LLM_URL": "http://127.0.0.1:11434/v1",
            "LOCAL_LLM_KEY": "local-secret",
        },
    ).review_context_pack(make_pack(), Path("."))

    assert transport.calls[0].url == "http://127.0.0.1:11434/v1/chat/completions"


def test_generic_provider_can_use_native_anthropic_protocol() -> None:
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"findings":[]}'}],
            },
        )
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="private-claude",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.ANTHROPIC_MESSAGES,
            structured_output=LLMStructuredOutput.JSON_SCHEMA,
            base_url="https://gateway.example/v1",
            api_key_env="KEY",
            api_version="2025-01-01",
        ),
    )

    provider(config, transport, {"KEY": "secret"}).review_context_pack(make_pack(), Path("."))

    call = transport.calls[0]
    assert call.url == "https://gateway.example/v1/messages"
    assert call.headers["x-api-key"] == "secret"
    assert call.headers["anthropic-version"] == "2025-01-01"
    assert "Authorization" not in call.headers


@pytest.mark.parametrize(
    ("api", "environment", "message"),
    [
        (
            LLMAPIConfig(
                protocol=LLMAPIProtocol.OPENAI_CHAT,
                structured_output=LLMStructuredOutput.JSON_OBJECT,
                base_url="https://attacker.example/v1",
                api_key_env="KEY",
            ),
            {"CI": "true", "KEY": "secret"},
            "base_url_env",
        ),
        (
            LLMAPIConfig(
                protocol=LLMAPIProtocol.OPENAI_CHAT,
                structured_output=LLMStructuredOutput.JSON_OBJECT,
                base_url_env="URL",
                api_key_env="KEY",
                allowed_hosts_env="ALLOWED",
            ),
            {
                "CI": "true",
                "URL": "https://attacker.example/v1",
                "KEY": "secret",
                "ALLOWED": "trusted.example",
            },
            "allowlist",
        ),
    ],
)
def test_custom_provider_rejects_untrusted_ci_endpoint(
    api: LLMAPIConfig,
    environment: Mapping[str, str],
    message: str,
) -> None:
    config = LLMConfig(provider=LLMProviderName.OPENAI_COMPATIBLE, model="model", api=api)

    with pytest.raises(LLMProviderError, match=message):
        provider(config, StubTransport(success_response()), environment)


def test_preset_provider_rejects_endpoint_outside_pinned_hosts() -> None:
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(base_url="https://attacker.example/v1"),
    )

    with pytest.raises(LLMProviderError, match="not allowed for openai_api"):
        provider(config, StubTransport(success_response()), {"OPENAI_API_KEY": "secret"})


def test_anthropic_preset_rejects_incompatible_protocol_override() -> None:
    config = LLMConfig(
        provider=LLMProviderName.ANTHROPIC_API,
        model="claude-sonnet-5",
        api=LLMAPIConfig(protocol=LLMAPIProtocol.OPENAI_CHAT),
    )

    with pytest.raises(LLMProviderError, match="protocol is fixed"):
        provider(
            config,
            StubTransport(success_response()),
            {"ANTHROPIC_API_KEY": "secret"},
        )


def test_openai_preset_preserves_chat_completions_protocol_support() -> None:
    transport = StubTransport(success_response())
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(protocol=LLMAPIProtocol.OPENAI_CHAT),
    )

    provider(
        config,
        transport,
        {"OPENAI_API_KEY": "secret"},
    ).review_context_pack(make_pack(), Path("."))

    call = transport.calls[0]
    assert call.url == "https://api.openai.com/v1/chat/completions"
    assert call.payload["store"] is False
    assert call.payload["max_completion_tokens"] == 4096
    assert "max_tokens" not in call.payload


def test_ci_preset_api_key_selector_cannot_be_overridden() -> None:
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(api_key_env="UNRELATED_CLOUD_SECRET"),
    )
    transport = StubTransport(responses_success_response())

    with pytest.raises(LLMProviderError, match="API key environment variable is fixed"):
        provider(
            config,
            transport,
            {
                "GITHUB_ACTIONS": "true",
                "OPENAI_API_KEY": "openai-secret",
                "UNRELATED_CLOUD_SECRET": "unrelated-secret",
                "APEX_RAY_API_ALLOWED_ENV_VARS": "UNRELATED_CLOUD_SECRET",
            },
        )

    assert transport.calls == []


@pytest.mark.parametrize(
    ("omitted_selector", "message"),
    [
        pytest.param("COMPANY_LLM_URL", "base URL", id="base-url"),
        pytest.param("COMPANY_LLM_KEY", "API key", id="api-key"),
        pytest.param("COMPANY_LLM_PROJECT", "header", id="header"),
    ],
)
def test_ci_custom_provider_requires_fixed_allowlist_for_each_environment_selector(
    omitted_selector: str,
    message: str,
) -> None:
    selectors = {
        "COMPANY_LLM_URL",
        "COMPANY_LLM_KEY",
        "COMPANY_LLM_PROJECT",
    }
    selectors.remove(omitted_selector)
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="company-reviewer",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url_env="COMPANY_LLM_URL",
            api_key_env="COMPANY_LLM_KEY",
            headers_from_env={"X-Project": "COMPANY_LLM_PROJECT"},
        ),
    )
    transport = StubTransport(success_response())

    with pytest.raises(LLMProviderError, match=message):
        provider(
            config,
            transport,
            {
                "CI": "true",
                "COMPANY_LLM_URL": "https://llm-gateway.example/v1",
                "COMPANY_LLM_KEY": "company-secret",
                "COMPANY_LLM_PROJECT": "apex-ray",
                "APEX_RAY_API_ALLOWED_HOSTS": "llm-gateway.example",
                "APEX_RAY_API_ALLOWED_ENV_VARS": ",".join(sorted(selectors)),
            },
        )

    assert transport.calls == []


def test_ci_custom_provider_cannot_select_the_host_allowlist_variable() -> None:
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="company-reviewer",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url_env="COMPANY_LLM_URL",
            api_key_env="COMPANY_LLM_KEY",
            allowed_hosts_env="REPOSITORY_SELECTED_ALLOWLIST",
        ),
    )

    with pytest.raises(LLMProviderError, match="host allowlist environment variable is fixed"):
        provider(
            config,
            StubTransport(success_response()),
            {
                "CI": "true",
                "COMPANY_LLM_URL": "https://attacker.example/v1",
                "COMPANY_LLM_KEY": "company-secret",
                "APEX_RAY_API_ALLOWED_ENV_VARS": "COMPANY_LLM_URL,COMPANY_LLM_KEY",
                "APEX_RAY_API_ALLOWED_HOSTS": "trusted.example",
                "REPOSITORY_SELECTED_ALLOWLIST": "attacker.example",
            },
        )


@pytest.mark.parametrize(
    ("config", "environment", "message"),
    [
        (
            LLMConfig(provider=LLMProviderName.OPENAI_API, model=None),
            {"OPENAI_API_KEY": "secret"},
            "explicit model",
        ),
        (
            LLMConfig(provider=LLMProviderName.OPENAI_API, model="gpt-5.6"),
            {},
            "OPENAI_API_KEY",
        ),
        (
            LLMConfig(
                provider=LLMProviderName.OPENAI_COMPATIBLE,
                model="custom",
                api=LLMAPIConfig(
                    protocol=LLMAPIProtocol.OPENAI_CHAT,
                    structured_output=LLMStructuredOutput.JSON_OBJECT,
                    base_url="https://gateway.example/v1",
                ),
            ),
            {},
            "api_key_env",
        ),
    ],
)
def test_api_provider_requires_model_and_key_environment(
    config: LLMConfig,
    environment: Mapping[str, str],
    message: str,
) -> None:
    with pytest.raises(LLMProviderError, match=message):
        provider(config, StubTransport(success_response()), environment)


def test_missing_api_key_is_classified_as_terminal_auth_failure() -> None:
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(max_retries=3),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider(config, StubTransport(success_response()), {})

    assert caught.value.category == "auth"
    assert caught.value.retryable is False
    assert classify_llm_provider_error(caught.value) == "failed_auth"


def test_transient_rate_limit_retries_and_honors_retry_after() -> None:
    sleeps: list[float] = []
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=429,
            headers={"Retry-After": "3"},
            data={"error": {"type": "rate_limit_error", "message": "Slow down."}},
        ),
        responses_success_response(),
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(max_retries=1, retry_max_seconds=5),
    )

    provider(config, transport, {"OPENAI_API_KEY": "secret"}, sleeps=sleeps).review_context_pack(make_pack(), Path("."))

    assert len(transport.calls) == 2
    assert sleeps == [3.0]


@pytest.mark.parametrize("retry_after", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_retry_after_uses_configured_backoff(retry_after: str) -> None:
    sleeps: list[float] = []
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=429,
            headers={"Retry-After": retry_after},
            data={"error": {"type": "rate_limit_error", "message": "Slow down."}},
        ),
        responses_success_response(),
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(
            max_retries=1,
            retry_backoff_seconds=0.5,
            retry_max_seconds=5,
        ),
    )

    provider(config, transport, {"OPENAI_API_KEY": "secret"}, sleeps=sleeps).review_context_pack(
        make_pack(),
        Path("."),
    )

    assert len(transport.calls) == 2
    assert sleeps == [0.5]


def test_quota_error_is_terminal_and_classified_separately() -> None:
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=429,
            headers={"Retry-After": "1"},
            data={
                "error": {
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                    "message": "No balance.",
                }
            },
        ),
        responses_success_response(),
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(max_retries=3),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider(config, transport, {"OPENAI_API_KEY": "secret"}).review_context_pack(make_pack(), Path("."))

    assert caught.value.category == "quota"
    assert classify_llm_provider_error(caught.value) == "failed_quota"
    assert len(transport.calls) == 1


def test_http_403_quota_details_are_not_collapsed_into_auth() -> None:
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=403,
            headers={},
            data={
                "error": {
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                    "message": "No balance.",
                }
            },
        ),
        responses_success_response(),
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(max_retries=2),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider(
            config,
            transport,
            {"OPENAI_API_KEY": "secret"},
        ).review_context_pack(make_pack(), Path("."))

    assert caught.value.category == "quota"
    assert len(transport.calls) == 1


def test_http_403_permission_error_is_not_retried_for_rate_limit_message_text() -> None:
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=403,
            headers={"Retry-After": "2"},
            data={
                "error": {
                    "type": "permission_denied_error",
                    "message": "This key may not access the rate limit administration policy.",
                }
            },
        ),
        responses_success_response(),
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(max_retries=1),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider(
            config,
            transport,
            {"OPENAI_API_KEY": "secret"},
        ).review_context_pack(make_pack(), Path("."))

    assert caught.value.category == "auth"
    assert caught.value.retryable is False
    assert len(transport.calls) == 1


def test_timeout_retries_but_malformed_transport_or_response_does_not() -> None:
    timeout_transport = StubTransport(
        JSONTransportError("request timed out", kind="timeout"),
        success_response(),
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="model",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url="https://gateway.example/v1",
            api_key_env="KEY",
            max_retries=2,
        ),
    )
    api_provider = provider(config, timeout_transport, {"KEY": "secret"})

    api_provider.review_context_pack(make_pack(), Path("."))

    assert len(timeout_transport.calls) == 2

    malformed_url_transport = StubTransport(
        JSONTransportError("API endpoint must use HTTPS.", kind="malformed"),
        success_response(),
    )
    with pytest.raises(LLMProviderError) as caught:
        provider(config, malformed_url_transport, {"KEY": "secret"}).review_context_pack(make_pack(), Path("."))

    assert caught.value.category == "malformed"
    assert len(malformed_url_transport.calls) == 1

    malformed_transport = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]},
        ),
        success_response(),
    )
    with pytest.raises(LLMProviderError) as caught:
        provider(config, malformed_transport, {"KEY": "secret"}).review_context_pack(make_pack(), Path("."))

    assert caught.value.category == "malformed"
    assert classify_llm_provider_error(caught.value) == "failed_malformed"
    assert len(malformed_transport.calls) == 1


def test_malformed_endpoint_configuration_is_terminal_before_transport() -> None:
    transport = StubTransport(success_response())
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="model",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url_env="ENDPOINT",
            api_key_env="KEY",
            max_retries=2,
        ),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider(
            config,
            transport,
            {"ENDPOINT": "https://gateway.example/v1 bad", "KEY": "secret"},
        )

    assert caught.value.category == "malformed"
    assert classify_llm_provider_error(caught.value) == "failed_malformed"
    assert transport.calls == []


def test_refusal_and_truncation_are_explicit_terminal_errors() -> None:
    refusal = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "Cannot comply."}],
                    }
                ],
            },
        )
    )
    openai = LLMConfig(provider=LLMProviderName.OPENAI_API, model="gpt-5.6")
    with pytest.raises(LLMProviderError) as refused:
        provider(openai, refusal, {"OPENAI_API_KEY": "secret"}).review_context_pack(make_pack(), Path("."))
    assert refused.value.category == "refusal"
    assert classify_llm_provider_error(refused.value) == "failed_refusal"

    truncation = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": '{"findings":[]}'},
                    }
                ]
            },
        )
    )
    kimi = LLMConfig(provider=LLMProviderName.KIMI_API, model="kimi-k3")
    with pytest.raises(LLMProviderError) as truncated:
        provider(kimi, truncation, {"MOONSHOT_API_KEY": "secret"}).review_context_pack(make_pack(), Path("."))
    assert truncated.value.category == "truncated"
    assert classify_llm_provider_error(truncated.value) == "failed_truncated"


@pytest.mark.parametrize(
    ("provider_name", "model", "key_env", "finish_reason"),
    [
        (
            LLMProviderName.DEEPSEEK_API,
            "deepseek-reasoner",
            "DEEPSEEK_API_KEY",
            "insufficient_system_resource",
        ),
        (
            LLMProviderName.ZAI_API,
            "glm-5",
            "ZAI_API_KEY",
            "network_error",
        ),
    ],
)
def test_transient_openai_compatible_finish_reason_retries(
    provider_name: LLMProviderName,
    model: str,
    key_env: str,
    finish_reason: str,
) -> None:
    sleeps: list[float] = []
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": '{"findings":[]}'},
                    }
                ]
            },
        ),
        success_response(),
    )
    config = LLMConfig(
        provider=provider_name,
        model=model,
        api=LLMAPIConfig(max_retries=1, retry_backoff_seconds=1),
    )

    result = provider(
        config,
        transport,
        {key_env: "secret"},
        sleeps=sleeps,
    ).review_context_pack(make_pack(), Path("."))

    assert result == []
    assert len(transport.calls) == 2
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    ("finish_reason", "category", "status"),
    [
        ("sensitive", "refusal", "failed_refusal"),
        (
            "model_context_window_exceeded",
            "truncated",
            "failed_truncated",
        ),
    ],
)
def test_zai_terminal_finish_reasons_are_classified_before_content_is_used(
    finish_reason: str,
    category: str,
    status: str,
) -> None:
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": '{"findings":[]}'},
                    }
                ]
            },
        )
    )
    config = LLMConfig(
        provider=LLMProviderName.ZAI_API,
        model="glm-5",
        api=LLMAPIConfig(max_retries=3),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider(
            config,
            transport,
            {"ZAI_API_KEY": "secret"},
        ).review_context_pack(make_pack(), Path("."))

    assert caught.value.category == category
    assert classify_llm_provider_error(caught.value) == status
    assert len(transport.calls) == 1


def test_provider_error_redacts_key_and_does_not_retry_auth() -> None:
    secret = "unit-test-private-provider-value"
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=401,
            headers={},
            data={"error": {"type": "authentication_error", "message": f"Invalid key {secret}"}},
        ),
        responses_success_response(),
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="gpt-5.6",
        api=LLMAPIConfig(max_retries=3),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider(config, transport, {"OPENAI_API_KEY": secret}).review_context_pack(make_pack(), Path("."))

    assert caught.value.category == "auth"
    assert secret not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
    assert classify_llm_provider_error(caught.value) == "failed_auth"
    assert len(transport.calls) == 1


def test_malformed_response_validation_redacts_known_secrets() -> None:
    secret = "unit-test-private-response-value"
    transport = StubTransport(
        JSONHTTPResponse(
            status_code=200,
            headers={},
            data={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": f'{{"findings":"{secret}"}}'},
                    }
                ]
            },
        )
    )
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="model",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url="https://gateway.example/v1",
            api_key_env="KEY",
        ),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider(config, transport, {"KEY": secret}).review_context_pack(make_pack(), Path("."))

    assert caught.value.category == "malformed"
    assert secret not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_header_values_reject_newline_injection() -> None:
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="model",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url="https://gateway.example/v1",
            api_key_env="KEY",
            headers_from_env={"X-Tenant": "TENANT"},
        ),
    )

    with pytest.raises(LLMProviderError, match="newline"):
        provider(
            config,
            StubTransport(success_response()),
            {"KEY": "secret", "TENANT": "safe\r\nX-Evil: yes"},
        )


@pytest.mark.parametrize("header", ["X Bad", "X\tBad", "X\x00Bad", "X-Ünicode"])
def test_custom_header_names_must_be_ascii_http_tokens(header: str) -> None:
    with pytest.raises(ValueError, match="Invalid API header name"):
        LLMAPIConfig(headers_from_env={header: "TENANT"})


def test_parse_api_usage_normalizes_deepseek_and_anthropic_fields() -> None:
    deepseek = parse_api_usage(
        {
            "prompt_tokens": 100,
            "prompt_cache_hit_tokens": 60,
            "prompt_cache_miss_tokens": 40,
            "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 9},
            "total_tokens": 120,
        },
        source="deepseek_api",
        protocol=LLMAPIProtocol.OPENAI_CHAT,
    )
    anthropic = parse_api_usage(
        {
            "input_tokens": 50,
            "cache_creation_input_tokens": 8,
            "cache_read_input_tokens": 30,
            "output_tokens": 5,
        },
        source="anthropic_api",
        protocol=LLMAPIProtocol.ANTHROPIC_MESSAGES,
    )

    assert deepseek is not None
    assert deepseek.cached_input_tokens == 60
    assert deepseek.cache_read_input_tokens == 60
    assert deepseek.reasoning_output_tokens == 9
    assert anthropic is not None
    assert anthropic.total_tokens == 93
    assert anthropic.cache_creation_input_tokens == 8


def test_provider_input_budget_estimates_cli_scaffold_conservatively() -> None:
    chars = 20_000

    assert estimate_tokens(chars) == 5_000
    assert estimate_provider_input_tokens(chars, provider=LLMProviderName.CODEX_CLI) == 21_250
    assert estimate_provider_input_tokens(chars, provider=LLMProviderName.CLAUDE_CODE_CLI) == 25_000


def test_provider_input_budget_keeps_direct_api_overhead_separate() -> None:
    chars = 20_000

    assert estimate_provider_input_tokens(chars, provider=LLMProviderName.OPENAI_API) == 7_179
    assert estimate_provider_input_tokens(chars, provider=LLMProviderName.DEEPSEEK_API) == 10_768
    assert estimate_provider_input_tokens(chars, provider=LLMProviderName.OPENAI_COMPATIBLE) == 11_024
    assert estimate_provider_input_tokens(0, provider=LLMProviderName.CODEX_CLI) == 0


def test_provider_factory_accepts_injected_api_transport() -> None:
    transport = StubTransport(success_response())
    config = LLMConfig(provider=LLMProviderName.DEEPSEEK_API, model="deepseek-v4-pro")

    result = provider_from_config(
        config,
        api_transport=transport,
        environment={"DEEPSEEK_API_KEY": "secret"},
        sleep_fn=lambda _seconds: None,
    )

    assert isinstance(result, APILLMProvider)


def test_same_provider_profile_api_overrides_only_explicit_fields() -> None:
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_COMPATIBLE,
        model="base-model",
        api=LLMAPIConfig(
            protocol=LLMAPIProtocol.OPENAI_CHAT,
            structured_output=LLMStructuredOutput.JSON_OBJECT,
            base_url_env="CUSTOM_LLM_BASE_URL",
            api_key_env="CUSTOM_LLM_API_KEY",
            headers_from_env={"X-Tenant": "CUSTOM_LLM_TENANT"},
            max_retries=5,
        ),
        profiles={
            "large-output": LLMProfile(
                provider=LLMProviderName.OPENAI_COMPATIBLE,
                model="large-model",
                api=LLMAPIConfig(max_output_tokens=8192),
            )
        },
    )
    config.routing.review_profile = "large-output"

    resolved, profile, _reason = review_config_for_pack(config, make_pack())

    assert profile == "large-output"
    assert resolved.api.protocol == LLMAPIProtocol.OPENAI_CHAT
    assert resolved.api.structured_output == LLMStructuredOutput.JSON_OBJECT
    assert resolved.api.base_url_env == "CUSTOM_LLM_BASE_URL"
    assert resolved.api.api_key_env == "CUSTOM_LLM_API_KEY"
    assert resolved.api.headers_from_env == {"X-Tenant": "CUSTOM_LLM_TENANT"}
    assert resolved.api.max_retries == 5
    assert resolved.api.max_output_tokens == 8192


def test_same_preset_provider_profile_preserves_custom_auth_and_headers() -> None:
    config = LLMConfig(
        provider=LLMProviderName.OPENAI_API,
        model="base-model",
        api=LLMAPIConfig(
            api_key_env="PRIVATE_OPENAI_KEY",
            headers_from_env={"OpenAI-Organization": "PRIVATE_OPENAI_ORG"},
            retry_backoff_seconds=1.5,
        ),
        profiles={
            "brief": LLMProfile(
                model="brief-model",
                api=LLMAPIConfig(max_output_tokens=1024),
            )
        },
    )
    config.routing.review_profile = "brief"

    resolved, profile, _reason = review_config_for_pack(config, make_pack())

    assert profile == "brief"
    assert resolved.provider == LLMProviderName.OPENAI_API
    assert resolved.api.api_key_env == "PRIVATE_OPENAI_KEY"
    assert resolved.api.headers_from_env == {"OpenAI-Organization": "PRIVATE_OPENAI_ORG"}
    assert resolved.api.retry_backoff_seconds == 1.5
    assert resolved.api.max_output_tokens == 1024
