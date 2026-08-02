import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import apex_ray.analyzers.dart.metadata as metadata_module
import apex_ray.analyzers.dart.platform_channels as platform_channels_module
import apex_ray.analyzers.dart.related_tests as related_tests_module
from apex_ray.analyzers.dart.directives import parse_dart_directives
from apex_ray.analyzers.dart.generated import is_generated_dart_path
from apex_ray.analyzers.dart.metadata import (
    build_dart_framework_metadata_index,
    collect_dart_framework_metadata,
)
from apex_ray.analyzers.dart.platform_channels import (
    build_platform_channel_index,
    extract_platform_channel_endpoints,
    platform_channel_contracts,
)
from apex_ray.analyzers.dart.related_tests import rank_related_dart_tests
from apex_ray.analyzers.dart.toolchain import resolve_dart_command, resolve_dart_toolchain
from apex_ray.models import DartAnalyzerConfig

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "dart_semantics"


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_dart_toolchain_resolution_is_ordered_and_never_shell_parses(tmp_path: Path) -> None:
    project_dart = _make_executable(tmp_path / ".fvm" / "flutter_sdk" / "bin" / "dart")
    path_dart = _make_executable(tmp_path / "path" / "dart")
    path_fvm = _make_executable(tmp_path / "path" / "fvm")
    commands = {"dart": str(path_dart), "fvm": str(path_fvm)}

    configured = resolve_dart_command(
        tmp_path,
        ["custom dart", "--flag=$(not-a-shell)"],
        which=commands.get,
    )
    assert configured is not None
    assert configured.command == ["custom dart", "--flag=$(not-a-shell)"]
    assert configured.source == "configured"

    local = resolve_dart_command(tmp_path, which=commands.get)
    assert local is not None
    assert local.command == [str(project_dart)]
    assert local.source == "project-fvm"

    project_dart.unlink()
    path_resolution = resolve_dart_command(tmp_path, which=commands.get)
    assert path_resolution is not None
    assert path_resolution.command == [str(path_dart)]
    assert path_resolution.source == "path"

    commands.pop("dart")
    fvm_resolution = resolve_dart_command(tmp_path, which=commands.get)
    assert fvm_resolution is not None
    assert fvm_resolution.command == [str(path_fvm), "dart"]
    assert fvm_resolution.source == "fvm"


def test_dart_toolchain_uses_only_an_unambiguous_flutter_sibling(tmp_path: Path) -> None:
    flutter = _make_executable(tmp_path / "flutter-sdk" / "bin" / "flutter")
    dart = _make_executable(flutter.parent / "dart")

    resolution = resolve_dart_command(tmp_path, which=lambda name: str(flutter) if name == "flutter" else None)

    assert resolution is not None
    assert resolution.command == [str(dart)]
    assert resolution.source == "flutter-sibling"

    symlink_dir = tmp_path / "linked-bin"
    symlink_dir.mkdir()
    flutter_link = symlink_dir / "flutter"
    flutter_link.symlink_to(flutter)
    _make_executable(symlink_dir / "dart")
    assert (
        resolve_dart_command(
            tmp_path,
            which=lambda name: str(flutter_link) if name == "flutter" else None,
        )
        is None
    )


def test_dart_toolchain_rejects_shell_strings_and_empty_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="argument list"):
        resolve_dart_command(tmp_path, "dart --enable-experiment")
    with pytest.raises(ValueError, match="non-empty"):
        resolve_dart_command(tmp_path, ["dart", ""])


def test_dart_toolchain_reports_an_invalid_configured_command(tmp_path: Path) -> None:
    missing = tmp_path / "missing-sdk" / "dart"

    resolution = resolve_dart_toolchain(
        tmp_path,
        DartAnalyzerConfig(command=[str(missing)]),
        probe_version=True,
        timeout_seconds=0.1,
    )

    assert resolution.command == [str(missing)]
    assert resolution.version is None
    assert resolution.error is not None
    assert "Unable to run configured Dart SDK command" in resolution.error
    assert resolution.remediation is not None
    assert "review.analyzer.dart.command" in resolution.remediation


def test_dart_toolchain_normalizes_the_sdk_version_label(tmp_path: Path) -> None:
    dart = _make_executable(tmp_path / "sdk" / "bin" / "dart")
    dart.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'Dart SDK version: 3.11.6 (stable)' >&2\n",
        encoding="utf-8",
    )

    resolution = resolve_dart_toolchain(
        tmp_path,
        DartAnalyzerConfig(command=[str(dart)]),
        probe_version=True,
    )

    assert resolution.version == "3.11.6 (stable)"
    assert resolution.error is None


