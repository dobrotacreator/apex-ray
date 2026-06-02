import json
import subprocess
from pathlib import Path

import pytest

from apex_ray.analyzers import (
    AnalyzerError,
    run_analyzers,
    run_python_analyzer,
    run_typescript_analyzer,
    typescript_analyzer_script,
)
from apex_ray.models import AnalyzerConfig, AnalyzerFile, AnalyzerResult, ChangedFile, FileKind


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
    monkeypatch.setattr("apex_ray.analyzers.shutil.which", lambda name: "/usr/bin/node")

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

    monkeypatch.setattr("apex_ray.analyzers._run_analyzer_process", fake_run)

    result = run_typescript_analyzer(repo, [changed], AnalyzerConfig(script_path="tools/analyze.js"))

    assert result is not None
    assert seen_command is not None
    assert seen_command[1] == str(script.resolve())


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
    assert result.backend_runs[1].name == "python"
    assert result.backend_runs[1].changed_files_count == 1


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

    monkeypatch.setattr("apex_ray.analyzers.shutil.which", lambda name: "/usr/bin/node")

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

    monkeypatch.setattr("apex_ray.analyzers._run_analyzer_process", fake_run)

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

    monkeypatch.setattr("apex_ray.analyzers.shutil.which", lambda name: "/usr/bin/node")
    monkeypatch.setattr("apex_ray.analyzers.time.monotonic", lambda: next(monotonic_values))

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

    monkeypatch.setattr("apex_ray.analyzers._run_analyzer_process", fake_run)

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

    monkeypatch.setattr("apex_ray.analyzers.shutil.which", lambda name: "/usr/bin/node")

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

    monkeypatch.setattr("apex_ray.analyzers._run_analyzer_process", fake_run)

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

    monkeypatch.setattr("apex_ray.analyzers.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

    monkeypatch.setattr("apex_ray.analyzers._run_analyzer_process", fake_run)

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

    monkeypatch.setattr("apex_ray.analyzers.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["node", str(script)], timeout=1)

    monkeypatch.setattr("apex_ray.analyzers._run_analyzer_process", fake_run)

    with pytest.raises(AnalyzerError) as exc:
        run_typescript_analyzer(
            tmp_path,
            [changed],
            AnalyzerConfig(script_path=str(script), timeout_seconds=1),
        )

    assert "timed out after 1s" in str(exc.value)
