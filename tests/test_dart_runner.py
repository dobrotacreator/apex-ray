import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from apex_ray.analyzers import AnalyzerError, dart_changed_files, run_analyzers, run_dart_analyzer
from apex_ray.analyzers.dart import mapping as dart_mapping_module
from apex_ray.analyzers.dart import runner as dart_runner_module
from apex_ray.analyzers.dart.lsp import DartLspResponseError, DartLspTimeout
from apex_ray.analyzers.dart.runner import _read_changed_dart_source
from apex_ray.analyzers.dart.toolchain import DartToolchainResolution
from apex_ray.context import build_context_packs
from apex_ray.models import (
    AnalyzerConfig,
    AnalyzerFile,
    AnalyzerReference,
    AnalyzerResult,
    AnalyzerSymbol,
    ChangedFile,
    ChangedHunk,
    DartAnalyzerConfig,
    DiffLine,
    DiffLineKind,
    FileKind,
    ReviewConfig,
    RiskSeverity,
    RiskSignal,
)


def _dart_file(
    path: str,
    *,
    kind: FileKind = FileKind.SOURCE,
    ignored: bool = False,
    new_path: str | None = None,
) -> ChangedFile:
    return ChangedFile(
        old_path=path,
        new_path=path if new_path is None else new_path,
        language="dart",
        file_kind=kind,
        is_ignored=ignored,
    )


def test_dart_changed_files_selects_reviewable_handwritten_files() -> None:
    source = _dart_file("lib/resource.dart")
    test = _dart_file("test/resource_test.dart", kind=FileKind.TEST)

    assert dart_changed_files(
        [
            source,
            test,
            _dart_file("lib/resource.g.dart", kind=FileKind.GENERATED),
            _dart_file("lib/ignored.dart", ignored=True),
            ChangedFile(
                old_path="lib/removed.dart",
                new_path=None,
                language="dart",
                file_kind=FileKind.SOURCE,
            ),
            ChangedFile(
                old_path="lib/not_dart.py",
                new_path="lib/not_dart.py",
                language="python",
                file_kind=FileKind.SOURCE,
            ),
        ]
    ) == [source, test]


def test_read_changed_dart_source_rejects_symlink_before_resolving_it(tmp_path: Path) -> None:
    target = tmp_path / "lib" / "target.dart"
    link = tmp_path / "lib" / "linked.dart"
    target.parent.mkdir()
    target.write_text("void target() {}\n", encoding="utf-8")
    try:
        link.symlink_to(target.name)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink"):
        _read_changed_dart_source(tmp_path.resolve(), "lib/linked.dart")


def test_generated_dart_never_gets_a_fallback_prompt_even_with_risk_signals(tmp_path: Path) -> None:
    generated = _dart_file("lib/resource.g.dart", kind=FileKind.GENERATED)
    generated.risk_signals = [
        RiskSignal(
            kind="platform_channel",
            severity=RiskSeverity.HIGH,
            reason="Generated channel binding changed.",
            file=generated.path,
        )
    ]

    assert build_context_packs([], [generated], ReviewConfig(), repo_root=tmp_path) == []


def test_dart_generated_suppression_does_not_change_other_language_risk_fallbacks(tmp_path: Path) -> None:
    generated = ChangedFile(
        old_path="src/generated/client.ts",
        new_path="src/generated/client.ts",
        language="typescript",
        file_kind=FileKind.GENERATED,
        risk_signals=[
            RiskSignal(
                kind="auth",
                severity=RiskSeverity.HIGH,
                reason="Generated client authentication contract changed.",
                file="src/generated/client.ts",
            )
        ],
    )

    packs = build_context_packs([], [generated], ReviewConfig(), repo_root=tmp_path)

    assert [pack.id for pack in packs] == ["src/generated/client.ts#diff"]


def test_repository_reference_mapping_uses_one_parse_pass_and_bounded_source_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    generated = tmp_path / "lib" / "resource.g.dart"
    source.parent.mkdir()
    source.write_text("void resource() {}\n", encoding="utf-8")
    generated.write_text("void generatedResource() {}\n", encoding="utf-8")
    payload = [{"uri": source.as_uri(), "range": _lsp_range(line, line)} for line in reversed(range(5_000))]
    payload.extend({"uri": generated.as_uri(), "range": _lsp_range(0, 0)} for _ in range(3))
    parse_calls = 0
    source_reads = 0
    original_candidate_parser = dart_mapping_module._reference_candidate_from_lsp_location
    original_bounded_reader = dart_mapping_module._read_bounded_repo_text

    def counted_candidate_parser(*args: object, **kwargs: Any) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return original_candidate_parser(*args, **kwargs)

    def counted_source_read(*args: object, **kwargs: Any) -> str | None:
        nonlocal source_reads
        source_reads += 1
        return original_bounded_reader(*args, **kwargs)

    monkeypatch.setattr(
        dart_mapping_module,
        "_reference_candidate_from_lsp_location",
        counted_candidate_parser,
    )
    monkeypatch.setattr(dart_mapping_module, "_read_bounded_repo_text", counted_source_read)
    reference_reader = dart_mapping_module.DartReferenceSourceReader(tmp_path)

    references, suppressed_generated = dart_runner_module._repository_references(
        tmp_path,
        payload,
        kind="read",
        limit=24,
        source_reader=reference_reader,
    )

    assert [reference.line for reference in references] == list(range(1, 25))
    assert parse_calls == len(payload)
    assert source_reads == 1
    assert reference_reader.files_read == 1
    assert suppressed_generated == 3


def test_run_analyzers_registers_dart_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = _dart_file("lib/resource.dart")
    result = AnalyzerResult(
        language="dart",
        projectRoot=str(tmp_path),
        files=[AnalyzerFile(path=changed.path)],
    )
    monkeypatch.setattr("apex_ray.analyzers.run_dart_analyzer", lambda *_args, **_kwargs: result)

    run = run_analyzers(tmp_path, [changed])

    assert run.results == [result]
    dart_run = next(backend for backend in run.backend_runs if backend.name == "dart")
    assert dart_run.display_name == "Dart"
    assert dart_run.changed_files_count == 1
    assert dart_run.result is result


