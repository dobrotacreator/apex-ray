import json
import shutil
import subprocess
from pathlib import Path

import pytest

import apex_ray.analyzers.go as go_analyzer_module
from apex_ray.analyzers import (
    PYTHON_DELETED_SYMBOL_RE,
    PYTHON_LANGUAGES,
    PYTHON_READ_ERRORS,
    PYTHON_SCAN_IGNORED_DIRS,
    AnalyzerError,
    go_analyzer_runtime_dir,
    python_changed_files,
    run_analyzers,
    run_go_analyzer,
    run_python_analyzer,
    run_typescript_analyzer,
    typescript_analyzer_script,
)
from apex_ray.models import AnalyzerConfig, AnalyzerFile, AnalyzerResult, ChangedFile, FileKind


def test_analyzers_public_exports_keep_legacy_python_constants() -> None:
    assert PYTHON_LANGUAGES == {"python"}
    assert PYTHON_READ_ERRORS == (OSError, UnicodeDecodeError, SyntaxError)
    assert ".git" in PYTHON_SCAN_IGNORED_DIRS
    assert PYTHON_DELETED_SYMBOL_RE.match("def removed()") is not None


def test_typescript_analyzer_uses_configured_script_path(tmp_path: Path) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("console.log('{}')\n", encoding="utf-8")

    assert typescript_analyzer_script(AnalyzerConfig(script_path=str(script))) == script.resolve()


def test_typescript_analyzer_resolves_relative_script_path_against_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    script = repo / "tools" / "analyze.js"
    subdir = repo / "packages" / "api"
    script.parent.mkdir(parents=True)
    subdir.mkdir(parents=True)
    script.write_text("console.log('{}')\n", encoding="utf-8")
    changed = ChangedFile(
        old_path="src/cart.ts",
        new_path="src/cart.ts",
        language="typescript",
        file_kind=FileKind.SOURCE,
    )
    seen_command: list[str] | None = None

    monkeypatch.chdir(subdir)
    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_command
        seen_command = args
        payload = {
            "language": "typescript",
            "projectRoot": str(repo),
            "tsconfigPath": None,
            "files": [],
            "warnings": [],
            "indexCache": None,
        }
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("apex_ray.analyzers.typescript._run_analyzer_process", fake_run)

    result = run_typescript_analyzer(repo, [changed], AnalyzerConfig(script_path="tools/analyze.js"))

    assert result is not None
    assert seen_command is not None
    assert seen_command[1] == str(script.resolve())


def test_typescript_analyzer_passes_internal_time_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("console.log('{}')\n", encoding="utf-8")
    changed = ChangedFile(
        old_path="src/cart.ts",
        new_path="src/cart.ts",
        language="typescript",
        file_kind=FileKind.SOURCE,
    )
    seen_command: list[str] | None = None

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_command
        seen_command = args
        payload = {
            "language": "typescript",
            "projectRoot": str(tmp_path),
            "tsconfigPath": None,
            "files": [],
            "warnings": [],
            "indexCache": None,
        }
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("apex_ray.analyzers.typescript._run_analyzer_process", fake_run)

    result = run_typescript_analyzer(
        tmp_path,
        [changed],
        AnalyzerConfig(script_path=str(script), timeout_seconds=10),
    )

    assert result is not None
    assert seen_command is not None
    budget_index = seen_command.index("--analysis-time-budget-ms")
    assert seen_command[budget_index + 1] == "9500"


def test_go_analyzer_prefers_bundled_runtime_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_file = tmp_path / "site-packages" / "apex_ray" / "analyzers" / "go.py"
    bundled_runtime = tmp_path / "site-packages" / "apex_ray" / "_bundled" / "go"
    module_file.parent.mkdir(parents=True)
    bundled_runtime.mkdir(parents=True)
    module_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(go_analyzer_module, "__file__", str(module_file))

    assert go_analyzer_runtime_dir() == bundled_runtime


def test_go_analyzer_passes_internal_time_budget_ranges_and_deleted_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = ChangedFile(
        old_path="internal/auth/service.go",
        new_path="internal/auth/service.go",
        language="go",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 10,
                "old_lines": 3,
                "new_start": 10,
                "new_lines": 3,
                "lines": [
                    {"kind": "delete", "old_line": 10, "content": "func removed() error {"},
                    {"kind": "add", "new_line": 10, "content": "func added() error {"},
                    {"kind": "add", "new_line": 11, "content": "    return nil"},
                ],
            },
            {
                "old_start": 20,
                "old_lines": 2,
                "new_start": 20,
                "new_lines": 0,
                "lines": [
                    {"kind": "delete", "old_line": 20, "content": "func removed() error {"},
                    {"kind": "delete", "old_line": 21, "content": "    return nil"},
                ],
            },
        ],
    )
    seen_command: list[str] | None = None
    runtime_dir = tmp_path / "go-runtime"
    runtime_dir.mkdir()

    monkeypatch.setattr("apex_ray.analyzers.go.shutil.which", lambda name: "/usr/bin/go")
    monkeypatch.setattr("apex_ray.analyzers.go.go_analyzer_runtime_dir", lambda: runtime_dir)

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_command
        seen_command = args
        assert cwd == runtime_dir
        assert timeout == 10
        payload = {
            "language": "go",
            "projectRoot": str(tmp_path),
            "tsconfigPath": None,
            "files": [],
            "warnings": [],
            "indexCache": None,
        }
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("apex_ray.analyzers.go._run_analyzer_process", fake_run)

    result = run_go_analyzer(tmp_path, [changed], AnalyzerConfig(timeout_seconds=10))

    assert result is not None
    assert seen_command is not None
    assert seen_command[:6] == ["go", "run", "./cmd/apex-ray-go-analyzer", "--repo", str(tmp_path), "--changed"]
    assert "internal/auth/service.go" in seen_command
    budget_index = seen_command.index("--analysis-time-budget-ms")
    assert seen_command[budget_index + 1] == "9500"
    assert ["--range", "internal/auth/service.go:10-11"] == seen_command[
        seen_command.index("--range") : seen_command.index("--range") + 2
    ]
    deleted_line_args = [
        seen_command[index : index + 4] for index, value in enumerate(seen_command) if value == "--deleted-line"
    ]
    assert [
        "--deleted-line",
        "internal/auth/service.go",
        "10",
        "func removed() error {",
    ] in deleted_line_args
    assert [
        "--deleted-line",
        "internal/auth/service.go",
        "20",
        "func removed() error {",
    ] in deleted_line_args


