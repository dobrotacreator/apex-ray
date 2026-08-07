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


class GitRemoteRefError(RuntimeError):
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
    errors: str = "replace",
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        encoding="utf-8",
        errors=errors,
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
    return run_git(
        ["diff", "--find-renames", "--find-copies", "--end-of-options", f"{base}...HEAD"],
        cwd=cwd,
    ).stdout


def diff_range(cwd: Path, old_ref: str, new_ref: str = "HEAD") -> str:
    return run_git(
        ["diff", "--find-renames", "--find-copies", "--end-of-options", old_ref, new_ref],
        cwd=cwd,
    ).stdout


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


def worktree_output_path_is_stable(cwd: Path, path: Path, *, directory: bool = False) -> bool:
    """Return whether writing path cannot change the worktree review target."""

    root = cwd.resolve()
    lexical = Path(os.path.abspath(path if path.is_absolute() else cwd / path))
    if lexical.is_symlink():
        return False
    if directory and lexical.exists() and not lexical.is_dir():
        return False
    resolved = lexical.resolve(strict=False)
    git_common_dir = common_dir(cwd)
    if git_common_dir is not None:
        git_relative = _relative_path_by_filesystem_identity(resolved, git_common_dir)
        if git_relative is not None:
            # Keep Git metadata immutable while allowing the one namespace that
            # resolve_local_data_root reserves for Apex Ray runtime artifacts.
            return bool(git_relative.parts) and git_relative.parts[0].casefold() == "apex-ray"
    relative_path = _relative_path_by_filesystem_identity(resolved, root)
    if relative_path is None:
        return True
    relative = relative_path.as_posix()
    if relative in {"", "."}:
        return False
    tracked = run_git(
        ["ls-files", "--error-unmatch", "--", f":(icase,literal){relative}"],
        cwd=cwd,
        check=False,
    )
    if tracked.returncode == 0:
        return False
    ignore_target = f"{relative}/" if directory else relative
    ignored = run_git(["check-ignore", "-q", "--no-index", "--", ignore_target], cwd=cwd, check=False)
    if ignored.returncode not in {0, 1}:
        raise GitError(
            ["check-ignore", "-q", "--no-index", "--", ignore_target],
            ignored.stderr,
            ignored.returncode,
        )
    return ignored.returncode == 0


def _relative_path_by_filesystem_identity(path: Path, directory: Path) -> Path | None:
    """Return a relative suffix even when aliases differ by symlink or case."""

    directory = directory.resolve()
    cursor = path
    suffix: list[str] = []
    while True:
        if cursor.exists():
            try:
                if os.path.samefile(cursor, directory):
                    return Path(*reversed(suffix)) if suffix else Path(".")
            except OSError:
                pass
        parent = cursor.parent
        if parent == cursor:
            return None
        suffix.append(cursor.name)
        cursor = parent


def rev_parse(cwd: Path, ref: str) -> str:
    return run_git(["rev-parse", "--verify", "--end-of-options", ref], cwd=cwd).stdout.strip()


def merge_base(cwd: Path, base: str, head: str = "HEAD") -> str:
    return run_git(["merge-base", "--end-of-options", base, head], cwd=cwd).stdout.strip()


def is_ancestor(cwd: Path, ancestor: str, descendant: str = "HEAD") -> bool:
    args = ["merge-base", "--is-ancestor", "--end-of-options", ancestor, descendant]
    proc = run_git(args, cwd=cwd, check=False)
    if proc.returncode not in {0, 1}:
        raise GitError(args, proc.stderr, proc.returncode)
    return proc.returncode == 0


def object_exists(cwd: Path, ref: str) -> bool:
    """Return whether the ref resolves and peels to a commit object."""

    proc = run_git(["cat-file", "-e", "--end-of-options", f"{ref}^{{commit}}"], cwd=cwd, check=False)
    return proc.returncode == 0


def fetch_remote_tracking_ref(cwd: Path, ref: str) -> None:
    """Fetch one exact remote branch into its matching remote-tracking ref."""

    remote, branch = _split_remote_tracking_ref(cwd, ref)
    _fetch_remote_branch(cwd, remote, branch, configured_ref=ref)


