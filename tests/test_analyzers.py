import json
import subprocess
from pathlib import Path

import pytest

from apex_ray.analyzers import AnalyzerError, run_typescript_analyzer, typescript_analyzer_script
from apex_ray.models import AnalyzerConfig, ChangedFile, FileKind


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
