import os
import shutil
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread


class GitError(RuntimeError):
    def __init__(self, args: list[str], stderr: str, returncode: int) -> None:
        self.args_list = args
        self.stderr = stderr.strip()
        self.returncode = returncode
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {self.stderr}")


class GitOutputLimitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitNulOutput:
    returncode: int
    entries: list[str]


def git_available() -> bool:
    return shutil.which("git") is not None


def run_git(
    args: list[str],
    cwd: Path,
    check: bool = True,
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GitError(args, proc.stderr, proc.returncode)
    return proc


def read_git_nul_output(
    args: list[str],
    cwd: Path,
    *,
    timeout: float | None = None,
    max_output_bytes: int,
    max_entries: int,
) -> GitNulOutput:
    if max_output_bytes < 0:
        raise ValueError("max_output_bytes must be non-negative.")
    if max_entries < 0:
        raise ValueError("max_entries must be non-negative.")
    command = ["git", *args]
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdout = process.stdout
    if stdout is None:  # pragma: no cover - PIPE guarantees stdout
        process.kill()
        process.wait()
        raise RuntimeError("Git streaming process did not expose stdout.")

    chunks: Queue[bytes | BaseException | None] = Queue(maxsize=2)
    stop_reader = Event()

    def enqueue(item: bytes | BaseException | None) -> bool:
        while not stop_reader.is_set():
            try:
                chunks.put(item, timeout=0.05)
                return True
            except Full:
                continue
        return False

    def read_stdout() -> None:
        try:
            while not stop_reader.is_set():
                chunk = stdout.read(64 * 1024)
                if not chunk:
                    break
                if not enqueue(chunk):
                    return
        except BaseException as exc:
            enqueue(exc)
        finally:
            enqueue(None)

    reader = Thread(target=read_stdout, name="apex-ray-git-stream", daemon=True)
    reader.start()
    entries: list[str] = []
    pending = bytearray()
    output_bytes = 0
    try:
        while True:
            try:
                item = chunks.get(timeout=_remaining_process_seconds(deadline, command, timeout))
            except Empty as exc:
                raise subprocess.TimeoutExpired(command, timeout or 0.0) from exc
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            output_bytes += len(item)
            if output_bytes > max_output_bytes:
                raise GitOutputLimitError(f"Git output exceeded the byte safety limit of {max_output_bytes}.")
            pending.extend(item)
            consumed = 0
            while (separator := pending.find(b"\0", consumed)) >= 0:
                raw_entry = bytes(pending[consumed:separator])
                consumed = separator + 1
                if raw_entry:
                    _append_nul_entry(entries, raw_entry, max_entries)
            if consumed:
                del pending[:consumed]
        if pending:
            _append_nul_entry(entries, bytes(pending), max_entries)
        returncode = process.wait(timeout=_remaining_process_seconds(deadline, command, timeout))
        return GitNulOutput(returncode=returncode, entries=entries)
    finally:
        stop_reader.set()
        if process.poll() is None:
            with suppress(OSError):
                process.kill()
        with suppress(OSError):
            stdout.close()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=1.0)
        reader.join(timeout=1.0)


def _append_nul_entry(entries: list[str], raw_entry: bytes, max_entries: int) -> None:
    if len(entries) >= max_entries:
        raise GitOutputLimitError(f"Git output exceeded the entry safety limit of {max_entries}.")
    entries.append(os.fsdecode(raw_entry))


def _remaining_process_seconds(
    deadline: float | None,
    command: list[str],
    timeout: float | None,
) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, timeout or 0.0)
    return remaining


def repo_root(
    cwd: Path,
    *,
    timeout: float | None = None,
) -> Path | None:
    if not git_available():
        return None
    proc = run_git(
        ["rev-parse", "--show-toplevel"],
        cwd=cwd,
        check=False,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def common_dir(cwd: Path) -> Path | None:
    if not git_available():
        return None
    proc = run_git(["rev-parse", "--git-common-dir"], cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    path = Path(proc.stdout.strip())
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def is_git_repo(cwd: Path, *, timeout: float | None = None) -> bool:
    if not git_available():
        return False
    proc = run_git(
        ["rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        check=False,
        timeout=timeout,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def diff_base(cwd: Path, base: str) -> str:
    return run_git(["diff", "--find-renames", "--find-copies", f"{base}...HEAD"], cwd=cwd).stdout


def diff_range(cwd: Path, old_ref: str, new_ref: str = "HEAD") -> str:
    return run_git(["diff", "--find-renames", "--find-copies", old_ref, new_ref], cwd=cwd).stdout


def diff_staged(cwd: Path) -> str:
    return run_git(["diff", "--cached", "--find-renames", "--find-copies"], cwd=cwd).stdout


def diff_worktree(cwd: Path) -> str:
    tracked_diff = run_git(["diff", "--find-renames", "--find-copies"], cwd=cwd).stdout
    untracked_diff = diff_untracked(cwd)
    return "\n".join(part for part in (tracked_diff.rstrip(), untracked_diff.rstrip()) if part) + (
        "\n" if tracked_diff or untracked_diff else ""
    )


def diff_untracked(cwd: Path) -> str:
    chunks: list[str] = []
    for file in untracked_files(cwd):
        proc = run_git(["diff", "--no-index", "--", "/dev/null", file], cwd=cwd, check=False)
        if proc.returncode not in {0, 1}:
            raise GitError(["diff", "--no-index", "--", "/dev/null", file], proc.stderr, proc.returncode)
        if proc.stdout:
            chunks.append(proc.stdout.rstrip())
    return "\n".join(chunks) + ("\n" if chunks else "")


def tracked_files(cwd: Path) -> list[str]:
    proc = run_git(["ls-files"], cwd=cwd, check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def untracked_files(cwd: Path) -> list[str]:
    proc = run_git(["ls-files", "--others", "--exclude-standard"], cwd=cwd, check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def rev_parse(cwd: Path, ref: str) -> str:
    return run_git(["rev-parse", "--verify", ref], cwd=cwd).stdout.strip()


def merge_base(cwd: Path, base: str, head: str = "HEAD") -> str:
    return run_git(["merge-base", base, head], cwd=cwd).stdout.strip()


def object_exists(cwd: Path, ref: str) -> bool:
    proc = run_git(["cat-file", "-e", f"{ref}^{{commit}}"], cwd=cwd, check=False)
    return proc.returncode == 0