def test_dart_toolchain_rejects_plugin_isolation_on_an_incompatible_sdk(tmp_path: Path) -> None:
    dart = _make_executable(tmp_path / "sdk" / "bin" / "dart")
    dart.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf '%s\\n' 'Dart SDK version: 3.10.1 (stable)' >&2\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' 'Could not find an option named --no-plugins.' >&2\n"
        "exit 64\n",
        encoding="utf-8",
    )

    resolution = resolve_dart_toolchain(
        tmp_path,
        DartAnalyzerConfig(command=[str(dart)], plugins=False),
        probe_version=True,
    )

    assert resolution.version == "3.10.1 (stable)"
    assert resolution.error is not None
    assert "cannot disable analyzer plugins" in resolution.error
    assert resolution.remediation is not None
    assert "plugins: true" in resolution.remediation


@pytest.mark.parametrize(
    "path",
    [
        "lib/account.g.dart",
        "lib/account.freezed.dart",
        "lib/router.config.dart",
        "test/client.mocks.dart",
        "lib/routes.gr.dart",
        "lib/api.chopper.dart",
    ],
)
def test_generated_dart_suffix_policy(path: str) -> None:
    assert is_generated_dart_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "lib/generated/model.dart",
        "lib/__generated__/model.dart",
        "build/flutter_assets/model.dart",
        "dist/lib/model.dart",
        "LIB/GENERATED/MODEL.DART",
        r"lib\generated\model.dart",
    ],
)
def test_generated_dart_directory_policy(path: str) -> None:
    assert is_generated_dart_path(path)


@pytest.mark.parametrize(
    "path",
    ["lib/account.dart", "lib/g.dart", "lib/freezed.dart", "lib/account_test.dart"],
)
def test_generated_dart_suffix_policy_avoids_broad_matches(path: str) -> None:
    assert not is_generated_dart_path(path)


def test_directive_scanner_is_lexical_bounded_and_preserves_parts() -> None:
    source = """
// import 'ignored.dart';
const fake = "export 'also_ignored.dart';";
library sample.feature;
import 'dart:async';
import 'package:sample/core.dart'
    if (dart.library.io) 'package:sample/io.dart'
    if (dart.library.html) 'package:sample/web.dart' show Client;
export r'src/public.dart' hide Internal;
part 'model.g.dart';
part of sample.feature;
"""

    directives = parse_dart_directives(source)

    assert [(item.kind, item.target) for item in directives] == [
        ("import", "dart:async"),
        ("import", "package:sample/core.dart"),
        ("export", "src/public.dart"),
        ("part", "model.g.dart"),
        ("part-of", "sample.feature"),
    ]
    assert directives[1].conditional_targets == (
        "package:sample/io.dart",
        "package:sample/web.dart",
    )
    assert directives[1].line == 6
    assert directives[1].end_line == 8

    assert len(parse_dart_directives("import 'a.dart'; import 'b.dart';", max_directives=1)) == 1
    assert parse_dart_directives(" " * 64 + "import 'late.dart';", max_source_chars=32) == []


def test_related_tests_rank_semantic_evidence_then_import_and_convention(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT / "workspace", repo)

    ranked = rank_related_dart_tests(
        repo,
        "packages/app/lib/src/profile_screen.dart",
        semantic_references=[
            "packages/app/integration_test/profile_journey_test.dart",
            "../outside_test.dart",
            "packages/app/test/src/generated_helper.mocks.dart",
        ],
        symbol_names={"ProfileScreen", "ProfileCubit"},
        limit=4,
    )

    assert ranked == [
        "packages/app/integration_test/profile_journey_test.dart",
        "packages/app/test/src/profile_screen_test.dart",
        "packages/consumer/test/cross_package_profile_test.dart",
        "packages/app/test/profile_screen_test.dart",
    ]


def test_related_tests_are_repo_bounded_existing_handwritten_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT / "workspace", repo)
    assert rank_related_dart_tests(repo, "packages/app/test/profile_screen_test.dart") == []
    assert rank_related_dart_tests(repo, "missing/lib/value.dart") == []