def test_run_analyzers_scopes_dart_partial_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = _dart_file("lib/resource.dart")
    result = AnalyzerResult(
        language="dart",
        projectRoot=str(tmp_path),
        files=[],
        warnings=["partial"],
        partial=True,
        failedFiles=[changed.path],
    )
    monkeypatch.setattr("apex_ray.analyzers.run_dart_analyzer", lambda *_args, **_kwargs: result)

    run = run_analyzers(tmp_path, [changed])

    assert run.fallback_reasons_by_path == {
        changed.path: "Dart analyzer failed for this file; using diff-only fallback context."
    }


class _FakeDartLspClient:
    instances: ClassVar[list[_FakeDartLspClient]] = []
    notifications_dropped: ClassVar[int] = 0

    def __init__(self, command: list[str], cwd: Path, **kwargs: object) -> None:
        self.command = command
        self.cwd = cwd
        self.kwargs = kwargs
        self.server_capabilities: dict[str, object] = {}
        self.opened: list[str] = []
        self.closed = False
        self.source_uri = ""
        self.test_uri = ""
        self.generated_uri = ""
        self.service_uri = ""
        self.contract_uri = ""
        self.ignored_uri = ""
        self.notification_calls: dict[tuple[str | None, str | None], int] = {}
        self.dropped_notifications = self.__class__.notifications_dropped
        self.__class__.instances.append(self)

    def __enter__(self) -> _FakeDartLspClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def initialize(self, root_uri: str, flutter_outline: bool = False, **_kwargs: object) -> dict[str, object]:
        assert root_uri.startswith("file:")
        assert flutter_outline is True
        self.server_capabilities = {
            "documentSymbolProvider": True,
            "callHierarchyProvider": True,
            "typeHierarchyProvider": True,
            "experimental": {"workspaceAnalysisComplete": True},
        }
        return {"capabilities": self.server_capabilities, "serverInfo": {"version": "3.test"}}

    def did_open(self, uri: str, text: str, **_kwargs: object) -> None:
        assert "ResourceScreen" in text
        self.opened.append(uri)
        self.source_uri = uri

    def request(self, method: str, params: object = None, **_kwargs: object) -> object:
        if method == "dart/workspace/analysis/complete":
            return None
        if method == "textDocument/documentSymbol":
            return [
                {
                    "name": "_ResourceScreenState",
                    "detail": "class _ResourceScreenState extends State<ResourceScreen>",
                    "kind": 5,
                    "range": _lsp_range(9, 16, end_character=0),
                    "selectionRange": _lsp_range(9, 9),
                    "children": [
                        {
                            "name": "build",
                            "detail": "Widget build(BuildContext context)",
                            "kind": 6,
                            "range": _lsp_range(11, 15, end_character=0),
                            "selectionRange": _lsp_range(11, 11),
                        }
                    ],
                }
            ]
        if method == "textDocument/references":
            return [
                {"uri": self.test_uri, "range": _lsp_range(3, 3)},
                {"uri": self.generated_uri, "range": _lsp_range(0, 0)},
                *([{"uri": self.ignored_uri, "range": _lsp_range(0, 0)}] if self.ignored_uri else []),
            ]
        if method == "textDocument/prepareCallHierarchy":
            return [_hierarchy_item("build", self.source_uri, 11)]
        if method == "callHierarchy/incomingCalls":
            return [
                {"from": _hierarchy_item("testWidgets", self.test_uri, 3), "fromRanges": []},
                *(
                    [{"from": _hierarchy_item("ignoredConsumer", self.ignored_uri, 0), "fromRanges": []}]
                    if self.ignored_uri
                    else []
                ),
            ]
        if method == "callHierarchy/outgoingCalls":
            return [
                {"to": _hierarchy_item("load", self.service_uri, 0), "fromRanges": []},
                *(
                    [{"to": _hierarchy_item("ignoredCall", self.ignored_uri, 0), "fromRanges": []}]
                    if self.ignored_uri
                    else []
                ),
            ]
        if method == "textDocument/prepareTypeHierarchy":
            return [_hierarchy_item("_ResourceScreenState", self.source_uri, 9)]
        if method == "typeHierarchy/supertypes":
            return [
                _hierarchy_item("ResourceContract", self.contract_uri, 0),
                *([_hierarchy_item("IgnoredContract", self.ignored_uri, 0)] if self.ignored_uri else []),
            ]
        if method == "typeHierarchy/subtypes":
            return []
        raise AssertionError(f"unexpected request: {method} {params!r}")

    def notifications(self, method: str | None = None, uri: str | None = None) -> list[dict[str, object]]:
        key = (method, uri)
        self.notification_calls[key] = self.notification_calls.get(key, 0) + 1
        if method == "dart/textDocument/publishFlutterOutline" and uri == self.source_uri:
            return [
                {
                    "method": method,
                    "params": {
                        "uri": uri,
                        "outline": {
                            "kind": "NEW_INSTANCE",
                            "className": "Text",
                            "range": _lsp_range(13, 13),
                            "codeRange": _lsp_range(13, 13),
                            "children": [],
                        },
                    },
                }
            ]
        if method == "textDocument/publishDiagnostics" and uri == self.source_uri:
            return []
        return []


def _lsp_range(start_line: int, end_line: int, *, end_character: int = 1) -> dict[str, object]:
    return {
        "start": {"line": start_line, "character": 0},
        "end": {"line": end_line, "character": end_character},
    }


def _hierarchy_item(name: str, uri: str, line: int) -> dict[str, object]:
    return {
        "name": name,
        "kind": 6,
        "uri": uri,
        "range": _lsp_range(line, line),
        "selectionRange": _lsp_range(line, line),
    }


