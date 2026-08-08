import re
from pathlib import Path

import pytest

from apex_ray.invocation import ApexRayLauncherKind
from apex_ray.repository_runtime import (
    RUNTIME_MODE_RELATIVE_PATH,
    RepositoryRuntimeError,
    RepositoryRuntimeMode,
    assert_repository_runtime,
    inspect_repository_runtime,
    write_source_runtime,
)


def _source_package_dir(root: Path) -> Path:
    package_dir = root / "src" / "apex_ray"
    package_dir.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "apex-ray"\nversion = "0.0.0"\nrequires-python = ">=3.14"\ndependencies = []\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.14"\n\n'
        '[[package]]\nname = "apex-ray"\nversion = "0.0.0"\nsource = { virtual = "." }\n',
        encoding="utf-8",
    )
    return package_dir


def _write_source_marker(root: Path, contents: str = "source\n") -> Path:
    path = root / RUNTIME_MODE_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def test_repository_runtime_is_legacy_without_runtime_metadata(tmp_path: Path) -> None:
    status = inspect_repository_runtime(tmp_path, runtime_version="0.1.17")

    assert status.mode == RepositoryRuntimeMode.LEGACY
    assert status.launcher is not None
    assert status.launcher.kind == ApexRayLauncherKind.BARE
    assert status.blocking is False
    assert_repository_runtime(tmp_path, runtime_version="0.1.17")


def test_repository_runtime_uses_the_exact_version_lock(tmp_path: Path) -> None:
    lock = tmp_path / ".apex-ray" / "version"
    lock.parent.mkdir(parents=True)
    lock.write_text("0.1.17\n", encoding="utf-8")

    status = inspect_repository_runtime(tmp_path, runtime_version="0.1.17")

    assert status.mode == RepositoryRuntimeMode.LOCKED
    assert status.launcher is not None
    assert status.launcher.kind == ApexRayLauncherKind.UVX
    assert status.launcher.version == "0.1.17"
    assert status.blocking is False


def test_repository_runtime_keeps_locked_mode_but_blocks_a_version_mismatch(tmp_path: Path) -> None:
    lock = tmp_path / ".apex-ray" / "version"
    lock.parent.mkdir(parents=True)
    lock.write_text("0.1.18\n", encoding="utf-8")

    status = inspect_repository_runtime(tmp_path, runtime_version="0.1.17")

    assert status.mode == RepositoryRuntimeMode.LOCKED
    assert status.launcher is not None
    assert status.launcher.version == "0.1.18"
    assert status.blocking is True
    with pytest.raises(RepositoryRuntimeError, match=r"requires Apex Ray 0\.1\.18"):
        assert_repository_runtime(tmp_path, runtime_version="0.1.17")


def test_repository_runtime_preserves_the_version_lock_error_for_an_invalid_lock(tmp_path: Path) -> None:
    lock = tmp_path / ".apex-ray" / "version"
    lock.parent.mkdir(parents=True)
    lock.write_text("not a version\n", encoding="utf-8")

    status = inspect_repository_runtime(tmp_path, runtime_version="0.1.17")

    assert status.mode == RepositoryRuntimeMode.INVALID
    with pytest.raises(RepositoryRuntimeError, match=r"Invalid Apex Ray version lock at .*\.apex-ray/version"):
        assert_repository_runtime(tmp_path, runtime_version="0.1.17")


def test_repository_runtime_uses_source_launcher_for_the_current_checkout(tmp_path: Path) -> None:
    package_dir = _source_package_dir(tmp_path)
    marker = _write_source_marker(tmp_path)

    status = inspect_repository_runtime(
        tmp_path,
        runtime_version="0.1.17",
        active_package_dir=package_dir,
    )

    assert status.mode == RepositoryRuntimeMode.SOURCE
    assert status.runtime_path == marker
    assert status.launcher is not None
    assert status.launcher.kind == ApexRayLauncherKind.SOURCE
    assert status.source_checkout_current is True
    assert status.blocking is False
    assert_repository_runtime(
        tmp_path,
        runtime_version="0.1.17",
        active_package_dir=package_dir,
    )


def test_repository_runtime_blocks_source_mode_from_another_install(tmp_path: Path) -> None:
    expected_package_dir = _source_package_dir(tmp_path)
    _write_source_marker(tmp_path)
    active_package_dir = tmp_path / "venv" / "site-packages" / "apex_ray"

    status = inspect_repository_runtime(
        tmp_path,
        runtime_version="0.1.17",
        active_package_dir=active_package_dir,
    )

    assert status.mode == RepositoryRuntimeMode.SOURCE
    assert status.source_checkout_current is False
    assert status.blocking is True
    assert str(expected_package_dir) in status.reason
    with pytest.raises(RepositoryRuntimeError, match=r"uv run --locked apex-ray"):
        assert_repository_runtime(
            tmp_path,
            runtime_version="0.1.17",
            active_package_dir=active_package_dir,
        )


