import json
import sys
from pathlib import Path
from typing import Any

import pytest

from apex_ray.analyzers.dart import cache as dart_cache
from apex_ray.analyzers.dart.cache import (
    build_dart_analysis_cache_key,
    dart_analysis_cache_path,
    load_dart_analysis_cache,
    write_dart_analysis_cache,
)
from apex_ray.models import (
    AnalyzerConfig,
    AnalyzerFile,
    AnalyzerReference,
    AnalyzerResult,
    AnalyzerSymbol,
    ChangedFile,
    FileKind,
)


def _changed(path: str = "lib/resource.dart") -> ChangedFile:
    return ChangedFile(
        old_path=path,
        new_path=path,
        language="dart",
        file_kind=FileKind.SOURCE,
    )


def test_dart_analysis_cache_round_trips_and_invalidates_workspace_changes(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("int value() => 1;\n", encoding="utf-8")
    manifest = tmp_path / "pubspec.yaml"
    manifest.write_text("name: example\n", encoding="utf-8")
    package_config = tmp_path / ".dart_tool" / "package_config.json"
    package_config.parent.mkdir()
    package_config.write_text('{"configVersion":2,"packages":[]}\n', encoding="utf-8")
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))
    result = AnalyzerResult(
        language="dart",
        projectRoot=str(tmp_path),
        files=[AnalyzerFile(path="lib/resource.dart")],
    )
    files = [Path("lib/resource.dart"), Path("pubspec.yaml")]

    written_stats = write_dart_analysis_cache(
        tmp_path,
        [_changed()],
        files,
        ["/sdk/bin/dart"],
        config,
        result,
    )
    cached = load_dart_analysis_cache(
        tmp_path,
        [_changed()],
        files,
        ["/sdk/bin/dart"],
        config,
    )

    assert written_stats is not None and written_stats.written is True
    assert cached is not None
    assert cached.language == "dart"
    assert cached.index_cache is not None
    assert cached.index_cache.hits == 1
    assert cached.index_cache.misses == 0

    package_config.write_text('{"configVersion":2,"packages":[{"name":"changed"}]}\n', encoding="utf-8")
    assert (
        load_dart_analysis_cache(
            tmp_path,
            [_changed()],
            files,
            ["/sdk/bin/dart"],
            config,
        )
        is None
    )
    write_dart_analysis_cache(
        tmp_path,
        [_changed()],
        files,
        ["/sdk/bin/dart"],
        config,
        result,
    )
    source.write_text("int value() => 2;\n", encoding="utf-8")
    assert (
        load_dart_analysis_cache(
            tmp_path,
            [_changed()],
            files,
            ["/sdk/bin/dart"],
            config,
        )
        is None
    )


def test_dart_analysis_cache_invalidates_command_and_diff(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("int value() => 1;\n", encoding="utf-8")
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))
    result = AnalyzerResult(language="dart", projectRoot=str(tmp_path))
    files = [Path("lib/resource.dart")]
    executable = tmp_path / "dart"
    executable.write_text("sdk-a", encoding="utf-8")
    command = [str(executable)]
    write_dart_analysis_cache(
        tmp_path,
        [_changed()],
        files,
        command,
        config,
        result,
    )

    assert (
        load_dart_analysis_cache(
            tmp_path,
            [_changed("lib/other.dart")],
            files,
            command,
            config,
        )
        is None
    )
    assert load_dart_analysis_cache(tmp_path, [_changed()], files, command, config) is not None

    executable.write_text("sdk-b", encoding="utf-8")
    assert load_dart_analysis_cache(tmp_path, [_changed()], files, command, config) is None
    assert (
        load_dart_analysis_cache(
            tmp_path,
            [_changed()],
            files,
            ["dart-b"],
            config,
        )
        is None
    )


@pytest.mark.parametrize("interpreter", [False, True])
def test_dart_cache_key_invalidates_repo_relative_command_files(
    tmp_path: Path,
    interpreter: bool,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    wrapper = tmp_path / "tools" / ("dart_wrapper.py" if interpreter else "dart")
    source.parent.mkdir()
    wrapper.parent.mkdir()
    source.write_text("int value() => 1;\n", encoding="utf-8")
    wrapper.write_text("print('wrapper-a')\n", encoding="utf-8")
    wrapper.chmod(0o755)
    command = [sys.executable, "tools/dart_wrapper.py"] if interpreter else ["tools/dart"]
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))

    before = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        [Path("lib/resource.dart")],
        command,
        config,
    )
    wrapper.write_text("print('wrapper-b')\n", encoding="utf-8")
    after = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        [Path("lib/resource.dart")],
        command,
        config,
    )

    assert before is not None
    assert after is not None
    assert after.fingerprint != before.fingerprint