def test_dart_analyzer_maps_lsp_semantics_and_filters_generated_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource_screen.dart"
    service = tmp_path / "lib" / "resource_service.dart"
    contract = tmp_path / "lib" / "resource_contract.dart"
    generated = tmp_path / "lib" / "resource_screen.g.dart"
    ignored = tmp_path / "private" / "credentials.dart"
    test = tmp_path / "test" / "resource_screen_test.dart"
    native = tmp_path / "android" / "app" / "src" / "main" / "kotlin" / "sample" / "Bridge.kt"
    source.parent.mkdir()
    test.parent.mkdir()
    ignored.parent.mkdir()
    native.parent.mkdir(parents=True)
    source.write_text(
        "import 'package:flutter/services.dart';\n"
        "import 'resource_service.dart';\n\n"
        "class ResourceScreen extends StatefulWidget {\n"
        "  const ResourceScreen({super.key});\n"
        "  @override\n"
        "  State<ResourceScreen> createState() => _ResourceScreenState();\n"
        "}\n\n"
        "class _ResourceScreenState extends State<ResourceScreen> {\n"
        "  @override\n"
        "  Widget build(BuildContext context) {\n"
        "    final channel = MethodChannel('sample/resource');\n"
        "    return Text(channel.invokeMethod<String>('load').toString());\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    service.write_text("String load() => 'resource';\n", encoding="utf-8")
    contract.write_text("abstract interface class ResourceContract {}\n", encoding="utf-8")
    generated.write_text("void generatedConsumer() {}\n", encoding="utf-8")
    ignored.write_text("const syntheticCredential = 'DO_NOT_SEND';\n", encoding="utf-8")
    test.write_text(
        "import 'package:flutter_test/flutter_test.dart';\n"
        "import '../lib/resource_screen.dart';\n\n"
        "void main() => testWidgets('resource', (tester) async {});\n",
        encoding="utf-8",
    )
    native.write_text(
        "fun configure(messenger: BinaryMessenger) {\n"
        '  val channel = MethodChannel(messenger, "sample/resource")\n'
        "  channel.setMethodCallHandler { call, result ->\n"
        '    when (call.method) { "load" -> result.success("resource") }\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="lib/resource_screen.dart",
        new_path="lib/resource_screen.dart",
        language="dart",
        file_kind=FileKind.SOURCE,
        hunks=[
            ChangedHunk(
                old_start=14,
                old_lines=1,
                new_start=14,
                new_lines=1,
                lines=[
                    DiffLine(
                        kind=DiffLineKind.ADD,
                        content="return Text(channel.invokeMethod<String>('load').toString());",
                        new_line=14,
                    )
                ],
            )
        ],
    )
    fake_class = _FakeDartLspClient
    fake_class.instances.clear()
    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["/sdk/bin/dart"], source="configured"),
    )
    original_init = fake_class.__init__

    def configured_init(self: _FakeDartLspClient, command: list[str], cwd: Path, **kwargs: Any) -> None:
        original_init(self, command, cwd, **kwargs)
        self.test_uri = test.as_uri()
        self.generated_uri = generated.as_uri()
        self.service_uri = service.as_uri()
        self.contract_uri = contract.as_uri()
        self.ignored_uri = ignored.as_uri()

    monkeypatch.setattr(fake_class, "__init__", configured_init)
    platform_channel_deadlines: list[float | None] = []
    framework_metadata_builds: list[str] = []
    reference_readers: list[dart_mapping_module.DartReferenceSourceReader] = []
    original_platform_channel_index = dart_runner_module.build_platform_channel_index
    original_framework_metadata_index = dart_runner_module.build_dart_framework_metadata_index

    def capture_platform_channel_deadline(*args: object, **kwargs: Any) -> object:
        platform_channel_deadlines.append(kwargs.get("deadline"))
        return original_platform_channel_index(*args, **kwargs)

    def capture_framework_metadata_build(path: str, source_text: str, **kwargs: Any) -> object:
        framework_metadata_builds.append(path)
        return original_framework_metadata_index(path, source_text, **kwargs)

    def capture_reference_reader(
        repo_root: Path,
        *,
        deadline: float | None = None,
    ) -> dart_mapping_module.DartReferenceSourceReader:
        reader = dart_mapping_module.DartReferenceSourceReader(repo_root, deadline=deadline)
        reference_readers.append(reader)
        return reader

    monkeypatch.setattr(dart_runner_module, "build_platform_channel_index", capture_platform_channel_deadline)
    monkeypatch.setattr(dart_runner_module, "build_dart_framework_metadata_index", capture_framework_metadata_build)
    monkeypatch.setattr(dart_runner_module, "DartReferenceSourceReader", capture_reference_reader)

    result = run_dart_analyzer(
        tmp_path,
        [changed],
        AnalyzerConfig(
            index_cache_enabled=False,
            dart=DartAnalyzerConfig(plugins=False),
        ),
        project_files=[
            Path("lib/resource_screen.dart"),
            Path("lib/resource_service.dart"),
            Path("lib/resource_contract.dart"),
            Path("lib/resource_screen.g.dart"),
            Path("test/resource_screen_test.dart"),
            Path("android/app/src/main/kotlin/sample/Bridge.kt"),
        ],
    )

    assert result is not None
    assert len(platform_channel_deadlines) == 1
    assert isinstance(platform_channel_deadlines[0], float)
    assert framework_metadata_builds == ["lib/resource_screen.dart"]
    assert len(reference_readers) == 1
    assert reference_readers[0].files_read > 0
    assert result.language == "dart"
    assert result.partial is False
    assert len(result.files) == 1
    analyzed = result.files[0]
    assert analyzed.imports == ["package:flutter/services.dart", "resource_service.dart"]
    assert [symbol.name for symbol in analyzed.changed_symbols] == [
        "_ResourceScreenState",
        "_ResourceScreenState.build",
    ]
    assert all(
        reference.file != "lib/resource_screen.g.dart"
        for symbol in analyzed.changed_symbols
        for reference in symbol.references
    )
    assert any(
        reference.file == "test/resource_screen_test.dart"
        for symbol in analyzed.changed_symbols
        for reference in symbol.references
    )
    assert any(
        callee.file == "lib/resource_service.dart" for symbol in analyzed.changed_symbols for callee in symbol.callees
    )
    assert any(
        item.file == "lib/resource_contract.dart" for symbol in analyzed.changed_symbols for item in symbol.contracts
    )
    assert any(
        item.file == "android/app/src/main/kotlin/sample/Bridge.kt"
        for symbol in analyzed.changed_symbols
        for item in symbol.contracts
    )
    assert all(
        any(item.text == "generated references suppressed from prompt context: 1" for item in symbol.metadata)
        for symbol in analyzed.changed_symbols
    )
    assert analyzed.related_tests == ["test/resource_screen_test.dart"]
    assert any("Flutter widget outline" in item.text for symbol in analyzed.changed_symbols for item in symbol.metadata)
    client = fake_class.instances[0]
    assert client.opened == [source.as_uri()]
    assert client.command[-6:] == [
        "language-server",
        "--client-id",
        "apex-ray",
        "--client-version",
        "0.1.11",
        "--no-plugins",
    ]
    assert client.closed is True

    packs = build_context_packs([result], [changed], ReviewConfig(), repo_root=tmp_path)
    assert packs
    serialized_packs = "\n".join(pack.model_dump_json() for pack in packs)
    assert "resource_screen.g.dart" not in serialized_packs
    serialized_result = result.model_dump_json()
    assert "private/credentials.dart" not in serialized_result
    assert "DO_NOT_SEND" not in serialized_result
    assert "private/credentials.dart" not in serialized_packs
    assert "DO_NOT_SEND" not in serialized_packs
    assert any(
        snippet.file == "android/app/src/main/kotlin/sample/Bridge.kt"
        for pack in packs
        for snippet in pack.contract_snippets
    )


