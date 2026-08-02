"""Small, dependency-free primitives for the Dart analysis server LSP protocol."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import BinaryIO
from urllib.parse import quote, unquote, urlsplit

DEFAULT_MAX_HEADER_BYTES = 16 * 1024
DEFAULT_MAX_CONTENT_BYTES = 16 * 1024 * 1024
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class DartLspError(RuntimeError):
    """Base class for Dart language-server transport failures."""


class DartLspProtocolError(DartLspError):
    """The peer sent an invalid LSP or JSON-RPC message."""


def encode_lsp_message(payload: Mapping[str, object]) -> bytes:
    """Encode one JSON-RPC object with LSP ``Content-Length`` framing."""

    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DartLspProtocolError(f"LSP payload is not JSON serializable: {exc}") from exc
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def write_lsp_frame(stream: BinaryIO, frame: bytes) -> None:
    """Write and flush one complete encoded LSP frame.

    Raw subprocess pipes may legally report a short write. Keep advancing until
    the entire frame is delivered so the peer cannot be left waiting for bytes
    promised by ``Content-Length``.
    """

    view = memoryview(frame)
    offset = 0
    while offset < len(view):
        written = stream.write(view[offset:])
        if written is None or written <= 0 or written > len(view) - offset:
            raise DartLspProtocolError("Dart LSP stream made no progress while writing a frame")
        offset += written
    stream.flush()


def read_lsp_message(
    stream: BinaryIO,
    *,
    max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
) -> dict[str, object]:
    """Read one framed JSON-RPC object from a binary stream.

    A clean EOF before the next header is represented by :class:`EOFError`.
    Truncated or malformed frames are protocol errors so callers can distinguish a
    crashed server from an ordinary end-of-stream after shutdown.
    """

    if max_header_bytes <= 0 or max_content_bytes <= 0:
        raise ValueError("LSP frame limits must be positive")

    headers: dict[str, str] = {}
    header_bytes = 0
    saw_header = False
    while True:
        remaining = max_header_bytes - header_bytes
        if remaining <= 0:
            raise DartLspProtocolError(f"LSP header exceeds {max_header_bytes} bytes")
        line = stream.readline(remaining + 1)
        if not line:
            if not saw_header:
                raise EOFError
            raise DartLspProtocolError("LSP stream ended before the header terminator")
        saw_header = True
        header_bytes += len(line)
        if header_bytes > max_header_bytes:
            raise DartLspProtocolError(f"LSP header exceeds {max_header_bytes} bytes")
        if line in {b"\r\n", b"\n"}:
            break
        if not line.endswith(b"\n"):
            raise DartLspProtocolError("LSP header line is not newline terminated")
        try:
            decoded = line.rstrip(b"\r\n").decode("ascii")
        except UnicodeDecodeError as exc:
            raise DartLspProtocolError("LSP headers must be ASCII") from exc
        if ":" not in decoded:
            raise DartLspProtocolError(f"Malformed LSP header line: {decoded!r}")
        name, value = decoded.split(":", 1)
        normalized_name = name.strip().lower()
        if not normalized_name:
            raise DartLspProtocolError("Malformed LSP header with an empty name")
        if normalized_name in headers:
            raise DartLspProtocolError(f"Duplicate LSP header: {name.strip()}")
        headers[normalized_name] = value.strip()

    raw_length = headers.get("content-length")
    if raw_length is None:
        raise DartLspProtocolError("LSP frame is missing the Content-Length header")
    try:
        content_length = int(raw_length, 10)
    except ValueError as exc:
        raise DartLspProtocolError(f"Invalid LSP Content-Length: {raw_length!r}") from exc
    if content_length < 0:
        raise DartLspProtocolError(f"Invalid LSP Content-Length: {raw_length!r}")
    if content_length > max_content_bytes:
        raise DartLspProtocolError(f"LSP content length {content_length} exceeds the {max_content_bytes}-byte limit")

    body = _read_exact(stream, content_length)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DartLspProtocolError(f"LSP frame contains invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DartLspProtocolError("LSP frame payload must be a JSON object")
    if not all(isinstance(key, str) for key in payload):
        raise DartLspProtocolError("LSP frame payload keys must be strings")
    return payload


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    remaining = size
    chunks: list[bytes] = []
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            received = size - remaining
            raise DartLspProtocolError(f"LSP stream ended before the message body ({received}/{size} bytes received)")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def python_index_to_utf16_character(text: str, index: int) -> int:
    """Convert a Python code-point index within a line to an LSP UTF-16 offset."""

    if index < 0 or index > len(text):
        raise ValueError(f"Python index {index} is outside text of length {len(text)}")
    return sum(2 if ord(character) > 0xFFFF else 1 for character in text[:index])


def utf16_character_to_python_index(text: str, character: int) -> int:
    """Convert an LSP UTF-16 offset within a line to a Python code-point index."""

    if character < 0:
        raise ValueError(f"UTF-16 character {character} is outside the line")
    consumed = 0
    for index, value in enumerate(text):
        if consumed == character:
            return index
        width = 2 if ord(value) > 0xFFFF else 1
        if consumed < character < consumed + width:
            raise ValueError(f"UTF-16 character {character} splits a surrogate pair")
        consumed += width
    if consumed == character:
        return len(text)
    raise ValueError(f"UTF-16 character {character} is outside the line ({consumed} code units)")


def path_to_file_uri(path: str | os.PathLike[str]) -> str:
    """Return a percent-encoded file URI for an absolute native or Windows path."""

    pure_path = _coerce_pure_path(path)
    if not pure_path.is_absolute():
        raise ValueError(f"File URI requires an absolute path: {pure_path}")
    if isinstance(pure_path, PureWindowsPath):
        posix = pure_path.as_posix()
        if posix.startswith("//"):
            authority_and_path = posix[2:]
            authority, separator, uri_path = authority_and_path.partition("/")
            if not separator:
                raise ValueError(f"UNC path requires a share: {pure_path}")
            return f"file://{quote(authority, safe='')}/{quote(uri_path, safe='/@:')}"
        return f"file:///{quote(posix, safe='/@:')}"
    return f"file://{quote(pure_path.as_posix(), safe='/@:')}"


def file_uri_to_path(uri: str, *, windows: bool | None = None) -> Path | PureWindowsPath:
    """Convert a local file URI to a native path or an explicitly Windows path."""

    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "file":
        raise ValueError(f"Expected a file URI, got: {uri}")
    if parsed.query or parsed.fragment:
        raise ValueError("File URIs with query strings or fragments are unsupported")

    use_windows = os.name == "nt" if windows is None else windows
    decoded_path = unquote(parsed.path)
    authority = unquote(parsed.netloc)
    if "\x00" in decoded_path or "\x00" in authority:
        raise ValueError("File URI contains a null byte")
    if use_windows:
        if authority and authority.lower() != "localhost":
            pure_windows_path = PureWindowsPath(f"//{authority}{decoded_path}")
            return Path(pure_windows_path) if windows is None else pure_windows_path
        if len(decoded_path) >= 3 and decoded_path[0] == "/" and decoded_path[2] == ":":
            decoded_path = decoded_path[1:]
        pure_windows_path = PureWindowsPath(decoded_path)
        if not pure_windows_path.is_absolute():
            raise ValueError(f"File URI path is not absolute: {uri}")
        return Path(pure_windows_path) if windows is None else pure_windows_path
    if authority and authority.lower() != "localhost":
        raise ValueError(f"Non-local file URI authority is unsupported on this platform: {authority}")
    native_path = Path(decoded_path)
    if not native_path.is_absolute():
        raise ValueError(f"File URI path is not absolute: {uri}")
    return native_path


def _coerce_pure_path(path: str | os.PathLike[str]) -> PurePath:
    if isinstance(path, PureWindowsPath):
        return path
    if isinstance(path, PurePosixPath):
        return path
    raw = os.fspath(path)
    if _WINDOWS_DRIVE_RE.match(raw) or raw.startswith("\\\\"):
        return PureWindowsPath(raw)
    return Path(raw)
