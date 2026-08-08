"""Repository-scoped Apex Ray launcher selection."""

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from apex_ray.invocation import ApexRayLauncher
from apex_ray.version_lock import (
    VERSION_LOCK_RELATIVE_PATH,
    VersionLockError,
    VersionLockState,
    VersionLockStatus,
    assert_version_lock,
    inspect_version_lock,
)

RUNTIME_MODE_RELATIVE_PATH = Path(".apex-ray/runtime")
SOURCE_RUNTIME_CONTENT = "source\n"
MAX_RUNTIME_MODE_BYTES = 32


class RepositoryRuntimeError(RuntimeError):
    pass


class RepositoryRuntimeMode(StrEnum):
    LOCKED = "locked"
    SOURCE = "source"
    LEGACY = "legacy"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class RepositoryRuntimeStatus:
    root: Path
    runtime_path: Path
    mode: RepositoryRuntimeMode
    runtime_version: str
    version_lock: VersionLockStatus
    launcher: ApexRayLauncher | None = None
    source_checkout_current: bool | None = None
    reason: str = ""

    @property
    def blocking(self) -> bool:
        return (
            self.mode == RepositoryRuntimeMode.INVALID
            or self.version_lock.blocking
            or self.source_checkout_current is False
        )


def inspect_repository_runtime(
    root: Path,
    *,
    runtime_version: str,
    active_package_dir: Path | None = None,
) -> RepositoryRuntimeStatus:
    """Inspect launcher metadata and current-source identity without mutating the repository."""

    root = root.resolve()
    runtime_path = root / RUNTIME_MODE_RELATIVE_PATH
    lock_status = inspect_version_lock(root, runtime_version=runtime_version)
    try:
        _validate_runtime_parent(root, create=False)
    except RepositoryRuntimeError as exc:
        return _invalid_status(root, runtime_path, runtime_version, lock_status, str(exc))

    runtime_present = runtime_path.exists() or runtime_path.is_symlink()
    version_path = root / VERSION_LOCK_RELATIVE_PATH
    version_present = version_path.exists() or version_path.is_symlink()
    if runtime_present and version_present:
        return _invalid_status(
            root,
            runtime_path,
            runtime_version,
            lock_status,
            "repository contains both .apex-ray/runtime and .apex-ray/version; choose one runtime mode",
        )

    if runtime_present:
        try:
            _read_source_runtime(runtime_path)
        except RepositoryRuntimeError as exc:
            return _invalid_status(root, runtime_path, runtime_version, lock_status, str(exc))
        _expected_package_dir, source_checkout_current, reason = _source_checkout_status(
            root,
            active_package_dir=active_package_dir,
        )
        return RepositoryRuntimeStatus(
            root=root,
            runtime_path=runtime_path,
            mode=RepositoryRuntimeMode.SOURCE,
            runtime_version=runtime_version,
            version_lock=lock_status,
            launcher=ApexRayLauncher.source(),
            source_checkout_current=source_checkout_current,
            reason=reason,
        )

    if lock_status.state == VersionLockState.INVALID:
        return _invalid_status(root, runtime_path, runtime_version, lock_status, lock_status.reason)
    if lock_status.state in {VersionLockState.CURRENT, VersionLockState.MISMATCH}:
        locked_version = lock_status.locked_version
        if locked_version is None:  # pragma: no cover - guarded by VersionLockStatus invariants
            return _invalid_status(
                root,
                runtime_path,
                runtime_version,
                lock_status,
                "version-locked runtime has no locked version",
            )
        return RepositoryRuntimeStatus(
            root=root,
            runtime_path=runtime_path,
            mode=RepositoryRuntimeMode.LOCKED,
            runtime_version=runtime_version,
            version_lock=lock_status,
            launcher=ApexRayLauncher.locked(locked_version),
            reason=lock_status.reason,
        )
    return RepositoryRuntimeStatus(
        root=root,
        runtime_path=runtime_path,
        mode=RepositoryRuntimeMode.LEGACY,
        runtime_version=runtime_version,
        version_lock=lock_status,
        launcher=ApexRayLauncher.bare(),
    )


def assert_repository_runtime(
    root: Path,
    *,
    runtime_version: str,
    active_package_dir: Path | None = None,
) -> RepositoryRuntimeStatus:
    """Reject invalid, mismatched, or non-current repository runtime state."""

    status = inspect_repository_runtime(
        root,
        runtime_version=runtime_version,
        active_package_dir=active_package_dir,
    )
    if status.mode == RepositoryRuntimeMode.INVALID:
        runtime_present = status.runtime_path.exists() or status.runtime_path.is_symlink()
        if status.version_lock.state == VersionLockState.INVALID and not runtime_present:
            try:
                assert_version_lock(status.root, runtime_version=runtime_version)
            except VersionLockError as exc:
                raise RepositoryRuntimeError(str(exc)) from exc
        raise RepositoryRuntimeError(f"Invalid Apex Ray repository runtime at {status.runtime_path}: {status.reason}")
    if status.mode == RepositoryRuntimeMode.LOCKED and status.version_lock.blocking:
        try:
            assert_version_lock(status.root, runtime_version=runtime_version)
        except VersionLockError as exc:
            raise RepositoryRuntimeError(str(exc)) from exc
    if status.mode == RepositoryRuntimeMode.SOURCE and status.source_checkout_current is False:
        raise RepositoryRuntimeError(
            f"{status.reason}. Re-run the requested command from {status.root} with `uv run --locked apex-ray ...`."
        )
    if status.mode == RepositoryRuntimeMode.SOURCE:
        assert_source_runtime_checkout(status.root, active_package_dir=active_package_dir)
    return status