def test_dart_analyzer_missing_sdk_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("int resource() => 1;\n", encoding="utf-8")
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(
            command=[],
            source="unavailable",
            error="No Dart SDK command could be resolved for this project.",
            remediation="Install Flutter or configure review.analyzer.dart.command.",
        ),
    )

    with pytest.raises(AnalyzerError, match=r"No Dart SDK.*Install Flutter"):
        run_dart_analyzer(tmp_path, [_dart_file("lib/resource.dart")])


def test_dart_analyzer_marks_reference_source_truncation_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource_screen.dart"
    referenced = tmp_path / "lib" / "referenced.dart"
    generated = tmp_path / "lib" / "referenced.g.dart"
    source.parent.mkdir()
    source.write_text(
        "import 'package:flutter/widgets.dart';\n\n"
        "class ResourceScreen extends StatefulWidget {\n"
        "  const ResourceScreen({super.key});\n"
        "  @override\n"
        "  State<ResourceScreen> createState() => _ResourceScreenState();\n"
        "}\n\n"
        "class _ResourceScreenState extends State<ResourceScreen> {\n"
        "  @override\n"
        "  Widget build(BuildContext context) {\n"
        "    return const SizedBox();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    referenced.write_text("x" * 65, encoding="utf-8")
    generated.write_text("void generated() {}\n", encoding="utf-8")
    changed = ChangedFile(
        old_path="lib/resource_screen.dart",
        new_path="lib/resource_screen.dart",
        language="dart",
        file_kind=FileKind.SOURCE,
        hunks=[
            ChangedHunk(
                old_start=12,
                old_lines=1,
                new_start=12,
                new_lines=1,
                lines=[DiffLine(kind=DiffLineKind.ADD, content="return const SizedBox();", new_line=12)],
            )
        ],
    )
    fake_class = _FakeDartLspClient
    fake_class.instances.clear()
    original_init = fake_class.__init__

    def configured_init(self: _FakeDartLspClient, command: list[str], cwd: Path, **kwargs: Any) -> None:
        original_init(self, command, cwd, **kwargs)
        self.test_uri = referenced.as_uri()
        self.service_uri = referenced.as_uri()
        self.contract_uri = referenced.as_uri()
        self.generated_uri = generated.as_uri()

    def bounded_reference_reader(
        repo_root: Path,
        *,
        deadline: float | None = None,
    ) -> dart_mapping_module.DartReferenceSourceReader:
        return dart_mapping_module.DartReferenceSourceReader(
            repo_root,
            max_file_bytes=64,
            max_total_source_bytes=128,
            deadline=deadline,
        )

    monkeypatch.setattr(fake_class, "__init__", configured_init)
    monkeypatch.setattr(dart_runner_module, "DartLspClient", fake_class)
    monkeypatch.setattr(dart_runner_module, "DartReferenceSourceReader", bounded_reference_reader)
    monkeypatch.setattr(
        dart_runner_module,
        "resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )

    result = run_dart_analyzer(
        tmp_path,
        [changed],
        AnalyzerConfig(index_cache_enabled=False),
        project_files=[
            Path("lib/resource_screen.dart"),
            Path("lib/referenced.dart"),
            Path("lib/referenced.g.dart"),
        ],
    )

    assert result is not None
    assert result.partial is True
    assert result.failed_files == []
    assert any("reference-source safety budget" in warning for warning in result.warnings)


def test_disabled_dart_analyzer_does_not_resolve_sdk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected SDK resolution")),
        raising=False,
    )

    assert (
        run_dart_analyzer(
            tmp_path,
            [_dart_file("lib/resource.dart")],
            AnalyzerConfig(dart={"enabled": False}),
        )
        is None
    )


def test_dart_analyzer_keeps_successful_files_when_one_lsp_document_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = (
        "import 'package:flutter/widgets.dart';\n\n"
        "class ResourceScreen extends StatefulWidget {\n"
        "  @override\n"
        "  State<ResourceScreen> createState() => _ResourceScreenState();\n"
        "}\n\n"
        "class _ResourceScreenState extends State<ResourceScreen> {\n"
        "  @override\n"
        "  Widget build(BuildContext context) => const SizedBox();\n"
        "}\n"
    )
    good = tmp_path / "lib" / "good.dart"
    broken = tmp_path / "lib" / "broken.dart"
    good.parent.mkdir()
    good.write_text(source_text, encoding="utf-8")
    broken.write_text(source_text, encoding="utf-8")

    def changed(path: str) -> ChangedFile:
        return ChangedFile(
            old_path=path,
            new_path=path,
            language="dart",
            file_kind=FileKind.SOURCE,
            hunks=[
                ChangedHunk(
                    old_start=10,
                    old_lines=1,
                    new_start=10,
                    new_lines=1,
                    lines=[DiffLine(kind=DiffLineKind.ADD, content="build changed", new_line=10)],
                )
            ],
        )

    fake_class = _FakeDartLspClient
    fake_class.instances.clear()
    original_request = fake_class.request
    activation_attempts = 0

    def partial_request(
        self: _FakeDartLspClient,
        method: str,
        params: object = None,
        **kwargs: object,
    ) -> object:
        nonlocal activation_attempts
        if method == "textDocument/documentSymbol" and isinstance(params, dict):
            document = params.get("textDocument")
            if isinstance(document, dict) and "broken.dart" in str(document.get("uri")):
                raise DartLspResponseError(-32603, "synthetic document failure")
            if isinstance(document, dict) and "good.dart" in str(document.get("uri")):
                activation_attempts += 1
                if activation_attempts == 1:
                    raise DartLspResponseError(-32007, "File is not being analyzed")
        return original_request(self, method, params, **kwargs)

    monkeypatch.setattr(fake_class, "request", partial_request)
    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )

    result = run_dart_analyzer(
        tmp_path,
        [changed("lib/good.dart"), changed("lib/broken.dart")],
        AnalyzerConfig(index_cache_enabled=False),
        project_files=[Path("lib/good.dart"), Path("lib/broken.dart")],
    )

    assert result is not None
    assert result.partial is True
    assert [file.path for file in result.files] == ["lib/good.dart"]
    assert result.failed_files == ["lib/broken.dart"]
    assert activation_attempts == 2
    assert any("synthetic document failure" in warning for warning in result.warnings)


