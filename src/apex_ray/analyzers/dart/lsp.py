"""Synchronous, bounded JSON-RPC transport for the Dart analysis server."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from queue import Empty, Full, Queue
from typing import BinaryIO, Final

from .protocol import (
    DEFAULT_MAX_CONTENT_BYTES,
    DartLspError,
    DartLspProtocolError,
    encode_lsp_message,
    path_to_file_uri,
    read_lsp_message,
    write_lsp_frame,
)

_DEFAULT_NOTIFICATION_LIMIT: Final = 512
_DEFAULT_NOTIFICATION_BYTES_LIMIT: Final = 32 * 1024 * 1024
_INBOUND_QUEUE_LIMIT: Final = 16
_WRITE_QUEUE_LIMIT: Final = 16
_THREAD_JOIN_TIMEOUT_SECONDS: Final = 0.5
_PROCESS_GRACE_SECONDS: Final = 0.5


class DartLspTimeout(DartLspError):
    """A language-server operation exceeded its deadline."""


class DartLspProcessExited(DartLspError):
    """The language-server process exited or closed stdout unexpectedly."""

    def __init__(self, message: str, *, returncode: int | None, stderr_tail: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail


class DartLspResponseError(DartLspError):
    """The server returned a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"Dart LSP request failed ({code}): {message}")
        self.code = code
        self.message = message
        self.data = data


class _StreamClosed:
    pass


class _ReaderFailed:
    def __init__(self, error: DartLspError) -> None:
        self.error = error


class _WriteJob:
    def __init__(self, frame: bytes) -> None:
        self.frame = frame
        self.completion: Queue[DartLspError | None] = Queue(maxsize=1)


type _Inbound = dict[str, object] | _StreamClosed | _ReaderFailed
type _Pending = dict[str, object] | DartLspError