@pytest.mark.parametrize("missing_name", ["pyproject.toml", "uv.lock"])
def test_repository_runtime_blocks_source_mode_without_project_metadata(
    tmp_path: Path,
    missing_name: str,
) -> None:
    package_dir = _source_package_dir(tmp_path)
    _write_source_marker(tmp_path)
    (tmp_path / missing_name).unlink()

    with pytest.raises(RepositoryRuntimeError, match=rf"regular {re.escape(missing_name)} file"):
        assert_repository_runtime(
            tmp_path,
            runtime_version="0.1.17",
            active_package_dir=package_dir,
        )


def test_repository_runtime_blocks_source_mode_with_a_stale_uv_lock(tmp_path: Path) -> None:
    package_dir = _source_package_dir(tmp_path)
    _write_source_marker(tmp_path)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    with pytest.raises(RepositoryRuntimeError, match=r"up-to-date uv\.lock"):
        assert_repository_runtime(
            tmp_path,
            runtime_version="0.1.17",
            active_package_dir=package_dir,
        )


def test_repository_runtime_rejects_a_source_package_path_that_escapes_the_repository(tmp_path: Path) -> None:
    outside_package = tmp_path.with_name(f"{tmp_path.name}-outside-package")
    outside_package.mkdir()
    source_parent = tmp_path / "src"
    source_parent.mkdir()
    try:
        (source_parent / "apex_ray").symlink_to(outside_package, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    _write_source_marker(tmp_path)

    status = inspect_repository_runtime(
        tmp_path,
        runtime_version="0.1.17",
        active_package_dir=outside_package,
    )

    assert status.blocking is True
    assert "must resolve inside the repository" in status.reason


def test_repository_runtime_rejects_source_marker_with_a_version_lock(tmp_path: Path) -> None:
    _source_package_dir(tmp_path)
    _write_source_marker(tmp_path)
    (tmp_path / ".apex-ray" / "version").write_text("0.1.17\n", encoding="utf-8")

    status = inspect_repository_runtime(tmp_path, runtime_version="0.1.17")

    assert status.mode == RepositoryRuntimeMode.INVALID
    assert status.launcher is None
    assert status.blocking is True
    assert "both" in status.reason
    with pytest.raises(RepositoryRuntimeError, match="both"):
        assert_repository_runtime(tmp_path, runtime_version="0.1.17")


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "source",
        "source\nextra\n",
        "locked\n",
        "source \n",
        "source\n" + ("x" * 32),
    ],
)
def test_repository_runtime_rejects_malformed_source_marker(tmp_path: Path, contents: str) -> None:
    _source_package_dir(tmp_path)
    _write_source_marker(tmp_path, contents)

    status = inspect_repository_runtime(tmp_path, runtime_version="0.1.17")

    assert status.mode == RepositoryRuntimeMode.INVALID
    assert status.blocking is True
    with pytest.raises(RepositoryRuntimeError, match="Invalid Apex Ray repository runtime"):
        assert_repository_runtime(tmp_path, runtime_version="0.1.17")


def test_repository_runtime_rejects_a_runtime_symlink(tmp_path: Path) -> None:
    target = tmp_path / "runtime-mode"
    target.write_text("source\n", encoding="utf-8")
    marker = tmp_path / RUNTIME_MODE_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    try:
        marker.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    status = inspect_repository_runtime(tmp_path, runtime_version="0.1.17")

    assert status.mode == RepositoryRuntimeMode.INVALID
    assert "regular file" in status.reason


def test_repository_runtime_rejects_an_external_metadata_parent(tmp_path: Path) -> None:
    outside = tmp_path.with_name(f"{tmp_path.name}-outside-runtime")
    outside.mkdir()
    (outside / "runtime").write_text("source\n", encoding="utf-8")
    try:
        (tmp_path / ".apex-ray").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    status = inspect_repository_runtime(tmp_path, runtime_version="0.1.17")

    assert status.mode == RepositoryRuntimeMode.INVALID
    assert "outside the repository" in status.reason


def test_write_source_runtime_uses_a_regular_repository_file(tmp_path: Path) -> None:
    path = write_source_runtime(tmp_path)

    assert path == tmp_path / RUNTIME_MODE_RELATIVE_PATH
    assert path.read_text(encoding="utf-8") == "source\n"
    assert path.is_file()
    assert not path.is_symlink()


def test_write_source_runtime_refuses_a_version_lock(tmp_path: Path) -> None:
    lock = tmp_path / ".apex-ray" / "version"
    lock.parent.mkdir(parents=True)
    lock.write_text("0.1.17\n", encoding="utf-8")

    with pytest.raises(RepositoryRuntimeError, match="version lock exists"):
        write_source_runtime(tmp_path)

    assert not (tmp_path / RUNTIME_MODE_RELATIVE_PATH).exists()
