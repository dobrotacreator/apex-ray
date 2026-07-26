from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPException
from ipaddress import ip_address
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")


@dataclass(frozen=True)
class JSONHTTPResponse:
    status_code: int
    headers: Mapping[str, str]
    data: object


class JSONTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: Literal["malformed", "network", "timeout"],
    ) -> None:
        super().__init__(message)
        self.kind = kind


class JSONTransport(Protocol):
    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
        use_system_proxy: bool,
    ) -> JSONHTTPResponse: ...


class _ResponseHandle(Protocol):
    def read(self, n: int = -1) -> bytes: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


@dataclass(frozen=True)
class _ValidatedAPIEndpoint:
    url: str
    host: str


def validate_api_endpoint_url(url: str) -> str:
    return _validated_api_endpoint(url).host


def _validated_api_endpoint(url: str) -> _ValidatedAPIEndpoint:
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise ValueError("API endpoint must not contain control characters.")
    if any(character.isspace() for character in url):
        raise ValueError("API endpoint contains a malformed path.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        detail = "invalid port" if "port" in str(exc).lower() else "malformed host"
        raise ValueError(f"API endpoint contains an {detail}.") from exc

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("API endpoint must not contain credentials.")
    if not host:
        raise ValueError("API endpoint must include a host.")
    if parsed.netloc.endswith(":"):
        raise ValueError("API endpoint contains an invalid port.")
    if any(character.isspace() or character in "/\\?#@" for character in host):
        raise ValueError("API endpoint contains a malformed host.")
    try:
        address = ip_address(host)
    except ValueError:
        try:
            normalized_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("API endpoint contains a malformed host.") from exc
        labels = normalized_host.split(".")
        numeric_address = len(labels) > 1 and all(label.isdigit() for label in labels)
        if (
            len(normalized_host) > 253
            or numeric_address
            or any(_HOST_LABEL.fullmatch(label) is None for label in labels)
        ):
            raise ValueError("API endpoint contains a malformed host.") from None
    else:
        normalized_host = str(address)

    loopback = normalized_host in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("API endpoint must use HTTPS (HTTP is allowed only for loopback tests).")
    if parsed.query or parsed.fragment:
        raise ValueError("API endpoint must not contain a query or fragment.")
    if (
        (parsed.path and not parsed.path.startswith("/"))
        or "\\" in parsed.path
        or any(character.isspace() for character in parsed.path)
        or _INVALID_PERCENT_ESCAPE.search(parsed.path)
    ):
        raise ValueError("API endpoint contains a malformed path.")
    try:
        normalized_path = quote(
            parsed.path,
            safe="/:@-._~!$&'()*+,;=%",
        )
    except UnicodeError as exc:
        raise ValueError("API endpoint contains a malformed path.") from exc

    normalized_netloc = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    if port is not None:
        normalized_netloc = f"{normalized_netloc}:{port}"
    normalized_url = urlunsplit(
        (
            parsed.scheme,
            normalized_netloc,
            normalized_path,
            "",
            "",
        )
    )
    return _ValidatedAPIEndpoint(url=normalized_url, host=normalized_host)


class UrllibJSONTransport:
    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
        use_system_proxy: bool,
    ) -> JSONHTTPResponse:
        try:
            endpoint = _validated_api_endpoint(url)
        except ValueError as exc:
            raise JSONTransportError(str(exc), kind="malformed") from exc

        request_headers = {name: value for name, value in headers.items() if name.lower() != "accept-encoding"}
        request_headers["Accept-Encoding"] = "identity"
        request_data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            request = Request(
                endpoint.url,
                data=request_data,
                headers=request_headers,
                method="POST",
            )
        except ValueError as exc:
            raise JSONTransportError(
                "API endpoint could not be encoded as a valid HTTP request.",
                kind="malformed",
            ) from exc
        try:
            if use_system_proxy and endpoint.host not in {"127.0.0.1", "::1", "localhost"}:
                opener = build_opener(_NoRedirectHandler())
            else:
                opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
            try:
                response = opener.open(request, timeout=timeout_seconds)
            except HTTPError as exc:
                response = exc
            with response:
                return _response_from_handle(response)
        except TimeoutError as exc:
            raise JSONTransportError("API request timed out.", kind="timeout") from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise JSONTransportError("API request timed out.", kind="timeout") from exc
            raise JSONTransportError("API request failed at the network boundary.", kind="network") from exc
        except ValueError as exc:
            raise JSONTransportError(
                "API endpoint could not be encoded as a valid HTTP request.",
                kind="malformed",
            ) from exc
        except (OSError, HTTPException) as exc:
            raise JSONTransportError("API request failed at the network boundary.", kind="network") from exc


def _response_from_handle(response: _ResponseHandle) -> JSONHTTPResponse:
    status = getattr(response, "status", None)
    if not isinstance(status, int):
        status = getattr(response, "code", None)
    if not isinstance(status, int):
        raise JSONTransportError("API response did not include an HTTP status.", kind="malformed")

    raw_headers = getattr(response, "headers", None)
    headers = (
        {str(name): str(value) for name, value in raw_headers.items()}
        if raw_headers is not None and hasattr(raw_headers, "items")
        else {}
    )
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        if not 200 <= status < 300:
            return JSONHTTPResponse(status_code=status, headers=headers, data={})
        raise JSONTransportError("API response exceeded the maximum supported size.", kind="malformed")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        if not 200 <= status < 300:
            return JSONHTTPResponse(status_code=status, headers=headers, data={})
        raise JSONTransportError("API response was not valid UTF-8 JSON.", kind="malformed") from exc
    return JSONHTTPResponse(status_code=status, headers=headers, data=data)
