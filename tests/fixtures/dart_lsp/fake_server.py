"""Deterministic subprocess used by Dart LSP transport tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_message(stream: BinaryIO) -> dict[str, object]:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            raise EOFError
        if line in {b"\r\n", b"\n"}:
            break
        name, value = line.decode("ascii").split(":", 1)
        headers[name.lower()] = value.strip()
    body = _read_exact(stream, int(headers["content-length"]))
    payload = json.loads(body)
    assert isinstance(payload, dict)
    return payload


def _write_message(stream: BinaryIO, payload: dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stream.write(body)
    stream.flush()


def _server_request(stream: BinaryIO, request_id: str, method: str, params: object) -> None:
    _write_message(
        stream,
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )


def _run_normal(stdin: BinaryIO, stdout: BinaryIO, *, shutdown_error: bool = False) -> int:
    held_requests: dict[str, object] = {}
    reverse_responses: dict[str, dict[str, object]] = {}
    while True:
        message = _read_message(stdin)
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            _server_request(
                stdout,
                "server-config",
                "workspace/configuration",
                {
                    "items": [
                        {"section": "dart"},
                        {"section": "flutter"},
                        {"section": "dart.analysis"},
                    ]
                },
            )
            _server_request(
                stdout,
                "server-register",
                "client/registerCapability",
                {"registrations": []},
            )
            _server_request(
                stdout,
                "server-progress",
                "window/workDoneProgress/create",
                {"token": "analysis"},
            )
            _server_request(stdout, "server-unknown", "test/unsupportedServerRequest", {})
            _write_message(
                stdout,
                {
                    "jsonrpc": "2.0",
                    "method": "$/progress",
                    "params": {"token": "analysis", "value": {"kind": "begin", "title": "Analyzing"}},
                },
            )
            _write_message(
                stdout,
                {
                    "jsonrpc": "2.0",
                    "method": "test/initializeParams",
                    "params": message.get("params"),
                },
            )
            _write_message(
                stdout,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "capabilities": {
                            "documentSymbolProvider": True,
                            "callHierarchyProvider": True,
                        },
                        "serverInfo": {"name": "fake-dart-lsp", "version": "1.0"},
                    },
                },
            )
            continue

        if method == "initialized":
            _write_message(
                stdout,
                {"jsonrpc": "2.0", "method": "dart/textDocument/publishOutline", "params": {"uri": "ready"}},
            )
            continue

        if method == "textDocument/didOpen":
            _write_message(
                stdout,
                {"jsonrpc": "2.0", "method": "test/didOpen", "params": message.get("params")},
            )
            continue

        if method == "test/first":
            held_requests["first"] = request_id
            continue

        if method == "test/second":
            _write_message(stdout, {"jsonrpc": "2.0", "id": request_id, "result": "second"})
            _write_message(stdout, {"jsonrpc": "2.0", "id": held_requests.pop("first"), "result": "first"})
            continue

        if method == "test/echo":
            _write_message(stdout, {"jsonrpc": "2.0", "id": request_id, "result": message.get("params")})
            continue

        if method == "test/error":
            _write_message(
                stdout,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32001, "message": "synthetic failure", "data": {"retry": False}},
                },
            )
            continue

        if method == "test/timeout":
            continue

        if method == "test/floodNotifications":
            assert isinstance(message.get("params"), dict)
            params = message["params"]
            assert isinstance(params, dict)
            count = params.get("count")
            payload_size = params.get("payloadSize")
            assert isinstance(count, int)
            assert isinstance(payload_size, int)
            for sequence in range(count):
                _write_message(
                    stdout,
                    {
                        "jsonrpc": "2.0",
                        "method": "test/flood",
                        "params": {"sequence": sequence, "payload": "x" * payload_size},
                    },
                )
            _write_message(stdout, {"jsonrpc": "2.0", "id": request_id, "result": count})
            continue

        if method == "test/floodNotificationSnapshots":
            assert isinstance(message.get("params"), dict)
            params = message["params"]
            assert isinstance(params, dict)
            count = params.get("count")
            allowed_uri = params.get("allowedUri")
            assert isinstance(count, int)
            assert isinstance(allowed_uri, str)
            for sequence in range(count):
                _write_message(
                    stdout,
                    {
                        "jsonrpc": "2.0",
                        "method": "dart/textDocument/publishOutline",
                        "params": {"uri": allowed_uri, "outline": {"sequence": sequence}},
                    },
                )
                _write_message(
                    stdout,
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/publishDiagnostics",
                        "params": {
                            "uri": f"file:///foreign/{sequence}.dart",
                            "diagnostics": [{"message": f"foreign-{sequence}"}],
                        },
                    },
                )
                _write_message(
                    stdout,
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/publishDiagnostics",
                        "params": {
                            "uri": allowed_uri,
                            "diagnostics": [{"message": f"diagnostic-{sequence}"}],
                        },
                    },
                )
                _write_message(
                    stdout,
                    {
                        "jsonrpc": "2.0",
                        "method": "dart/textDocument/publishFlutterOutline",
                        "params": {
                            "uri": allowed_uri,
                            "outline": {"kind": "DART_ELEMENT", "label": f"outline-{sequence}"},
                        },
                    },
                )
            _write_message(stdout, {"jsonrpc": "2.0", "id": request_id, "result": count * 4})
            continue

        if method == "$/cancelRequest":
            _write_message(
                stdout,
                {"jsonrpc": "2.0", "method": "test/cancelled", "params": message.get("params")},
            )
            continue

        if method == "shutdown":
            if shutdown_error:
                _write_message(
                    stdout,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32002, "message": "shutdown rejected"},
                    },
                )
            else:
                _write_message(stdout, {"jsonrpc": "2.0", "id": request_id, "result": None})
            continue

        if method == "exit":
            return 0

        if method is not None and request_id is not None:
            _write_message(stdout, {"jsonrpc": "2.0", "id": request_id, "result": None})
            continue

        if request_id in {"server-config", "server-register", "server-progress", "server-unknown"}:
            reverse_responses[str(request_id)] = message
            if len(reverse_responses) == 4:
                _write_message(
                    stdout,
                    {
                        "jsonrpc": "2.0",
                        "method": "test/reverseRequestsHandled",
                        "params": {"responses": reverse_responses},
                    },
                )


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    if mode == "malformed":
        stdout.write(b"Content-Length: nope\r\n\r\n{}")
        stdout.flush()
        time.sleep(0.05)
        return 3
    if mode == "exit":
        sys.stderr.write("intentional fake server exit\n")
        sys.stderr.flush()
        return 7
    if mode == "stderr":
        sys.stderr.write("x" * 200_000 + "TAIL-MARKER")
        sys.stderr.flush()
        return _run_normal(stdin, stdout)
    if mode == "spawn-child":
        marker = Path(sys.argv[2])
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import pathlib,time,sys; time.sleep(1); pathlib.Path(sys.argv[1]).write_text('alive')",
                str(marker),
            ]
        )
        print(child.pid, file=sys.stderr, flush=True)
        time.sleep(30)
        return 0
    if mode == "environment":
        _write_message(
            stdout,
            {"jsonrpc": "2.0", "method": "test/environment", "params": {"value": os.getenv("APEX_RAY_LSP_TEST")}},
        )
        return _run_normal(stdin, stdout)
    if mode == "shutdown-error":
        return _run_normal(stdin, stdout, shutdown_error=True)
    return _run_normal(stdin, stdout)


if __name__ == "__main__":
    raise SystemExit(main())