class DartLspClient:
    """Thread-safe synchronous LSP client around one Dart language-server process.

    ``deadline`` is an absolute ``time.monotonic()`` value for the whole analyzer
    run. Each request is additionally constrained by its own timeout/deadline.
    """

    def __init__(
        self,
        command: Sequence[str],
        cwd: Path,
        *,
        timeout: float = 10.0,
        deadline: float | None = None,
        request_timeout: float | None = None,
        global_deadline: float | None = None,
        stderr_limit_bytes: int = 64 * 1024,
        notification_limit: int = _DEFAULT_NOTIFICATION_LIMIT,
        notification_bytes_limit: int = _DEFAULT_NOTIFICATION_BYTES_LIMIT,
        max_message_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
        env: Mapping[str, str] | None = None,
        workspace_configuration: Mapping[str, object] | None = None,
    ) -> None:
        if not command or not all(command):
            raise ValueError("Dart LSP command must contain at least one non-empty argument")
        if request_timeout is not None:
            if timeout != 10.0 and timeout != request_timeout:
                raise ValueError("Specify either timeout or request_timeout, not both")
            timeout = request_timeout
        if global_deadline is not None:
            if deadline is not None and deadline != global_deadline:
                raise ValueError("Specify either deadline or global_deadline, not both")
            deadline = global_deadline
        if timeout <= 0:
            raise ValueError("Dart LSP request timeout must be positive")
        if (
            stderr_limit_bytes <= 0
            or notification_limit <= 0
            or notification_bytes_limit <= 0
            or max_message_bytes <= 0
        ):
            raise ValueError("Dart LSP buffer limits must be positive")

        self.command = [str(argument) for argument in command]
        self.cwd = Path(cwd)
        self.timeout = float(timeout)
        self.deadline = deadline
        self.stderr_limit_bytes = stderr_limit_bytes
        self.notification_limit = notification_limit
        self.notification_bytes_limit = notification_bytes_limit
        self.max_message_bytes = max_message_bytes
        self._extra_env = dict(env or {})
        self._workspace_configuration = dict(workspace_configuration or {})

        self._process: subprocess.Popen[bytes] | None = None
        self._inbound: Queue[_Inbound] = Queue(maxsize=_INBOUND_QUEUE_LIMIT)
        self._writes: Queue[_WriteJob] = Queue(maxsize=_WRITE_QUEUE_LIMIT)
        self._write_waiters: set[_WriteJob] = set()
        self._pending: dict[int | str, Queue[_Pending]] = {}
        self._pending_lock = threading.Lock()
        self._failure_lock = threading.Lock()
        self._failure: DartLspError | None = None
        self._request_id = 0
        self._request_id_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._notifications: deque[dict[str, object]] = deque()
        self._notification_sizes: deque[int] = deque()
        self._notification_bytes = 0
        self._notification_condition = threading.Condition()
        self._dropped_notifications = 0
        self._initialized = False
        self._shutdown_started = False
        self._expecting_exit = False
        self._root_uri: str | None = None
        self.server_capabilities: dict[str, object] = {}

    def __enter__(self) -> DartLspClient:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None and self.is_running and self._failure is None:
            try:
                self.shutdown()
                return
            except DartLspError:
                pass
        self.close()

    @property
    def returncode(self) -> int | None:
        process = self._process
        return None if process is None else process.poll()

    @property
    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def stderr_tail(self) -> str:
        with self._stderr_lock:
            return bytes(self._stderr).decode("utf-8", errors="replace")

    @property
    def dropped_notifications(self) -> int:
        with self._notification_condition:
            return self._dropped_notifications

    @property
    def notification_bytes(self) -> int:
        """Approximate serialized bytes retained by the notification buffer."""

        with self._notification_condition:
            return self._notification_bytes

    def start(self) -> None:
        if self._process is not None:
            if self.is_running:
                return
            raise DartLspError("Dart language server client cannot be restarted")
        if not self.cwd.is_dir():
            raise DartLspError(f"Dart language server working directory does not exist: {self.cwd}")

        process_env = os.environ.copy()
        process_env.update(self._extra_env)
        try:
            if os.name == "nt":
                self._process = subprocess.Popen(
                    self.command,
                    cwd=self.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=process_env,
                    bufsize=0,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                self._process = subprocess.Popen(
                    self.command,
                    cwd=self.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=process_env,
                    bufsize=0,
                    start_new_session=True,
                )
        except OSError as exc:
            raise DartLspError(f"Unable to start Dart language server {self.command[0]!r}: {exc}") from exc

        process = self._process
        assert process.stdout is not None
        assert process.stderr is not None
        assert process.stdin is not None
        self._threads = [
            threading.Thread(target=self._stdout_reader, args=(process.stdout,), name="dart-lsp-stdout", daemon=True),
            threading.Thread(target=self._stderr_reader, args=(process.stderr,), name="dart-lsp-stderr", daemon=True),
            threading.Thread(target=self._stdin_writer, args=(process.stdin,), name="dart-lsp-stdin", daemon=True),
            threading.Thread(target=self._dispatcher, name="dart-lsp-dispatch", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def initialize(
        self,
        root_uri: str,
        flutter_outline: bool = False,
        *,
        initialization_options: Mapping[str, object] | None = None,
        capabilities: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self._ensure_started()
        if self._initialized:
            raise DartLspError("Dart language server is already initialized")
        options: dict[str, object] = {
            "onlyAnalyzeProjectsWithOpenFiles": True,
            "outline": True,
            "flutterOutline": flutter_outline,
        }
        options.update(initialization_options or {})
        client_capabilities = _default_client_capabilities()
        if capabilities is not None:
            client_capabilities = _deep_merge(client_capabilities, capabilities)
        self._root_uri = root_uri
        root_path = file_uri_to_native_path(root_uri)
        params: dict[str, object] = {
            "processId": os.getpid(),
            "clientInfo": {"name": "apex-ray"},
            "rootPath": str(root_path) if root_path is not None else None,
            "rootUri": root_uri,
            "capabilities": client_capabilities,
            "initializationOptions": options,
            "workspaceFolders": [{"uri": root_uri, "name": Path(root_path or self.cwd).name}],
        }
        response = self.request("initialize", params)
        if not isinstance(response, dict):
            raise DartLspProtocolError("Dart LSP initialize result must be a JSON object")
        raw_capabilities = response.get("capabilities", {})
        if not isinstance(raw_capabilities, dict):
            raise DartLspProtocolError("Dart LSP initialize capabilities must be a JSON object")
        self.server_capabilities = dict(raw_capabilities)
        self._initialized = True
        self.notify("initialized", {})
        return response

    def did_open(
        self,
        uri: str,
        text: str,
        *,
        version: int = 1,
        language_id: str = "dart",
    ) -> None:
        if not self._initialized:
            raise DartLspError("Dart language server must be initialized before opening documents")
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": version,
                    "text": text,
                }
            },
        )

    def open_document(
        self,
        path_or_uri: str | Path,
        text: str,
        *,
        version: int = 1,
        language_id: str = "dart",
    ) -> None:
        raw = str(path_or_uri)
        if raw.startswith("file:"):
            uri = raw
        else:
            path = Path(path_or_uri)
            uri = path_to_file_uri(path if path.is_absolute() else self.cwd / path)
        self.did_open(uri, text, version=version, language_id=language_id)

    def did_close(self, uri: str) -> None:
        self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def request(
        self,
        method: str,
        params: object = None,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> object:
        self._ensure_started()
        effective_deadline, deadline_description = self._request_deadline(
            method,
            timeout=timeout,
            deadline=deadline,
        )
        if effective_deadline <= time.monotonic():
            raise DartLspTimeout(f"Dart LSP request {method!r} exceeded {deadline_description}")
        request_id = self._next_request_id()
        response_queue: Queue[_Pending] = Queue(maxsize=1)
        with self._pending_lock:
            self._raise_if_failed()
            self._pending[request_id] = response_queue
        payload: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._send_payload(
                payload,
                deadline=effective_deadline,
                deadline_description=deadline_description,
                operation=f"request {method!r}",
            )
            remaining = effective_deadline - time.monotonic()
            if remaining <= 0:
                raise Empty
            response = response_queue.get(timeout=remaining)
        except Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self._cancel_request(request_id)
            raise DartLspTimeout(f"Dart LSP request {method!r} exceeded {deadline_description}") from exc
        except BaseException:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise

        if isinstance(response, DartLspError):
            raise response
        error = response.get("error")
        if error is not None:
            if not isinstance(error, dict):
                raise DartLspProtocolError(f"Dart LSP error response for {method!r} is malformed")
            code = error.get("code", -32603)
            message = error.get("message", "Unknown JSON-RPC error")
            if not isinstance(code, int) or not isinstance(message, str):
                raise DartLspProtocolError(f"Dart LSP error response for {method!r} is malformed")
            raise DartLspResponseError(code, message, error.get("data"))
        if "result" not in response:
            raise DartLspProtocolError(f"Dart LSP response for {method!r} has neither result nor error")
        return response["result"]

    def notify(
        self,
        method: str,
        params: object = None,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> None:
        self._ensure_started()
        effective_deadline, deadline_description = self._request_deadline(
            method,
            timeout=timeout,
            deadline=deadline,
        )
        if effective_deadline <= time.monotonic():
            raise DartLspTimeout(f"Dart LSP notification {method!r} exceeded {deadline_description}")
        payload: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send_payload(
            payload,
            deadline=effective_deadline,
            deadline_description=deadline_description,
            operation=f"notification {method!r}",
        )

    def notifications(self, method: str | None = None, uri: str | None = None) -> list[dict[str, object]]:
        with self._notification_condition:
            return [
                message.copy()
                for message in self._notifications
                if (method is None or message.get("method") == method)
                and (uri is None or _notification_uri(message) == uri)
            ]

    def wait_for_notification(
        self,
        method: str,
        *,
        uri: str | None = None,
        timeout: float | None = None,
        deadline: float | None = None,
        consume: bool = True,
    ) -> dict[str, object]:
        effective_deadline, deadline_description = self._request_deadline(
            method,
            timeout=timeout,
            deadline=deadline,
        )
        with self._notification_condition:
            while True:
                for index, message in enumerate(self._notifications):
                    if message.get("method") == method and (uri is None or _notification_uri(message) == uri):
                        if consume:
                            del self._notifications[index]
                            self._notification_bytes -= self._notification_sizes[index]
                            del self._notification_sizes[index]
                        return message.copy()
                self._raise_if_failed()
                remaining = effective_deadline - time.monotonic()
                if remaining <= 0:
                    raise DartLspTimeout(f"Dart LSP notification {method!r} exceeded {deadline_description}")
                self._notification_condition.wait(timeout=remaining)

    def shutdown(self) -> None:
        if self._process is None or self._shutdown_started:
            return
        self._shutdown_started = True
        if not self._initialized:
            self.close()
            return
        try:
            if self.is_running and self._failure is None:
                self.request("shutdown", None, timeout=min(self.timeout, 2.0))
                self._expecting_exit = True
                self.notify("exit")
                process = self._process
                try:
                    process.wait(timeout=min(self.timeout, 2.0))
                except subprocess.TimeoutExpired:
                    self._terminate_process_group()
        finally:
            if self.is_running:
                self._terminate_process_group()
            self._finish_threads()

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._set_failure(DartLspError("Dart language server client was closed"))
        if process.poll() is None:
            self._terminate_process_group()
        self._finish_threads()

    def _ensure_started(self) -> None:
        if self._process is None:
            raise DartLspError("Dart language server has not been started")
        self._raise_if_failed()
        if self._process.poll() is not None:
            raise self._process_exit_error()

    def _request_deadline(
        self,
        method: str,
        *,
        timeout: float | None,
        deadline: float | None,
    ) -> tuple[float, str]:
        del method
        request_timeout = self.timeout if timeout is None else timeout
        if request_timeout <= 0 or request_timeout != request_timeout:
            raise ValueError("Dart LSP request timeout must be positive")
        if deadline is not None and deadline != deadline:
            raise ValueError("Dart LSP request deadline must not be NaN")
        local_deadline = time.monotonic() + request_timeout
        description = f"{request_timeout:g}s timeout"
        if deadline is not None and deadline < local_deadline:
            local_deadline = deadline
            description = "request deadline"
        if self.deadline is not None and self.deadline <= local_deadline:
            local_deadline = self.deadline
            description = "global deadline"
        return local_deadline, description

    def _next_request_id(self) -> int:
        with self._request_id_lock:
            self._request_id += 1
            return self._request_id

    def _send_payload(
        self,
        payload: Mapping[str, object],
        *,
        deadline: float | None = None,
        deadline_description: str | None = None,
        operation: str = "message",
    ) -> None:
        encoded = encode_lsp_message(payload)
        if deadline is None:
            deadline, default_description = self._request_deadline(operation, timeout=None, deadline=None)
            deadline_description = deadline_description or default_description
        description = deadline_description or "write deadline"
        if deadline <= time.monotonic():
            raise DartLspTimeout(f"Dart LSP {operation} exceeded {description}")

        job = _WriteJob(encoded)
        with self._failure_lock:
            if self._failure is not None:
                raise self._failure
            self._write_waiters.add(job)
        try:
            self._enqueue_write(job, deadline, operation, description)
            try:
                outcome = job.completion.get(timeout=max(0.0, deadline - time.monotonic()))
            except Empty as exc:
                error = self._write_timeout(operation, description)
                raise error from exc
            if outcome is not None:
                raise outcome
        finally:
            with self._failure_lock:
                self._write_waiters.discard(job)

    def _enqueue_write(self, job: _WriteJob, deadline: float, operation: str, description: str) -> None:
        while True:
            with self._failure_lock:
                if self._failure is not None:
                    raise self._failure
                try:
                    self._writes.put_nowait(job)
                    return
                except Full:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._write_timeout(operation, description)
            self._stop.wait(timeout=min(remaining, 0.01))

    def _write_timeout(self, operation: str, description: str) -> DartLspTimeout:
        error = DartLspTimeout(f"Dart LSP {operation} exceeded {description} while writing to the server")
        self._set_failure(error)
        self._terminate_process_group()
        return error

    def _stdin_writer(self, stream: BinaryIO) -> None:
        while not self._stop.is_set():
            try:
                job = self._writes.get(timeout=0.05)
            except Empty:
                continue
            try:
                self._raise_if_failed()
                write_lsp_frame(stream, job.frame)
            except DartLspError as exc:
                self._set_failure(exc)
                self._complete_write(job, exc)
                return
            except BrokenPipeError, OSError, ValueError:
                error = self._process_exit_error()
                self._set_failure(error)
                self._complete_write(job, error)
                return
            self._complete_write(job, None)

    @staticmethod
    def _complete_write(job: _WriteJob, outcome: DartLspError | None) -> None:
        with suppress(Full):
            job.completion.put_nowait(outcome)

    def _stdout_reader(self, stream: BinaryIO) -> None:
        try:
            while not self._stop.is_set():
                try:
                    message = read_lsp_message(stream, max_content_bytes=self.max_message_bytes)
                except EOFError:
                    self._put_inbound(_StreamClosed())
                    return
                except DartLspError as exc:
                    self._put_inbound(_ReaderFailed(exc))
                    return
                self._put_inbound(message)
        except BaseException as exc:
            self._put_inbound(_ReaderFailed(DartLspProtocolError(f"Dart LSP reader failed: {exc}")))

    def _stderr_reader(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr.extend(chunk)
                    overflow = len(self._stderr) - self.stderr_limit_bytes
                    if overflow > 0:
                        del self._stderr[:overflow]
        except OSError, ValueError:
            return

    def _put_inbound(self, item: _Inbound) -> None:
        while not self._stop.is_set():
            try:
                self._inbound.put(item, timeout=0.05)
                return
            except Full:
                continue

    def _dispatcher(self) -> None:
        while not self._stop.is_set():
            try:
                inbound = self._inbound.get(timeout=0.05)
            except Empty:
                continue
            if isinstance(inbound, _ReaderFailed):
                self._set_failure(inbound.error)
                return
            if isinstance(inbound, _StreamClosed):
                if not self._expecting_exit:
                    self._set_failure(self._process_exit_error())
                return
            try:
                self._dispatch_message(inbound)
            except DartLspError as exc:
                self._set_failure(exc)
                return
            except BaseException as exc:
                self._set_failure(DartLspProtocolError(f"Dart LSP dispatcher failed: {exc}"))
                return

    def _dispatch_message(self, message: dict[str, object]) -> None:
        if message.get("jsonrpc") not in {None, "2.0"}:
            raise DartLspProtocolError("Dart LSP message has an unsupported JSON-RPC version")
        method = message.get("method")
        request_id = message.get("id")
        if method is not None:
            if not isinstance(method, str):
                raise DartLspProtocolError("Dart LSP message method must be a string")
            if request_id is not None:
                self._respond_to_server_request(request_id, method, message.get("params"))
            else:
                self._record_notification(message)
            return
        if request_id is None or not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
            raise DartLspProtocolError("Dart LSP response has no valid id")
        with self._pending_lock:
            response_queue = self._pending.pop(request_id, None)
        if response_queue is not None:
            response_queue.put(message)

    def _respond_to_server_request(self, request_id: object, method: str, params: object) -> None:
        if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
            raise DartLspProtocolError("Dart LSP server request has no valid id")
        if method == "workspace/configuration":
            result = self._configuration_response(params)
            response: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "result": result}
        elif method in {
            "client/registerCapability",
            "client/unregisterCapability",
            "window/workDoneProgress/create",
        }:
            response = {"jsonrpc": "2.0", "id": request_id, "result": None}
        elif method == "workspace/workspaceFolders":
            folders: list[dict[str, str]] = []
            if self._root_uri is not None:
                folders.append({"uri": self._root_uri, "name": self.cwd.name})
            response = {"jsonrpc": "2.0", "id": request_id, "result": folders}
        else:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unsupported server request: {method}"},
            }
        self._send_payload(response)

    def _configuration_response(self, params: object) -> list[object]:
        if not isinstance(params, dict):
            return []
        items = params.get("items")
        if not isinstance(items, list):
            return []
        results: list[object] = []
        for item in items:
            if not isinstance(item, dict):
                results.append(None)
                continue
            section = item.get("section")
            results.append(self._configuration_section(section) if isinstance(section, str) else None)
        return results

    def _configuration_section(self, section: str) -> object:
        if section in self._workspace_configuration:
            return self._workspace_configuration[section]
        current: object = self._workspace_configuration
        for component in section.split("."):
            if not isinstance(current, Mapping) or component not in current:
                return None
            current = current[component]
        return current

    def _record_notification(self, message: dict[str, object]) -> None:
        size = len(encode_lsp_message(message))
        with self._notification_condition:
            if size > self.notification_bytes_limit:
                self._dropped_notifications += 1
                self._notification_condition.notify_all()
                return
            while self._notifications and (
                len(self._notifications) >= self.notification_limit
                or self._notification_bytes + size > self.notification_bytes_limit
            ):
                self._notifications.popleft()
                self._notification_bytes -= self._notification_sizes.popleft()
                self._dropped_notifications += 1
            self._notifications.append(message)
            self._notification_sizes.append(size)
            self._notification_bytes += size
            self._notification_condition.notify_all()

    def _cancel_request(self, request_id: int | str) -> None:
        with suppress(DartLspError):
            self._send_payload(
                {
                    "jsonrpc": "2.0",
                    "method": "$/cancelRequest",
                    "params": {"id": request_id},
                }
            )

    def _raise_if_failed(self) -> None:
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            raise failure

    def _set_failure(self, error: DartLspError) -> None:
        with self._failure_lock:
            if self._failure is not None:
                return
            self._failure = error
            write_waiters = list(self._write_waiters)
            while True:
                try:
                    self._writes.get_nowait()
                except Empty:
                    break
        for write_waiter in write_waiters:
            self._complete_write(write_waiter, error)
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for response_queue in pending:
            with suppress(Full):
                response_queue.put_nowait(error)
        with self._notification_condition:
            self._notification_condition.notify_all()

    def _process_exit_error(self) -> DartLspProcessExited:
        process = self._process
        returncode: int | None = None
        if process is not None:
            returncode = process.poll()
            if returncode is None:
                with suppress(subprocess.TimeoutExpired):
                    returncode = process.wait(timeout=0.05)
            stderr_thread = next((thread for thread in self._threads if thread.name == "dart-lsp-stderr"), None)
            if stderr_thread is not None and stderr_thread is not threading.current_thread():
                stderr_thread.join(timeout=0.05)
        tail = self.stderr_tail.strip()
        status = f" with exit code {returncode}" if returncode is not None else " after closing stdout"
        detail = f": {tail}" if tail else ""
        return DartLspProcessExited(
            f"Dart language server exited{status}{detail}",
            returncode=returncode,
            stderr_tail=tail,
        )

    def _terminate_process_group(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            with suppress(OSError):
                os.kill(process.pid, signal.CTRL_BREAK_EVENT)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            except PermissionError:
                process.terminate()
        try:
            process.wait(timeout=_PROCESS_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except OSError, subprocess.TimeoutExpired:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except PermissionError:
                process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_PROCESS_GRACE_SECONDS)

    def _finish_threads(self) -> None:
        self._stop.set()
        process = self._process
        if process is not None and process.stdin is not None:
            with suppress(OSError, ValueError):
                process.stdin.close()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)


def _default_client_capabilities() -> dict[str, object]:
    return {
        "workspace": {"configuration": True, "workspaceFolders": True},
        "window": {"workDoneProgress": True},
        "textDocument": {
            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
            "references": {"dynamicRegistration": False},
            "callHierarchy": {"dynamicRegistration": False},
            "typeHierarchy": {"dynamicRegistration": False},
            "synchronization": {"dynamicRegistration": False, "didSave": False},
        },
    }


def _deep_merge(base: Mapping[str, object], overlay: Mapping[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _notification_uri(message: Mapping[str, object]) -> str | None:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    uri = params.get("uri")
    if isinstance(uri, str):
        return uri
    text_document = params.get("textDocument")
    if isinstance(text_document, dict):
        nested_uri = text_document.get("uri")
        if isinstance(nested_uri, str):
            return nested_uri
    return None


def file_uri_to_native_path(uri: str) -> Path | None:
    from .protocol import file_uri_to_path

    try:
        path = file_uri_to_path(uri)
    except ValueError:
        return None
    return path if isinstance(path, Path) else None


__all__ = [
    "DartLspClient",
    "DartLspError",
    "DartLspProcessExited",
    "DartLspProtocolError",
    "DartLspResponseError",
    "DartLspTimeout",
]