def test_dart_cache_key_invalidates_resolved_sdk_version(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("int value() => 1;\n", encoding="utf-8")
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))

    before = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        [Path("lib/resource.dart")],
        [],
        config,
        toolchain_version="3.11.0",
    )
    after = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        [Path("lib/resource.dart")],
        [],
        config,
        toolchain_version="3.12.0",
    )

    assert before is not None
    assert after is not None
    assert after.fingerprint != before.fingerprint


def test_dart_cache_key_includes_ignored_generated_sources_and_pub_lock(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "resource.dart"
    generated = tmp_path / "lib" / "generated" / "resource.g.dart"
    manifest = tmp_path / "pubspec.yaml"
    lockfile = tmp_path / "pubspec.lock"
    analysis_options = tmp_path / "analysis_options.yaml"
    shared_lints = tmp_path / "config" / "lints.yaml"
    source.parent.mkdir()
    generated.parent.mkdir()
    shared_lints.parent.mkdir()
    source.write_text("int value() => generatedValue;\n", encoding="utf-8")
    generated.write_text("const generatedValue = 1;\n", encoding="utf-8")
    manifest.write_text("name: example\n", encoding="utf-8")
    lockfile.write_text("packages: {}\n", encoding="utf-8")
    analysis_options.write_text("include: config/lints.yaml\n", encoding="utf-8")
    shared_lints.write_text("linter:\n  rules:\n    avoid_print: true\n", encoding="utf-8")
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))
    filtered_inventory = [Path("lib/resource.dart")]

    before = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        filtered_inventory,
        [],
        config,
    )
    generated.write_text("const generatedValue = 2;\n", encoding="utf-8")
    after_generated = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        filtered_inventory,
        [],
        config,
    )
    lockfile.write_text("packages:\n  changed: true\n", encoding="utf-8")
    after_lock = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        filtered_inventory,
        [],
        config,
    )
    manifest.write_text("name: renamed_example\n", encoding="utf-8")
    after_manifest = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        filtered_inventory,
        [],
        config,
    )
    shared_lints.write_text("linter:\n  rules:\n    avoid_print: false\n", encoding="utf-8")
    after_lints = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        filtered_inventory,
        [],
        config,
    )

    assert before is not None
    assert after_generated is not None
    assert after_lock is not None
    assert after_manifest is not None
    assert after_lints is not None
    assert after_generated.fingerprint != before.fingerprint
    assert after_lock.fingerprint != after_generated.fingerprint
    assert after_manifest.fingerprint != after_lock.fingerprint
    assert after_lints.fingerprint != after_manifest.fingerprint


