from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from apex_ray.analyzers.dart.toolchain import resolve_dart_toolchain
from apex_ray.classify import classify_diff, detect_file_kind, detect_language
from apex_ray.cli import app
from apex_ray.config import load_config
from apex_ray.diff import parse_unified_diff
from apex_ray.discovery import discover_project
from apex_ray.llm.cache import (
    REVIEW_PROMPT_VERSION,
    REVIEW_SHALLOW_PROMPT_VERSION,
    VERIFIER_PROMPT_VERSION,
)
from apex_ray.llm.prompts import build_review_prompt, build_shallow_review_prompt, build_verifier_prompt
from apex_ray.models import (
    AnalyzerSymbol,
    ContextPack,
    DartAnalyzerConfig,
    FileKind,
    Finding,
    FindingConfidence,
    FindingSeverity,
    ReviewConfig,
    TargetMode,
)

runner = CliRunner()


def test_dart_version_probe_prefers_the_sdk_version_across_both_streams(tmp_path: Path) -> None:
    dart = tmp_path / "dart"
    dart.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'wrapper notice'\nprintf '%s\\n' 'Dart SDK version: 3.12.2 (stable)' >&2\n",
        encoding="utf-8",
    )
    dart.chmod(0o755)

    resolution = resolve_dart_toolchain(
        tmp_path,
        DartAnalyzerConfig(command=[str(dart)]),
        probe_version=True,
    )

    assert resolution.version == "3.12.2 (stable)"
    assert resolution.error is None


def _dart_pack() -> ContextPack:
    return ContextPack(
        id="lib/profile_screen.dart#_ProfileScreenState:8",
        file="lib/profile_screen.dart",
        changed_lines=[(16, 22)],
        impact_notes=[
            "Flutter state lifecycle metadata is available.",
            "A widget test references this declaration.",
        ],
        symbol=AnalyzerSymbol(
            name="_ProfileScreenState",
            kind="class",
            startLine=8,
            endLine=30,
            exported=False,
            signature="class _ProfileScreenState extends State<ProfileScreen>",
        ),
    )