def test_go_analyzer_passes_deleted_go_files_as_diff_only_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = ChangedFile(
        old_path="internal/auth/removed.go",
        new_path=None,
        language="go",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 1,
                "old_lines": 2,
                "new_start": 1,
                "new_lines": 0,
                "lines": [
                    {"kind": "delete", "old_line": 1, "content": "func Removed() error {"},
                    {"kind": "delete", "old_line": 2, "content": "    return nil"},
                ],
            }
        ],
    )
    seen_command: list[str] | None = None
    runtime_dir = tmp_path / "go-runtime"
    runtime_dir.mkdir()

    monkeypatch.setattr("apex_ray.analyzers.go.shutil.which", lambda name: "/usr/bin/go")
    monkeypatch.setattr("apex_ray.analyzers.go.go_analyzer_runtime_dir", lambda: runtime_dir)

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_command
        seen_command = args
        payload = {
            "language": "go",
            "projectRoot": str(tmp_path),
            "tsconfigPath": None,
            "files": [
                {
                    "path": "internal/auth/removed.go",
                    "tsconfigPath": None,
                    "symbols": [],
                    "imports": [],
                    "exports": [],
                    "relatedTests": [],
                    "changedSymbols": [
                        {
                            "name": "Removed",
                            "kind": "function",
                            "startLine": 1,
                            "endLine": 2,
                            "exported": True,
                            "signature": "removed Go function: func Removed() error {",
                            "references": [],
                            "callees": [],
                            "contracts": [],
                            "metadata": [],
                        }
                    ],
                }
            ],
            "warnings": [],
            "indexCache": None,
            "partial": False,
            "failedFiles": [],
            "shardFailures": [],
        }
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("apex_ray.analyzers.go._run_analyzer_process", fake_run)

    result = run_go_analyzer(tmp_path, [changed], AnalyzerConfig(timeout_seconds=10))

    assert result is not None
    assert seen_command is not None
    changed_index = seen_command.index("--changed")
    next_option = next(
        index for index in range(changed_index + 1, len(seen_command)) if seen_command[index].startswith("--")
    )
    assert seen_command[changed_index + 1 : next_option] == []
    assert [
        "--deleted-line",
        "internal/auth/removed.go",
        "1",
        "func Removed() error {",
    ] in [seen_command[index : index + 4] for index, value in enumerate(seen_command) if value == "--deleted-line"]
    assert [
        "--deleted-line",
        "internal/auth/removed.go",
        "2",
        "    return nil",
    ] in [seen_command[index : index + 4] for index, value in enumerate(seen_command) if value == "--deleted-line"]
    assert result.partial is False
    assert result.failed_files == []
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "Removed"
    assert symbol.start_line == 1
    assert symbol.end_line == 2


def test_go_analyzer_collects_semantic_context(tmp_path: Path) -> None:
    if shutil.which("go") is None:
        pytest.skip("go is required for the Go analyzer integration test")
    (tmp_path / "internal" / "auth").mkdir(parents=True)
    (tmp_path / "internal" / "api").mkdir(parents=True)
    (tmp_path / "go.mod").write_text("module example.com/review\n\ngo 1.24\n", encoding="utf-8")
    auth_source = (
        "package auth\n\n"
        "import (\n"
        '    "context"\n'
        '    "fmt"\n'
        ")\n\n"
        "type Repository interface {\n"
        "    Save(context.Context, string) error\n"
        "}\n\n"
        "type Authorizer interface {\n"
        "    Authorize(context.Context, string) error\n"
        "}\n\n"
        "type Service struct {\n"
        "    repo Repository\n"
        "}\n\n"
        "func NewService(repo Repository) *Service {\n"
        "    return &Service{repo: repo}\n"
        "}\n\n"
        "func (s *Service) Authorize(ctx context.Context, id string) error {\n"
        "    if err := s.repo.Save(ctx, id); err != nil {\n"
        '        return fmt.Errorf("save auth: %w", err)\n'
        "    }\n"
        "    return nil\n"
        "}\n"
    )
    (tmp_path / "internal" / "auth" / "service.go").write_text(auth_source, encoding="utf-8")
    (tmp_path / "internal" / "api" / "handler.go").write_text(
        "package api\n\n"
        "import (\n"
        '    "net/http"\n\n'
        '    "example.com/review/internal/auth"\n'
        ")\n\n"
        "func Register(mux *http.ServeMux, svc *auth.Service) {\n"
        '    mux.HandleFunc("/resource", func(w http.ResponseWriter, r *http.Request) {\n'
        '        _ = svc.Authorize(r.Context(), "resource-id")\n'
        "    })\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "internal" / "auth" / "service_test.go").write_text(
        "package auth\n\n"
        "import (\n"
        '    "context"\n'
        '    "testing"\n'
        ")\n\n"
        "type fakeRepo struct{}\n\n"
        "func (fakeRepo) Save(context.Context, string) error { return nil }\n\n"
        "func TestAuthorize(t *testing.T) {\n"
        "    service := NewService(fakeRepo{})\n"
        '    if err := service.Authorize(context.Background(), "id"); err != nil {\n'
        "        t.Fatal(err)\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    changed_line = next(index for index, line in enumerate(auth_source.splitlines(), start=1) if "s.repo.Save" in line)
    changed = ChangedFile(
        old_path="internal/auth/service.go",
        new_path="internal/auth/service.go",
        language="go",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": changed_line,
                "old_lines": 1,
                "new_start": changed_line,
                "new_lines": 1,
                "lines": [
                    {
                        "kind": "delete",
                        "old_line": changed_line,
                        "content": "    if err := s.repo.Save(ctx, id); err != nil {",
                    },
                    {
                        "kind": "add",
                        "new_line": changed_line,
                        "content": "    if err := s.repo.Save(ctx, id); err != nil {",
                    },
                ],
            }
        ],
    )

    result = run_go_analyzer(tmp_path, [changed], AnalyzerConfig(timeout_seconds=30))

    assert result is not None
    assert result.language == "go"
    file_result = result.files[0]
    assert file_result.path == "internal/auth/service.go"
    assert "context" in file_result.imports
    assert {"Authorizer", "NewService", "Repository", "Service"} <= set(file_result.exports)
    assert [symbol.name for symbol in file_result.changed_symbols] == ["Service.Authorize"]
    symbol = file_result.changed_symbols[0]
    assert "func (s *Service) Authorize(ctx context.Context, id string) error" in symbol.signature
    assert ("internal/api/handler.go", "call", 'svc.Authorize(r.Context(), "resource-id")') in {
        (reference.file, reference.kind, reference.text) for reference in symbol.references
    }
    assert any(reference.file == "internal/auth/service_test.go" for reference in symbol.references)
    assert any(reference.text == "Save(context.Context, string) error" for reference in symbol.callees)
    assert any(reference.text.startswith("type Service struct") for reference in symbol.contracts)
    assert "context boundary: context.Context" in {reference.text for reference in symbol.metadata}
    assert any(reference.text.startswith("error wrapping: fmt.Errorf") for reference in symbol.metadata)
    assert file_result.related_tests == ["internal/auth/service_test.go"]


def test_run_analyzers_scopes_unavailable_backend_fallback_to_matching_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "review.py").write_text("def review() -> bool:\n    return True\n", encoding="utf-8")
    changed = [
        ChangedFile(
            old_path="src/cart.ts",
            new_path="src/cart.ts",
            language="typescript",
            file_kind=FileKind.SOURCE,
        ),
        ChangedFile(
            old_path="src/review.py",
            new_path="src/review.py",
            language="python",
            file_kind=FileKind.SOURCE,
        ),
    ]

    def fail_typescript(*args: object, **kwargs: object) -> AnalyzerResult | None:
        raise AnalyzerError("boom")

    monkeypatch.setattr("apex_ray.analyzers.run_typescript_analyzer", fail_typescript)

    result = run_analyzers(tmp_path, changed)

    assert [analyzer_result.language for analyzer_result in result.results] == ["python"]
    assert result.warnings == ["TypeScript analyzer unavailable: boom"]
    assert result.fallback_reasons_by_path == {
        "src/cart.ts": "TypeScript analyzer unavailable: boom; using diff-only fallback context."
    }
    assert result.backend_runs[0].name == "typescript"
    assert result.backend_runs[0].changed_files_count == 1
    assert result.backend_runs[0].warning == "TypeScript analyzer unavailable: boom"
    assert result.backend_runs[1].name == "go"
    assert result.backend_runs[1].changed_files_count == 0
    assert result.backend_runs[2].name == "python"
    assert result.backend_runs[2].changed_files_count == 1


