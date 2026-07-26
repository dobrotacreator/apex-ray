from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


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
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            headers=dict(headers),
            method="POST",
        )
        try:
            if use_system_proxy:
                opener = build_opener(_NoRedirectHandler())
            else:
                opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
            response = opener.open(request, timeout=timeout_seconds)
            with response:
                return _response_from_handle(response)
        except HTTPError as exc:
            with exc:
                return _response_from_handle(exc)
        except TimeoutError as exc:
            raise JSONTransportError("API request timed out.", kind="timeout") from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise JSONTransportError("API request timed out.", kind="timeout") from exc
            raise JSONTransportError("API request failed at the network boundary.", kind="network") from exc
        except OSError as exc:
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
        raise JSONTransportError("API response exceeded the maximum supported size.", kind="malformed")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if not 200 <= status < 300:
            return JSONHTTPResponse(status_code=status, headers=headers, data={})
        raise JSONTransportError("API response was not valid UTF-8 JSON.", kind="malformed") from exc
    return JSONHTTPResponse(status_code=status, headers=headers, data=data)