def test_related_test_limit_counts_only_relevant_inventory_entries(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "value.dart"
    related = tmp_path / "test" / "value_test.dart"
    source.parent.mkdir()
    related.parent.mkdir()
    source.write_text("int value() => 1;\n", encoding="utf-8")
    related.write_text("import '../lib/value.dart';\n", encoding="utf-8")

    ranked = rank_related_dart_tests(
        tmp_path,
        "lib/value.dart",
        candidate_paths=[
            "assets/000.png",
            "assets/001.png",
            "assets/002.png",
            "test/value_test.dart",
        ],
        max_files=1,
    )

    assert ranked == ["test/value_test.dart"]


def test_package_manifest_limit_counts_only_manifests(tmp_path: Path) -> None:
    asset = tmp_path / "assets" / "ignored.bin"
    source = tmp_path / "packages" / "shared" / "lib" / "value.dart"
    manifest = tmp_path / "packages" / "shared" / "pubspec.yaml"
    related = tmp_path / "packages" / "app" / "test" / "consumer_test.dart"
    asset.parent.mkdir()
    source.parent.mkdir(parents=True)
    related.parent.mkdir(parents=True)
    asset.write_bytes(b"asset")
    source.write_text("int value() => 1;\n", encoding="utf-8")
    manifest.write_text("name: shared\n", encoding="utf-8")
    related.write_text("import 'package:shared/value.dart';\n", encoding="utf-8")

    ranked = rank_related_dart_tests(
        tmp_path,
        "packages/shared/lib/value.dart",
        candidate_paths=[
            "assets/ignored.bin",
            "packages/app/test/consumer_test.dart",
            "packages/shared/lib/value.dart",
            "packages/shared/pubspec.yaml",
        ],
        max_files=1,
    )

    assert ranked == ["packages/app/test/consumer_test.dart"]


def test_related_test_index_scans_and_reads_once_for_multiple_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "pubspec.yaml"
    first_source = tmp_path / "lib" / "first.dart"
    second_source = tmp_path / "lib" / "second.dart"
    first_test = tmp_path / "test" / "first_consumer_test.dart"
    second_test = tmp_path / "test" / "second_consumer_test.dart"
    first_source.parent.mkdir()
    first_test.parent.mkdir()
    manifest.write_text("name: sample\n", encoding="utf-8")
    first_source.write_text("int first() => 1;\n", encoding="utf-8")
    second_source.write_text("int second() => 2;\n", encoding="utf-8")
    first_test.write_text("import '../lib/first.dart';\n", encoding="utf-8")
    second_test.write_text("import '../lib/second.dart';\n", encoding="utf-8")
    inventory = [
        "assets/ignored.png",
        "lib/first.dart",
        "lib/second.dart",
        "pubspec.yaml",
        "test/first_consumer_test.dart",
        "test/second_consumer_test.dart",
    ]

    reads: dict[str, int] = {}
    directive_scans = 0
    original_read = related_tests_module._read_bounded_text
    original_parse = related_tests_module.parse_dart_directives

    def counted_read(path: Path, max_bytes: int) -> str | None:
        relative = path.relative_to(tmp_path).as_posix()
        reads[relative] = reads.get(relative, 0) + 1
        return original_read(path, max_bytes)

    def counted_parse(source: str, **kwargs: int):
        nonlocal directive_scans
        directive_scans += 1
        return original_parse(source, **kwargs)

    monkeypatch.setattr(related_tests_module, "_read_bounded_text", counted_read)
    monkeypatch.setattr(related_tests_module, "parse_dart_directives", counted_parse)

    index = related_tests_module.build_dart_related_test_index(
        tmp_path,
        candidate_paths=inventory,
    )
    first_ranked = rank_related_dart_tests(
        tmp_path,
        "lib/first.dart",
        index=index,
        limit=1,
    )
    second_ranked = rank_related_dart_tests(
        tmp_path,
        "lib/second.dart",
        index=index,
        limit=1,
    )

    assert first_ranked == ["test/first_consumer_test.dart"]
    assert second_ranked == ["test/second_consumer_test.dart"]
    assert reads == {
        "pubspec.yaml": 1,
        "test/first_consumer_test.dart": 1,
        "test/second_consumer_test.dart": 1,
    }
    assert directive_scans == 2
    assert index.files_read == 3


def test_related_test_index_precomputes_symbol_evidence_for_repeated_ranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_source = tmp_path / "lib" / "first.dart"
    second_source = tmp_path / "lib" / "second.dart"
    related = tmp_path / "test" / "shared_consumer_test.dart"
    first_source.parent.mkdir()
    related.parent.mkdir()
    first_source.write_text("class FirstService {}\n", encoding="utf-8")
    second_source.write_text("class SecondService {}\n", encoding="utf-8")
    related.write_text(
        "void main() { FirstService(); SecondService(); }\n",
        encoding="utf-8",
    )

    index = related_tests_module.build_dart_related_test_index(
        tmp_path,
        candidate_paths=[
            "lib/first.dart",
            "lib/second.dart",
            "test/shared_consumer_test.dart",
        ],
    )

    def fail_if_ranking_rescans_text(*args: object, **kwargs: object) -> None:
        raise AssertionError("ranking rescanned test source instead of using its run-scoped index")

    monkeypatch.setattr(related_tests_module.re, "search", fail_if_ranking_rescans_text)

    assert rank_related_dart_tests(
        tmp_path,
        "lib/first.dart",
        symbol_names=["FirstService"],
        index=index,
    ) == ["test/shared_consumer_test.dart"]
    assert rank_related_dart_tests(
        tmp_path,
        "lib/second.dart",
        symbol_names=["SecondService"],
        index=index,
    ) == ["test/shared_consumer_test.dart"]


def test_related_test_ranking_stops_at_the_index_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "value.dart"
    related = tmp_path / "test" / "value_test.dart"
    source.parent.mkdir()
    related.parent.mkdir()
    source.write_text("int value() => 1;\n", encoding="utf-8")
    related.write_text("import '../lib/value.dart';\n", encoding="utf-8")
    clock = {"now": 0.0}
    monkeypatch.setattr(related_tests_module, "monotonic", lambda: clock["now"])

    index = related_tests_module.build_dart_related_test_index(
        tmp_path,
        candidate_paths=["lib/value.dart", "test/value_test.dart"],
        deadline=1.0,
    )
    clock["now"] = 1.0

    assert rank_related_dart_tests(tmp_path, "lib/value.dart", index=index) == []


def test_related_test_symbol_index_is_exact_for_dart_dollar_identifiers() -> None:
    source = "Alpha alpha_beta αλφα foo$bar 1$tail $prefix trailing$ $$ a$b$c"
    evidence, memory_bytes, truncated = related_tests_module._index_symbol_evidence(
        source,
        max_memory_bytes=1_000_000,
        deadline=None,
    )
    names = [
        "Alpha",
        "alpha_beta",
        "αλφα",
        "foo$bar",
        "$bar",
        "foo$",
        "$",
        "$tail",
        "$prefix",
        "trailing$",
        "a$b$c",
        "$b$",
    ]

    assert evidence is not None
    assert {name: name in evidence for name in names} == {
        name: bool(related_tests_module.re.search(rf"\b{related_tests_module.re.escape(name)}\b", source))
        for name in names
    }
    assert memory_bytes <= 1_000_000
    assert truncated is False


def test_related_test_symbol_index_uses_path_only_evidence_at_memory_cap(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "source.dart"
    related = tmp_path / "test" / "unrelated_test.dart"
    source.parent.mkdir()
    related.parent.mkdir()
    source.write_text("class ExpensiveSymbol {}\n", encoding="utf-8")
    related.write_text("void main() { ExpensiveSymbol(); }\n", encoding="utf-8")

    index = related_tests_module.build_dart_related_test_index(
        tmp_path,
        candidate_paths=["lib/source.dart", "test/unrelated_test.dart"],
        max_identifier_memory_bytes=1,
    )

    assert index.identifier_memory_bytes == 0
    assert index.truncated is True
    assert index.tests[0].symbol_evidence is None
    assert (
        rank_related_dart_tests(
            tmp_path,
            "lib/source.dart",
            symbol_names=["ExpensiveSymbol"],
            index=index,
        )
        == []
    )
    assert rank_related_dart_tests(
        tmp_path,
        "lib/source.dart",
        semantic_references=["test/unrelated_test.dart"],
        symbol_names=["ExpensiveSymbol"],
        index=index,
    ) == ["test/unrelated_test.dart"]


def test_related_test_index_enforces_total_source_byte_cap(tmp_path: Path) -> None:
    manifest = tmp_path / "pubspec.yaml"
    test_file = tmp_path / "test" / "value_test.dart"
    test_file.parent.mkdir()
    manifest.write_text("name: sample\n", encoding="utf-8")
    test_file.write_text("import '../lib/value.dart';\n" + "x" * 128, encoding="utf-8")
    cap = manifest.stat().st_size

    index = related_tests_module.build_dart_related_test_index(
        tmp_path,
        candidate_paths=["pubspec.yaml", "test/value_test.dart"],
        max_total_source_bytes=cap,
    )

    assert index.source_bytes_read <= cap
    assert index.files_read == 1
    assert index.skipped_text_files == 1
    assert index.truncated is True
    assert len(index.tests) == 1
    assert index.tests[0].directives == ()
    assert index.tests[0].symbol_evidence is None


def test_related_test_inventory_rejects_symlink_into_omitted_source(tmp_path: Path) -> None:
    omitted = tmp_path / "private" / "test" / "secret_test.dart"
    link = tmp_path / "test" / "allowed_test.dart"
    omitted.parent.mkdir(parents=True)
    link.parent.mkdir(parents=True)
    omitted.write_text("const syntheticCredential = 'DO_NOT_SEND';\n", encoding="utf-8")
    try:
        link.symlink_to(omitted)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    index = related_tests_module.build_dart_related_test_index(
        tmp_path,
        candidate_paths=["test/allowed_test.dart"],
    )

    assert index.tests == ()
    assert index.files_read == 0


def test_platform_channel_inventory_rejects_symlink_into_omitted_source(tmp_path: Path) -> None:
    omitted = tmp_path / "private" / "Secret.kt"
    link = tmp_path / "android" / "MainActivity.kt"
    omitted.parent.mkdir(parents=True)
    link.parent.mkdir(parents=True)
    omitted.write_text(
        'val channel = MethodChannel(messenger, "private/DO_NOT_SEND")\n',
        encoding="utf-8",
    )
    try:
        link.symlink_to(omitted)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    index = build_platform_channel_index(
        tmp_path,
        candidate_paths=["android/MainActivity.kt"],
    )

    assert index.endpoints == ()
    assert index.scanned_files == 0


def test_related_test_index_returns_partial_result_when_deadline_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_test = tmp_path / "test" / "first_test.dart"
    second_test = tmp_path / "test" / "second_test.dart"
    first_test.parent.mkdir()
    first_test.write_text("import '../lib/first.dart';\n", encoding="utf-8")
    second_test.write_text("import '../lib/second.dart';\n", encoding="utf-8")

    now = 0.0
    reads: list[str] = []
    parses = 0
    original_read = related_tests_module._read_bounded_text
    original_parse = related_tests_module.parse_dart_directives

    def monotonic() -> float:
        return now

    def counted_read(path: Path, max_bytes: int) -> str | None:
        reads.append(path.relative_to(tmp_path).as_posix())
        return original_read(path, max_bytes)

    def expiring_parse(source: str, **kwargs: int):
        nonlocal now, parses
        parses += 1
        parsed = original_parse(source, **kwargs)
        now = 10.0
        return parsed

    monkeypatch.setattr(related_tests_module, "monotonic", monotonic)
    monkeypatch.setattr(related_tests_module, "_read_bounded_text", counted_read)
    monkeypatch.setattr(related_tests_module, "parse_dart_directives", expiring_parse)

    index = related_tests_module.build_dart_related_test_index(
        tmp_path,
        candidate_paths=["test/first_test.dart", "test/second_test.dart"],
        deadline=10.0,
    )

    assert [candidate.path for candidate in index.tests] == ["test/first_test.dart"]
    assert reads == ["test/first_test.dart"]
    assert parses == 1
    assert index.files_read == 1
    assert index.truncated is True


def test_framework_metadata_requires_concrete_imports_and_is_deterministic() -> None:
    source = (FIXTURE_ROOT / "framework_sample.dart").read_text(encoding="utf-8")

    metadata = collect_dart_framework_metadata("lib/framework_sample.dart", source, max_items=64)
    labels = [(item.line, item.text) for item in metadata]

    assert labels == sorted(labels)
    assert (14, "flutter widget declaration: StatefulWidget") in labels
    assert (21, "flutter state declaration: State<SampleScreen>") in labels
    assert (26, "lifecycle method: initState") in labels
    assert (33, "async BuildContext use after await; mounted guard: present") in labels
    assert (37, "lifecycle method: dispose") in labels
    assert any(text == "state management declaration: Cubit" for _, text in labels)
    assert any(text == "state transition call: emit" for _, text in labels)
    assert any(text == "MobX annotation: observable" for _, text in labels)
    assert any(text == "dependency injection: GetIt lookup" for _, text in labels)
    assert any(text == "routing declaration: GoRoute" for _, text in labels)
    assert any(text == "navigation call: context.go" for _, text in labels)
    assert any(text == "serialization annotation: JsonSerializable" for _, text in labels)
    assert any(text == "boundary: secure storage" for _, text in labels)

    assert (
        collect_dart_framework_metadata(
            "lib/plain.dart",
            "class StatefulWidget {}\nvoid emit() {}\n",
        )
        == []
    )
    assert collect_dart_framework_metadata("lib/model.g.dart", source) == []


def test_framework_metadata_respects_symbol_range_and_cap() -> None:
    source = (FIXTURE_ROOT / "framework_sample.dart").read_text(encoding="utf-8")
    metadata = collect_dart_framework_metadata(
        "lib/framework_sample.dart",
        source,
        start_line=30,
        end_line=40,
        max_items=2,
    )

    assert len(metadata) == 2
    assert all(30 <= item.line <= 40 for item in metadata)


def test_framework_metadata_index_scans_source_once_for_multiple_symbol_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (FIXTURE_ROOT / "framework_sample.dart").read_text(encoding="utf-8")
    calls = {"directives": 0, "mask": 0, "async": 0}
    original_directives = metadata_module.parse_dart_directives
    original_mask = metadata_module._mask_comments_and_strings
    original_async = metadata_module._collect_async_context

    def counted_directives(value: str):
        calls["directives"] += 1
        return original_directives(value)

    def counted_mask(value: str, *, deadline: float | None = None) -> str:
        calls["mask"] += 1
        return original_mask(value, deadline=deadline)

    def counted_async(masked: str, add: metadata_module.MetadataSink, *, deadline: float | None) -> bool:
        calls["async"] += 1
        return original_async(masked, add, deadline=deadline)

    monkeypatch.setattr(metadata_module, "parse_dart_directives", counted_directives)
    monkeypatch.setattr(metadata_module, "_mask_comments_and_strings", counted_mask)
    monkeypatch.setattr(metadata_module, "_collect_async_context", counted_async)

    index = build_dart_framework_metadata_index("lib/framework_sample.dart", source)
    first = index.for_range(start_line=14, end_line=29)
    second = index.for_range(start_line=30, end_line=40)

    assert first
    assert second
    assert calls == {"directives": 1, "mask": 1, "async": 1}


def test_framework_metadata_index_caps_retained_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata_module, "_DART_METADATA_INDEX_LIMIT", 2)
    source = "\n".join(
        [
            "import 'package:flutter/material.dart';",
            "class First extends StatelessWidget {}",
            "class Second extends StatelessWidget {}",
            "class Third extends StatelessWidget {}",
        ]
    )

    index = build_dart_framework_metadata_index("lib/widgets.dart", source)

    assert len(index.references) == 2
    assert index.truncated is True


def test_framework_metadata_index_honors_an_absolute_deadline_between_scan_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (FIXTURE_ROOT / "framework_sample.dart").read_text(encoding="utf-8")
    clock = {"now": 0.0}
    original_directives = metadata_module.parse_dart_directives
    mask_calls = 0

    def expiring_directives(value: str):
        parsed = original_directives(value)
        clock["now"] = 2.0
        return parsed

    def counted_mask(value: str, *, deadline: float | None = None) -> str:
        nonlocal mask_calls
        mask_calls += 1
        return value

    monkeypatch.setattr(metadata_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(metadata_module, "parse_dart_directives", expiring_directives)
    monkeypatch.setattr(metadata_module, "_mask_comments_and_strings", counted_mask)

    index = build_dart_framework_metadata_index(
        "lib/framework_sample.dart",
        source,
        deadline=1.0,
    )

    assert index.references == ()
    assert index.truncated is True
    assert mask_calls == 0


def test_framework_metadata_range_query_stops_at_its_inherited_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (FIXTURE_ROOT / "framework_sample.dart").read_text(encoding="utf-8")
    clock = {"now": 0.0}
    monkeypatch.setattr(metadata_module.time, "monotonic", lambda: clock["now"])

    index = build_dart_framework_metadata_index(
        "lib/framework_sample.dart",
        source,
        deadline=1.0,
    )
    assert index.references

    clock["now"] = 1.0
    assert index.for_range(start_line=1, end_line=100) == []


def test_framework_metadata_index_does_not_rescan_nested_async_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    depth = 96
    source = "\n".join(
        [
            "import 'package:flutter/material.dart';",
            "Future<void> outer() async {",
            "  await Future<void>.value();",
            *(f"  Future<void> nested{level}() async {{" for level in range(depth)),
            "  await Future<void>.value();",
            "  context.go('/done');",
            *("  }" for _ in range(depth)),
            "}",
        ]
    )
    async_event_scans = 0
    async_events = 0
    original_events = metadata_module._iter_async_context_events

    def counted_events(masked: str):
        nonlocal async_event_scans, async_events
        async_event_scans += 1
        for event in original_events(masked):
            async_events += 1
            yield event

    monkeypatch.setattr(metadata_module, "_iter_async_context_events", counted_events)

    metadata = collect_dart_framework_metadata("lib/nested.dart", source)

    assert [item.text for item in metadata].count("async BuildContext use after await; mounted guard: absent") == 1
    assert async_event_scans == 1
    assert async_events <= 2 * depth + 8


def test_framework_metadata_nested_async_context_does_not_inherit_outer_await() -> None:
    source = """
import 'package:flutter/material.dart';
Future<void> outer() async {
  await Future<void>.value();
  callback(() async {
    context.go('/inner');
  });
  context.go('/outer');
}
"""

    metadata = collect_dart_framework_metadata("lib/nested.dart", source)

    async_context_lines = [
        item.line for item in metadata if item.text == "async BuildContext use after await; mounted guard: absent"
    ]
    assert async_context_lines == [8]


def test_framework_metadata_does_not_treat_arbitrary_mounted_read_as_a_guard() -> None:
    source = """
import 'package:flutter/material.dart';
Future<void> open() async {
  await Future<void>.value();
  final wasMounted = mounted;
  context.go('/unguarded');
}
"""

    metadata = collect_dart_framework_metadata("lib/arbitrary_mounted.dart", source)

    assert (6, "async BuildContext use after await; mounted guard: absent") in {
        (item.line, item.text) for item in metadata
    }


def test_framework_metadata_tracks_mounted_guard_control_flow_and_scope() -> None:
    source = """
import 'package:flutter/material.dart';
Future<void> open() async {
  await Future<void>.value();
  if (mounted) {
    context.go('/inside-positive-guard');
  }
  context.go('/outside-positive-guard');
  if (!mounted) return;
  context.go('/after-inline-exit');
  await Future<void>.value();
  if (!context.mounted) {
    return;
  }
  context.go('/after-braced-exit');
  await Future<void>.value();
  if (context.mounted) context.go('/inline-positive-guard');
  context.go('/after-inline-positive-guard');
}
"""

    metadata = collect_dart_framework_metadata("lib/scoped_mounted.dart", source)
    async_context = {
        item.line: item.text for item in metadata if item.text.startswith("async BuildContext use after await")
    }

    assert async_context == {
        6: "async BuildContext use after await; mounted guard: present",
        8: "async BuildContext use after await; mounted guard: absent",
        10: "async BuildContext use after await; mounted guard: present",
        15: "async BuildContext use after await; mounted guard: present",
        17: "async BuildContext use after await; mounted guard: present",
        18: "async BuildContext use after await; mounted guard: absent",
    }


def test_platform_channel_index_matches_only_exact_literal_channels(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT / "channels", repo)

    index = build_platform_channel_index(repo)
    endpoint_keys = {
        (endpoint.language, endpoint.side, endpoint.channel_type, endpoint.channel_name) for endpoint in index.endpoints
    }
    assert ("dart", "dart", "method", "sample/profile") in endpoint_keys
    assert ("kotlin", "native", "method", "sample/profile") in endpoint_keys
    assert ("swift", "native", "method", "sample/profile") in endpoint_keys
    assert all(endpoint.channel_name != "dynamic-channel" for endpoint in index.endpoints)

    contracts = platform_channel_contracts(index, "lib/profile_bridge.dart")
    assert [(item.file, item.kind) for item in contracts] == [
        ("android/app/src/main/kotlin/sample/MainActivity.kt", "contract"),
        ("ios/Runner/AppDelegate.swift", "contract"),
    ]
    assert all("refresh" in item.text for item in contracts)
    assert all("unrelated" not in item.text for item in contracts)


def test_platform_channel_index_filters_asset_heavy_candidate_inventory(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "bridge.dart"
    generated = tmp_path / "lib" / "bridge.g.dart"
    source.parent.mkdir()
    source.write_text(
        "import 'package:flutter/services.dart';\nfinal bridge = MethodChannel('sample/bridge');\n",
        encoding="utf-8",
    )
    generated.write_text(
        "import 'package:flutter/services.dart';\nfinal generated = MethodChannel('sample/generated');\n",
        encoding="utf-8",
    )
    assets = [f"assets/generated/asset-{index:05d}.png" for index in range(2_000)]

    index = build_platform_channel_index(
        tmp_path,
        candidate_paths=[*assets, "lib/bridge.g.dart", "lib/bridge.dart"],
        max_files=1,
    )

    assert [endpoint.channel_name for endpoint in index.endpoints] == ["sample/bridge"]
    assert index.scanned_files == 1
    assert index.skipped_files == 0
    assert index.truncated is False


def test_platform_channel_index_stops_when_deadline_expires_during_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "bridge.dart"
    source.parent.mkdir()
    source.write_text(
        "import 'package:flutter/services.dart';\nfinal bridge = MethodChannel('sample/bridge');\n",
        encoding="utf-8",
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(platform_channels_module.time, "monotonic", lambda: clock["now"])

    def candidates() -> Iterator[str]:
        yield "assets/ignored.png"
        clock["now"] = 2.0
        yield "lib/bridge.dart"

    index = build_platform_channel_index(tmp_path, candidate_paths=candidates(), deadline=1.0)

    assert index.endpoints == ()
    assert index.scanned_files == 0
    assert index.truncated is True


def test_platform_channel_index_returns_partial_result_at_total_source_byte_cap(tmp_path: Path) -> None:
    first = tmp_path / "lib" / "a_bridge.dart"
    second = tmp_path / "lib" / "b_bridge.dart"
    first.parent.mkdir()
    first.write_text(
        "import 'package:flutter/services.dart';\nfinal first = MethodChannel('sample/first');\n",
        encoding="utf-8",
    )
    second.write_text(
        "import 'package:flutter/services.dart';\nfinal second = MethodChannel('sample/second');\n",
        encoding="utf-8",
    )
    first_bytes = first.stat().st_size

    index = build_platform_channel_index(
        tmp_path,
        candidate_paths=["lib/a_bridge.dart", "lib/b_bridge.dart"],
        max_total_source_bytes=first_bytes,
    )

    assert [endpoint.channel_name for endpoint in index.endpoints] == ["sample/first"]
    assert index.scanned_files == 1
    assert index.scanned_bytes == first_bytes
    assert index.truncated is True


def test_platform_channel_index_with_expired_deadline_does_not_read_sources(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "bridge.dart"
    source.parent.mkdir()
    source.write_text(
        "import 'package:flutter/services.dart';\nfinal bridge = MethodChannel('sample/bridge');\n",
        encoding="utf-8",
    )

    index = build_platform_channel_index(
        tmp_path,
        candidate_paths=["lib/bridge.dart"],
        deadline=time.monotonic() - 1.0,
    )

    assert index.endpoints == ()
    assert index.scanned_files == 0
    assert index.scanned_bytes == 0
    assert index.truncated is True


def test_platform_channel_extractor_keeps_multiple_channels_unmixed() -> None:
    source = """
import 'package:flutter/services.dart';
const firstName = 'sample/first';
final first = MethodChannel(firstName);
final second = MethodChannel('sample/second');
Future<void> run() async {
  await first.invokeMethod<void>('one');
  await second.invokeMethod<void>('two');
}
"""

    endpoints = extract_platform_channel_endpoints("lib/bridge.dart", source)

    assert [(endpoint.channel_name, [method.name for method in endpoint.methods]) for endpoint in endpoints] == [
        ("sample/first", ["one"]),
        ("sample/second", ["two"]),
    ]


def test_platform_channel_extractor_rejects_dynamic_expressions_and_scopes_handlers() -> None:
    dart_source = """
import 'package:flutter/services.dart';
const prefix = 'sample/' + environment;
final ignored = MethodChannel('sample/' + environment);
final MethodChannel first = MethodChannel('sample/first');
final second = MethodChannel('sample/second');
"""
    kotlin_source = """
fun configure(messenger: BinaryMessenger) {
  val first = MethodChannel(messenger, "sample/first")
  first.setMethodCallHandler { call, result ->
    when (call.method) { "one" -> result.success(null) }
  }
  val second = MethodChannel(messenger, "sample/second")
  second.setMethodCallHandler { call, result ->
    when (call.method) { "two" -> result.success(null) }
  }
  when (unrelated) { "not-a-channel-method" -> println("ignored") }
}
"""

    dart_endpoints = extract_platform_channel_endpoints("lib/bridge.dart", dart_source)
    native_endpoints = extract_platform_channel_endpoints("android/Bridge.kt", kotlin_source)

    assert [(endpoint.variable, endpoint.channel_name) for endpoint in dart_endpoints] == [
        ("first", "sample/first"),
        ("second", "sample/second"),
    ]
    assert [(endpoint.channel_name, [method.name for method in endpoint.methods]) for endpoint in native_endpoints] == [
        ("sample/first", ["one"]),
        ("sample/second", ["two"]),
    ]