def test_run_analyzers_scopes_unavailable_go_backend_to_go_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "review.py").write_text("def review() -> bool:\n    return True\n", encoding="utf-8")
    changed = [
        ChangedFile(
            old_path="internal/auth/service.go",
            new_path="internal/auth/service.go",
            language="go",
            file_kind=FileKind.SOURCE,
        ),
        ChangedFile(
            old_path="src/review.py",
            new_path="src/review.py",
            language="python",
            file_kind=FileKind.SOURCE,
        ),
    ]

    def fail_go(*args: object, **kwargs: object) -> AnalyzerResult | None:
        raise AnalyzerError("go missing")

    monkeypatch.setattr("apex_ray.analyzers.run_go_analyzer", fail_go)

    result = run_analyzers(tmp_path, changed)

    assert [analyzer_result.language for analyzer_result in result.results] == ["python"]
    assert result.warnings == ["Go analyzer unavailable: go missing"]
    assert result.fallback_reasons_by_path == {
        "internal/auth/service.go": "Go analyzer unavailable: go missing; using diff-only fallback context."
    }
    assert result.backend_runs[0].name == "typescript"
    assert result.backend_runs[0].changed_files_count == 0
    assert result.backend_runs[1].name == "go"
    assert result.backend_runs[1].changed_files_count == 1
    assert result.backend_runs[1].warning == "Go analyzer unavailable: go missing"
    assert result.backend_runs[2].name == "python"
    assert result.backend_runs[2].changed_files_count == 1


def test_run_analyzers_returns_backend_results_and_partial_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = [
        ChangedFile(
            old_path="src/cart.ts",
            new_path="src/cart.ts",
            language="typescript",
            file_kind=FileKind.SOURCE,
        )
    ]
    analyzer_result = AnalyzerResult(
        language="typescript",
        projectRoot=str(tmp_path),
        tsconfigPath=None,
        files=[AnalyzerFile(path="src/cart.ts")],
        warnings=["partial"],
        indexCache=None,
        partial=True,
        failedFiles=["src/cart.ts"],
    )

    monkeypatch.setattr("apex_ray.analyzers.run_typescript_analyzer", lambda *args, **kwargs: analyzer_result)

    result = run_analyzers(tmp_path, changed)

    assert result.results == [analyzer_result]
    assert result.warnings == []
    assert result.fallback_reasons_by_path == {
        "src/cart.ts": "TypeScript analyzer shard failed; using diff-only fallback context."
    }
    assert result.backend_runs[0].result == analyzer_result


