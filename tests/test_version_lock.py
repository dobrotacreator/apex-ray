import os
import stat
from pathlib import Path

import pytest

from apex_ray.version_lock import (
    VersionLockError,
    VersionLockState,
    assert_version_lock,
    ensure_version_lock,
    inspect_version_lock,
    publishable_runtime_version,
    render_uvx_argv,
    render_uvx_command,
    write_version_lock,
)


def test_version_lock_is_missing_until_initialized(tmp_path: Path) -> None:
    status = inspect_version_lock(tmp_path, runtime_version="0.1.13")

    assert status.state == VersionLockState.MISSING
    assert status.locked_version is None
    assert status.runtime_version == "0.1.13"
    assert_version_lock(tmp_path, runtime_version="0.1.13")


def test_version_lock_requires_the_running_version(tmp_path: Path) -> None:
    path = write_version_lock(tmp_path, "0.1.13")

    assert path.read_text(encoding="utf-8") == "0.1.13\n"
    assert inspect_version_lock(tmp_path, runtime_version="0.1.13").state == VersionLockState.CURRENT

    status = inspect_version_lock(tmp_path, runtime_version="0.1.12")
    assert status.state == VersionLockState.MISMATCH
    assert status.locked_version == "0.1.13"
    with pytest.raises(VersionLockError, match=r"requires Apex Ray 0\.1\.13.*0\.1\.12"):
        assert_version_lock(tmp_path, runtime_version="0.1.12")


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "0.1.13\n0.1.14\n",
        "0.1.13; touch owned\n",
        "0+unknown\n",
    ],
)
def test_version_lock_rejects_malformed_or_unpublished_values(tmp_path: Path, contents: str) -> None:
    path = tmp_path / ".apex-ray" / "version"
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")

    status = inspect_version_lock(tmp_path, runtime_version="0.1.13")

    assert status.state == VersionLockState.INVALID
    with pytest.raises(VersionLockError, match="Invalid Apex Ray version lock"):
        assert_version_lock(tmp_path, runtime_version="0.1.13")


def test_version_lock_rejects_a_symlink_even_when_it_points_inside_repo(tmp_path: Path) -> None:
    target = tmp_path / "locked-version"
    target.write_text("0.1.13\n", encoding="utf-8")
    path = tmp_path / ".apex-ray" / "version"
    path.parent.mkdir(parents=True)
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    status = inspect_version_lock(tmp_path, runtime_version="0.1.13")

    assert status.state == VersionLockState.INVALID
    assert "regular file" in status.reason


def test_version_lock_write_rejects_unknown_runtime_version(tmp_path: Path) -> None:
    with pytest.raises(VersionLockError, match="cannot be pinned"):
        write_version_lock(tmp_path, "0+unknown")

    assert not (tmp_path / ".apex-ray" / "version").exists()


def test_ensure_version_lock_requires_explicit_update_for_a_mismatch(tmp_path: Path) -> None:
    path = write_version_lock(tmp_path, "0.1.12")

    with pytest.raises(VersionLockError, match=r"requires Apex Ray 0\.1\.12"):
        ensure_version_lock(tmp_path, runtime_version="0.1.13")

    assert path.read_text(encoding="utf-8") == "0.1.12\n"

    updated = ensure_version_lock(tmp_path, runtime_version="0.1.13", update=True)

    assert updated == path
    assert path.read_text(encoding="utf-8") == "0.1.13\n"


def test_version_lock_rejects_an_external_parent_symlink_without_writing(tmp_path: Path) -> None:
    outside = tmp_path.with_name(f"{tmp_path.name}-outside-lock")
    outside.mkdir()
    try:
        (tmp_path / ".apex-ray").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(VersionLockError, match="outside the repository"):
        write_version_lock(tmp_path, "0.1.13")

    assert not (outside / "version").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable to Windows")
def test_version_lock_uses_a_readable_mode_and_preserves_existing_mode(tmp_path: Path) -> None:
    path = write_version_lock(tmp_path, "0.1.12")

    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    path.chmod(0o640)

    write_version_lock(tmp_path, "0.1.13")

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_version_lock_replace_failure_preserves_the_previous_lock_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_version_lock(tmp_path, "0.1.12")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replace unavailable")

    monkeypatch.setattr("apex_ray.version_lock.os.replace", fail_replace)

    with pytest.raises(VersionLockError, match="Unable to write"):
        write_version_lock(tmp_path, "0.1.13")

    assert path.read_text(encoding="utf-8") == "0.1.12\n"
    assert list(path.parent.glob(".version.*.tmp")) == []


def test_render_uvx_command_uses_an_exact_version_and_quotes_arguments() -> None:
    command = render_uvx_command("0.1.13", "review", "--only-pack", "pack with spaces")

    assert command == "uvx --python 3.14 apex-ray@0.1.13 review --only-pack 'pack with spaces'"


def test_render_uvx_argv_returns_an_exact_unshellified_launcher() -> None:
    argv = render_uvx_argv("0.1.13", "review", "--only-pack", "pack with spaces")

    assert argv == [
        "uvx",
        "--python",
        "3.14",
        "apex-ray@0.1.13",
        "review",
        "--only-pack",
        "pack with spaces",
    ]


def test_publishable_runtime_version_falls_back_for_source_only_metadata() -> None:
    assert publishable_runtime_version("0.1.17") == "0.1.17"
    assert publishable_runtime_version("0+unknown") is None


def test_render_uvx_command_rejects_an_unsafe_version() -> None:
    with pytest.raises(VersionLockError, match="Invalid Apex Ray version"):
        render_uvx_command("0.1.13; echo unsafe", "doctor")