def test_dart_analyzer_keeps_successful_files_when_final_metadata_sync_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("class ResourceScreen {}\n", encoding="utf-8")
    fake_class = _FakeDartLspClient
    fake_class.instances.clear()
    sync_calls = 0

    def final_sync_timeout(*_args: object, **_kwargs: object) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise DartLspTimeout("synthetic final metadata timeout")

    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )
    monkeypatch.setattr("apex_ray.analyzers.dart.runner._flutter_outline_enabled", lambda *_args: True)
    monkeypatch.setattr("apex_ray.analyzers.dart.runner._wait_for_workspace_analysis", final_sync_timeout)

    result = run_dart_analyzer(
        tmp_path,
        [_dart_file("lib/resource.dart")],
        AnalyzerConfig(index_cache_enabled=False),
        project_files=[Path("lib/resource.dart")],
    )

    assert result is not None
    assert [file.path for file in result.files] == ["lib/resource.dart"]
    assert result.partial is True
    assert result.failed_files == []
    assert any("final metadata" in warning for warning in result.warnings)


def test_dart_analyzer_marks_swallowed_workspace_sync_timeout_non_cacheable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("class ResourceScreen {}\n", encoding="utf-8")

    class WorkspaceSyncTimeoutClient(_FakeDartLspClient):
        def request(self, method: str, params: object = None, **kwargs: object) -> object:
            if method == "dart/workspace/analysis/complete":
                raise DartLspTimeout("synthetic workspace synchronization timeout")
            return super().request(method, params, **kwargs)

    fake_class = WorkspaceSyncTimeoutClient
    fake_class.instances.clear()
    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )
    monkeypatch.setattr("apex_ray.analyzers.dart.runner._flutter_outline_enabled", lambda *_args: True)

    result = run_dart_analyzer(
        tmp_path,
        [_dart_file("lib/resource.dart")],
        AnalyzerConfig(index_cache_dir=str(tmp_path / "cache")),
        project_files=[Path("lib/resource.dart")],
    )

    assert result is not None
    assert [file.path for file in result.files] == ["lib/resource.dart"]
    assert result.partial is True
    assert result.index_cache is None
    assert not (tmp_path / "cache").exists()
    assert any("workspace/analysis/complete timed out" in warning for warning in result.warnings)


def test_dart_analyzer_reports_dropped_lsp_notifications_as_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("class ResourceScreen {}\n", encoding="utf-8")
    fake_class = _FakeDartLspClient
    fake_class.instances.clear()
    monkeypatch.setattr(fake_class, "notifications_dropped", 7)
    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )
    monkeypatch.setattr("apex_ray.analyzers.dart.runner._flutter_outline_enabled", lambda *_args: True)

    result = run_dart_analyzer(
        tmp_path,
        [_dart_file("lib/resource.dart")],
        AnalyzerConfig(index_cache_enabled=False),
        project_files=[Path("lib/resource.dart")],
    )

    assert result is not None
    assert result.partial is True
    assert any("dropped 7 LSP notification" in warning for warning in result.warnings)


def test_dart_diagnostics_use_latest_publish_state() -> None:
    uri = "file:///repo/lib/resource.dart"

    class DiagnosticClient:
        def notifications(self, method: str | None = None, uri: str | None = None) -> list[dict[str, object]]:
            assert method == "textDocument/publishDiagnostics"
            return [
                {
                    "method": method,
                    "params": {
                        "uri": uri,
                        "diagnostics": [
                            {
                                "severity": 1,
                                "range": _lsp_range(2, 2),
                                "message": "stale diagnostic",
                            }
                        ],
                    },
                },
                {"method": method, "params": {"uri": uri, "diagnostics": []}},
            ]

    metadata = dart_runner_module._diagnostic_metadata(
        DiagnosticClient(),
        "lib/resource.dart",
        uri,
        AnalyzerSymbol(name="resource", kind="function", startLine=1, endLine=10),
    )

    assert metadata == []