def resolve_pre_push_base(cwd: Path, ref: str) -> str:
    """Refresh a remote base when possible, or validate an existing local commit."""

    remotes = _configured_remotes(cwd)
    remote_branch = _match_remote_tracking_ref(ref, remotes)
    if remote_branch is not None:
        remote, branch = remote_branch
        _fetch_remote_branch(cwd, remote, branch, configured_ref=ref)
        return f"{remote}/{branch}"
    if ref.startswith("refs/remotes/"):
        raise GitRemoteRefError(f"Configured pre-push base {ref!r} does not name a configured remote-tracking ref.")
    if ref.startswith("origin/") and "origin" not in remotes:
        raise GitRemoteRefError(f"Configured pre-push base {ref!r} requires configured remote 'origin'.")
    full_object_id = _is_full_object_id(cwd, ref)
    if full_object_id and object_exists(cwd, ref):
        return ref

    if "origin" in remotes and not ref.startswith("refs/"):
        validated_refs = _validated_remote_branch_refs(cwd, "origin", ref)
        if validated_refs is not None:
            source_ref, _ = validated_refs
            args = ["ls-remote", "--exit-code", "--heads", "--", "origin", source_ref]
            remote_match = run_git(args, cwd=cwd, check=False)
            if remote_match.returncode == 0 and _ls_remote_contains_ref(remote_match.stdout, source_ref):
                _fetch_remote_branch(cwd, "origin", ref, configured_ref=ref)
                return f"origin/{ref}"
            if remote_match.returncode not in {0, 2}:
                raise GitError(args, remote_match.stderr, remote_match.returncode)

    if not full_object_id and object_exists(cwd, ref):
        return ref
    raise GitRemoteRefError(f"Configured pre-push base {ref!r} does not resolve to a commit.")


def _fetch_remote_branch(cwd: Path, remote: str, branch: str, *, configured_ref: str) -> None:
    validated_refs = _validated_remote_branch_refs(cwd, remote, branch)
    if validated_refs is None:
        raise GitRemoteRefError(f"Configured pre-push base {configured_ref!r} is not a valid remote-tracking ref.")
    source_ref, target_ref = validated_refs
    run_git(
        [
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "--",
            remote,
            f"+{source_ref}:{target_ref}",
        ],
        cwd=cwd,
    )


def _validated_remote_branch_refs(
    cwd: Path,
    remote: str,
    branch: str,
) -> tuple[str, str] | None:
    source_ref = f"refs/heads/{branch}"
    target_ref = f"refs/remotes/{remote}/{branch}"
    for candidate in (source_ref, target_ref):
        validation = run_git(["check-ref-format", candidate], cwd=cwd, check=False)
        if validation.returncode != 0:
            return None
    return source_ref, target_ref


def _ls_remote_contains_ref(stdout: str, expected_ref: str) -> bool:
    return any(line.partition("\t")[2] == expected_ref for line in stdout.splitlines())


def _is_full_object_id(cwd: Path, ref: str) -> bool:
    object_format = run_git(["rev-parse", "--show-object-format"], cwd=cwd).stdout.strip()
    object_id_length = {"sha1": 40, "sha256": 64}.get(object_format)
    return object_id_length == len(ref) and all(character in "0123456789abcdefABCDEF" for character in ref)


def _configured_remotes(cwd: Path) -> list[str]:
    return [line for line in run_git(["remote"], cwd=cwd).stdout.splitlines() if line]


def _split_remote_tracking_ref(cwd: Path, ref: str) -> tuple[str, str]:
    remote_branch = _match_remote_tracking_ref(ref, _configured_remotes(cwd))
    if remote_branch is not None:
        return remote_branch
    raise GitRemoteRefError(
        f"Configured pre-push base {ref!r} is not an exact remote-tracking ref. "
        "Use '<remote>/<branch>' or 'refs/remotes/<remote>/<branch>'."
    )


def _match_remote_tracking_ref(ref: str, remotes: list[str]) -> tuple[str, str] | None:
    for remote in sorted(remotes, key=len, reverse=True):
        for prefix in (f"refs/remotes/{remote}/", f"{remote}/"):
            if ref.startswith(prefix) and len(ref) > len(prefix):
                return remote, ref[len(prefix) :]
    return None