def assert_source_runtime_checkout(
    root: Path,
    *,
    active_package_dir: Path | None = None,
) -> Path:
    """Validate that source mode can run this checkout before setup writes begin."""

    root = root.resolve()
    assert_source_uv_project(root)
    expected_package_dir, source_checkout_current, reason = _source_checkout_status(
        root,
        active_package_dir=active_package_dir,
    )
    if not source_checkout_current:
        raise RepositoryRuntimeError(f"Apex Ray source mode requires the current checkout: {reason}.")
    return expected_package_dir


def assert_source_uv_project(root: Path) -> None:
    """Reject a source launcher whose locked uv project cannot run as declared."""

    root = root.resolve()
    uv_executable = shutil.which("uv")
    if uv_executable is None:
        raise RepositoryRuntimeError("Apex Ray source mode requires `uv` on PATH.")
    for name in ("pyproject.toml", "uv.lock"):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise RepositoryRuntimeError(f"Apex Ray source mode requires a regular {name} file at {path}.")
    try:
        completed = subprocess.run(
            [uv_executable, "lock", "--check", "--offline", "--project", str(root)],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepositoryRuntimeError(f"Unable to validate the source uv.lock at {root / 'uv.lock'}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f" ({detail[-1]})" if detail else ""
        raise RepositoryRuntimeError(
            f"Apex Ray source mode requires an up-to-date uv.lock at {root / 'uv.lock'}{suffix}."
        )


def write_source_runtime(root: Path) -> Path:
    """Atomically select source-checkout mode for a repository without a version lock."""

    root = root.resolve()
    runtime_path = root / RUNTIME_MODE_RELATIVE_PATH
    version_path = root / VERSION_LOCK_RELATIVE_PATH
    parent = _validate_runtime_parent(root, create=True)
    if version_path.exists() or version_path.is_symlink():
        raise RepositoryRuntimeError(
            f"Cannot enable Apex Ray source mode while a version lock exists at {version_path}."
        )
    _validate_runtime_target(runtime_path)
    if runtime_path.exists() and _read_source_runtime(runtime_path) == RepositoryRuntimeMode.SOURCE:
        return runtime_path

    temporary: Path | None = None
    descriptor = -1
    try:
        mode = (runtime_path.stat().st_mode & 0o777) if runtime_path.exists() else 0o644
        descriptor, temporary_name = tempfile.mkstemp(prefix=".runtime.", suffix=".tmp", dir=parent)
        temporary = Path(temporary_name)
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(SOURCE_RUNTIME_CONTENT)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, runtime_path)
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
        raise RepositoryRuntimeError(f"Unable to write Apex Ray repository runtime at {runtime_path}: {exc}") from exc
    return runtime_path


def _source_checkout_status(
    root: Path,
    *,
    active_package_dir: Path | None = None,
) -> tuple[Path, bool, str]:
    declared_package_dir = root / "src" / "apex_ray"
    expected_package_dir = declared_package_dir.resolve(strict=False)
    current_package_dir = (active_package_dir or Path(__file__).parent).resolve(strict=False)
    if not expected_package_dir.is_relative_to(root):
        return (
            expected_package_dir,
            False,
            f"repository source package {declared_package_dir} must resolve inside the repository, "
            f"but it resolves to {expected_package_dir}",
        )
    if not declared_package_dir.is_dir() or declared_package_dir.is_symlink():
        return (
            expected_package_dir,
            False,
            f"repository source package must be a regular directory at {declared_package_dir}",
        )
    if current_package_dir != expected_package_dir:
        return (
            expected_package_dir,
            False,
            f"running package is from {current_package_dir}, not {expected_package_dir}",
        )
    return expected_package_dir, True, ""


def _invalid_status(
    root: Path,
    runtime_path: Path,
    runtime_version: str,
    lock_status: VersionLockStatus,
    reason: str,
) -> RepositoryRuntimeStatus:
    return RepositoryRuntimeStatus(
        root=root,
        runtime_path=runtime_path,
        mode=RepositoryRuntimeMode.INVALID,
        runtime_version=runtime_version,
        version_lock=lock_status,
        reason=reason,
    )


def _read_source_runtime(path: Path) -> RepositoryRuntimeMode:
    if path.is_symlink() or not path.is_file():
        raise RepositoryRuntimeError("the runtime marker must be a regular file, not a symlink or directory")
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_RUNTIME_MODE_BYTES + 1)
        if len(payload) > MAX_RUNTIME_MODE_BYTES:
            raise RepositoryRuntimeError(f"the runtime marker exceeds {MAX_RUNTIME_MODE_BYTES} bytes")
        raw = payload.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise RepositoryRuntimeError(f"unable to read the runtime marker: {exc}") from exc
    if raw != SOURCE_RUNTIME_CONTENT:
        raise RepositoryRuntimeError("the runtime marker must contain exactly 'source' followed by one newline")
    return RepositoryRuntimeMode.SOURCE


def _validate_runtime_target(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RepositoryRuntimeError(f"Runtime marker must be a regular file: {path}")


def _validate_runtime_parent(root: Path, *, create: bool) -> Path:
    resolved_root = root.resolve()
    parent = root / RUNTIME_MODE_RELATIVE_PATH.parent
    if create:
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RepositoryRuntimeError(f"unable to create the runtime marker directory: {exc}") from exc
    if not parent.exists() and not parent.is_symlink():
        return parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise RepositoryRuntimeError(f"unable to resolve the runtime marker directory: {exc}") from exc
    if not resolved_parent.is_relative_to(resolved_root):
        raise RepositoryRuntimeError(f"runtime marker directory points outside the repository: {parent}")
    if not resolved_parent.is_dir():
        raise RepositoryRuntimeError(f"runtime marker parent is not a directory: {parent}")
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