def test_final_notification_reconciliation_replaces_stale_metadata() -> None:
    uri = "file:///repo/lib/resource.dart"
    notification_calls: dict[str | None, int] = {}

    class ClearedNotificationClient:
        def notifications(self, method: str | None = None, uri: str | None = None) -> list[dict[str, object]]:
            notification_calls[method] = notification_calls.get(method, 0) + 1
            if method == "textDocument/publishDiagnostics":
                return [{"method": method, "params": {"uri": uri, "diagnostics": []}}]
            if method == "dart/textDocument/publishFlutterOutline":
                return [{"method": method, "params": {"uri": uri, "outline": None}}]
            return []

    stale_symbol = AnalyzerSymbol(
        name="resource",
        kind="function",
        startLine=1,
        endLine=10,
        metadata=[
            AnalyzerReference(
                file="lib/resource.dart",
                line=2,
                endLine=2,
                text="Dart analyzer diagnostic (error): stale",
                kind="metadata",
            ),
            AnalyzerReference(
                file="lib/resource.dart",
                line=3,
                endLine=3,
                text="Flutter widget outline: stale",
                kind="metadata",
            ),
            AnalyzerReference(
                file="lib/resource.dart",
                line=4,
                endLine=4,
                text="lifecycle method: dispose",
                kind="metadata",
            ),
        ],
    )
    analyzed = AnalyzerFile(
        path="lib/resource.dart",
        symbols=[stale_symbol, stale_symbol.model_copy(update={"name": "resource.second"})],
        changedSymbols=[stale_symbol, stale_symbol.model_copy(update={"name": "resource.second"})],
    )

    reconciled = dart_runner_module._reconcile_lsp_document_metadata(
        ClearedNotificationClient(),
        [analyzed],
        {"lib/resource.dart": uri},
        flutter_outline=True,
    )[0]

    for symbol in [*reconciled.symbols, *reconciled.changed_symbols]:
        assert [item.text for item in symbol.metadata] == ["lifecycle method: dispose"]
    assert notification_calls == {
        "dart/textDocument/publishFlutterOutline": 1,
        "textDocument/publishDiagnostics": 1,
    }


def test_notification_metadata_snapshot_is_bounded_and_marks_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uri = "file:///repo/lib/resource.dart"

    class DiagnosticClient:
        def notifications(self, method: str | None = None, uri: str | None = None) -> list[dict[str, object]]:
            if method != "textDocument/publishDiagnostics":
                return []
            return [
                {
                    "method": method,
                    "params": {
                        "uri": uri,
                        "diagnostics": [
                            {
                                "severity": 1,
                                "range": _lsp_range(index, index),
                                "message": f"diagnostic-{index}",
                            }
                            for index in range(3)
                        ],
                    },
                }
            ]

    symbol = AnalyzerSymbol(name="resource", kind="function", startLine=1, endLine=10)
    analyzed = AnalyzerFile(path="lib/resource.dart", symbols=[symbol], changedSymbols=[symbol])
    warnings: list[str] = []
    partial_files: set[str] = set()
    monkeypatch.setattr(dart_runner_module, "DART_DIAGNOSTIC_NODE_LIMIT_PER_FILE", 2)

    reconciled = dart_runner_module._reconcile_lsp_document_metadata(
        DiagnosticClient(),
        [analyzed],
        {"lib/resource.dart": uri},
        flutter_outline=False,
        deadline=time.monotonic() + 10,
        warnings=warnings,
        semantic_partial_files=partial_files,
    )[0]

    assert len(reconciled.changed_symbols[0].metadata) == 2
    assert partial_files == {"lib/resource.dart"}
    assert any("notification-metadata safety budget" in warning for warning in warnings)


def test_flutter_outline_flattening_is_iterative_and_bounded() -> None:
    root: dict[str, object] = {"label": "node-0", "children": []}
    cursor = root
    for index in range(1, 2_000):
        child: dict[str, object] = {"label": f"node-{index}", "children": []}
        cursor["children"] = [child]
        cursor = child

    flattened = dart_runner_module._flatten_outline(root, limit=32)

    assert [node["label"] for node in flattened] == [f"node-{index}" for index in range(32)]


def test_flutter_auto_detection_honors_aggregate_manifest_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "packages" / "first" / "pubspec.yaml"
    second = tmp_path / "packages" / "second" / "pubspec.yaml"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("name: first\n", encoding="utf-8")
    second.write_text("name: second\n", encoding="utf-8")
    reads: list[str] = []
    original_reader = dart_runner_module._read_changed_dart_source

    def counted_reader(repo_root: Path, path: str, *, max_bytes: int) -> str:
        reads.append(path)
        return original_reader(repo_root, path, max_bytes=max_bytes)

    monkeypatch.setattr(dart_runner_module, "DART_FLUTTER_MANIFEST_TOTAL_BYTE_LIMIT", first.stat().st_size)
    monkeypatch.setattr(dart_runner_module, "_read_changed_dart_source", counted_reader)
    partial = [False]

    enabled = dart_runner_module._flutter_outline_enabled(
        tmp_path,
        {},
        [Path("packages/first/pubspec.yaml"), Path("packages/second/pubspec.yaml")],
        "auto",
        time.monotonic() + 10,
        partial,
    )

    assert enabled is False
    assert partial == [True]
    assert reads == ["packages/first/pubspec.yaml"]