def test_python_analyzer_collects_changed_symbols_imports_exports_and_related_tests(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "calculator.py").write_text(
        "from decimal import Decimal\n\n"
        "RATE: Decimal = Decimal('1.10')\n\n"
        "def helper(value: Decimal) -> Decimal:\n"
        "    return value * RATE\n\n"
        "def calculate_total(price: Decimal, quantity: int) -> Decimal:\n"
        "    subtotal = price * quantity\n"
        "    return helper(subtotal)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_calculator.py").write_text(
        "from calculator import calculate_total\n\n"
        "def test_calculate_total() -> None:\n"
        "    assert calculate_total(Decimal('2'), 3)\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/calculator.py",
        new_path="src/calculator.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 8,
                "old_lines": 3,
                "new_start": 8,
                "new_lines": 3,
                "lines": [
                    {"kind": "context", "content": "def calculate_total(price: Decimal, quantity: int) -> Decimal:"},
                    {"kind": "delete", "content": "    subtotal = price"},
                    {"kind": "add", "content": "    subtotal = price * quantity"},
                    {"kind": "context", "content": "    return helper(subtotal)"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert result.language == "python"
    assert result.files[0].path == "src/calculator.py"
    assert "from decimal import Decimal" in result.files[0].imports
    assert {"RATE", "helper", "calculate_total"} <= set(result.files[0].exports)
    assert [symbol.name for symbol in result.files[0].changed_symbols] == ["calculate_total"]
    assert (
        result.files[0].changed_symbols[0].signature == "def calculate_total(price: Decimal, quantity: int) -> Decimal"
    )
    assert result.files[0].related_tests == ["tests/test_calculator.py"]


def test_python_analyzer_collects_class_methods_decorators_and_base_contracts(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "handlers.py").write_text(
        "class BaseHandler:\n"
        "    pass\n\n"
        "class ResourceHandler(BaseHandler):\n"
        "    @classmethod\n"
        "    def build(cls, value: str) -> 'ResourceHandler':\n"
        "        return cls()\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/handlers.py",
        new_path="src/handlers.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 6,
                "old_lines": 2,
                "new_start": 6,
                "new_lines": 2,
                "lines": [
                    {"kind": "context", "content": "    def build(cls, value: str) -> 'ResourceHandler':"},
                    {"kind": "delete", "content": "        return ResourceHandler()"},
                    {"kind": "add", "content": "        return cls()"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    file_result = result.files[0]
    class_symbol = next(symbol for symbol in file_result.symbols if symbol.name == "ResourceHandler")
    method_symbol = next(symbol for symbol in file_result.symbols if symbol.name == "ResourceHandler.build")
    assert class_symbol.contracts[0].text == "BaseHandler"
    assert method_symbol.metadata[0].text == "@classmethod"
    assert [symbol.name for symbol in file_result.changed_symbols] == ["ResourceHandler", "ResourceHandler.build"]


def test_run_analyzers_adds_python_partial_fallback_for_syntax_errors(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "broken.py").write_text("def broken(:\n    return True\n", encoding="utf-8")
    changed = [
        ChangedFile(
            old_path="src/broken.py",
            new_path="src/broken.py",
            language="python",
            file_kind=FileKind.SOURCE,
        )
    ]

    result = run_analyzers(tmp_path, changed)

    assert result.results[0].language == "python"
    assert result.results[0].partial is True
    assert result.results[0].failed_files == ["src/broken.py"]
    assert any("Unable to parse Python file src/broken.py" in warning for warning in result.results[0].warnings)
    assert result.fallback_reasons_by_path == {
        "src/broken.py": "Python analyzer failed; using diff-only fallback context."
    }


def test_python_analyzer_rejects_paths_outside_repo(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("def leaked_secret() -> str:\n    return 'secret'\n", encoding="utf-8")
    changed = ChangedFile(
        old_path="../outside.py",
        new_path="../outside.py",
        language="python",
        file_kind=FileKind.SOURCE,
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert result.files == []
    assert result.partial is True
    assert result.failed_files == ["../outside.py"]
    assert result.warnings == ["Unsafe Python file path ../outside.py; using diff-only fallback context."]


def test_python_analyzer_treats_invalid_source_encoding_as_partial_fallback(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "binary.py").write_bytes(b"\xff\xfe\xfa")
    changed = ChangedFile(
        old_path="src/binary.py",
        new_path="src/binary.py",
        language="python",
        file_kind=FileKind.SOURCE,
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert result.files == []
    assert result.partial is True
    assert result.failed_files == ["src/binary.py"]
    assert any("Unable to read Python file src/binary.py" in warning for warning in result.warnings)


def test_python_analyzer_handles_invalid_related_test_encoding(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "calculator.py").write_text(
        "def calculate_total(price: int, quantity: int) -> int:\n    return price * quantity\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_calculator.py").write_bytes(b"\xff\xfe\xfa")
    changed = ChangedFile(
        old_path="src/calculator.py",
        new_path="src/calculator.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 2,
                "old_lines": 1,
                "new_start": 2,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return price"},
                    {"kind": "add", "content": "    return price * quantity"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert result.partial is False
    assert result.files[0].related_tests == ["tests/test_calculator.py"]


def test_python_analyzer_respects_empty_dunder_all(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "exports.py").write_text(
        "__all__ = []\n\ndef public_helper() -> bool:\n    return True\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/exports.py",
        new_path="src/exports.py",
        language="python",
        file_kind=FileKind.SOURCE,
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert result.files[0].exports == []


def test_python_analyzer_synthesizes_deleted_symbol_before_kept_symbol(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "handlers.py").write_text(
        "def kept() -> bool:\n    return True\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/handlers.py",
        new_path="src/handlers.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 1,
                "old_lines": 5,
                "new_start": 1,
                "new_lines": 2,
                "lines": [
                    {"kind": "delete", "content": "def removed() -> bool:"},
                    {"kind": "delete", "content": "    return True"},
                    {"kind": "delete", "content": ""},
                    {"kind": "context", "content": "def kept() -> bool:"},
                    {"kind": "context", "content": "    return True"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert [symbol.name for symbol in result.files[0].symbols] == ["kept"]
    assert [symbol.name for symbol in result.files[0].changed_symbols] == ["removed"]
    assert result.files[0].changed_symbols[0].signature == "removed Python function: def removed() -> bool:"


def test_python_analyzer_collects_nested_class_method_changed_symbols(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "nested.py").write_text(
        "class Outer:\n    class Inner:\n        def handle(self, value: int) -> int:\n            return value + 1\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/nested.py",
        new_path="src/nested.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 4,
                "old_lines": 1,
                "new_start": 4,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "            return value"},
                    {"kind": "add", "content": "            return value + 1"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert "Outer.Inner.handle" in {symbol.name for symbol in result.files[0].symbols}
    assert "Outer.Inner.handle" in {symbol.name for symbol in result.files[0].changed_symbols}


def test_python_analyzer_collects_workspace_references_and_callees(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pricing.py").write_text(
        "def apply_discount(amount: int) -> int:\n"
        "    return amount\n\n"
        "def calculate_total(price: int, quantity: int) -> int:\n"
        "    subtotal = price * quantity\n"
        "    return apply_discount(subtotal)\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "checkout.py").write_text(
        "from pricing import calculate_total as total_for_cart\n\n"
        "def checkout(price: int, quantity: int) -> int:\n"
        "    return total_for_cart(price, quantity)\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "report.py").write_text(
        "import pricing\n\n"
        "def render(price: int, quantity: int) -> int:\n"
        "    return pricing.calculate_total(price, quantity)\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/pricing.py",
        new_path="src/pricing.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 5,
                "old_lines": 2,
                "new_start": 5,
                "new_lines": 2,
                "lines": [
                    {"kind": "delete", "content": "    subtotal = price"},
                    {"kind": "add", "content": "    subtotal = price * quantity"},
                    {"kind": "context", "content": "    return apply_discount(subtotal)"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "calculate_total"
    assert {(reference.file, reference.kind, reference.text) for reference in symbol.references} == {
        ("src/checkout.py", "call", "total_for_cart(price, quantity)"),
        ("src/report.py", "call", "pricing.calculate_total(price, quantity)"),
    }
    assert [(callee.file, callee.kind, callee.text) for callee in symbol.callees] == [
        ("src/pricing.py", "callee", "def apply_discount(amount: int) -> int")
    ]


def test_python_analyzer_does_not_treat_db_session_get_as_external_io(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "repository.py").write_text(
        "class ResourceRepository:\n"
        "    def __init__(self, session):\n"
        "        self._session = session\n\n"
        "    async def load_resource(self, resource_id: str):\n"
        "        record = await self._session.get(resource_id)\n"
        "        await self._session.commit()\n"
        "        return record\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/repository.py",
        new_path="src/repository.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 6,
                "old_lines": 2,
                "new_start": 6,
                "new_lines": 2,
                "lines": [
                    {"kind": "delete", "content": "        record = await self._session.get(resource_id)"},
                    {"kind": "add", "content": "        record = await self._session.get(resource_id)"},
                    {"kind": "context", "content": "        await self._session.commit()"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    symbol = next(
        symbol for symbol in result.files[0].changed_symbols if symbol.name == "ResourceRepository.load_resource"
    )
    metadata_texts = [reference.text for reference in symbol.metadata]
    assert "external I/O call: await self._session.get(resource_id)" not in metadata_texts
    assert "transaction boundary: await self._session.commit()" in metadata_texts


def test_python_analyzer_keeps_boundary_metadata_off_parent_classes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text(
        "class ResourceService:\n"
        "    def __init__(self, session):\n"
        "        self._session = session\n\n"
        "    async def save(self) -> None:\n"
        "        await self._session.commit()\n\n"
        "    def display_name(self) -> str:\n"
        "        return 'resource'\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/service.py",
        new_path="src/service.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 9,
                "old_lines": 1,
                "new_start": 9,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "        return 'resource'"},
                    {"kind": "add", "content": "        return 'resource'"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    metadata_by_symbol = {
        symbol.name: [reference.text for reference in symbol.metadata] for symbol in result.files[0].changed_symbols
    }
    assert metadata_by_symbol["ResourceService"] == []
    assert metadata_by_symbol["ResourceService.display_name"] == []


def test_python_analyzer_resolves_precise_migration_boundary_metadata(tmp_path: Path) -> None:
    (tmp_path / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / "migrations" / "versions" / "resource_status.py").write_text(
        "from alembic import op\n"
        "from alembic.op import execute\n\n"
        "def upgrade() -> None:\n"
        "    execute('select 1')\n"
        "    with op.batch_alter_table('resource') as batch_op:\n"
        "        batch_op.alter_column('status')\n\n"
        "def local_operation(op) -> None:\n"
        "    op.execute('not migration')\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="migrations/versions/resource_status.py",
        new_path="migrations/versions/resource_status.py",
        language="python",
        file_kind=FileKind.MIGRATION,
        hunks=[
            {
                "old_start": 5,
                "old_lines": 3,
                "new_start": 5,
                "new_lines": 3,
                "lines": [
                    {"kind": "add", "content": "    execute('select 1')"},
                    {"kind": "context", "content": "    with op.batch_alter_table('resource') as batch_op:"},
                    {"kind": "context", "content": "        batch_op.alter_column('status')"},
                ],
            },
            {
                "old_start": 10,
                "old_lines": 1,
                "new_start": 10,
                "new_lines": 1,
                "lines": [
                    {"kind": "add", "content": "    op.execute('not migration')"},
                ],
            },
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    metadata_by_symbol = {
        symbol.name: [reference.text for reference in symbol.metadata] for symbol in result.files[0].changed_symbols
    }
    assert "migration operation: execute('select 1')" in metadata_by_symbol["upgrade"]
    assert "migration operation: batch_op.alter_column('status')" in metadata_by_symbol["upgrade"]
    assert metadata_by_symbol["local_operation"] == []


def test_python_analyzer_collects_direct_external_import_monkeypatch_and_event_priority(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "adapter.py").write_text(
        "from requests import post\n\n"
        "def send_resource(payload: dict[str, str]):\n"
        "    return post('/resources', json=payload)\n\n"
        "def configure(monkeypatch) -> None:\n"
        "    monkeypatch.setenv('RESOURCE_MODE', 'test')\n\n"
        "class ResourceWorker:\n"
        "    def __init__(self, queue_client):\n"
        "        self._queue_client = queue_client\n\n"
        "    async def publish(self, event) -> None:\n"
        "        await self._queue_client.send(event)\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/adapter.py",
        new_path="src/adapter.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 4,
                "old_lines": 1,
                "new_start": 4,
                "new_lines": 1,
                "lines": [{"kind": "add", "content": "    return post('/resources', json=payload)"}],
            },
            {
                "old_start": 7,
                "old_lines": 1,
                "new_start": 7,
                "new_lines": 1,
                "lines": [{"kind": "add", "content": "    monkeypatch.setenv('RESOURCE_MODE', 'test')"}],
            },
            {
                "old_start": 14,
                "old_lines": 1,
                "new_start": 14,
                "new_lines": 1,
                "lines": [{"kind": "add", "content": "        await self._queue_client.send(event)"}],
            },
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    metadata_by_symbol = {
        symbol.name: [reference.text for reference in symbol.metadata] for symbol in result.files[0].changed_symbols
    }
    assert "external I/O call: post('/resources', json=payload)" in metadata_by_symbol["send_resource"]
    assert "test fixture override: monkeypatch.setenv('RESOURCE_MODE', 'test')" in metadata_by_symbol["configure"]
    assert "worker/event boundary: await self._queue_client.send(event)" in metadata_by_symbol["ResourceWorker.publish"]
    assert "external I/O call: await self._queue_client.send(event)" not in metadata_by_symbol["ResourceWorker.publish"]


def test_python_changed_files_includes_migrations() -> None:
    changed = ChangedFile(
        old_path="migrations/versions/resource_status.py",
        new_path="migrations/versions/resource_status.py",
        language="python",
        file_kind=FileKind.MIGRATION,
    )

    assert python_changed_files([changed]) == [changed]


def test_python_analyzer_reference_and_callee_limits_follow_constants_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pricing.py").write_text(
        "def helper_one() -> int:\n"
        "    return 1\n\n"
        "def helper_two() -> int:\n"
        "    return 2\n\n"
        "def helper_three() -> int:\n"
        "    return 3\n\n"
        "def calculate_total() -> int:\n"
        "    return helper_one() + helper_two() + helper_three()\n",
        encoding="utf-8",
    )
    for name in ["a", "b", "c"]:
        (tmp_path / "src" / f"consumer_{name}.py").write_text(
            f"from pricing import calculate_total\n\ndef render_{name}() -> int:\n    return calculate_total()\n",
            encoding="utf-8",
        )
    monkeypatch.setattr("apex_ray.analyzers.python.constants.PYTHON_REFERENCE_LIMIT", 1)
    monkeypatch.setattr("apex_ray.analyzers.python.constants.PYTHON_CALLEE_LIMIT", 1)
    changed = ChangedFile(
        old_path="src/pricing.py",
        new_path="src/pricing.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 11,
                "old_lines": 1,
                "new_start": 11,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return helper_one()"},
                    {"kind": "add", "content": "    return helper_one() + helper_two() + helper_three()"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert [(reference.file, reference.kind, reference.text) for reference in symbol.references] == [
        ("src/consumer_a.py", "call", "calculate_total()")
    ]
    assert len(symbol.callees) == 1
    assert (symbol.callees[0].file, symbol.callees[0].kind) == ("src/pricing.py", "callee")
    assert symbol.callees[0].text in {
        "def helper_one() -> int",
        "def helper_two() -> int",
        "def helper_three() -> int",
    }


def test_python_analyzer_resolves_relative_and_package_module_import_references(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "pricing.py").write_text(
        "def calculate_total(price: int, quantity: int) -> int:\n    return price * quantity\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "checkout.py").write_text(
        "from .pricing import calculate_total as total\n"
        "from app import pricing as pricing_module\n\n"
        "def checkout(price: int, quantity: int) -> int:\n"
        "    subtotal = total(price, quantity)\n"
        "    return subtotal + pricing_module.calculate_total(price, quantity)\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="app/pricing.py",
        new_path="app/pricing.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 2,
                "old_lines": 1,
                "new_start": 2,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return price"},
                    {"kind": "add", "content": "    return price * quantity"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "calculate_total"
    assert {(reference.file, reference.kind, reference.text) for reference in symbol.references} == {
        ("app/checkout.py", "call", "total(price, quantity)"),
        ("app/checkout.py", "call", "pricing_module.calculate_total(price, quantity)"),
    }


def test_python_analyzer_resolves_function_local_import_references(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "pricing.py").write_text(
        "def calculate_total(price: int, quantity: int) -> int:\n    return price * quantity\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "checkout.py").write_text(
        "def checkout(price: int, quantity: int) -> int:\n"
        "    from .pricing import calculate_total as total\n"
        "    from app import pricing as pricing_module\n\n"
        "    subtotal = total(price, quantity)\n"
        "    return subtotal + pricing_module.calculate_total(price, quantity)\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="app/pricing.py",
        new_path="app/pricing.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 2,
                "old_lines": 1,
                "new_start": 2,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return price"},
                    {"kind": "add", "content": "    return price * quantity"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "calculate_total"
    assert {(reference.file, reference.kind, reference.text) for reference in symbol.references} == {
        ("app/checkout.py", "call", "total(price, quantity)"),
        ("app/checkout.py", "call", "pricing_module.calculate_total(price, quantity)"),
    }


def test_python_analyzer_collects_method_references_from_instances_and_class_calls(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "handlers.py").write_text(
        "class ResourceHandler:\n    def handle(self, value: str) -> str:\n        return value.strip()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "routes.py").write_text(
        "from handlers import ResourceHandler\n\n"
        "def route(value: str) -> str:\n"
        "    handler = ResourceHandler()\n"
        "    return handler.handle(value)\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/handlers.py",
        new_path="src/handlers.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 3,
                "old_lines": 1,
                "new_start": 3,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "        return value"},
                    {"kind": "add", "content": "        return value.strip()"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    method = next(symbol for symbol in result.files[0].changed_symbols if symbol.name == "ResourceHandler.handle")
    assert [(reference.file, reference.kind, reference.text) for reference in method.references] == [
        ("src/routes.py", "call", "handler.handle(value)")
    ]


def test_python_analyzer_resolves_instance_references_to_imported_class_identity(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "other" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "handlers.py").write_text(
        "class ResourceHandler:\n    def handle(self, value: str) -> str:\n        return value.strip()\n",
        encoding="utf-8",
    )
    (tmp_path / "other" / "handlers.py").write_text(
        "class ResourceHandler:\n    def handle(self, value: str) -> str:\n        return value.upper()\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "routes.py").write_text(
        "from app.handlers import ResourceHandler\n\n"
        "def route(value: str) -> str:\n"
        "    handler: ResourceHandler = ResourceHandler()\n"
        "    return handler.handle(value)\n",
        encoding="utf-8",
    )
    (tmp_path / "other" / "routes.py").write_text(
        "from other.handlers import ResourceHandler\n\n"
        "def route(value: str) -> str:\n"
        "    handler = ResourceHandler()\n"
        "    return handler.handle(value)\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="app/handlers.py",
        new_path="app/handlers.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 3,
                "old_lines": 1,
                "new_start": 3,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "        return value"},
                    {"kind": "add", "content": "        return value.strip()"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    method = next(symbol for symbol in result.files[0].changed_symbols if symbol.name == "ResourceHandler.handle")
    assert [(reference.file, reference.kind, reference.text) for reference in method.references] == [
        ("app/routes.py", "call", "handler.handle(value)")
    ]


def test_python_analyzer_does_not_leak_instance_types_across_function_scopes(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "handlers.py").write_text(
        "class ResourceHandler:\n    def handle(self, value: str) -> str:\n        return value.strip()\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "routes.py").write_text(
        "from app.handlers import ResourceHandler\n\n"
        "def build(value: str) -> str:\n"
        "    handler = ResourceHandler()\n"
        "    return handler.handle(value)\n\n"
        "def route(handler, value: str) -> str:\n"
        "    return handler.handle(value)\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="app/handlers.py",
        new_path="app/handlers.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 3,
                "old_lines": 1,
                "new_start": 3,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "        return value"},
                    {"kind": "add", "content": "        return value.strip()"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    method = next(symbol for symbol in result.files[0].changed_symbols if symbol.name == "ResourceHandler.handle")
    assert [(reference.file, reference.kind, reference.text) for reference in method.references] == [
        ("app/routes.py", "call", "handler.handle(value)")
    ]


def test_python_analyzer_does_not_guess_unknown_attribute_callees(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text(
        "def get() -> int:\n    return 1\n\ndef process(client) -> int:\n    return client.get() + 1\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/service.py",
        new_path="src/service.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 5,
                "old_lines": 1,
                "new_start": 5,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return client.get()"},
                    {"kind": "add", "content": "    return client.get() + 1"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    process = result.files[0].changed_symbols[0]
    assert process.name == "process"
    assert process.callees == []


def test_python_analyzer_synthesizes_deleted_function_symbols(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "handlers.py").write_text(
        "def kept() -> bool:\n    return True\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/handlers.py",
        new_path="src/handlers.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 1,
                "old_lines": 5,
                "new_start": 1,
                "new_lines": 2,
                "lines": [
                    {"kind": "delete", "content": "def removed(value: str) -> str:"},
                    {"kind": "delete", "content": "    return value"},
                    {"kind": "delete", "content": ""},
                    {"kind": "context", "content": "def kept() -> bool:"},
                    {"kind": "context", "content": "    return True"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert [symbol.name for symbol in result.files[0].symbols] == ["kept"]
    assert [symbol.name for symbol in result.files[0].changed_symbols] == ["removed"]
    assert result.files[0].changed_symbols[0].signature == "removed Python function: def removed(value: str) -> str:"


def test_python_analyzer_synthesizes_deleted_method_symbols_with_class_context(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "handlers.py").write_text(
        "class ResourceHandler:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "routes.py").write_text(
        "from app.handlers import ResourceHandler\n\n"
        "def route(value: str) -> str:\n"
        "    handler = ResourceHandler()\n"
        "    return handler.handle(value)\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="app/handlers.py",
        new_path="app/handlers.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 1,
                "old_lines": 3,
                "new_start": 1,
                "new_lines": 2,
                "lines": [
                    {"kind": "context", "content": "class ResourceHandler:"},
                    {"kind": "delete", "content": "    def handle(self, value: str) -> str:"},
                    {"kind": "delete", "content": "        return value"},
                    {"kind": "add", "content": "    pass"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    deleted_method = next(
        symbol for symbol in result.files[0].changed_symbols if symbol.name == "ResourceHandler.handle"
    )
    assert deleted_method.kind == "method"
    assert deleted_method.signature == "removed Python method: def handle(self, value: str) -> str:"
    assert [(reference.file, reference.kind, reference.text) for reference in deleted_method.references] == [
        ("app/routes.py", "call", "handler.handle(value)")
    ]


def test_python_analyzer_marks_workspace_scan_partial_when_file_limit_is_reached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "a.py").write_text("def changed() -> bool:\n    return True\n", encoding="utf-8")
    (tmp_path / "z.py").write_text("def other() -> bool:\n    return True\n", encoding="utf-8")
    monkeypatch.setattr("apex_ray.analyzers.python.constants.PYTHON_WORKSPACE_FILE_LIMIT", 1, raising=False)
    changed = ChangedFile(
        old_path="a.py",
        new_path="a.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 2,
                "old_lines": 1,
                "new_start": 2,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return False"},
                    {"kind": "add", "content": "    return True"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert result.files[0].path == "a.py"
    assert result.partial is True
    assert any("Python workspace scan reached file limit (1)" in warning for warning in result.warnings)


def test_python_analyzer_scores_related_tests_with_import_aliases(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "pricing.py").write_text(
        "def calculate_total(price: int, quantity: int) -> int:\n    return price * quantity\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_pricing_alias.py").write_text(
        "from app.pricing import calculate_total as total\n\ndef test_total() -> None:\n    assert total(2, 3) == 6\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_pricing_name_only.py").write_text(
        "def test_name_only() -> None:\n    assert 'pricing' == 'pricing'\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="app/pricing.py",
        new_path="app/pricing.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 2,
                "old_lines": 1,
                "new_start": 2,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return price"},
                    {"kind": "add", "content": "    return price * quantity"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert result.files[0].related_tests[:2] == [
        "tests/test_pricing_alias.py",
        "tests/test_pricing_name_only.py",
    ]


def test_python_analyzer_related_test_limit_follows_constants_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "pricing.py").write_text(
        "def calculate_total(price: int, quantity: int) -> int:\n    return price * quantity\n",
        encoding="utf-8",
    )
    for name in ["a", "b", "c"]:
        (tmp_path / "tests" / f"test_pricing_{name}.py").write_text(
            "from app.pricing import calculate_total\n\n"
            f"def test_total_{name}() -> None:\n"
            "    assert calculate_total(2, 3) == 6\n",
            encoding="utf-8",
        )
    monkeypatch.setattr("apex_ray.analyzers.python.constants.PYTHON_RELATED_TEST_LIMIT", 1)
    changed = ChangedFile(
        old_path="app/pricing.py",
        new_path="app/pricing.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 2,
                "old_lines": 1,
                "new_start": 2,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return price"},
                    {"kind": "add", "content": "    return price * quantity"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    assert result.files[0].related_tests == ["tests/test_pricing_a.py"]


def test_typescript_analyzer_resolves_workspace_tsconfig_extends(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    repo = tmp_path / "repo"
    package_root = repo / "packages" / "tsconfig"
    app_root = repo / "apps" / "api"
    package_root.mkdir(parents=True)
    (app_root / "src").mkdir(parents=True)
    (package_root / "package.json").write_text(
        '{"name":"@acme/tsconfig","version":"0.0.0","private":true}\n',
        encoding="utf-8",
    )
    (package_root / "base.json").write_text(
        '{"compilerOptions":{"strict":true}}\n',
        encoding="utf-8",
    )
    (package_root / "node.json").write_text(
        '{"extends":"./base.json","compilerOptions":{"target":"ES2022","module":"NodeNext","moduleResolution":"NodeNext"}}\n',
        encoding="utf-8",
    )
    (app_root / "tsconfig.json").write_text(
        '{"extends":"@acme/tsconfig/node.json","include":["src/**/*.ts"]}\n',
        encoding="utf-8",
    )
    (app_root / "src" / "index.ts").write_text(
        "export function answer(): number {\n  return 42;\n}\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="apps/api/src/index.ts",
        new_path="apps/api/src/index.ts",
        language="typescript",
        file_kind=FileKind.SOURCE,
    )

    result = run_typescript_analyzer(repo, [changed])

    assert result is not None
    assert not any("@acme/tsconfig/node.json" in warning for warning in result.warnings)
    assert result.files[0].path == "apps/api/src/index.ts"


def test_typescript_analyzer_uses_focused_program_for_large_change_sets(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    repo = tmp_path / "repo"
    src = repo / "src"
    src.mkdir(parents=True)
    (repo / "tsconfig.json").write_text('{"compilerOptions":{"target":"ES2022"},"include":["src/**/*.ts"]}\n')
    changed: list[ChangedFile] = []
    for index in range(40):
        rel_path = f"src/file-{index}.ts"
        (repo / rel_path).write_text(f"export const value{index} = {index};\n", encoding="utf-8")
        changed.append(
            ChangedFile(
                old_path=rel_path,
                new_path=rel_path,
                language="typescript",
                file_kind=FileKind.SOURCE,
            )
        )

    result = run_typescript_analyzer(repo, changed)

    assert result is not None
    assert len(result.files) == 40
    assert any("using focused program roots" in warning for warning in result.warnings)


def test_typescript_analyzer_skips_expensive_reference_scans_for_changed_test_files(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    repo = tmp_path / "repo"
    src = repo / "src"
    src.mkdir(parents=True)
    (repo / "tsconfig.json").write_text('{"compilerOptions":{"target":"ES2022"},"include":["src/**/*.ts"]}\n')
    (src / "util.ts").write_text("export function answer(): number {\n  return 42;\n}\n", encoding="utf-8")
    (src / "util.test.ts").write_text(
        "import { answer } from './util';\nexport function testHelper(): number {\n  return answer() + 1;\n}\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/util.test.ts",
        new_path="src/util.test.ts",
        language="typescript",
        file_kind=FileKind.TEST,
        hunks=[
            {
                "old_start": 2,
                "old_lines": 3,
                "new_start": 2,
                "new_lines": 3,
                "lines": [
                    {"kind": "context", "content": "export function testHelper(): number {"},
                    {"kind": "delete", "content": "  return answer();"},
                    {"kind": "add", "content": "  return answer() + 1;"},
                    {"kind": "context", "content": "}"},
                ],
            }
        ],
    )

    result = run_typescript_analyzer(repo, [changed])

    assert result is not None
    assert result.files[0].changed_symbols
    assert all(not symbol.references for symbol in result.files[0].changed_symbols)
    assert all(not symbol.callees for symbol in result.files[0].changed_symbols)


def test_typescript_analyzer_returns_partial_result_when_a_shard_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("console.log('{}')\n", encoding="utf-8")
    changed = [
        ChangedFile(
            old_path=f"src/file-{index}.ts",
            new_path=f"src/file-{index}.ts",
            language="typescript",
            file_kind=FileKind.SOURCE,
        )
        for index in range(3)
    ]
    seen_shards: list[list[str]] = []

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        changed_index = args.index("--changed") + 1
        option_index = next(
            (index for index in range(changed_index, len(args)) if args[index].startswith("--")),
            len(args),
        )
        shard_files = args[changed_index:option_index]
        seen_shards.append(shard_files)
        if shard_files == ["src/file-1.ts"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
        payload = {
            "language": "typescript",
            "projectRoot": str(tmp_path),
            "tsconfigPath": None,
            "files": [{"path": path, "symbols": [], "imports": [], "exports": []} for path in shard_files],
            "warnings": [f"warning for {shard_files[0]}"],
            "indexCache": None,
        }
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("apex_ray.analyzers.typescript._run_analyzer_process", fake_run)

    result = run_typescript_analyzer(
        tmp_path,
        changed,
        AnalyzerConfig(script_path=str(script), timeout_seconds=1, changed_file_shard_size=1),
    )

    assert result is not None
    assert seen_shards == [["src/file-0.ts"], ["src/file-1.ts"], ["src/file-2.ts"]]
    assert [file.path for file in result.files] == ["src/file-0.ts", "src/file-2.ts"]
    assert "warning for src/file-0.ts" in result.warnings
    assert "warning for src/file-2.ts" in result.warnings
    assert any("partial TypeScript analyzer result" in warning for warning in result.warnings)
    assert any("src/file-1.ts" in warning and "timed out after 1s" in warning for warning in result.warnings)
    assert result.partial is True
    assert result.failed_files == ["src/file-1.ts"]
    assert len(result.shard_failures) == 1
    assert result.shard_failures[0].status == "timeout"
    assert result.shard_failures[0].files == ["src/file-1.ts"]


def test_typescript_analyzer_respects_total_timeout_across_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("console.log('{}')\n", encoding="utf-8")
    changed = [
        ChangedFile(
            old_path=f"src/file-{index}.ts",
            new_path=f"src/file-{index}.ts",
            language="typescript",
            file_kind=FileKind.SOURCE,
        )
        for index in range(3)
    ]
    seen_shards: list[list[str]] = []
    monotonic_values = iter([0.0, 0.0, 2.1])

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")
    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", lambda: next(monotonic_values))

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        changed_index = args.index("--changed") + 1
        option_index = next(
            (index for index in range(changed_index, len(args)) if args[index].startswith("--")),
            len(args),
        )
        shard_files = args[changed_index:option_index]
        seen_shards.append(shard_files)
        payload = {
            "language": "typescript",
            "projectRoot": str(tmp_path),
            "tsconfigPath": None,
            "files": [{"path": path, "symbols": [], "imports": [], "exports": []} for path in shard_files],
            "warnings": [],
            "indexCache": None,
        }
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("apex_ray.analyzers.typescript._run_analyzer_process", fake_run)

    result = run_typescript_analyzer(
        tmp_path,
        changed,
        AnalyzerConfig(script_path=str(script), timeout_seconds=2, changed_file_shard_size=1),
    )

    assert result is not None
    assert seen_shards == [["src/file-0.ts"]]
    assert [file.path for file in result.files] == ["src/file-0.ts"]
    assert any("partial TypeScript analyzer result" in warning for warning in result.warnings)
    assert any("total timeout after 2s" in warning for warning in result.warnings)
    assert result.partial is True
    assert result.failed_files == ["src/file-1.ts", "src/file-2.ts"]


def test_typescript_analyzer_scales_total_timeout_for_large_adaptive_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("console.log('{}')\n", encoding="utf-8")
    changed = [
        ChangedFile(
            old_path=f"src/file-{index}.ts",
            new_path=f"src/file-{index}.ts",
            language="typescript",
            file_kind=FileKind.SOURCE,
        )
        for index in range(5)
    ]
    seen_shards: list[list[str]] = []
    seen_timeouts: list[float] = []
    monotonic_values = iter([0.0, 0.0, 2.1, 4.1])

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")
    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", lambda: next(monotonic_values))

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        changed_index = args.index("--changed") + 1
        option_index = next(
            (index for index in range(changed_index, len(args)) if args[index].startswith("--")),
            len(args),
        )
        shard_files = args[changed_index:option_index]
        seen_shards.append(shard_files)
        seen_timeouts.append(timeout)
        payload = {
            "language": "typescript",
            "projectRoot": str(tmp_path),
            "tsconfigPath": None,
            "files": [{"path": path, "symbols": [], "imports": [], "exports": []} for path in shard_files],
            "warnings": [],
            "indexCache": None,
        }
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("apex_ray.analyzers.typescript._run_analyzer_process", fake_run)

    result = run_typescript_analyzer(
        tmp_path,
        changed,
        AnalyzerConfig(
            script_path=str(script),
            timeout_seconds=2,
            changed_file_shard_size=10,
            adaptive_sharding=True,
            large_change_file_threshold=5,
            large_change_shard_size=2,
        ),
    )

    assert result is not None
    assert seen_shards == [
        ["src/file-0.ts", "src/file-1.ts"],
        ["src/file-2.ts", "src/file-3.ts"],
        ["src/file-4.ts"],
    ]
    assert seen_timeouts[:2] == [2, 2]
    assert seen_timeouts[2] == pytest.approx(1.9)
    assert result.partial is False


def test_typescript_analyzer_caps_scaled_total_timeout_for_large_adaptive_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("console.log('{}')\n", encoding="utf-8")
    changed = [
        ChangedFile(
            old_path=f"src/file-{index}.ts",
            new_path=f"src/file-{index}.ts",
            language="typescript",
            file_kind=FileKind.SOURCE,
        )
        for index in range(9)
    ]
    seen_shards: list[list[str]] = []
    seen_timeouts: list[float] = []
    monotonic_values = iter([0.0, 0.0, 2.1, 4.1, 6.1, 8.1])

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")
    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", lambda: next(monotonic_values))

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        changed_index = args.index("--changed") + 1
        option_index = next(
            (index for index in range(changed_index, len(args)) if args[index].startswith("--")),
            len(args),
        )
        shard_files = args[changed_index:option_index]
        seen_shards.append(shard_files)
        seen_timeouts.append(timeout)
        payload = {
            "language": "typescript",
            "projectRoot": str(tmp_path),
            "tsconfigPath": None,
            "files": [{"path": path, "symbols": [], "imports": [], "exports": []} for path in shard_files],
            "warnings": [],
            "indexCache": None,
        }
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("apex_ray.analyzers.typescript._run_analyzer_process", fake_run)

    result = run_typescript_analyzer(
        tmp_path,
        changed,
        AnalyzerConfig(
            script_path=str(script),
            timeout_seconds=2,
            changed_file_shard_size=20,
            adaptive_sharding=True,
            large_change_file_threshold=5,
            large_change_shard_size=2,
        ),
    )

    assert result is not None
    assert seen_shards == [
        ["src/file-0.ts", "src/file-1.ts"],
        ["src/file-2.ts", "src/file-3.ts"],
        ["src/file-4.ts", "src/file-5.ts"],
        ["src/file-6.ts", "src/file-7.ts"],
    ]
    assert seen_timeouts[:3] == [2, 2, 2]
    assert seen_timeouts[3] == pytest.approx(1.9)
    assert any("total timeout after 8s" in warning for warning in result.warnings)
    assert result.partial is True
    assert result.failed_files == ["src/file-8.ts"]


def test_typescript_analyzer_adaptive_sharding_uses_smaller_large_change_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("console.log('{}')\n", encoding="utf-8")
    changed = [
        ChangedFile(
            old_path=f"src/file-{index}.ts",
            new_path=f"src/file-{index}.ts",
            language="typescript",
            file_kind=FileKind.SOURCE,
        )
        for index in range(5)
    ]
    seen_shards: list[list[str]] = []

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        changed_index = args.index("--changed") + 1
        option_index = next(
            (index for index in range(changed_index, len(args)) if args[index].startswith("--")),
            len(args),
        )
        shard_files = args[changed_index:option_index]
        seen_shards.append(shard_files)
        payload = {
            "language": "typescript",
            "projectRoot": str(tmp_path),
            "tsconfigPath": None,
            "files": [{"path": path, "symbols": [], "imports": [], "exports": []} for path in shard_files],
            "warnings": [],
            "indexCache": None,
        }
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("apex_ray.analyzers.typescript._run_analyzer_process", fake_run)

    result = run_typescript_analyzer(
        tmp_path,
        changed,
        AnalyzerConfig(
            script_path=str(script),
            changed_file_shard_size=10,
            adaptive_sharding=True,
            large_change_file_threshold=5,
            large_change_shard_size=2,
        ),
    )

    assert result is not None
    assert seen_shards == [
        ["src/file-0.ts", "src/file-1.ts"],
        ["src/file-2.ts", "src/file-3.ts"],
        ["src/file-4.ts"],
    ]


def test_typescript_analyzer_raises_when_all_shards_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("console.log('{}')\n", encoding="utf-8")
    changed = [
        ChangedFile(
            old_path=f"src/file-{index}.ts",
            new_path=f"src/file-{index}.ts",
            language="typescript",
            file_kind=FileKind.SOURCE,
        )
        for index in range(2)
    ]

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

    monkeypatch.setattr("apex_ray.analyzers.typescript._run_analyzer_process", fake_run)

    with pytest.raises(AnalyzerError) as exc:
        run_typescript_analyzer(
            tmp_path,
            changed,
            AnalyzerConfig(script_path=str(script), changed_file_shard_size=1),
        )

    assert "failed for all shards" in str(exc.value)
    assert "boom" in str(exc.value)


def test_typescript_analyzer_timeout_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("setTimeout(() => {}, 10000)\n", encoding="utf-8")
    changed = ChangedFile(
        old_path="src/cart.ts",
        new_path="src/cart.ts",
        language="typescript",
        file_kind=FileKind.SOURCE,
    )

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["node", str(script)], timeout=1)

    monkeypatch.setattr("apex_ray.analyzers.typescript._run_analyzer_process", fake_run)

    with pytest.raises(AnalyzerError) as exc:
        run_typescript_analyzer(
            tmp_path,
            [changed],
            AnalyzerConfig(script_path=str(script), timeout_seconds=1),
        )

    assert "timed out after 1s" in str(exc.value)
