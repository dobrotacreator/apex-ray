from __future__ import annotations

import io
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path, PureWindowsPath

import pytest

from apex_ray.analyzers.dart.lsp import (
    DartLspClient,
    DartLspProcessExited,
    DartLspProtocolError,
    DartLspResponseError,
    DartLspTimeout,
)
from apex_ray.analyzers.dart.protocol import (
    DartLspError,
    encode_lsp_message,
    file_uri_to_path,
    path_to_file_uri,
    python_index_to_utf16_character,
    read_lsp_message,
    utf16_character_to_python_index,
    write_lsp_frame,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dart_lsp" / "fake_server.py"


class _ChunkedReader(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        return super().read(min(size, 3) if size >= 0 else 3)


class _PartialWriter(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushed = False

    def write(self, data: bytes) -> int:
        return super().write(data[:3])

    def flush(self) -> None:
        self.flushed = True
        super().flush()


class _FailingWriter(io.BytesIO):
    def __init__(self, stage: str) -> None:
        super().__init__()
        self.stage = stage

    def write(self, data: bytes) -> int:
        if self.stage == "write":
            raise BrokenPipeError("synthetic broken pipe")
        return super().write(data)

    def flush(self) -> None:
        if self.stage == "flush":
            raise OSError("synthetic flush failure")
        super().flush()


_DEADLINE_TEST_SERVER = r"""
import json
import sys
import time

stdin = sys.stdin.buffer
stdout = sys.stdout.buffer


def read_message():
    content_length = None
    while True:
        line = stdin.readline()
        if line in {b"\r\n", b"\n"}:
            break
        name, value = line.decode("ascii").split(":", 1)
        if name.lower() == "content-length":
            content_length = int(value)
    return json.loads(stdin.read(content_length))


def send(payload):
    body = json.dumps(payload, separators=(",", ":")).encode()
    stdout.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    stdout.flush()


while True:
    message = read_message()
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})
    elif method == "test/timeout":
        send({"jsonrpc": "2.0", "method": "test/requestSeen", "params": {}})
        time.sleep(30)
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        time.sleep(30)
"""


def _client(tmp_path: Path, mode: str = "normal", **kwargs: object) -> DartLspClient:
    return DartLspClient([sys.executable, str(FIXTURE), mode], tmp_path, **kwargs)


def test_framing_reads_partial_binary_chunks_and_unicode() -> None:
    payload = {"jsonrpc": "2.0", "id": 7, "result": {"message": "Привет 👋"}}

    encoded = encode_lsp_message(payload)

    assert encoded.startswith(b"Content-Length: ")
    assert read_lsp_message(_ChunkedReader(encoded)) == payload


def test_framing_writes_the_complete_frame_when_stream_writes_partially() -> None:
    encoded = encode_lsp_message({"jsonrpc": "2.0", "id": 7, "result": "ok"})
    stream = _PartialWriter()

    write_lsp_frame(stream, encoded)

    assert stream.getvalue() == encoded
    assert stream.flushed is True


@pytest.mark.parametrize("stage", ["write", "flush"])
def test_framing_normalizes_stream_io_failures(stage: str) -> None:
    encoded = encode_lsp_message({"jsonrpc": "2.0", "method": "test"})

    with pytest.raises(DartLspError, match="write") as exc_info:
        write_lsp_frame(_FailingWriter(stage), encoded)

    assert isinstance(exc_info.value.__cause__, OSError)


@pytest.mark.parametrize(
    "raw, match",
    [
        (b"X-Test: true\r\n\r\n{}", "Content-Length"),
        (b"Content-Length: nope\r\n\r\n{}", "Content-Length"),
        (b"Content-Length: 5\r\n\r\n{}", "ended before"),
        (b"Content-Length: 2\r\n\r\n[]", "JSON object"),
        (b"Content-Length: 1\r\n\r\n{", "invalid JSON"),
    ],
)
def test_framing_rejects_malformed_messages(raw: bytes, match: str) -> None:
    with pytest.raises(DartLspProtocolError, match=match):
        read_lsp_message(io.BytesIO(raw))


def test_framing_enforces_header_and_content_bounds() -> None:
    with pytest.raises(DartLspProtocolError, match="header"):
        read_lsp_message(io.BytesIO(b"X: " + b"a" * 20 + b"\r\n\r\n{}"), max_header_bytes=16)
    with pytest.raises(DartLspProtocolError, match="exceeds"):
        read_lsp_message(io.BytesIO(b"Content-Length: 100\r\n\r\n"), max_content_bytes=10)


def test_utf16_helpers_handle_surrogate_pairs_and_reject_split_positions() -> None:
    text = "a😀é"

    assert python_index_to_utf16_character(text, 0) == 0
    assert python_index_to_utf16_character(text, 2) == 3
    assert utf16_character_to_python_index(text, 3) == 2
    assert utf16_character_to_python_index(text, 4) == 3
    with pytest.raises(ValueError, match="surrogate pair"):
        utf16_character_to_python_index(text, 2)
    with pytest.raises(ValueError, match="outside"):
        utf16_character_to_python_index(text, 5)


def test_file_uri_round_trip_preserves_spaces_unicode_and_windows_drive(tmp_path: Path) -> None:
    posix_path = (tmp_path / "space dir" / "écran.dart").resolve()
    posix_uri = path_to_file_uri(posix_path)

    assert "%20" in posix_uri
    assert "%C3%A9" in posix_uri
    assert file_uri_to_path(posix_uri) == posix_path

    windows_path = PureWindowsPath("C:/Program Files/应用/main.dart")
    windows_uri = path_to_file_uri(windows_path)
    assert windows_uri == "file:///C:/Program%20Files/%E5%BA%94%E7%94%A8/main.dart"
    assert file_uri_to_path(windows_uri, windows=True) == windows_path
    with pytest.raises(ValueError, match="not absolute"):
        file_uri_to_path("file:relative/main.dart")
    with pytest.raises(ValueError, match="null byte"):
        file_uri_to_path("file:///tmp/%00.dart")


def test_client_initializes_handles_reverse_requests_and_preserves_notifications(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        workspace_configuration={"dart": {"analysis": True}, "flutter": "enabled"},
    ) as client:
        result = client.initialize(
            path_to_file_uri(tmp_path),
            flutter_outline=True,
            initialization_options={"onlyAnalyzeProjectsWithOpenFiles": True},
        )
        reverse = client.wait_for_notification("test/reverseRequestsHandled", timeout=1)
        outline = client.wait_for_notification(
            "dart/textDocument/publishOutline",
            timeout=1,
            consume=False,
        )

        assert result["serverInfo"] == {"name": "fake-dart-lsp", "version": "1.0"}
        assert client.server_capabilities["documentSymbolProvider"] is True
        responses = reverse["params"]["responses"]
        assert responses["server-config"]["result"] == [
            {"analysis": True},
            "enabled",
            True,
        ]
        assert responses["server-register"]["result"] is None
        assert responses["server-progress"]["result"] is None
        assert responses["server-unknown"]["error"]["code"] == -32601
        assert client.notifications("$/progress")[0]["params"]["token"] == "analysis"
        assert outline["params"]["uri"] == "ready"

    assert client.returncode == 0
    assert not client.is_running


def test_client_did_open_sends_utf8_text_document_payload(tmp_path: Path) -> None:
    uri = path_to_file_uri(tmp_path / "lib" / "écran.dart")
    with _client(tmp_path) as client:
        client.initialize(path_to_file_uri(tmp_path), flutter_outline=False)
        client.did_open(uri, "void main() { print('👋'); }\n", version=4)
        notification = client.wait_for_notification("test/didOpen", uri=uri, timeout=1)

    assert notification["params"] == {
        "textDocument": {
            "uri": uri,
            "languageId": "dart",
            "version": 4,
            "text": "void main() { print('👋'); }\n",
        }
    }


def test_open_document_resolves_relative_paths_against_server_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    with _client(tmp_path) as client:
        client.initialize(path_to_file_uri(tmp_path))
        client.open_document("lib/main.dart", "void main() {}\n")
        notification = client.wait_for_notification("test/didOpen", timeout=1)

    assert notification["params"]["textDocument"]["uri"] == path_to_file_uri(tmp_path / "lib" / "main.dart")


def test_client_correlates_concurrent_out_of_order_responses(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        client.initialize(path_to_file_uri(tmp_path))
        results: dict[str, object] = {}

        first = threading.Thread(target=lambda: results.__setitem__("first", client.request("test/first")))
        second = threading.Thread(target=lambda: results.__setitem__("second", client.request("test/second")))
        first.start()
        time.sleep(0.02)
        second.start()
        first.join(timeout=1)
        second.join(timeout=1)

        assert not first.is_alive()
        assert not second.is_alive()
        assert results == {"first": "first", "second": "second"}


def test_client_surfaces_json_rpc_errors(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        client.initialize(path_to_file_uri(tmp_path))
        with pytest.raises(DartLspResponseError, match="synthetic failure") as exc_info:
            client.request("test/error")

    assert exc_info.value.code == -32001
    assert exc_info.value.data == {"retry": False}


def test_client_enforces_request_and_global_deadlines(tmp_path: Path) -> None:
    with _client(tmp_path, timeout=0.05) as client:
        with pytest.raises(DartLspTimeout, match="test/timeout"):
            client.request("test/timeout")
        cancelled = client.wait_for_notification("test/cancelled", timeout=1)
        assert cancelled["params"] == {"id": 1}

    with _client(tmp_path, timeout=5, deadline=time.monotonic() + 0.05) as client:
        with pytest.raises(DartLspTimeout, match="global deadline"):
            client.request("test/timeout")


def test_timed_out_request_does_not_wait_for_a_fresh_cancellation_timeout(tmp_path: Path) -> None:
    client = DartLspClient(
        [sys.executable, "-c", _DEADLINE_TEST_SERVER],
        tmp_path,
        timeout=2,
    )
    client.start()
    request_errors: list[BaseException] = []
    notification_errors: list[BaseException] = []
    request = threading.Thread(
        target=lambda: _capture_error(
            request_errors,
            lambda: client.request("test/timeout", timeout=0.2),
        )
    )
    blocked_notification = threading.Thread(
        target=lambda: _capture_error(
            notification_errors,
            lambda: client.notify("test/fill-stdin", {"payload": "x" * 2_000_000}),
        )
    )

    try:
        request.start()
        client.wait_for_notification("test/requestSeen", timeout=1)
        blocked_notification.start()
        request.join(timeout=0.5)

        assert not request.is_alive()
        assert len(request_errors) == 1
        assert isinstance(request_errors[0], DartLspTimeout)
    finally:
        client.close()
        request.join(timeout=1)
        blocked_notification.join(timeout=1)


def test_graceful_shutdown_wait_obeys_global_analyzer_deadline(tmp_path: Path) -> None:
    deadline = time.monotonic() + 1
    client = DartLspClient(
        [sys.executable, "-c", _DEADLINE_TEST_SERVER],
        tmp_path,
        timeout=5,
        deadline=deadline,
    )
    client.start()
    client.initialize(path_to_file_uri(tmp_path))

    started = time.monotonic()
    client.shutdown()
    elapsed = time.monotonic() - started

    assert elapsed < 1.8
    assert not client.is_running


def test_client_write_deadline_terminates_unresponsive_server_and_wakes_requests(tmp_path: Path) -> None:
    client = DartLspClient(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        timeout=0.05,
    )
    client.start()
    errors: list[BaseException] = []
    blocked = threading.Thread(
        target=lambda: _capture_error(
            errors,
            lambda: client.request("test/blocked-write", {"payload": "x" * 1_000_000}),
        )
    )
    pending = threading.Thread(target=lambda: _capture_error(errors, lambda: client.request("test/pending-write", {})))

    try:
        blocked.start()
        time.sleep(0.01)
        pending.start()
        blocked.join(timeout=0.5)
        pending.join(timeout=0.5)

        assert not blocked.is_alive()
        assert not pending.is_alive()
        assert len(errors) == 2
        assert all(isinstance(error, DartLspTimeout) for error in errors)
        assert not client.is_running
    finally:
        client.close()
        blocked.join(timeout=1)
        pending.join(timeout=1)


def test_client_notify_write_obeys_timeout_when_server_stops_reading(tmp_path: Path) -> None:
    client = DartLspClient(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        timeout=0.05,
    )
    client.start()
    errors: list[BaseException] = []
    notification = threading.Thread(
        target=lambda: _capture_error(
            errors,
            lambda: client.notify("test/blocked-notification", {"payload": "x" * 1_000_000}),
        )
    )

    try:
        notification.start()
        notification.join(timeout=0.5)

        assert not notification.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], DartLspTimeout)
        assert not client.is_running
    finally:
        client.close()
        notification.join(timeout=1)


def test_client_reports_malformed_frames_and_process_exit_with_bounded_stderr(tmp_path: Path) -> None:
    with _client(tmp_path, "malformed") as malformed:
        with pytest.raises(DartLspProtocolError, match="Content-Length"):
            malformed.request("test/echo", {})

    with _client(tmp_path, "exit") as exited:
        with pytest.raises(DartLspProcessExited, match="intentional fake server exit") as exc_info:
            exited.request("test/echo", {})
    assert exc_info.value.returncode == 7

    with _client(tmp_path, "stderr", stderr_limit_bytes=128) as noisy:
        assert noisy.request("test/echo", {"ok": True}) == {"ok": True}
        deadline = time.monotonic() + 1
        while "TAIL-MARKER" not in noisy.stderr_tail and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(noisy.stderr_tail.encode()) <= 128
        assert noisy.stderr_tail.endswith("TAIL-MARKER")


def test_client_bounds_aggregate_notification_memory(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        notification_limit=20,
        notification_bytes_limit=800,
    ) as client:
        assert client.request("test/floodNotifications", {"count": 10, "payloadSize": 100}) == 10
        notifications = client.notifications("test/flood")

    assert 0 < len(notifications) < 10
    assert notifications[-1]["params"]["sequence"] == 9
    assert client.notification_bytes <= 800
    assert client.dropped_notifications == 10 - len(notifications)


def test_client_passes_explicit_environment_without_mutating_parent(tmp_path: Path) -> None:
    with _client(tmp_path, "environment", env={"APEX_RAY_LSP_TEST": "isolated"}) as client:
        notification = client.wait_for_notification("test/environment", timeout=1)

    assert notification["params"] == {"value": "isolated"}


def test_close_terminates_the_server_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "orphan.txt"
    client = DartLspClient(
        [sys.executable, str(FIXTURE), "spawn-child", str(marker)],
        tmp_path,
        timeout=0.1,
    )
    client.start()

    deadline = time.monotonic() + 1
    while not client.stderr_tail.strip() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.stderr_tail.strip().isdigit()

    client.close()
    time.sleep(1.2)

    assert client.returncode is not None
    assert not marker.exists()


def test_close_wakes_pending_requests_and_failed_shutdown_still_cleans_up(tmp_path: Path) -> None:
    client = _client(tmp_path, timeout=5)
    client.start()
    errors: list[BaseException] = []
    request = threading.Thread(
        target=lambda: _capture_error(errors, lambda: client.request("test/timeout")),
    )
    request.start()
    time.sleep(0.05)

    client.close()
    request.join(timeout=1)

    assert not request.is_alive()
    assert len(errors) == 1
    assert "closed" in str(errors[0]).lower()

    shutdown_client = _client(tmp_path, "shutdown-error")
    shutdown_client.start()
    shutdown_client.initialize(path_to_file_uri(tmp_path))
    with pytest.raises(DartLspResponseError, match="shutdown rejected"):
        shutdown_client.shutdown()
    assert not shutdown_client.is_running


def _capture_error(errors: list[BaseException], operation: Callable[[], object]) -> None:
    try:
        operation()
    except BaseException as exc:
        errors.append(exc)