def test_dart_analyzer_config_is_nested_bounded_and_backwards_compatible(tmp_path: Path) -> None:
    legacy = ReviewConfig.model_validate({"analyzer": {"timeout_seconds": 17}})

    assert legacy.analyzer.timeout_seconds == 17
    assert legacy.analyzer.dart.enabled is True
    assert legacy.analyzer.dart.command == []
    assert legacy.analyzer.dart.flutter == "auto"
    assert legacy.analyzer.dart.plugins is True
    assert legacy.analyzer.dart.max_changed_symbols == 80
    assert legacy.analyzer.dart.max_references_per_symbol == 24
    assert legacy.analyzer.dart.max_callees_per_symbol == 16
    assert legacy.analyzer.dart.max_related_tests_per_file == 12
    assert legacy.analyzer.dart.max_dependency_package_anchors == 16

    config_path = tmp_path / ".apex-ray" / "config.yml"
    config_path.parent.mkdir()
    config_path.write_text(
        "review:\n"
        "  analyzer:\n"
        "    dart:\n"
        "      enabled: false\n"
        "      command: [/opt/flutter/bin/dart]\n"
        "      flutter: enabled\n"
        "      plugins: false\n"
        "      max_changed_symbols: 40\n"
        "      max_references_per_symbol: 10\n"
        "      max_callees_per_symbol: 8\n"
        "      max_related_tests_per_file: 6\n"
        "      max_dependency_package_anchors: 4\n",
        encoding="utf-8",
    )

    loaded, _ = load_config(tmp_path)

    assert loaded.analyzer.dart.model_dump() == {
        "enabled": False,
        "command": ["/opt/flutter/bin/dart"],
        "flutter": "enabled",
        "plugins": False,
        "max_changed_symbols": 40,
        "max_references_per_symbol": 10,
        "max_callees_per_symbol": 8,
        "max_related_tests_per_file": 6,
        "max_dependency_package_anchors": 4,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("flutter", "sometimes"),
        ("max_changed_symbols", 0),
        ("max_references_per_symbol", 0),
        ("max_callees_per_symbol", -1),
        ("max_related_tests_per_file", 0),
        ("max_dependency_package_anchors", 0),
        ("command", "dart --enable-experiment"),
        ("command", ["dart", ""]),
        ("command", ["dart\x00wrapper"]),
    ],
)
def test_dart_analyzer_config_rejects_invalid_limits_and_mode(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ReviewConfig.model_validate({"analyzer": {"dart": {field: value}}})


def test_discovery_detects_nested_dart_pub_and_flutter_without_following_manifest_symlinks(
    tmp_path: Path,
) -> None:
    package = tmp_path / "packages" / "mobile_app"
    package.mkdir(parents=True)
    (package / "app.dart").write_text("void main() {}\n", encoding="utf-8")
    (package / "pubspec.yaml").write_text(
        "name: neutral_mobile_app\ndependencies:\n  flutter:\n    sdk: flutter\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside-pubspec.yaml"
    outside.write_text(
        "dependencies:\n  flutter:\n    sdk: flutter\n",
        encoding="utf-8",
    )
    (tmp_path / "pubspec.yaml").symlink_to(outside)

    project = discover_project(tmp_path)

    assert project.detected_languages == ["dart"]
    assert "pub" in project.package_managers
    assert "flutter" in project.framework_hints


def test_discovery_does_not_follow_a_pubspec_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        "dependencies:\n  flutter:\n    sdk: flutter\n",
        encoding="utf-8",
    )
    (tmp_path / "pubspec.yaml").symlink_to(outside)

    project = discover_project(tmp_path)

    assert "flutter" not in project.framework_hints


@pytest.mark.parametrize("manifest", ["pubspec.yaml", "pubspec.yml"])
def test_pubspec_is_a_dependency_manifest(manifest: str) -> None:
    assert detect_file_kind(manifest) == FileKind.DEPENDENCY


@pytest.mark.parametrize(
    "path",
    [
        "lib/model.g.dart",
        "lib/model.freezed.dart",
        "lib/injection.config.dart",
        "test/service.mocks.dart",
        "lib/router.gr.dart",
        "lib/client.chopper.dart",
    ],
)
def test_common_dart_codegen_outputs_are_generated_before_test_classification(path: str) -> None:
    assert detect_file_kind(path) == FileKind.GENERATED


@pytest.mark.parametrize(
    "path",
    [
        "test/profile_test.dart",
        "integration_test/login_flow.dart",
        "packages/core/test/parser_test.dart",
    ],
)
def test_dart_test_layouts_are_classified_as_tests(path: str) -> None:
    assert detect_language(path) == "dart"
    assert detect_file_kind(path) == FileKind.TEST


def test_pubspec_lock_remains_a_lockfile() -> None:
    assert detect_file_kind("pubspec.lock") == FileKind.LOCKFILE


def test_dart_flutter_boundaries_emit_conservative_risk_signals() -> None:
    summary = parse_unified_diff(
        "diff --git a/lib/platform_bridge.dart b/lib/platform_bridge.dart\n"
        "--- a/lib/platform_bridge.dart\n"
        "+++ b/lib/platform_bridge.dart\n"
        "@@ -1 +1,5 @@\n"
        "-void connect() {}\n"
        "+final channel = MethodChannel('neutral.example/session');\n"
        "+await channel.invokeMethod('refresh');\n"
        "+if (context.mounted) context.go('/profile');\n"
        "+final prefs = await SharedPreferences.getInstance();\n"
        "+return Profile.fromJson(payload);\n",
        target_mode=TargetMode.PATCH,
    )

    classified = classify_diff(summary, ignore_patterns=[])

    assert classified.files[0].language == "dart"
    assert {signal.kind for signal in classified.files[0].risk_signals} >= {
        "navigation",
        "platform_channel",
        "persistence",
        "serialization",
        "state_lifecycle",
    }


def test_dart_risk_signals_preserve_generic_boundary_keywords() -> None:
    summary = parse_unified_diff(
        "diff --git a/lib/repository.dart b/lib/repository.dart\n"
        "--- a/lib/repository.dart\n"
        "+++ b/lib/repository.dart\n"
        "@@ -1 +1 @@\n"
        "-Future<void> save() async {}\n"
        "+await database.transaction((tx) async => tx.commit());\n",
        target_mode=TargetMode.PATCH,
    )

    classified = classify_diff(summary, ignore_patterns=[])

    assert classified.files[0].language == "dart"
    assert any(signal.kind == "persistence" for signal in classified.files[0].risk_signals)


def test_dart_prompts_cover_flutter_failure_modes_without_republishing_diagnostics() -> None:
    pack = _dart_pack()
    finding = Finding(
        title="Controller survives widget disposal",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        line=20,
        failure_mode="A listener can update disposed state.",
        evidence="The changed lifecycle path has no corresponding cleanup evidence.",
        suggested_fix="Dispose the owned controller and subscription.",
        suggested_test="Unmount the widget while the stream remains active.",
        context_pack_id=pack.id,
    )

    deep = build_review_prompt(pack)
    shallow = build_shallow_review_prompt(pack)
    verifier = build_verifier_prompt(finding, pack)

    assert "Language hint: Dart/Flutter." in deep
    assert "async BuildContext" in deep
    assert "BLoC/Cubit" in deep
    assert "GoRouter/Navigator" in deep
    assert "MethodChannel/EventChannel/BasicMessageChannel" in deep
    assert "accessibility, responsiveness, rebuild scope" in deep
    assert "diagnostics as context, not review findings" in deep
    assert "dart analyze" in deep and "flutter_lints" in deep

    assert "Language hint: Dart/Flutter." in shallow
    assert "lifecycle/cleanup" in shallow
    assert "async BuildContext" in shallow
    assert "diagnostics as context, not findings" in shallow

    assert "Language hint: Dart/Flutter." in verifier
    assert "concrete supplied Dart/Flutter evidence" in verifier
    assert "diagnostics as context, not findings" in verifier


def test_dart_prompt_changes_bump_all_cache_versions() -> None:
    assert REVIEW_PROMPT_VERSION == "review-v10"
    assert REVIEW_SHALLOW_PROMPT_VERSION == "review-shallow-v3"
    assert VERIFIER_PROMPT_VERSION == "verify-v10"


def test_doctor_reports_resolved_dart_toolchain_without_eager_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".apex-ray").mkdir()
    (tmp_path / ".apex-ray" / "config.yml").write_text(
        "review:\n  analyzer:\n    dart:\n      command: ['/SDK With Space/bin/dart']\n",
        encoding="utf-8",
    )
    seen: list[tuple[Path, list[str]]] = []

    def resolve(repo_root: Path, config: DartAnalyzerConfig) -> SimpleNamespace:
        seen.append((repo_root, config.command))
        return SimpleNamespace(
            command=["/SDK With Space/bin/dart"],
            source="configured",
            version="Dart SDK version: 3.11.6 (stable)",
            error=None,
            remediation=None,
        )

    monkeypatch.setattr("apex_ray.cli.main._resolve_dart_toolchain_for_doctor", resolve)

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 0
    assert seen == [(tmp_path.resolve(), ["/SDK With Space/bin/dart"])]
    assert "- Dart analyzer enabled: true" in result.stdout
    assert '- Dart SDK command: ["/SDK With Space/bin/dart"]' in result.stdout
    assert "- Dart SDK source: configured" in result.stdout
    assert "- Dart SDK version: 3.11.6 (stable)" in result.stdout
    assert "- Dart analyzer available: true" in result.stdout