def test_dart_analyzer_marks_truncated_flutter_detection_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("class ResourceScreen {}\n", encoding="utf-8")
    fake_class = _FakeDartLspClient
    fake_class.instances.clear()

    def truncated_detection(*args: object) -> bool:
        partial = args[-1]
        assert isinstance(partial, list)
        partial[0] = True
        return True

    monkeypatch.setattr(dart_runner_module, "DartLspClient", fake_class)
    monkeypatch.setattr(dart_runner_module, "_flutter_outline_enabled", truncated_detection)
    monkeypatch.setattr(
        dart_runner_module,
        "resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )

    result = run_dart_analyzer(
        tmp_path,
        [_dart_file("lib/resource.dart")],
        AnalyzerConfig(index_cache_enabled=False),
        project_files=[Path("lib/resource.dart")],
    )

    assert result is not None
    assert result.partial is True
    assert any("Flutter auto-detection" in warning for warning in result.warnings)


def test_dart_optional_request_timeout_marks_result_partial_and_non_cacheable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text(
        "\n" * 11 + "class ResourceScreen {}\n" + "\n" * 5,
        encoding="utf-8",
    )

    class TimeoutReferencesClient(_FakeDartLspClient):
        def request(self, method: str, params: object = None, **kwargs: object) -> object:
            if method == "textDocument/references":
                raise DartLspTimeout("synthetic optional request timeout")
            return super().request(method, params, **kwargs)

    fake_class = TimeoutReferencesClient
    fake_class.instances.clear()
    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )
    monkeypatch.setattr("apex_ray.analyzers.dart.runner._flutter_outline_enabled", lambda *_args: True)
    changed = _dart_file("lib/resource.dart")
    changed.hunks = [
        ChangedHunk(
            old_start=12,
            old_lines=1,
            new_start=12,
            new_lines=1,
            lines=[DiffLine(kind=DiffLineKind.ADD, content="class ResourceScreen {}", new_line=12)],
        )
    ]

    result = run_dart_analyzer(
        tmp_path,
        [changed],
        AnalyzerConfig(index_cache_dir=str(tmp_path / "cache")),
        project_files=[Path("lib/resource.dart")],
    )

    assert result is not None
    assert result.partial is True
    assert result.index_cache is None
    assert not (tmp_path / "cache").exists()
    assert any("textDocument/references timed out" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("file_limit", "byte_limit"),
    [
        pytest.param(1, 1_000_000, id="file-cap"),
        pytest.param(100, 32, id="aggregate-byte-cap"),
    ],
)
def test_dart_analyzer_bounds_retained_changed_source_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_limit: int,
    byte_limit: int,
) -> None:
    source_text = "class ResourceScreen {}\n"
    paths = ["lib/first.dart", "lib/second.dart"]
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source_text, encoding="utf-8")
    fake_class = _FakeDartLspClient
    fake_class.instances.clear()
    source_reads = 0
    original_reader = dart_runner_module._read_changed_dart_source

    def counted_reader(*args: object, **kwargs: Any) -> str:
        nonlocal source_reads
        source_reads += 1
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(dart_runner_module, "DART_CHANGED_SOURCE_FILE_LIMIT", file_limit, raising=False)
    monkeypatch.setattr(dart_runner_module, "DART_CHANGED_SOURCE_TOTAL_BYTE_LIMIT", byte_limit, raising=False)
    monkeypatch.setattr(dart_runner_module, "_read_changed_dart_source", counted_reader)
    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )
    monkeypatch.setattr("apex_ray.analyzers.dart.runner._flutter_outline_enabled", lambda *_args: True)

    result = run_dart_analyzer(
        tmp_path,
        [_dart_file(path) for path in paths],
        AnalyzerConfig(index_cache_enabled=False),
        project_files=[Path(path) for path in paths],
    )

    assert result is not None
    assert [file.path for file in result.files] == ["lib/first.dart"]
    assert result.failed_files == ["lib/second.dart"]
    assert result.partial is True
    assert len(fake_class.instances[0].opened) == 1
    assert source_reads == 1
    assert any("changed-source safety limit" in warning for warning in result.warnings)


def test_dart_analyzer_bounds_retained_anchor_source_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = "class ResourceScreen {}\n"
    paths = ["lib/changed.dart", "packages/one/lib/one.dart", "packages/two/lib/two.dart"]
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source_text, encoding="utf-8")
    fake_class = _FakeDartLspClient
    fake_class.instances.clear()
    source_reads = 0
    original_reader = dart_runner_module._read_changed_dart_source

    def counted_reader(*args: object, **kwargs: Any) -> str:
        nonlocal source_reads
        source_reads += 1
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(
        dart_runner_module,
        "DART_ANCHOR_TOTAL_SOURCE_BYTE_LIMIT",
        len(source_text.encode("utf-8")),
        raising=False,
    )
    monkeypatch.setattr(
        dart_runner_module,
        "reverse_dependency_anchors",
        lambda *_args, **_kwargs: [Path(paths[1]), Path(paths[2])],
    )
    monkeypatch.setattr(dart_runner_module, "_read_changed_dart_source", counted_reader)
    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )
    monkeypatch.setattr("apex_ray.analyzers.dart.runner._flutter_outline_enabled", lambda *_args: True)

    result = run_dart_analyzer(
        tmp_path,
        [_dart_file(paths[0])],
        AnalyzerConfig(index_cache_enabled=False),
        project_files=[Path(path) for path in paths],
    )

    assert result is not None
    assert [file.path for file in result.files] == [paths[0]]
    assert len(fake_class.instances[0].opened) == 2
    assert source_reads == 2
    assert any("anchor-source safety limit" in warning for warning in result.warnings)


def test_dart_analyzer_bounds_retained_document_symbols(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("class ResourceScreen {}\n", encoding="utf-8")
    changed = _dart_file("lib/resource.dart")
    changed.hunks = [
        ChangedHunk(
            old_start=1,
            old_lines=1,
            new_start=1,
            new_lines=1,
            lines=[DiffLine(kind=DiffLineKind.ADD, content="class ResourceScreen {}", new_line=1)],
        )
    ]
    fake_class = _FakeDartLspClient
    fake_class.instances.clear()
    original_request = fake_class.request

    def symbols_request(
        self: _FakeDartLspClient,
        method: str,
        params: object = None,
        **kwargs: object,
    ) -> object:
        if method == "textDocument/documentSymbol":
            return [
                {
                    "name": f"ResourceScreen{index}",
                    "kind": 5,
                    "range": _lsp_range(0, 0),
                    "selectionRange": _lsp_range(0, 0),
                }
                for index in range(100)
            ]
        return original_request(self, method, params, **kwargs)

    monkeypatch.setattr(fake_class, "request", symbols_request)
    monkeypatch.setattr(dart_runner_module, "DART_DOCUMENT_SYMBOL_LIMIT_PER_FILE", 2)
    monkeypatch.setattr(dart_runner_module, "DART_DOCUMENT_SYMBOL_LIMIT_TOTAL", 2)
    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )
    monkeypatch.setattr("apex_ray.analyzers.dart.runner._flutter_outline_enabled", lambda *_args: True)

    result = run_dart_analyzer(
        tmp_path,
        [changed],
        AnalyzerConfig(index_cache_enabled=False),
        project_files=[Path("lib/resource.dart")],
    )

    assert result is not None
    assert result.partial is True
    assert len(result.files[0].symbols) == 2
    assert len(result.files[0].changed_symbols) == 2
    assert result.files[0].uncovered_changed_ranges == [(1, 1)]
    assert any("document-symbol safety limit" in warning for warning in result.warnings)