def test_dart_cache_is_disabled_for_external_pub_path_dependency(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "lib" / "resource.dart"
    external_source = tmp_path / "shared" / "lib" / "shared.dart"
    package_config = repo / ".dart_tool" / "package_config.json"
    source.parent.mkdir(parents=True)
    external_source.parent.mkdir(parents=True)
    package_config.parent.mkdir(parents=True)
    source.write_text("int value() => sharedValue;\n", encoding="utf-8")
    external_source.write_text("const sharedValue = 1;\n", encoding="utf-8")
    (repo / "pubspec.yaml").write_text(
        "name: example\ndependencies:\n  shared:\n    path: ../shared\n",
        encoding="utf-8",
    )
    (repo / "pubspec.lock").write_text(
        "packages:\n"
        "  shared:\n"
        "    dependency: direct main\n"
        "    description:\n"
        "      path: ../shared\n"
        "      relative: true\n"
        "    source: path\n"
        "    version: 1.0.0\n",
        encoding="utf-8",
    )
    package_config.write_text(
        json.dumps(
            {
                "configVersion": 2,
                "packages": [
                    {
                        "name": "shared",
                        "rootUri": "../../shared",
                        "packageUri": "lib/",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    key = build_dart_analysis_cache_key(
        repo,
        [_changed()],
        [Path("lib/resource.dart"), Path("pubspec.yaml")],
        [],
        AnalyzerConfig(index_cache_dir=str(repo / "cache")),
    )

    assert key is None


def test_dart_cache_key_follows_repo_local_package_analysis_include(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "resource.dart"
    shared_lints = tmp_path / "packages" / "shared" / "lib" / "lints.yaml"
    package_config = tmp_path / ".dart_tool" / "package_config.json"
    source.parent.mkdir(parents=True)
    shared_lints.parent.mkdir(parents=True)
    package_config.parent.mkdir(parents=True)
    source.write_text("int value() => 1;\n", encoding="utf-8")
    shared_lints.write_text("linter:\n  rules:\n    avoid_print: true\n", encoding="utf-8")
    (tmp_path / "analysis_options.yaml").write_text(
        "include: package:shared/lints.yaml\n",
        encoding="utf-8",
    )
    (tmp_path / "pubspec.yaml").write_text("name: example\n", encoding="utf-8")
    (tmp_path / "pubspec.lock").write_text(
        "packages:\n"
        "  shared:\n"
        "    dependency: direct main\n"
        "    description:\n"
        "      path: packages/shared\n"
        "      relative: true\n"
        "    source: path\n"
        "    version: 1.0.0\n",
        encoding="utf-8",
    )
    package_config.write_text(
        json.dumps(
            {
                "configVersion": 2,
                "packages": [
                    {
                        "name": "shared",
                        "rootUri": "../packages/shared",
                        "packageUri": "lib/",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))

    before = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        [Path("lib/resource.dart")],
        [],
        config,
    )
    shared_lints.write_text("linter:\n  rules:\n    avoid_print: false\n", encoding="utf-8")
    after = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        [Path("lib/resource.dart")],
        [],
        config,
    )

    assert before is not None
    assert after is not None
    assert after.fingerprint != before.fingerprint


def test_dart_cache_key_separates_reference_allowlists(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "resource.dart"
    ignored = tmp_path / "private" / "credentials.dart"
    source.parent.mkdir(parents=True)
    ignored.parent.mkdir(parents=True)
    source.write_text("int value() => credential;\n", encoding="utf-8")
    ignored.write_text("const credential = 'DO_NOT_SEND';\n", encoding="utf-8")
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))
    broad_inventory = [Path("lib/resource.dart"), Path("private/credentials.dart")]
    narrow_inventory = [Path("lib/resource.dart")]
    sensitive_symbol = AnalyzerSymbol(
        name="value",
        kind="function",
        startLine=1,
        endLine=1,
        references=[
            AnalyzerReference(
                file="private/credentials.dart",
                line=1,
                text="const credential = 'DO_NOT_SEND';",
                kind="read",
            )
        ],
    )
    result = AnalyzerResult(
        language="dart",
        projectRoot=str(tmp_path),
        files=[
            AnalyzerFile(
                path="lib/resource.dart",
                symbols=[sensitive_symbol],
                relatedTests=[],
                imports=[],
                exports=[],
                changedSymbols=[sensitive_symbol],
                uncoveredChangedRanges=[],
            )
        ],
    )

    broad_key = build_dart_analysis_cache_key(tmp_path, [_changed()], broad_inventory, [], config)
    narrow_key = build_dart_analysis_cache_key(tmp_path, [_changed()], narrow_inventory, [], config)
    assert broad_key is not None
    assert narrow_key is not None
    assert broad_key.fingerprint != narrow_key.fingerprint

    written = write_dart_analysis_cache(
        tmp_path,
        [_changed()],
        broad_inventory,
        [],
        config,
        result,
        cache_key=broad_key,
    )
    assert written is not None
    assert (
        load_dart_analysis_cache(
            tmp_path,
            [_changed()],
            narrow_inventory,
            [],
            config,
            cache_key=narrow_key,
        )
        is None
    )


def test_dart_cache_rebinds_project_root_when_shared_between_checkouts(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    shared_cache = tmp_path / "shared-cache"
    for repo in (first, second):
        source = repo / "lib" / "resource.dart"
        source.parent.mkdir(parents=True)
        source.write_text("int value() => 1;\n", encoding="utf-8")
    config = AnalyzerConfig(index_cache_dir=str(shared_cache))
    files = [Path("lib/resource.dart")]
    first_key = build_dart_analysis_cache_key(first, [_changed()], files, [], config)
    second_key = build_dart_analysis_cache_key(second, [_changed()], files, [], config)
    assert first_key is not None
    assert second_key is not None
    assert first_key.fingerprint == second_key.fingerprint

    result = AnalyzerResult(
        language="dart",
        projectRoot=str(first.resolve()),
        files=[AnalyzerFile(path="lib/resource.dart")],
    )
    written = write_dart_analysis_cache(
        first,
        [_changed()],
        files,
        [],
        config,
        result,
        cache_key=first_key,
    )
    relocated = load_dart_analysis_cache(
        second,
        [_changed()],
        files,
        [],
        config,
        cache_key=second_key,
    )

    assert written is not None
    assert relocated is not None
    assert relocated.project_root == str(second.resolve())
    serialized_cache = dart_analysis_cache_path(first, config).read_text(encoding="utf-8")
    assert str(first.resolve()) not in serialized_cache


@pytest.mark.parametrize("suffix", [".kt", ".java", ".swift", ".m", ".mm"])
def test_dart_cache_key_invalidates_platform_channel_source_changes(
    tmp_path: Path,
    suffix: str,
) -> None:
    dart_source = tmp_path / "lib" / "bridge.dart"
    dart_source.parent.mkdir()
    dart_source.write_text("const channel = 'example';\n", encoding="utf-8")
    native_source = tmp_path / "platform" / f"Bridge{suffix}"
    native_source.parent.mkdir()
    native_source.write_text("const channel = 'before';\n", encoding="utf-8")
    files = [Path("lib/bridge.dart"), Path("platform") / native_source.name]
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))

    before = build_dart_analysis_cache_key(tmp_path, [_changed("lib/bridge.dart")], files, [], config)
    native_source.write_text("const channel = 'after';\n", encoding="utf-8")
    after = build_dart_analysis_cache_key(tmp_path, [_changed("lib/bridge.dart")], files, [], config)

    assert before is not None
    assert after is not None
    assert after.fingerprint != before.fingerprint


def test_dart_analysis_cache_ignores_corrupt_oversized_and_disabled_cache(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("int value() => 1;\n", encoding="utf-8")
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))
    cache_path = dart_analysis_cache_path(tmp_path, config)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("not-json", encoding="utf-8")

    assert (
        load_dart_analysis_cache(
            tmp_path,
            [_changed()],
            [Path("lib/resource.dart")],
            ["dart"],
            config,
        )
        is None
    )

    cache_path.write_text(json.dumps({"padding": "x" * (17 * 1024 * 1024)}), encoding="utf-8")
    assert (
        load_dart_analysis_cache(
            tmp_path,
            [_changed()],
            [Path("lib/resource.dart")],
            ["dart"],
            config,
        )
        is None
    )

    disabled = AnalyzerConfig(index_cache_enabled=False, index_cache_dir=str(tmp_path / "disabled"))
    assert (
        write_dart_analysis_cache(
            tmp_path,
            [_changed()],
            [Path("lib/resource.dart")],
            ["dart"],
            disabled,
            AnalyzerResult(language="dart", projectRoot=str(tmp_path)),
        )
        is None
    )
    assert not (tmp_path / "disabled").exists()


def test_dart_analysis_cache_ignores_recursively_nested_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("int value() => 1;\n", encoding="utf-8")
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))
    cache_path = dart_analysis_cache_path(tmp_path, config)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        dart_cache.json,
        "loads",
        lambda _text: (_ for _ in ()).throw(RecursionError("synthetic nesting")),
    )

    assert (
        load_dart_analysis_cache(
            tmp_path,
            [_changed()],
            [Path("lib/resource.dart")],
            [],
            config,
        )
        is None
    )


def test_dart_cache_key_stops_during_inventory_when_deadline_expires(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    first = tmp_path / "lib" / "first.dart"
    second = tmp_path / "lib" / "second.dart"
    first.parent.mkdir()
    first.write_text("int first() => 1;\n", encoding="utf-8")
    second.write_text("int second() => 2;\n", encoding="utf-8")
    now = [0.0]
    source_opens = 0
    real_open = Path.open

    class ExpiringInventory(list[Path]):
        def __iter__(self):
            for index, item in enumerate(super().__iter__()):
                if index == 1:
                    now[0] = 2.0
                yield item

    def counting_open(path: Path, *args: Any, **kwargs: Any):
        nonlocal source_opens
        if path in {first, second}:
            source_opens += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(dart_cache.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(Path, "open", counting_open)

    key = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        ExpiringInventory([Path("lib/first.dart"), Path("lib/second.dart")]),
        [],
        AnalyzerConfig(index_cache_dir=str(tmp_path / "cache")),
        deadline=1.0,
    )

    assert key is None
    assert source_opens == 0, "expired inventory must not proceed to workspace hashing"


def test_dart_cache_key_stops_during_file_hash_when_deadline_expires(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_bytes(b"a" * (256 * 1024))
    now = [0.0]
    source_reads = 0
    real_open = Path.open

    class ExpiringReader:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._handle.__exit__(*args)

        def read(self, size: int = -1) -> bytes:
            nonlocal source_reads
            source_reads += 1
            chunk = self._handle.read(size)
            now[0] = 2.0
            return chunk

    def expiring_open(path: Path, *args: Any, **kwargs: Any):
        handle = real_open(path, *args, **kwargs)
        return ExpiringReader(handle) if path == source else handle

    monkeypatch.setattr(dart_cache.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(Path, "open", expiring_open)

    key = build_dart_analysis_cache_key(
        tmp_path,
        [_changed()],
        [Path("lib/resource.dart")],
        [],
        AnalyzerConfig(index_cache_dir=str(tmp_path / "cache")),
        deadline=1.0,
    )

    assert key is None
    assert source_reads == 1, "deadline should be rechecked after each hash read"


def test_precomputed_dart_cache_key_reuses_one_workspace_traversal(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("int value() => 1;\n", encoding="utf-8")
    files = [Path("lib/resource.dart")]
    changed = [_changed()]
    command: list[str] = []
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))
    result = AnalyzerResult(language="dart", projectRoot=str(tmp_path))
    source_opens = 0
    real_open = Path.open

    def counting_open(path: Path, *args: Any, **kwargs: Any):
        nonlocal source_opens
        if path == source:
            source_opens += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    key = build_dart_analysis_cache_key(tmp_path, changed, files, command, config)

    assert key is not None
    assert (
        load_dart_analysis_cache(
            tmp_path,
            changed,
            files,
            command,
            config,
            cache_key=key,
        )
        is None
    )
    assert (
        write_dart_analysis_cache(
            tmp_path,
            changed,
            files,
            command,
            config,
            result,
            cache_key=key,
        )
        is not None
    )
    assert (
        load_dart_analysis_cache(
            tmp_path,
            changed,
            files,
            command,
            config,
            cache_key=key,
        )
        is not None
    )
    assert source_opens == 1


def test_dart_cache_skips_disk_io_after_deadline(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("int value() => 1;\n", encoding="utf-8")
    files = [Path("lib/resource.dart")]
    changed = [_changed()]
    command: list[str] = []
    config = AnalyzerConfig(index_cache_dir=str(tmp_path / "cache"))
    result = AnalyzerResult(language="dart", projectRoot=str(tmp_path))
    key = build_dart_analysis_cache_key(tmp_path, changed, files, command, config)
    assert key is not None
    assert (
        write_dart_analysis_cache(
            tmp_path,
            changed,
            files,
            command,
            config,
            result,
            cache_key=key,
        )
        is not None
    )
    cache_path = dart_analysis_cache_path(tmp_path, config)
    original = cache_path.read_text(encoding="utf-8")
    cache_reads = 0
    real_read_text = Path.read_text

    def counting_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal cache_reads
        if path == cache_path:
            cache_reads += 1
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(dart_cache.time, "monotonic", lambda: 2.0)
    monkeypatch.setattr(Path, "read_text", counting_read_text)

    assert (
        load_dart_analysis_cache(
            tmp_path,
            changed,
            files,
            command,
            config,
            cache_key=key,
            deadline=1.0,
        )
        is None
    )
    assert (
        write_dart_analysis_cache(
            tmp_path,
            changed,
            files,
            command,
            config,
            result,
            cache_key=key,
            deadline=1.0,
        )
        is None
    )
    assert cache_reads == 0
    assert real_read_text(cache_path, encoding="utf-8") == original