def test_doctor_reports_incompatible_plugin_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    dart = tmp_path / "sdk" / "bin" / "dart"
    dart.parent.mkdir(parents=True)
    dart.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf '%s\\n' 'Dart SDK version: 3.10.1 (stable)' >&2\n"
        "  exit 0\n"
        "fi\n"
        "exit 64\n",
        encoding="utf-8",
    )
    dart.chmod(0o755)
    (tmp_path / ".apex-ray").mkdir()
    (tmp_path / ".apex-ray" / "config.yml").write_text(
        f"review:\n  analyzer:\n    dart:\n      command: [{dart}]\n      plugins: false\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "- Dart SDK version: 3.10.1 (stable)" in result.stdout
    assert "- Dart analyzer available: false" in result.stdout
    assert "cannot disable analyzer plugins" in result.stdout
    assert "Upgrade the selected Dart or Flutter SDK" in result.stdout


def test_doctor_reports_actionable_missing_dart_sdk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apex_ray.cli.main._resolve_dart_toolchain_for_doctor",
        lambda _root, _config: SimpleNamespace(
            command=[],
            source="unavailable",
            version=None,
            error="No Dart SDK command could be resolved.",
            remediation="Install Flutter or Dart, configure FVM, or set review.analyzer.dart.command.",
        ),
    )

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "- Dart SDK command: not found" in result.stdout
    assert "- Dart SDK source: unavailable" in result.stdout
    assert "- Dart SDK version: unavailable" in result.stdout
    assert "- Dart analyzer available: false" in result.stdout
    assert "- Dart SDK error: No Dart SDK command could be resolved." in result.stdout
    assert "- Dart remediation: Install Flutter or Dart" in result.stdout


def test_doctor_skips_sdk_resolution_when_dart_analyzer_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".apex-ray").mkdir()
    (tmp_path / ".apex-ray" / "config.yml").write_text(
        "review:\n  analyzer:\n    dart:\n      enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "apex_ray.cli.main._resolve_dart_toolchain_for_doctor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected Dart SDK probe")),
    )

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "- Dart analyzer enabled: false" in result.stdout
    assert "- Dart SDK command: skipped (analyzer disabled)" in result.stdout
    assert "- Dart SDK source: disabled" in result.stdout
    assert "- Dart SDK version: skipped" in result.stdout
    assert "- Dart analyzer available: false" in result.stdout
    assert "Dart remediation" not in result.stdout


def test_doctor_does_not_mark_a_failed_dart_probe_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apex_ray.cli.main._resolve_dart_toolchain_for_doctor",
        lambda _root, _config: SimpleNamespace(
            command=["/missing/flutter/bin/dart"],
            source="configured",
            version=None,
            error="Unable to run configured Dart SDK command '/missing/flutter/bin/dart'.",
            remediation="Fix review.analyzer.dart.command.",
        ),
    )

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 0
    assert '- Dart SDK command: ["/missing/flutter/bin/dart"]' in result.stdout
    assert "- Dart analyzer available: false" in result.stdout
    assert "- Dart SDK error: Unable to run configured Dart SDK command" in result.stdout
    assert "- Dart remediation: Fix review.analyzer.dart.command." in result.stdout