def test_dart_symbol_packs_preserve_non_symbol_and_limited_diff_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "many.dart"
    source.parent.mkdir()
    source.write_text(
        "\n".join(
            [
                "",
                "",
                "class Container {",
                "  int first() => 1;",
                "",
                "",
                "",
                "",
                "",
                "  int second() => 2;",
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="lib/many.dart",
        new_path="lib/many.dart",
        language="dart",
        file_kind=FileKind.SOURCE,
        hunks=[
            ChangedHunk(
                old_start=1,
                old_lines=1,
                new_start=1,
                new_lines=0,
                lines=[
                    DiffLine(
                        kind=DiffLineKind.DELETE,
                        content="import 'dart:async';",
                        old_line=1,
                    )
                ],
            ),
            ChangedHunk(
                old_start=4,
                old_lines=1,
                new_start=4,
                new_lines=1,
                lines=[DiffLine(kind=DiffLineKind.ADD, content="int first() => 1;", new_line=4)],
            ),
            ChangedHunk(
                old_start=10,
                old_lines=1,
                new_start=10,
                new_lines=1,
                lines=[DiffLine(kind=DiffLineKind.ADD, content="int second() => 2;", new_line=10)],
            ),
        ],
    )
    fake_class = _FakeDartLspClient
    fake_class.instances.clear()
    original_request = fake_class.request

    def symbols_request(
        self: _FakeDartLspClient,
        method: str,
        params: object = None,
        **kwargs: object,
    ) -> object:
        if method == "textDocument/documentSymbol":
            return [
                {
                    "name": "Container",
                    "kind": 5,
                    "range": _lsp_range(2, 10),
                    "selectionRange": _lsp_range(2, 2),
                    "children": [
                        {
                            "name": "first",
                            "kind": 6,
                            "range": _lsp_range(3, 3),
                            "selectionRange": _lsp_range(3, 3),
                        },
                        {
                            "name": "second",
                            "kind": 6,
                            "range": _lsp_range(9, 499),
                            "selectionRange": _lsp_range(9, 9),
                        },
                    ],
                }
            ]
        return original_request(self, method, params, **kwargs)

    def did_open(self: _FakeDartLspClient, uri: str, _text: str, **_kwargs: object) -> None:
        self.opened.append(uri)
        self.source_uri = uri

    monkeypatch.setattr(fake_class, "request", symbols_request)
    monkeypatch.setattr(fake_class, "did_open", did_open)
    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner._flutter_outline_enabled",
        lambda *_args, **_kwargs: True,
    )

    result = run_dart_analyzer(
        tmp_path,
        [changed],
        AnalyzerConfig(index_cache_enabled=False, dart={"max_changed_symbols": 1}),
        project_files=[Path("lib/many.dart")],
    )

    assert result is not None
    analyzed = result.files[0]
    assert [symbol.name for symbol in analyzed.changed_symbols] == ["Container"]
    assert analyzed.uncovered_changed_ranges == [(1, 1), (4, 4), (10, 10)]
    packs = build_context_packs([result], [changed], ReviewConfig(), repo_root=tmp_path)
    residual = next(pack for pack in packs if pack.id == "lib/many.dart#diff-uncovered")
    assert residual.changed_lines == [(1, 1), (4, 4), (10, 10)]
    assert "-import 'dart:async';" in residual.diff_snippet
    assert "+int second() => 2;" in residual.diff_snippet


def test_dart_analyzer_opens_minimal_reverse_dependency_package_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = tmp_path / "packages" / "core"
    mobile = tmp_path / "apps" / "mobile"
    (core / "lib").mkdir(parents=True)
    (mobile / "lib").mkdir(parents=True)
    (core / "pubspec.yaml").write_text("name: core\n", encoding="utf-8")
    (mobile / "pubspec.yaml").write_text(
        "name: mobile\ndependencies:\n  core:\n    path: ../../packages/core\n",
        encoding="utf-8",
    )
    changed_source = core / "lib" / "core.dart"
    changed_source.write_text(
        "import 'package:flutter/widgets.dart'; // ResourceScreen\nint value() => 1;\n",
        encoding="utf-8",
    )
    anchor = mobile / "lib" / "mobile.dart"
    anchor.write_text("// ResourceScreen reverse-dependency anchor\n", encoding="utf-8")
    changed = ChangedFile(
        old_path="packages/core/lib/core.dart",
        new_path="packages/core/lib/core.dart",
        language="dart",
        file_kind=FileKind.SOURCE,
        hunks=[
            ChangedHunk(
                old_start=2,
                old_lines=1,
                new_start=2,
                new_lines=1,
                lines=[DiffLine(kind=DiffLineKind.ADD, content="int value() => 1;", new_line=2)],
            )
        ],
    )
    fake_class = _FakeDartLspClient
    fake_class.instances.clear()
    monkeypatch.setattr("apex_ray.analyzers.dart.runner.DartLspClient", fake_class)
    monkeypatch.setattr(
        "apex_ray.analyzers.dart.runner.resolve_dart_toolchain",
        lambda *_args, **_kwargs: DartToolchainResolution(command=["dart"], source="path"),
    )

    result = run_dart_analyzer(
        tmp_path,
        [changed],
        AnalyzerConfig(index_cache_enabled=False),
        project_files=[
            Path("packages/core/pubspec.yaml"),
            Path("packages/core/lib/core.dart"),
            Path("apps/mobile/pubspec.yaml"),
            Path("apps/mobile/lib/mobile.dart"),
        ],
    )

    assert result is not None
    assert set(fake_class.instances[0].opened) == {changed_source.as_uri(), anchor.as_uri()}
