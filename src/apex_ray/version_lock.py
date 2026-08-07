"""Repository-scoped Apex Ray runtime version locks."""

import os
import shlex
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from packaging.version import InvalidVersion, Version

VERSION_LOCK_RELATIVE_PATH = Path(".apex-ray/version")
MAX_VERSION_LOCK_BYTES = 128
UNPUBLISHABLE_VERSIONS = {"0+unknown"}


class VersionLockError(RuntimeError):
    pass


class VersionLockState(StrEnum):
    MISSING = "missing"
    CURRENT = "current"
    MISMATCH = "mismatch"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class VersionLockStatus:
    path: Path
    state: VersionLockState
    runtime_version: str
    locked_version: str | None = None
    reason: str = ""

    @property
    def blocking(self) -> bool:
        return self.state in {VersionLockState.MISMATCH, VersionLockState.INVALID}


def inspect_version_lock(root: Path, *, runtime_version: str) -> VersionLockStatus:
    """Inspect a repository lock without mutating the project."""
    path = root / VERSION_LOCK_RELATIVE_PATH
    try:
        _validate_lock_parent(root, create=False)
        if not path.exists() and not path.is_symlink():
            return VersionLockStatus(path, VersionLockState.MISSING, runtime_version)
        if path.is_symlink() or not path.is_file():
            raise VersionLockError("the lock must be a regular file, not a symlink or directory")
        with path.open("rb") as stream:
            payload = stream.read(MAX_VERSION_LOCK_BYTES + 1)
        if len(payload) > MAX_VERSION_LOCK_BYTES:
            raise VersionLockError(f"the lock exceeds {MAX_VERSION_LOCK_BYTES} bytes")
        raw = payload.decode("utf-8")
        if not raw.endswith("\n") or raw.count("\n") != 1:
            raise VersionLockError("the lock must contain exactly one newline-terminated version")
        locked_version = _canonical_version(raw.removesuffix("\n"), purpose="lock")
        canonical_runtime = _canonical_version(runtime_version, purpose="runtime")
    except (OSError, UnicodeError, VersionLockError) as exc:
        return VersionLockStatus(
            path,
            VersionLockState.INVALID,
            runtime_version,
            reason=str(exc),
        )
    if locked_version == canonical_runtime:
        return VersionLockStatus(
            path,
            VersionLockState.CURRENT,
            canonical_runtime,
            locked_version=locked_version,
        )
    return VersionLockStatus(
        path,
        VersionLockState.MISMATCH,
        canonical_runtime,
        locked_version=locked_version,
        reason=f"repository requires Apex Ray {locked_version}, but the running version is {canonical_runtime}",
    )


def assert_version_lock(root: Path, *, runtime_version: str) -> VersionLockStatus:
    """Reject malformed or mismatched locks while allowing legacy unlocked repositories."""
    status = inspect_version_lock(root, runtime_version=runtime_version)
    if status.state == VersionLockState.INVALID:
        raise VersionLockError(f"Invalid Apex Ray version lock at {status.path}: {status.reason}")
    if status.state == VersionLockState.MISMATCH:
        raise VersionLockError(
            f"This repository requires Apex Ray {status.locked_version}, but the running version is "
            f"{status.runtime_version}. Re-run the requested command with "
            f"`{render_uvx_command(status.locked_version or '')}`."
        )
    return status


def validate_version_lock_target(root: Path) -> Path:
    """Validate that a lock update cannot escape the repository before other artifacts change."""
    path = root / VERSION_LOCK_RELATIVE_PATH
    _validate_lock_parent(root, create=False)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise VersionLockError(f"Cannot replace non-regular Apex Ray version lock: {path}")
    return path


def ensure_version_lock(root: Path, *, runtime_version: str, update: bool = False) -> Path | None:
    """Create a missing lock or explicitly update a mismatched regular lock."""
    status = inspect_version_lock(root, runtime_version=runtime_version)
    if status.state == VersionLockState.CURRENT:
        return None
    if status.state == VersionLockState.MISSING:
        return write_version_lock(root, runtime_version)
    if not update:
        assert_version_lock(root, runtime_version=runtime_version)
    return write_version_lock(root, runtime_version)


def write_version_lock(root: Path, version: str) -> Path:
    """Atomically write the exact publishable runtime version for a project."""
    canonical = _canonical_version(version, purpose="runtime")
    path = root / VERSION_LOCK_RELATIVE_PATH
    parent = _validate_lock_parent(root, create=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise VersionLockError(f"Cannot replace non-regular Apex Ray version lock: {path}")
    temporary: Path | None = None
    descriptor = -1
    try:
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
        descriptor, temporary_name = tempfile.mkstemp(prefix=".version.", suffix=".tmp", dir=parent)
        temporary = Path(temporary_name)
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(f"{canonical}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(parent)
    except OSError as exc:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise VersionLockError(f"Unable to write Apex Ray version lock at {path}: {exc}") from exc
    return path


def render_uvx_argv(version: str, *arguments: str) -> list[str]:
    """Build the exact uvx launcher argv used by version-locked commands."""
    canonical = _canonical_version(version, purpose="version")
    return ["uvx", "--python", "3.14", f"apex-ray@{canonical}", *arguments]


def publishable_runtime_version(version: str) -> str | None:
    """Return a canonical package-index version, or None for source-only runtimes."""
    try:
        return _canonical_version(version, purpose="runtime")
    except VersionLockError:
        return None


def render_uvx_command(version: str, *arguments: str) -> str:
    """Render the shell-safe exact uvx launcher used by managed artifacts."""
    return shlex.join(render_uvx_argv(version, *arguments))


def _canonical_version(raw: str, *, purpose: str) -> str:
    if raw in UNPUBLISHABLE_VERSIONS:
        raise VersionLockError(f"Apex Ray {purpose} version {raw!r} cannot be pinned from a package index")
    try:
        parsed = Version(raw)
    except InvalidVersion as exc:
        raise VersionLockError(f"Invalid Apex Ray {purpose} version: {raw!r}") from exc
    canonical = str(parsed)
    if raw != canonical:
        raise VersionLockError(f"Invalid Apex Ray {purpose} version: use canonical PEP 440 form {canonical!r}")
    return canonical


def _validate_lock_parent(root: Path, *, create: bool) -> Path:
    resolved_root = root.resolve()
    parent = root / VERSION_LOCK_RELATIVE_PATH.parent
    if create:
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VersionLockError(f"unable to create the version lock directory: {exc}") from exc
    if not parent.exists() and not parent.is_symlink():
        return parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise VersionLockError(f"unable to resolve the version lock directory: {exc}") from exc
    if not resolved_parent.is_relative_to(resolved_root):
        raise VersionLockError(f"version lock directory points outside the repository: {parent}")
    if not resolved_parent.is_dir():
        raise VersionLockError(f"version lock parent is not a directory: {parent}")
    return parent


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
