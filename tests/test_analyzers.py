import json
import os
import shutil
import subprocess
import time
import tracemalloc
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from threading import Event

import pytest

import apex_ray.analyzers.go as go_analyzer_module
import apex_ray.analyzers.typescript as typescript_analyzer_module
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
from apex_ray.models import (
    AnalyzerConfig,
    AnalyzerFile,
    AnalyzerResult,
    ChangedFile,
    FileKind,
    RiskSeverity,
    RiskSignal,
)


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


def test_typescript_merge_deduplicates_global_warnings_and_preserves_occurrences() -> None:
    first = AnalyzerResult(
        language="typescript",
        projectRoot="/repo",
        files=[],
        warnings=["workspace index is partial", "first shard only"],
        partial=True,
    )
    second = AnalyzerResult(
        language="typescript",
        projectRoot="/repo",
        files=[],
        warnings=["workspace index is partial", "workspace index is partial"],
        partial=True,
    )

    merged = typescript_analyzer_module._merge_analyzer_results([first, second])

    assert merged.warnings == ["workspace index is partial", "first shard only"]
    assert [summary.model_dump(mode="json") for summary in merged.warning_summaries] == [
        {
            "message": "workspace index is partial",
            "occurrences": 3,
            "shard_indexes": [1, 2],
        },
        {
            "message": "first shard only",
            "occurrences": 1,
            "shard_indexes": [1],
        },
    ]


def test_typescript_merge_aggregates_structured_coverage_and_shard_metrics() -> None:
    first = AnalyzerResult.model_validate(
        {
            "language": "typescript",
            "projectRoot": "/repo",
            "files": [{"path": "src/first.ts"}],
            "warnings": ["workspace index is partial"],
            "partial": True,
            "coverage": {
                "partial": True,
                "reasonCodes": ["workspace_index_partial"],
                "scopes": ["workspace_index"],
                "failedFileCount": 0,
            },
            "metrics": {
                "wallDurationMs": 7,
                "stageDurationsMs": {"workspace_index": 3, "changed_files": 4},
                "shards": [
                    {
                        "index": 1,
                        "total": 1,
                        "status": "partial",
                        "wallDurationMs": 7,
                        "stageDurationsMs": {"workspace_index": 3, "changed_files": 4},
                        "changedFileCount": 1,
                        "analyzedFileCount": 1,
                        "failedFileCount": 0,
                        "warningCount": 1,
                        "partialReasonCodes": ["workspace_index_partial"],
                        "indexCacheHits": 2,
                        "indexCacheMisses": 3,
                    }
                ],
            },
        }
    )
    second = AnalyzerResult.model_validate(
        {
            "language": "typescript",
            "projectRoot": "/repo",
            "files": [],
            "warnings": ["analysis budget exhausted"],
            "partial": True,
            "failedFiles": ["src/second.ts"],
            "coverage": {
                "partial": True,
                "reasonCodes": ["analysis_time_budget_exhausted", "changed_file_analysis_incomplete"],
                "scopes": ["analyzer", "changed_files"],
                "failedFileCount": 1,
            },
            "metrics": {
                "wallDurationMs": 11,
                "stageDurationsMs": {"workspace_index": 5, "changed_files": 6},
                "shards": [
                    {
                        "index": 1,
                        "total": 1,
                        "status": "timeout",
                        "wallDurationMs": 11,
                        "stageDurationsMs": {"workspace_index": 5, "changed_files": 6},
                        "changedFileCount": 1,
                        "analyzedFileCount": 0,
                        "failedFileCount": 1,
                        "warningCount": 1,
                        "partialReasonCodes": [
                            "analysis_time_budget_exhausted",
                            "changed_file_analysis_incomplete",
                        ],
                    }
                ],
            },
        }
    )

    merged = typescript_analyzer_module._merge_analyzer_results([first, second])

    assert merged.coverage is not None
    assert merged.coverage.partial is True
    assert merged.coverage.reason_codes == [
        "workspace_index_partial",
        "analysis_time_budget_exhausted",
        "changed_file_analysis_incomplete",
    ]
    assert merged.coverage.scopes == ["workspace_index", "analyzer", "changed_files"]
    assert merged.coverage.failed_file_count == 1
    assert merged.metrics is not None
    assert merged.metrics.wall_duration_ms == 18
    assert merged.metrics.stage_durations_ms == {"workspace_index": 8, "changed_files": 10}
    assert [(shard.index, shard.total) for shard in merged.metrics.shards] == [(1, 2), (2, 2)]
    assert [shard.status for shard in merged.metrics.shards] == ["partial", "timeout"]


def test_typescript_analyzer_fallback_retains_custom_config_extends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    source = repo / "src" / "cart.ts"
    base_config = repo / "configs" / "base.custom"
    final_config = repo / "configs" / "final.rules"
    unrelated_config = repo / "configs" / "unrelated.private"
    script = runtime / "analyze.js"
    source.parent.mkdir(parents=True)
    base_config.parent.mkdir(parents=True)
    runtime.mkdir()
    source.write_text("export const cart = true;\n", encoding="utf-8")
    (repo / "tsconfig.json").write_text(
        '{"extends":"./configs/base.custom","include":["src/**/*.ts"]}\n',
        encoding="utf-8",
    )
    base_config.write_text('{"extends":"./final.rules"}\n', encoding="utf-8")
    final_config.write_text('{"compilerOptions":{"strict":true}}\n', encoding="utf-8")
    unrelated_config.write_text('{"compilerOptions":{"strict":false}}\n', encoding="utf-8")
    script.write_text("console.log('{}')\n", encoding="utf-8")
    changed = ChangedFile(
        old_path="src/cart.ts",
        new_path="src/cart.ts",
        language="typescript",
        file_kind=FileKind.SOURCE,
    )
    seen_manifest: dict[str, object] | None = None

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_manifest
        manifest_index = args.index("--file-manifest") + 1
        seen_manifest = json.loads(Path(args[manifest_index]).read_text(encoding="utf-8"))
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

    result = run_typescript_analyzer(
        repo,
        [changed],
        AnalyzerConfig(script_path=str(script)),
    )

    assert result is not None
    assert seen_manifest is not None
    assert seen_manifest["config_files"] == [
        "configs/base.custom",
        "configs/final.rules",
        "tsconfig.json",
    ]
    assert "configs/unrelated.private" not in seen_manifest["config_files"]


def test_typescript_analyzer_fallback_rejects_gitignored_custom_config_extends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    source = repo / "src" / "cart.ts"
    private_config = repo / "configs" / "private.custom"
    script = runtime / "analyze.js"
    source.parent.mkdir(parents=True)
    private_config.parent.mkdir(parents=True)
    runtime.mkdir()
    source.write_text("export const cart = true;\n", encoding="utf-8")
    (repo / "tsconfig.json").write_text(
        '{"extends":"./configs/private.custom","include":["src/**/*.ts"]}\n',
        encoding="utf-8",
    )
    private_config.write_text('{"compilerOptions":{"strict":true}}\n', encoding="utf-8")
    (repo / ".gitignore").write_text("configs/private.custom\n", encoding="utf-8")
    script.write_text("console.log('{}')\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "add", ".gitignore", "src/cart.ts", "tsconfig.json"],
        cwd=repo,
        check=True,
    )
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", "configs/private.custom"],
        cwd=repo,
        check=False,
    )
    assert ignored.returncode == 0
    changed = ChangedFile(
        old_path="src/cart.ts",
        new_path="src/cart.ts",
        language="typescript",
        file_kind=FileKind.SOURCE,
    )
    seen_manifest: dict[str, object] | None = None

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_manifest
        manifest_index = args.index("--file-manifest") + 1
        seen_manifest = json.loads(Path(args[manifest_index]).read_text(encoding="utf-8"))
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

    result = run_typescript_analyzer(
        repo,
        [changed],
        AnalyzerConfig(script_path=str(script)),
    )

    assert result is not None
    assert seen_manifest is not None
    assert seen_manifest["config_files"] == ["tsconfig.json"]


def test_typescript_analyzer_fallback_retains_tracked_ignored_package_config_extends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    source = repo / "src" / "cart.ts"
    package = repo / "packages" / "review-config"
    exports_package = repo / "packages" / "export-review"
    conventional_package = repo / "packages" / "conventional-review"
    base_config = package / "base.custom"
    nested_config = package / "nested.rules"
    exported_config = exports_package / "strict.custom"
    exported_nested_config = exports_package / "nested.rules"
    conventional_config = conventional_package / "tsconfig"
    conventional_nested_config = conventional_package / "nested.rules"
    script = runtime / "analyze.js"
    source.parent.mkdir(parents=True)
    package.mkdir(parents=True)
    exports_package.mkdir(parents=True)
    conventional_package.mkdir(parents=True)
    runtime.mkdir()
    source.write_text("export const cart = true;\n", encoding="utf-8")
    (repo / "tsconfig.json").write_text(
        ('{"extends":["review-config","export-review/strict","conventional-review"],"include":["src/**/*.ts"]}\n'),
        encoding="utf-8",
    )
    (package / "package.json").write_text(
        '{"name":"review-config","tsconfig":"./base.custom"}\n',
        encoding="utf-8",
    )
    base_config.write_text('{"extends":"./nested.rules"}\n', encoding="utf-8")
    nested_config.write_text('{"compilerOptions":{"strict":true}}\n', encoding="utf-8")
    (exports_package / "package.json").write_text(
        '{"name":"export-review","exports":{"./strict":"./strict.custom"}}\n',
        encoding="utf-8",
    )
    exported_config.write_text('{"extends":"./nested.rules"}\n', encoding="utf-8")
    exported_nested_config.write_text("{}\n", encoding="utf-8")
    (conventional_package / "package.json").write_text(
        '{"name":"conventional-review"}\n',
        encoding="utf-8",
    )
    conventional_config.write_text('{"extends":"./nested.rules"}\n', encoding="utf-8")
    conventional_nested_config.write_text("{}\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "packages/review-config/base.custom\n",
        encoding="utf-8",
    )
    script.write_text("console.log('{}')\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "add",
            ".gitignore",
            "src/cart.ts",
            "tsconfig.json",
            "packages/conventional-review/nested.rules",
            "packages/conventional-review/package.json",
            "packages/conventional-review/tsconfig",
            "packages/export-review/nested.rules",
            "packages/export-review/package.json",
            "packages/export-review/strict.custom",
            "packages/review-config/package.json",
            "packages/review-config/nested.rules",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "add", "-f", "packages/review-config/base.custom"],
        cwd=repo,
        check=True,
    )
    changed = ChangedFile(
        old_path="src/cart.ts",
        new_path="src/cart.ts",
        language="typescript",
        file_kind=FileKind.SOURCE,
    )
    seen_manifest: dict[str, object] | None = None

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_manifest
        manifest_index = args.index("--file-manifest") + 1
        seen_manifest = json.loads(Path(args[manifest_index]).read_text(encoding="utf-8"))
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

    result = run_typescript_analyzer(
        repo,
        [changed],
        AnalyzerConfig(script_path=str(script)),
    )

    assert result is not None
    assert seen_manifest is not None
    assert seen_manifest["package_files"] == [
        "packages/conventional-review/package.json",
        "packages/export-review/package.json",
        "packages/review-config/package.json",
    ]
    assert seen_manifest["config_files"] == [
        "packages/conventional-review/nested.rules",
        "packages/conventional-review/package.json",
        "packages/conventional-review/tsconfig",
        "packages/export-review/nested.rules",
        "packages/export-review/package.json",
        "packages/export-review/strict.custom",
        "packages/review-config/base.custom",
        "packages/review-config/nested.rules",
        "packages/review-config/package.json",
        "tsconfig.json",
    ]


def test_typescript_fallback_does_not_read_ignored_custom_config_extends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "files.json"
    (tmp_path / "tsconfig.json").write_text(
        '{"extends":"./ignored/private.custom"}\n',
        encoding="utf-8",
    )
    private_config = tmp_path / "ignored" / "private.custom"
    private_config.parent.mkdir()
    private_config.write_text('{"extends":"./nested.rules"}\n', encoding="utf-8")
    (tmp_path / "ignored" / "nested.rules").write_text("{}\n", encoding="utf-8")
    original_read = typescript_analyzer_module._read_inventory_config
    reads: list[Path] = []

    def record_read(
        repo_root: Path,
        relative_path: Path,
        *,
        check_deadline: Callable[[], None] = lambda: None,
    ) -> str | None:
        reads.append(relative_path)
        return original_read(
            repo_root,
            relative_path,
            check_deadline=check_deadline,
        )

    monkeypatch.setattr(typescript_analyzer_module, "_read_inventory_config", record_read)

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        ignored_patterns=["ignored/**"],
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["config_files"] == ["tsconfig.json"]
    assert Path("ignored/private.custom") not in reads
    assert Path("ignored/nested.rules") not in reads


def test_typescript_fallback_missing_config_candidates_do_not_spawn_git_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_extends = [f"./configs/missing-{index:04d}.custom" for index in range(1_000)]
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"extends": missing_extends}),
        encoding="utf-8",
    )
    popen_calls = 0

    class VisibleGitProcess:
        returncode = 1

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            raise AssertionError("completed Git process must not be killed")

    def fake_popen(*args: object, **kwargs: object) -> VisibleGitProcess:
        nonlocal popen_calls
        popen_calls += 1
        return VisibleGitProcess()

    monkeypatch.setattr(
        typescript_analyzer_module.subprocess,
        "Popen",
        fake_popen,
    )

    inventory = typescript_analyzer_module._TypescriptInventory(
        paths=[Path("tsconfig.json")],
        partial_reason=None,
    )
    retained = typescript_analyzer_module._retain_fallback_config_extends(
        tmp_path,
        inventory,
        [],
        git_backed=True,
        check_deadline=lambda: None,
    )

    assert retained.paths == [Path("tsconfig.json")]
    assert popen_calls == 0


def test_typescript_fallback_batches_existing_config_visibility_in_one_git_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_directory = tmp_path / "configs"
    config_directory.mkdir()
    extends_values: list[str] = []
    for index in range(1_000):
        relative_path = f"configs/base-{index:04d}.custom"
        (tmp_path / relative_path).write_text("{}\n", encoding="utf-8")
        extends_values.append(f"./{relative_path}")
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"extends": extends_values}),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    real_popen = typescript_analyzer_module.subprocess.Popen
    popen_calls = 0

    def counting_popen(
        args: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.Popen[bytes]:
        nonlocal popen_calls
        popen_calls += 1
        return real_popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(
        typescript_analyzer_module.subprocess,
        "Popen",
        counting_popen,
    )
    inventory = typescript_analyzer_module._TypescriptInventory(
        paths=[Path("tsconfig.json")],
        partial_reason=None,
    )

    retained = typescript_analyzer_module._retain_fallback_config_extends(
        tmp_path,
        inventory,
        [],
        git_backed=True,
        check_deadline=lambda: None,
    )

    assert len(retained.paths) == 1_001
    assert popen_calls == 1


def test_typescript_fallback_git_visibility_has_an_independent_process_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0

    def advancing_clock() -> float:
        nonlocal clock
        clock += 0.6
        return clock

    class StalledGitProcess:
        returncode: int | None = None
        killed = False
        stopped = Event()

        class Stdin:
            writes = 0

            def write(self, data: bytes) -> int:
                self.writes += 1
                return len(data)

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        class Stdout:
            def __init__(self, stopped: Event) -> None:
                self.stopped = stopped

            def read(self, size: int = -1) -> bytes:
                self.stopped.wait(timeout=5)
                return b""

            def close(self) -> None:
                self.stopped.set()

        def __init__(self) -> None:
            self.stdin = self.Stdin()
            self.stdout = self.Stdout(self.stopped)

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            if self.killed:
                self.returncode = -9
                return self.returncode
            raise subprocess.TimeoutExpired(["git", "check-ignore"], timeout or 0.0)

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.stopped.set()

    process = StalledGitProcess()
    monkeypatch.setattr(
        typescript_analyzer_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        typescript_analyzer_module.time,
        "monotonic",
        advancing_clock,
    )

    visible = typescript_analyzer_module._git_fallback_config_path_visible(
        tmp_path,
        "configs/base.custom",
        check_deadline=lambda: None,
    )

    assert visible is None
    assert process.killed is True
    assert process.stdin.writes == 1


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ((b"", b"", b""), True),
        ((b".gitignore", b"1", b"configs/*.custom"), False),
        ((b".gitignore", b"2", b"!configs/base.custom"), True),
        ((b".gitignore", b"3", b"\\!literal.custom"), False),
    ],
)
def test_typescript_fallback_git_visibility_parses_coherent_protocol_records(
    metadata: tuple[bytes, bytes, bytes],
    expected: bool,
) -> None:
    path = b"configs/base.custom"
    stdin = BytesIO()
    stdout = BytesIO(b"\0".join((*metadata, path, b"")))

    visible = typescript_analyzer_module._GitFallbackConfigVisibilityChecker._exchange(
        stdin,
        stdout,
        path,
    )

    assert visible is expected
    assert stdin.getvalue() == path + b"\0"


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param(
            (b"garbage-source", b"not-a-line", b""),
            id="forged-empty-pattern",
        ),
        pytest.param(
            (b"", b"", b"!fabricated-negation"),
            id="forged-negation-without-source",
        ),
        pytest.param((b".gitignore", b"", b"*.custom"), id="missing-line"),
        pytest.param((b"", b"1", b"*.custom"), id="missing-source"),
        pytest.param((b".gitignore", b"1", b""), id="missing-pattern"),
        pytest.param((b".gitignore", b"0", b"*.custom"), id="zero-line"),
        pytest.param((b".gitignore", b"01", b"*.custom"), id="noncanonical-line"),
        pytest.param((b".gitignore", b"-1", b"*.custom"), id="negative-line"),
    ],
)
def test_typescript_fallback_git_visibility_rejects_malformed_protocol_records(
    metadata: tuple[bytes, bytes, bytes],
) -> None:
    path = b"configs/base.custom"
    stdin = BytesIO()
    stdout = BytesIO(b"\0".join((*metadata, path, b"")))

    visible = typescript_analyzer_module._GitFallbackConfigVisibilityChecker._exchange(
        stdin,
        stdout,
        path,
    )

    assert visible is None
    assert stdin.getvalue() == path + b"\0"


def test_typescript_fallback_retains_git_negated_custom_config_extends(
    tmp_path: Path,
) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "base.custom").write_text("{}\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"extends":"./configs/base.custom"}\n',
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(
        "configs/*.custom\n!configs/base.custom\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    inventory = typescript_analyzer_module._TypescriptInventory(
        paths=[Path("tsconfig.json")],
        partial_reason=None,
    )

    retained = typescript_analyzer_module._retain_fallback_config_extends(
        tmp_path,
        inventory,
        [],
        git_backed=True,
        check_deadline=lambda: None,
    )

    assert retained.paths == [
        Path("tsconfig.json"),
        Path("configs/base.custom"),
    ]


def test_typescript_fallback_caps_custom_config_extends_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_FILE_MANIFEST_ENTRY_LIMIT", 2)
    manifest_path = tmp_path / "files.json"
    (tmp_path / "tsconfig.json").write_text(
        '{"extends":"./configs/base.custom"}\n',
        encoding="utf-8",
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "base.custom").write_text(
        '{"extends":"./nested.rules"}\n',
        encoding="utf-8",
    )
    (config_dir / "nested.rules").write_text(
        '{"extends":"./unbounded.private"}\n',
        encoding="utf-8",
    )
    (config_dir / "unbounded.private").write_text("{}\n", encoding="utf-8")

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["config_files"] == [
        "configs/base.custom",
        "tsconfig.json",
    ]
    assert "fallback config discovery reached the 2 relevant-file safety limit" in payload["partial_reason"]


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
    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", lambda: 100.0)

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


def test_typescript_analyzer_manifest_respects_project_ignore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("console.log('{}')\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "vendor").mkdir()
    (tmp_path / "generated").mkdir()
    (tmp_path / "src" / "cart.ts").write_text("export const cart = true;\n", encoding="utf-8")
    (tmp_path / "src" / "globals.d.ts").write_text(
        "declare function charge(accountId: string): Promise<void>;\n",
        encoding="utf-8",
    )
    (tmp_path / "vendor" / "generated.ts").write_text("export const generated = true;\n", encoding="utf-8")
    (tmp_path / "vendor" / "generated.d.ts").write_text(
        "declare function generated(): void;\n",
        encoding="utf-8",
    )
    (tmp_path / "generated" / "client.ts").write_text("export const client = true;\n", encoding="utf-8")
    changed = ChangedFile(
        old_path="src/cart.ts",
        new_path="src/cart.ts",
        language="typescript",
        file_kind=FileKind.SOURCE,
    )
    seen_manifest: dict[str, object] | None = None

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_manifest
        manifest_index = args.index("--file-manifest") + 1
        seen_manifest = json.loads(Path(args[manifest_index]).read_text(encoding="utf-8"))
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
        AnalyzerConfig(script_path=str(script)),
        ignored_patterns=["vendor/**", "**/generated/**"],
    )

    assert result is not None
    assert seen_manifest is not None
    assert seen_manifest["version"] == 2
    assert sorted(seen_manifest["files"]) == ["analyze.js", "src/cart.ts", "src/globals.d.ts"]
    assert seen_manifest["package_files"] == []
    assert seen_manifest["config_files"] == []


def test_typescript_manifest_includes_modern_module_and_declaration_extensions(tmp_path: Path) -> None:
    manifest_path = tmp_path / "files.json"
    project_files = [
        Path("src/browser.mjs"),
        Path("src/worker.cjs"),
        Path("src/service.mts"),
        Path("src/config.cts"),
        Path("src/public-api.d.mts"),
        Path("src/legacy-api.d.cts"),
        Path("README.md"),
    ]

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=project_files,
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "files": sorted(path.as_posix() for path in project_files[:-1]),
        "package_files": [],
        "config_files": [],
    }


def test_typescript_manifest_includes_package_and_config_metadata(tmp_path: Path) -> None:
    manifest_path = tmp_path / "files.json"
    project_files = [
        Path("src/service.ts"),
        Path("package.json"),
        Path("tsconfig.json"),
        Path("configs/tsconfig.shared.json"),
        Path("README.md"),
    ]

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=project_files,
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "files": ["src/service.ts"],
        "package_files": ["package.json"],
        "config_files": [
            "configs/tsconfig.shared.json",
            "package.json",
            "tsconfig.json",
        ],
    }


def test_typescript_manifest_follows_recursive_relative_jsonc_and_extensionless_config_extends(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "files.json"
    config_files = {
        Path("tsconfig.json"): """\
{
  // Comments and trailing commas are valid in TypeScript configs.
  "compilerOptions": {
    "baseUrl": "https://example.test/*not-a-comment*/",
  },
  "extends": "./configs/base.jsonc",
}
""",
        Path("configs/base.jsonc"): """\
{
  "description": "// still part of a string",
  "extends": "./extensionless",
}
""",
        Path("configs/extensionless"): """\
{
  "description": "escaped quote: \\" /* still a string */",
  "extends": "./final.rules",
}
""",
        Path("configs/final.rules"): """\
{
  "compilerOptions": {
    "strict": true,
  },
}
""",
    }
    for relative_path, content in config_files.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    source_path = Path("src/service.ts")
    (tmp_path / source_path).parent.mkdir(parents=True)
    (tmp_path / source_path).write_text("export const service = true;\n", encoding="utf-8")
    project_files = [source_path, *config_files]

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=project_files,
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "files": ["src/service.ts"],
        "package_files": [],
        "config_files": [
            "configs/base.jsonc",
            "configs/extensionless",
            "configs/final.rules",
            "tsconfig.json",
        ],
    }


def test_typescript_manifest_follows_package_exports_for_root_subpath_and_all_conditions(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "files.json"
    project_content = {
        Path("tsconfig.json"): json.dumps(
            {
                "extends": [
                    "@acme/review-config/strict",
                    "root-review-config",
                ]
            }
        ),
        Path("packages/review-config/package.json"): json.dumps(
            {
                "name": "@acme/review-config",
                "exports": {
                    "./strict": {
                        "types": "./configs/strict.rules",
                        "default": [
                            "./configs/default.custom",
                            "./../outside.custom",
                        ],
                    }
                },
            }
        ),
        Path("packages/review-config/configs/strict.rules"): "{}",
        Path("packages/review-config/configs/default.custom"): "{}",
        Path("packages/outside.custom"): "{}",
        Path("packages/root-review-config/package.json"): json.dumps(
            {
                "name": "root-review-config",
                "exports": {
                    "types": "./configs/types.custom",
                    "default": ["./configs/default.rules"],
                },
            }
        ),
        Path("packages/root-review-config/configs/types.custom"): "{}",
        Path("packages/root-review-config/configs/default.rules"): "{}",
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=list(project_content),
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["config_files"] == [
        "packages/review-config/configs/default.custom",
        "packages/review-config/configs/strict.rules",
        "packages/review-config/package.json",
        "packages/root-review-config/configs/default.rules",
        "packages/root-review-config/configs/types.custom",
        "packages/root-review-config/package.json",
        "tsconfig.json",
    ]


def test_typescript_manifest_follows_package_tsconfig_and_conventional_config_candidates(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "files.json"
    project_content = {
        Path("tsconfig.json"): json.dumps(
            {
                "extends": [
                    "tsconfig-field",
                    "conventional-root",
                    "conventional-subpath/strict",
                ]
            }
        ),
        Path("packages/tsconfig-field/package.json"): json.dumps(
            {
                "name": "tsconfig-field",
                "tsconfig": "configs/base.custom",
            }
        ),
        Path("packages/tsconfig-field/configs/base.custom"): "{}",
        Path("packages/conventional-root/package.json"): '{"name":"conventional-root"}',
        Path("packages/conventional-root/tsconfig.json"): '{"extends":"./nested.rules"}',
        Path("packages/conventional-root/nested.rules"): "{}",
        Path("packages/conventional-subpath/package.json"): '{"name":"conventional-subpath"}',
        Path("packages/conventional-subpath/strict.json"): '{"extends":"./nested.custom"}',
        Path("packages/conventional-subpath/nested.custom"): "{}",
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=list(project_content),
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["config_files"] == [
        "packages/conventional-root/nested.rules",
        "packages/conventional-root/package.json",
        "packages/conventional-root/tsconfig.json",
        "packages/conventional-subpath/nested.custom",
        "packages/conventional-subpath/package.json",
        "packages/conventional-subpath/strict.json",
        "packages/tsconfig-field/configs/base.custom",
        "packages/tsconfig-field/package.json",
        "tsconfig.json",
    ]


def test_typescript_manifest_prefers_bare_package_tsconfig_over_exports(tmp_path: Path) -> None:
    manifest_path = tmp_path / "files.json"
    project_content = {
        Path("tsconfig.json"): '{"extends":"priority-config"}',
        Path("packages/priority-config/package.json"): json.dumps(
            {
                "name": "priority-config",
                "tsconfig": "./base.custom",
                "exports": "./dist.js",
            }
        ),
        Path("packages/priority-config/base.custom"): "{}",
        Path("packages/priority-config/dist.js"): "export default {};\n",
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=list(project_content),
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["config_files"] == [
        "packages/priority-config/base.custom",
        "packages/priority-config/package.json",
        "tsconfig.json",
    ]
    assert "packages/priority-config/dist.js" not in manifest["config_files"]


def test_typescript_manifest_treats_null_exports_as_conventional_fallback(tmp_path: Path) -> None:
    manifest_path = tmp_path / "files.json"
    project_content = {
        Path("tsconfig.json"): '{"extends":"null-exports-config"}',
        Path("packages/null-exports-config/package.json"): json.dumps(
            {
                "name": "null-exports-config",
                "exports": None,
            }
        ),
        Path("packages/null-exports-config/tsconfig"): '{"extends":"./nested.custom"}',
        Path("packages/null-exports-config/nested.custom"): "{}",
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=list(project_content),
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["config_files"] == [
        "packages/null-exports-config/nested.custom",
        "packages/null-exports-config/package.json",
        "packages/null-exports-config/tsconfig",
        "tsconfig.json",
    ]


def test_typescript_manifest_follows_wildcard_package_export_subpath(tmp_path: Path) -> None:
    manifest_path = tmp_path / "files.json"
    project_content = {
        Path("tsconfig.json"): '{"extends":"wildcard-config/strict"}',
        Path("packages/wildcard-config/package.json"): json.dumps(
            {
                "name": "wildcard-config",
                "exports": {
                    "./*": {
                        "types": "./configs/*.rules",
                        "default": "./fallback/*.custom",
                    }
                },
            }
        ),
        Path("packages/wildcard-config/configs/strict.rules"): "{}",
        Path("packages/wildcard-config/fallback/strict.custom"): "{}",
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=list(project_content),
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["config_files"] == [
        "packages/wildcard-config/configs/strict.rules",
        "packages/wildcard-config/fallback/strict.custom",
        "packages/wildcard-config/package.json",
        "tsconfig.json",
    ]


def test_typescript_package_export_traversal_is_bounded_and_deduplicated() -> None:
    repeated_target = "./configs/strict.custom"
    repeated_exports = [repeated_target] * 500_000
    deadline_checks = 0

    def check_deadline() -> None:
        nonlocal deadline_checks
        deadline_checks += 1

    tracemalloc.start()
    started_at = time.monotonic()
    try:
        targets = typescript_analyzer_module._package_export_targets(
            repeated_exports,
            "",
            check_deadline=check_deadline,
        )
        _current, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert targets == (repeated_target,)
    assert deadline_checks <= typescript_analyzer_module.TS_PACKAGE_EXPORT_ENTRY_LIMIT + 4
    assert peak_bytes < 1024 * 1024
    assert time.monotonic() - started_at < 1.0

    unique_exports = [
        f"./configs/target-{index}.custom"
        for index in range(typescript_analyzer_module.TS_PACKAGE_EXPORT_TARGET_LIMIT + 10)
    ]
    unique_targets = typescript_analyzer_module._package_export_targets(
        unique_exports,
        "",
    )
    assert len(unique_targets) == typescript_analyzer_module.TS_PACKAGE_EXPORT_TARGET_LIMIT
    assert len(set(unique_targets)) == len(unique_targets)


def test_typescript_package_export_truncation_is_reported_only_for_rejected_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_PACKAGE_EXPORT_ENTRY_LIMIT", 20)
    monkeypatch.setattr(typescript_analyzer_module, "TS_PACKAGE_EXPORT_TARGET_LIMIT", 3)
    complete_reasons: list[str] = []

    complete_targets = typescript_analyzer_module._package_export_targets(
        ["./one", "./two", "./three"],
        "",
        on_limit=complete_reasons.append,
    )

    assert complete_targets == ("./one", "./two", "./three")
    assert complete_reasons == []

    exact_key_reasons: list[str] = []
    exact_key_targets = typescript_analyzer_module._package_export_targets(
        {
            "./miss-0/*": "./miss-0/*.custom",
            "./miss-1/*": "./miss-1/*.custom",
            "./miss-2/*": "./miss-2/*.custom",
            "./wanted": "./wanted.custom",
        },
        "wanted",
        on_limit=exact_key_reasons.append,
    )

    assert exact_key_targets == ("./wanted.custom",)
    assert exact_key_reasons == []

    truncated_reasons: list[str] = []
    truncated_targets = typescript_analyzer_module._package_export_targets(
        ["./one", "./two", "./three", "./four"],
        "",
        on_limit=truncated_reasons.append,
    )

    assert truncated_targets == ("./one", "./two", "./three")
    assert len(truncated_reasons) == 1
    assert "package exports traversal reached" in truncated_reasons[0]


def test_typescript_package_export_entry_truncation_marks_manifest_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_PACKAGE_EXPORT_ENTRY_LIMIT", 4)
    project_content = {
        Path("tsconfig.json"): '{"extends":"review-config/wanted"}',
        Path("packages/review-config/package.json"): json.dumps(
            {
                "name": "review-config",
                "exports": {
                    "./miss-0/*": "./miss-0/*.custom",
                    "./miss-1/*": "./miss-1/*.custom",
                    "./miss-2/*": "./miss-2/*.custom",
                    "./*": "./*.custom",
                },
            }
        ),
        Path("packages/review-config/wanted.custom"): "{}",
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=list(project_content),
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "package exports traversal reached" in payload["partial_reason"]


def test_typescript_package_export_rejects_giant_wildcard_target_before_materializing() -> None:
    wildcard_pattern = "*" * 100_000
    wildcard_value = "x" * 100_000
    partial_reasons: list[str] = []

    tracemalloc.start()
    try:
        targets = typescript_analyzer_module._package_export_targets(
            {"./*": wildcard_pattern},
            wildcard_value,
            on_limit=partial_reasons.append,
        )
        _current, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert targets == ()
    assert len(partial_reasons) == 1
    assert "retained-target-byte safety limits" in partial_reasons[0]
    assert peak_bytes < 2 * 1024 * 1024


def test_typescript_package_export_charges_aggregate_bytes_after_deduplication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        typescript_analyzer_module,
        "TS_PACKAGE_EXPORT_SINGLE_TARGET_BYTE_LIMIT",
        100,
    )
    monkeypatch.setattr(
        typescript_analyzer_module,
        "TS_PACKAGE_EXPORT_TARGET_BYTES_LIMIT",
        13,
    )
    partial_reasons: list[str] = []

    targets = typescript_analyzer_module._package_export_targets(
        {"./*": ["./aa*", "./bb*", "./aa*", "./*"]},
        "x",
        on_limit=partial_reasons.append,
    )

    assert targets == ("./aax", "./bbx", "./x")
    assert partial_reasons == []


def test_typescript_package_export_handles_surrogateescaped_and_invalid_targets() -> None:
    surrogateescape_reasons: list[str] = []

    targets = typescript_analyzer_module._package_export_targets(
        ["./config-\udcff.custom"],
        "",
        on_limit=surrogateescape_reasons.append,
    )

    assert targets == ("./config-\udcff.custom",)
    assert surrogateescape_reasons == []

    invalid_reasons: list[str] = []
    invalid_targets = typescript_analyzer_module._package_export_targets(
        ["./config-\ud800.custom"],
        "",
        on_limit=invalid_reasons.append,
    )

    assert invalid_targets == ()
    assert len(invalid_reasons) == 1
    assert "package exports traversal reached" in invalid_reasons[0]


def test_typescript_manifest_marks_invalid_package_export_surrogate_partial(
    tmp_path: Path,
) -> None:
    project_content = {
        Path("tsconfig.json"): '{"extends":"review-config"}',
        Path("packages/review-config/package.json"): json.dumps(
            {
                "name": "review-config",
                "exports": {".": "./config-\ud800.custom"},
            }
        ),
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=list(project_content),
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["config_files"] == [
        "packages/review-config/package.json",
        "tsconfig.json",
    ]
    assert payload["partial_reason"].count("package exports traversal reached") == 1


def test_typescript_package_index_uses_aggregate_byte_and_object_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_package_paths = [
        Path("packages/00-invalid/package.json"),
        Path("packages/01-nameless/package.json"),
    ]
    for package_path, content in zip(
        invalid_package_paths,
        ("{", '{"exports": "./base.custom"}'),
        strict=True,
    ):
        target = tmp_path / package_path
        target.parent.mkdir(parents=True)
        target.write_text(content, encoding="utf-8")
    package_paths: list[Path] = []
    for index in range(4):
        package_path = Path(f"packages/config-{index}/package.json")
        target = tmp_path / package_path
        target.parent.mkdir(parents=True)
        target.write_text(
            json.dumps(
                {
                    "name": f"config-{index}",
                    "exports": {".": ["./base.custom"] * 1_000},
                }
            ),
            encoding="utf-8",
        )
        package_paths.append(package_path)
    inventory_by_path = {path.as_posix(): path for path in [*invalid_package_paths, *package_paths]}
    reads: list[Path] = []
    original_read = typescript_analyzer_module._read_inventory_config

    def record_read(
        repo_root: Path,
        relative_path: Path,
        *,
        check_deadline: Callable[[], None] = lambda: None,
    ) -> str | None:
        reads.append(relative_path)
        return original_read(
            repo_root,
            relative_path,
            check_deadline=check_deadline,
        )

    monkeypatch.setattr(typescript_analyzer_module, "_read_inventory_config", record_read)
    monkeypatch.setattr(typescript_analyzer_module, "TS_PACKAGE_INDEX_ENTRY_LIMIT", 2)
    monkeypatch.setattr(
        typescript_analyzer_module,
        "TS_PACKAGE_INDEX_BYTE_LIMIT",
        sum((tmp_path / path).stat().st_size for path in package_paths[:2]),
    )

    tracemalloc.start()
    try:
        package_index = typescript_analyzer_module._build_typescript_package_index(
            tmp_path,
            inventory_by_path,
        )
        _current, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert sorted(package_index.packages) == ["config-0", "config-1"]
    assert reads == [*invalid_package_paths, *package_paths[:2]]
    assert peak_bytes < 2 * 1024 * 1024
    assert package_index.partial_reason is not None
    assert "package metadata index reached" in package_index.partial_reason
    assert "2-object" in package_index.partial_reason

    root_config = Path("tsconfig.json")
    (tmp_path / root_config).write_text(
        '{"extends":"config-3"}\n',
        encoding="utf-8",
    )
    manifest_path = tmp_path / "files.json"
    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=[root_config, *invalid_package_paths, *package_paths],
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "package metadata index reached" in payload["partial_reason"]


def test_typescript_package_candidate_groups_have_one_aggregate_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        typescript_analyzer_module,
        "TS_PACKAGE_EXTENDS_CANDIDATE_GROUP_LIMIT",
        3,
    )
    packages = tuple(
        typescript_analyzer_module._TypescriptPackageConfig(
            directory=f"packages/config-{index}",
            tsconfig=None,
            exports={"./strict": [f"./target-{target}.custom" for target in range(10)]},
        )
        for index in range(10)
    )

    partial_reasons: list[str] = []
    candidate_groups = typescript_analyzer_module._inventory_package_extends_candidate_groups(
        ("review-config", "strict"),
        {"review-config": packages},
        on_limit=partial_reasons.append,
    )

    assert len(candidate_groups) == 3
    assert candidate_groups == tuple(sorted(set(candidate_groups)))
    assert len(partial_reasons) == 1
    assert "3-candidate-group safety limit" in partial_reasons[0]


def test_typescript_manifest_reads_utf16_config_chain(tmp_path: Path) -> None:
    manifest_path = tmp_path / "files.json"
    root_config = tmp_path / "tsconfig.json"
    root_config.write_bytes(b"\xff\xfe" + '{"extends":"./configs/base.custom"}'.encode("utf-16-le"))
    base_config = tmp_path / "configs" / "base.custom"
    base_config.parent.mkdir()
    base_config.write_bytes(b"\xfe\xff" + '{"extends":"./final.rules"}'.encode("utf-16-be"))
    final_config = tmp_path / "configs" / "final.rules"
    final_config.write_text("{}\n", encoding="utf-8")

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=[
            Path("tsconfig.json"),
            Path("configs/base.custom"),
            Path("configs/final.rules"),
        ],
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["config_files"] == [
        "configs/base.custom",
        "configs/final.rules",
        "tsconfig.json",
    ]


def test_typescript_manifest_rejects_leaf_symlink_swapped_during_config_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "files.json"
    (tmp_path / "tsconfig.json").write_text(
        '{"extends":"./configs/swapped.custom"}',
        encoding="utf-8",
    )
    swapped_config = tmp_path / "configs" / "swapped.custom"
    swapped_config.parent.mkdir()
    swapped_config.write_text("{}\n", encoding="utf-8")
    leaked_config = tmp_path / "configs" / "leaked.rules"
    leaked_config.write_text("{}\n", encoding="utf-8")
    symlink_target = tmp_path / "symlink-target.json"
    symlink_target.write_text('{"extends":"./leaked.rules"}', encoding="utf-8")

    original_is_file = Path.is_file
    original_os_open = typescript_analyzer_module.os.open
    swapped = False

    def swap_leaf() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        swapped_config.unlink()
        swapped_config.symlink_to(symlink_target)

    def swap_before_path_open(path: Path) -> bool:
        result = original_is_file(path)
        if path == swapped_config:
            swap_leaf()
        return result

    def swap_before_fd_open(path: str | Path, flags: int, mode: int = 0o777) -> int:
        if Path(path) == swapped_config:
            swap_leaf()
        return original_os_open(path, flags, mode)

    monkeypatch.setattr(Path, "is_file", swap_before_path_open)
    monkeypatch.setattr(typescript_analyzer_module.os, "open", swap_before_fd_open)

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=[
            Path("tsconfig.json"),
            Path("configs/swapped.custom"),
            Path("configs/leaked.rules"),
        ],
    )

    assert swapped
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["config_files"] == [
        "configs/swapped.custom",
        "tsconfig.json",
    ]


def test_typescript_manifest_does_not_read_packages_for_relative_extends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "files.json"
    (tmp_path / "tsconfig.json").write_text('{"extends":"./base.custom"}', encoding="utf-8")
    (tmp_path / "base.custom").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"unused"}', encoding="utf-8")
    original_read = typescript_analyzer_module._read_inventory_config
    reads: list[Path] = []

    def record_read(
        repo_root: Path,
        relative_path: Path,
        *,
        check_deadline: Callable[[], None] = lambda: None,
    ) -> str | None:
        reads.append(relative_path)
        return original_read(
            repo_root,
            relative_path,
            check_deadline=check_deadline,
        )

    monkeypatch.setattr(typescript_analyzer_module, "_read_inventory_config", record_read)

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=[
            Path("tsconfig.json"),
            Path("base.custom"),
            Path("package.json"),
        ],
    )

    assert Path("package.json") not in reads


def test_typescript_manifest_does_not_follow_config_extends_outside_project_inventory(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "files.json"
    (tmp_path / "tsconfig.json").write_text(
        '{"extends":"./ignored/private.config"}\n',
        encoding="utf-8",
    )
    ignored_config = tmp_path / "ignored" / "private.config"
    ignored_config.parent.mkdir()
    ignored_config.write_text(
        '{"extends":"../also-ignored.jsonc"}\n',
        encoding="utf-8",
    )
    (tmp_path / "ignored" / "also-ignored.jsonc").write_text("{}\n", encoding="utf-8")

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=[
            Path("src/service.ts"),
            Path("tsconfig.json"),
        ],
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "files": ["src/service.ts"],
        "package_files": [],
        "config_files": ["tsconfig.json"],
    }


def test_typescript_manifest_streams_large_inventory_with_bounded_memory(tmp_path: Path) -> None:
    inventory = [
        Path(f"packages/workspace-{index:05d}/src/component-{index:05d}-with-a-long-name.tsx")
        for index in range(30_000)
    ]
    # Project discovery has already classified these paths before it passes the
    # inventory to the analyzer. Exclude pathlib's one-time suffix cache from
    # the writer-specific allocation measurement.
    for path in inventory:
        _ = path.suffix
    manifest_path = tmp_path / "files.json"

    tracemalloc.start()
    try:
        typescript_analyzer_module._write_typescript_file_manifest(
            tmp_path,
            manifest_path,
            project_files=inventory,
        )
        _current, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    output_bytes = manifest_path.stat().st_size
    assert peak_bytes < output_bytes * 4
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(payload["files"]) == len(inventory)


def test_typescript_manifest_marks_entry_limit_truncation_without_exceeding_consumer_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert typescript_analyzer_module.TS_FILE_MANIFEST_ENTRY_LIMIT == 50_000
    monkeypatch.setattr(typescript_analyzer_module, "TS_FILE_MANIFEST_ENTRY_LIMIT", 3)
    inventory = [Path(f"src/file-{index}.ts") for index in range(4)]
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=inventory[:3],
    )
    exact_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert exact_payload["files"] == [path.as_posix() for path in inventory[:3]]
    assert "partial_reason" not in exact_payload

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=inventory,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["files"] == [path.as_posix() for path in inventory[:3]]
    assert "producer reached the 3-entry safety limit" in payload["partial_reason"]


def test_typescript_manifest_deduplicates_canonical_paths_before_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_FILE_MANIFEST_ENTRY_LIMIT", 2)
    monkeypatch.setattr(typescript_analyzer_module, "TS_INVENTORY_CASE_SENSITIVE", False)
    inventory = [
        Path("A.ts"),
        Path("a.ts"),
        Path("A.ts"),
        Path("b.ts"),
        Path("b.ts"),
    ]
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=inventory,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["files"] == ["A.ts", "b.ts"]
    assert "partial_reason" not in payload

    exact_size = manifest_path.stat().st_size
    monkeypatch.setattr(
        typescript_analyzer_module,
        "TS_FILE_MANIFEST_BYTE_LIMIT",
        exact_size,
    )
    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=inventory,
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["files"] == [
        "A.ts",
        "b.ts",
    ]


def test_typescript_supplied_inventory_filters_candidates_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_FALLBACK_INVENTORY_ENTRY_LIMIT", 2)
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=[
            Path("a.txt"),
            Path("b.md"),
            Path("../outside.ts"),
            Path("/absolute.ts"),
            Path("src/changed.ts"),
            Path("tsconfig.json"),
            Path("src/dropped.ts"),
        ],
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["files"] == ["src/changed.ts"]
    assert payload["package_files"] == []
    assert payload["config_files"] == ["tsconfig.json"]
    assert "supplied project inventory reached the 2-entry safety limit" in payload["partial_reason"]


@pytest.mark.parametrize("inventory_source", ["supplied", "git", "walk"])
def test_typescript_inventory_source_limit_does_not_starve_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory_source: str,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_FILE_MANIFEST_ENTRY_LIMIT", 2)
    monkeypatch.setattr(typescript_analyzer_module, "TS_FALLBACK_INVENTORY_ENTRY_LIMIT", 20)
    source_paths = [Path(f"a-source-{index}.ts") for index in range(3)]
    package_path = Path("z-metadata/package.json")
    config_path = Path("z-metadata/tsconfig.json")
    project_files = [*source_paths, package_path, config_path]
    for source_path in source_paths:
        (tmp_path / source_path).write_text(
            f"export const value{source_path.stem[-1]} = true;\n",
            encoding="utf-8",
        )
    (tmp_path / package_path).parent.mkdir()
    (tmp_path / package_path).write_text('{"name":"metadata"}\n', encoding="utf-8")
    (tmp_path / config_path).write_text("{}\n", encoding="utf-8")

    if inventory_source == "git":
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        supplied_files: list[Path] | None = None
    elif inventory_source == "walk":
        original_scandir = typescript_analyzer_module.os.scandir

        class _SortedScandir:
            def __init__(self, directory: str | os.PathLike[str]) -> None:
                with original_scandir(directory) as entries:
                    self._entries = sorted(entries, key=lambda entry: entry.name)

            def __enter__(self) -> object:
                return iter(self._entries)

            def __exit__(self, *_args: object) -> None:
                return None

        monkeypatch.setattr(typescript_analyzer_module.os, "scandir", _SortedScandir)
        supplied_files = None
    else:
        supplied_files = project_files

    manifest_path = tmp_path / "files.json"
    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=supplied_files,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["files"] == [path.as_posix() for path in source_paths[:2]]
    assert payload["package_files"] == [package_path.as_posix()]
    assert payload["config_files"] == [
        package_path.as_posix(),
        config_path.as_posix(),
    ]
    expected_limit_reason = (
        "producer reached the 2-entry safety limit"
        if inventory_source == "supplied"
        else "2 relevant-file safety limit"
    )
    assert expected_limit_reason in payload["partial_reason"]


@pytest.mark.parametrize(
    ("entry_limit", "output_byte_limit"),
    [
        (2, 1024 * 1024),
        (100, 64),
    ],
)
def test_typescript_git_inventory_ignores_unrelated_files_before_safety_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_limit: int,
    output_byte_limit: int,
) -> None:
    monkeypatch.setattr(
        typescript_analyzer_module,
        "TS_FALLBACK_INVENTORY_ENTRY_LIMIT",
        entry_limit,
    )
    monkeypatch.setattr(
        typescript_analyzer_module,
        "TS_FALLBACK_GIT_OUTPUT_BYTE_LIMIT",
        output_byte_limit,
    )
    for index in range(10):
        noise_path = tmp_path / f"a-noise-{index:02d}-{'x' * 32}.txt"
        noise_path.write_text("not TypeScript inventory input\n", encoding="utf-8")
    source_path = tmp_path / "z-src" / "service.ts"
    source_path.parent.mkdir()
    source_path.write_text("export const service = true;\n", encoding="utf-8")
    (source_path.parent / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["files"] == ["z-src/service.ts"]
    assert payload["config_files"] == ["z-src/tsconfig.json"]
    assert "TypeScript Git fallback inventory reached" not in payload.get("partial_reason", "")


@pytest.mark.parametrize("inventory_source", ["supplied", "git"])
def test_typescript_inventory_prioritizes_critical_configs_over_ordinary_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory_source: str,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_FILE_MANIFEST_ENTRY_LIMIT", 3)
    monkeypatch.setattr(typescript_analyzer_module, "TS_FALLBACK_INVENTORY_ENTRY_LIMIT", 20)
    project_content = {
        Path("a.json"): "{}",
        Path("b.json"): "{}",
        Path("z/package.json"): '{"name":"review-config"}',
        Path("z/tsconfig.json"): '{"extends":"./base.custom"}',
        Path("z/base.custom"): "{}",
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    supplied_files: list[Path] | None = list(project_content)
    if inventory_source == "git":
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        supplied_files = None
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=supplied_files,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["package_files"] == ["z/package.json"]
    assert payload["config_files"] == [
        "z/base.custom",
        "z/package.json",
        "z/tsconfig.json",
    ]
    assert "partial_reason" in payload


def test_typescript_inventory_byte_limits_prioritize_critical_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT", 180)
    ordinary_paths = [Path(f"{prefix}/{'x' * 100}.json") for prefix in ("a", "b")]
    critical_paths = [
        Path("z/package.json"),
        Path("z/tsconfig.json"),
        Path("z/base.custom"),
    ]
    project_content = {
        ordinary_paths[0]: "{}",
        ordinary_paths[1]: "{}",
        critical_paths[0]: '{"name":"review-config"}',
        critical_paths[1]: '{"extends":"./base.custom"}',
        critical_paths[2]: "{}",
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=list(project_content),
    )

    raw_manifest = manifest_path.read_bytes()
    payload = json.loads(raw_manifest)
    assert len(raw_manifest) <= typescript_analyzer_module.TS_FILE_MANIFEST_BYTE_LIMIT
    assert payload["package_files"] == ["z/package.json"]
    assert set(
        [
            "z/base.custom",
            "z/package.json",
            "z/tsconfig.json",
        ]
    ) <= set(payload["config_files"])
    assert "partial_reason" in payload


def test_typescript_manifest_config_byte_budget_prioritizes_critical_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    byte_reason = typescript_analyzer_module._typescript_manifest_partial_reason(
        entry_limited=False,
        byte_limited=True,
    )
    assert byte_reason is not None
    manifest_limit = typescript_analyzer_module._typescript_file_manifest_base_size(byte_reason) + 90
    monkeypatch.setattr(
        typescript_analyzer_module,
        "TS_FILE_MANIFEST_BYTE_LIMIT",
        manifest_limit,
    )
    ordinary_paths = [Path(f"{prefix}/{'x' * 200}.json") for prefix in ("a", "b")]
    project_content = {
        ordinary_paths[0]: "{}",
        ordinary_paths[1]: "{}",
        Path("z/package.json"): '{"name":"review-config"}',
        Path("z/tsconfig.json"): '{"extends":"./base.custom"}',
        Path("z/base.custom"): "{}",
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=list(project_content),
    )

    raw_manifest = manifest_path.read_bytes()
    payload = json.loads(raw_manifest)
    assert len(raw_manifest) <= manifest_limit
    assert payload["package_files"] == ["z/package.json"]
    assert payload["config_files"] == [
        "z/base.custom",
        "z/package.json",
        "z/tsconfig.json",
    ]
    assert f"{manifest_limit}-byte safety limit" in payload["partial_reason"]


@pytest.mark.parametrize("metadata_first", [False, True])
def test_typescript_retained_byte_budget_keeps_source_and_ordinary_metadata_fair_share(
    monkeypatch: pytest.MonkeyPatch,
    metadata_first: bool,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_FALLBACK_INVENTORY_PATH_BYTE_LIMIT", 240)
    monkeypatch.setattr(typescript_analyzer_module, "TS_FALLBACK_INVENTORY_ENTRY_LIMIT", 20)
    source_paths = [Path(f"src/source-{index}-{'s' * 35}.ts") for index in range(3)]
    metadata_paths = [Path(f"meta/config-{index}-{'m' * 35}.json") for index in range(3)]
    ordered_paths = [*metadata_paths, *source_paths] if metadata_first else [*source_paths, *metadata_paths]
    retention = typescript_analyzer_module._TypescriptInventoryRetention(
        "TypeScript fairness test",
        enforce_section_entry_limits=False,
    )

    for path in ordered_paths:
        retention.add(path)

    inventory = retention.build()
    assert sorted(path for path in inventory.paths if path.suffix == ".ts") == source_paths[:2]
    assert sorted(path for path in inventory.paths if path.suffix == ".json") == metadata_paths[:2]
    assert "retained-path safety limit" in inventory.partial_reason


@pytest.mark.parametrize("inventory_source", ["supplied", "git"])
def test_typescript_source_like_config_is_critical_in_both_manifest_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory_source: str,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_FILE_MANIFEST_ENTRY_LIMIT", 2)
    monkeypatch.setattr(typescript_analyzer_module, "TS_FALLBACK_INVENTORY_ENTRY_LIMIT", 20)
    project_content = {
        Path("a.ts"): "export const a = true;",
        Path("b.ts"): "export const b = true;",
        Path("z/base.ts"): "{}",
        Path("tsconfig.json"): '{"extends":"./z/base.ts"}',
    }
    for relative_path, content in project_content.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    supplied_files: list[Path] | None = list(project_content)
    if inventory_source == "git":
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        supplied_files = None
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=supplied_files,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["files"] == ["a.ts", "z/base.ts"]
    assert payload["config_files"] == ["tsconfig.json", "z/base.ts"]
    assert "partial_reason" in payload


def test_typescript_manifest_byte_limit_reserves_metadata_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_FILE_MANIFEST_BYTE_LIMIT", 420)
    source_paths = [Path(f"src/{character * 180}.ts") for character in ("a", "b", "c")]
    package_path = Path("meta/package.json")
    config_path = Path("meta/tsconfig.json")
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=[*source_paths, package_path, config_path],
    )

    raw = manifest_path.read_bytes()
    payload = json.loads(raw)
    assert len(raw) <= 420
    assert payload["package_files"] == [package_path.as_posix()]
    assert payload["config_files"] == [
        package_path.as_posix(),
        config_path.as_posix(),
    ]
    assert "420-byte safety limit" in payload["partial_reason"]


def test_typescript_supplied_inventory_uses_case_insensitive_config_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_INVENTORY_CASE_SENSITIVE", False)
    manifest_path = tmp_path / "files.json"
    root_config = Path("tsconfig.json")
    actual_extended_config = Path("Configs/Base.Custom")
    (tmp_path / root_config).write_text(
        '{"extends":"./configs/base.custom"}\n',
        encoding="utf-8",
    )
    (tmp_path / actual_extended_config).parent.mkdir()
    (tmp_path / actual_extended_config).write_text("{}\n", encoding="utf-8")

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=[root_config, actual_extended_config],
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["config_files"] == [
        actual_extended_config.as_posix(),
        root_config.as_posix(),
    ]


def test_typescript_supplied_inventory_accepts_surrogateescaped_paths(tmp_path: Path) -> None:
    manifest_path = tmp_path / "files.json"
    surrogate_path = Path("src/bad\udcff.ts")

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=[surrogate_path],
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["files"] == [surrogate_path.as_posix()]


def test_typescript_supplied_inventory_rejects_invalid_surrogate_without_crashing(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=[Path("src/bad\ud800.ts")],
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["files"] == []
    assert "filesystem-unrepresentable paths" in payload["partial_reason"]


def test_typescript_fallback_rejects_invalid_surrogate_extends_without_crashing(
    tmp_path: Path,
) -> None:
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"extends": "./config-\ud800.custom"}),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["config_files"] == ["tsconfig.json"]
    assert "filesystem-unrepresentable config path" in payload["partial_reason"]


def test_typescript_fallback_inventory_filters_and_caps_during_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typescript_analyzer_module, "TS_FILE_MANIFEST_ENTRY_LIMIT", 2)
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    for index in range(100):
        (source_dir / f"irrelevant-{index:03d}.txt").write_text("ignored\n", encoding="utf-8")
    for index in range(3):
        (source_dir / f"source-{index}.ts").write_text(
            f"export const source{index} = true;\n",
            encoding="utf-8",
        )
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(payload["files"]) == 2
    assert set(payload["files"]) <= {
        "src/source-0.ts",
        "src/source-1.ts",
        "src/source-2.ts",
    }
    assert "fallback inventory reached the 2 relevant-file safety limit" in payload["partial_reason"]


def test_typescript_walk_inventory_aggregates_many_filesystem_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()

    class _Entry:
        def __init__(self, path: Path, kind: str) -> None:
            self.path = str(path)
            self._kind = kind

        def is_dir(self, *, follow_symlinks: bool) -> bool:
            assert not follow_symlinks
            if self._kind == "inspect":
                raise OSError("inspect failed")
            return self._kind == "directory"

        def is_file(self, *, follow_symlinks: bool) -> bool:
            assert not follow_symlinks
            if self._kind == "file-inspect":
                raise OSError("file inspect failed")
            return False

    entries = [
        *(_Entry(root / f"inspect-{index}", "inspect") for index in range(500)),
        *(_Entry(root / f"file-inspect-{index}", "file-inspect") for index in range(500)),
        *(_Entry(root / f"directory-{index}", "directory") for index in range(1_000)),
    ]

    class _Scandir:
        def __enter__(self) -> object:
            return iter(entries)

        def __exit__(self, *_args: object) -> None:
            return None

    def failing_scandir(directory: str | os.PathLike[str]) -> _Scandir:
        if Path(directory) == root:
            return _Scandir()
        raise OSError("directory read failed")

    monkeypatch.setattr(typescript_analyzer_module.os, "scandir", failing_scandir)
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
    )

    raw_manifest = manifest_path.read_bytes()
    payload = json.loads(raw_manifest)
    assert payload["files"] == []
    assert "could not inspect 1000 filesystem entries" in payload["partial_reason"]
    assert "could not read 1000 directories" in payload["partial_reason"]
    assert len(payload["partial_reason"]) < 500
    assert len(raw_manifest) <= typescript_analyzer_module.TS_FILE_MANIFEST_BYTE_LIMIT


def test_typescript_manifest_byte_limit_includes_partial_marker_and_closing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert typescript_analyzer_module.TS_FILE_MANIFEST_BYTE_LIMIT == 16 * 1024 * 1024
    inventory = [
        Path("src/short.ts"),
        Path(f"src/{'x' * 220}.ts"),
        Path("src/tail.ts"),
    ]
    manifest_path = tmp_path / "files.json"

    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=inventory,
    )
    exact_size = manifest_path.stat().st_size
    monkeypatch.setattr(typescript_analyzer_module, "TS_FILE_MANIFEST_BYTE_LIMIT", exact_size)
    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=inventory,
    )
    exact_payload = json.loads(manifest_path.read_bytes())
    assert manifest_path.stat().st_size == exact_size
    assert "partial_reason" not in exact_payload

    monkeypatch.setattr(typescript_analyzer_module, "TS_FILE_MANIFEST_BYTE_LIMIT", exact_size - 1)
    typescript_analyzer_module._write_typescript_file_manifest(
        tmp_path,
        manifest_path,
        project_files=inventory,
    )
    raw = manifest_path.read_bytes()
    payload = json.loads(raw)
    assert len(raw) <= exact_size - 1
    assert payload["files"] == ["src/short.ts", "src/tail.ts"]
    assert f"producer reached the {exact_size - 1}-byte safety limit" in payload["partial_reason"]


def test_typescript_manifest_atomic_write_preserves_existing_target_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "files.json"
    manifest_path.write_text('{"version":1,"files":["original.ts"]}', encoding="utf-8")
    original_dumps = json.dumps
    encoded_paths = 0

    def fail_during_stream(value: object, *args: object, **kwargs: object) -> str:
        nonlocal encoded_paths
        if isinstance(value, str):
            encoded_paths += 1
            if encoded_paths == 2:
                raise OSError("simulated manifest write failure")
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(typescript_analyzer_module.json, "dumps", fail_during_stream)

    with pytest.raises(OSError, match="simulated manifest write failure"):
        typescript_analyzer_module._write_typescript_file_manifest(
            tmp_path,
            manifest_path,
            project_files=[Path("src/first.ts"), Path("src/second.ts")],
        )

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "files": ["original.ts"],
    }
    assert list(tmp_path.glob(".files.json.*.tmp")) == []


def test_typescript_manifest_generation_stops_at_the_total_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "files.json"
    manifest_path.write_text('{"version":1,"files":["original.ts"]}', encoding="utf-8")
    clock = [0.0]

    def advancing_clock() -> float:
        clock[0] += 0.2
        return clock[0]

    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", advancing_clock)

    with pytest.raises(AnalyzerError, match="total timeout after 1s while building repository inventory"):
        typescript_analyzer_module._write_typescript_file_manifest(
            tmp_path,
            manifest_path,
            project_files=[Path(f"src/file-{index:04d}.ts") for index in range(100)],
            deadline=1.0,
            total_timeout_seconds=1.0,
        )

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "files": ["original.ts"],
    }
    assert list(tmp_path.glob(".files.json.*.tmp")) == []


def test_typescript_fallback_inventory_reports_the_total_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "files.json"
    manifest_path.write_text('{"version":1,"files":["original.ts"]}', encoding="utf-8")
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    for index in range(20):
        (source_dir / f"file-{index:02d}.ts").write_text(
            f"export const value{index} = {index};\n",
            encoding="utf-8",
        )
    clock = [0.0]

    def advancing_clock() -> float:
        clock[0] += 0.2
        return clock[0]

    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", advancing_clock)

    with pytest.raises(
        AnalyzerError,
        match="total timeout after 1s while building repository inventory",
    ):
        typescript_analyzer_module._write_typescript_file_manifest(
            tmp_path,
            manifest_path,
            deadline=1.0,
            total_timeout_seconds=1.0,
        )

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "files": ["original.ts"],
    }
    assert list(tmp_path.glob(".files.json.*.tmp")) == []


def test_run_analyzers_reuses_supplied_project_inventory_for_typescript_manifest(
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
    seen_manifest: dict[str, object] | None = None

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(
        "apex_ray.analyzers.typescript._walk_bounded_typescript_inventory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected second inventory scan")),
    )

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal seen_manifest
        manifest_index = args.index("--file-manifest") + 1
        seen_manifest = json.loads(Path(args[manifest_index]).read_text(encoding="utf-8"))
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

    project_files = [Path("src/helper.ts"), Path("src/cart.ts"), Path("README.md")]
    result = run_analyzers(
        tmp_path,
        [changed],
        AnalyzerConfig(script_path=str(script)),
        project_files=project_files,
    )

    assert result.results
    assert project_files == [Path("src/helper.ts"), Path("src/cart.ts"), Path("README.md")]
    assert seen_manifest == {
        "version": 2,
        "files": ["src/cart.ts", "src/helper.ts"],
        "package_files": [],
        "config_files": [],
    }


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


def test_go_analyzer_omits_zero_ranges_for_deletion_only_modified_hunks(tmp_path: Path) -> None:
    changed = ChangedFile(
        old_path="internal/auth/service.go",
        new_path="internal/auth/service.go",
        language="go",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 1,
                "old_lines": 1,
                "new_start": 0,
                "new_lines": 0,
                "lines": [
                    {"kind": "delete", "old_line": 1, "content": "func Removed() error {"},
                ],
            }
        ],
    )

    args = go_analyzer_module._go_analyzer_args(tmp_path, [changed], AnalyzerConfig(timeout_seconds=10))

    assert "internal/auth/service.go:0-0" not in args
    assert "--range" not in args
    assert [
        "--deleted-line",
        "internal/auth/service.go",
        "1",
        "func Removed() error {",
    ] in [args[index : index + 4] for index, value in enumerate(args) if value == "--deleted-line"]


def test_go_analyzer_anchors_modified_deleted_lines_to_new_file_coordinates(tmp_path: Path) -> None:
    changed = ChangedFile(
        old_path="internal/auth/service.go",
        new_path="internal/auth/service.go",
        language="go",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 30,
                "old_lines": 1,
                "new_start": 10,
                "new_lines": 1,
                "lines": [
                    {"kind": "add", "new_line": 10, "content": "func Added() error {"},
                    {"kind": "delete", "old_line": 30, "content": "func Removed() error {"},
                ],
            }
        ],
    )

    args = go_analyzer_module._go_analyzer_args(tmp_path, [changed], AnalyzerConfig(timeout_seconds=10))

    deleted_line_args = [args[index : index + 4] for index, value in enumerate(args) if value == "--deleted-line"]
    assert [
        "--deleted-line",
        "internal/auth/service.go",
        "11",
        "func Removed() error {",
    ] in deleted_line_args
    assert [
        "--deleted-line",
        "internal/auth/service.go",
        "30",
        "func Removed() error {",
    ] not in deleted_line_args


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
            "files": (
                []
                if shard_files == ["src/file-0.ts"]
                else [{"path": path, "symbols": [], "imports": [], "exports": []} for path in shard_files]
            ),
            "warnings": ["shared workspace warning", f"warning for {shard_files[0]}"],
            "indexCache": None,
            "partial": shard_files == ["src/file-0.ts"],
            "failedFiles": ["src/file-0.ts"] if shard_files == ["src/file-0.ts"] else [],
            "shardFailures": (
                [
                    {
                        "index": 1,
                        "total": 1,
                        "files": ["src/file-0.ts"],
                        "reason": "internal analyzer budget exhausted",
                        "status": "timeout",
                    }
                ]
                if shard_files == ["src/file-0.ts"]
                else []
            ),
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
    assert [file.path for file in result.files] == ["src/file-2.ts"]
    assert "warning for src/file-0.ts" in result.warnings
    assert "warning for src/file-2.ts" in result.warnings
    assert result.warnings.count("shared workspace warning") == 1
    assert any("partial TypeScript analyzer result" in warning for warning in result.warnings)
    assert any("src/file-1.ts" in warning and "timed out after" in warning for warning in result.warnings)
    assert result.partial is True
    assert result.failed_files == ["src/file-0.ts", "src/file-1.ts"]
    assert [failure.status for failure in result.shard_failures] == ["timeout", "timeout"]
    assert [failure.files for failure in result.shard_failures] == [["src/file-0.ts"], ["src/file-1.ts"]]
    assert result.coverage is not None
    assert result.coverage.failed_file_count == 2
    assert "shard_timeout" in result.coverage.reason_codes
    assert result.metrics is not None
    assert [(shard.index, shard.total) for shard in result.metrics.shards] == [(1, 3), (2, 3), (3, 3)]
    assert [shard.status for shard in result.metrics.shards] == ["timeout", "timeout", "complete"]
    assert result.metrics.wall_duration_ms >= max(shard.wall_duration_ms for shard in result.metrics.shards)
    assert "total" not in result.metrics.stage_durations_ms
    shared_summary = next(
        summary for summary in result.warning_summaries if summary.message == "shared workspace warning"
    )
    assert shared_summary.occurrences == 2
    assert shared_summary.shard_indexes == [1, 3]
    outer_failure_summary = next(
        summary
        for summary in result.warning_summaries
        if summary.message.startswith("Returning partial TypeScript analyzer result")
    )
    assert outer_failure_summary.shard_indexes == [2]


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
    clock = [0.0]

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")
    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", lambda: clock[0])

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
        clock[0] = 2.1
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


def test_typescript_analyzer_total_timeout_includes_manifest_generation(
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
    clock = [0.0]

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")
    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", lambda: clock[0])

    def slow_manifest(
        _repo_root: Path,
        manifest_path: Path,
        _ignored_patterns: list[str] | None = None,
        *,
        project_files: list[Path] | None = None,
        deadline: float | None = None,
        total_timeout_seconds: float | None = None,
    ) -> None:
        assert deadline == 2.0
        assert total_timeout_seconds == 2.0
        clock[0] = 2.1
        manifest_path.write_text('{"version":1,"files":[]}', encoding="utf-8")

    monkeypatch.setattr("apex_ray.analyzers.typescript._write_typescript_file_manifest", slow_manifest)
    monkeypatch.setattr(
        "apex_ray.analyzers.typescript._run_analyzer_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analyzer should not start")),
    )

    with pytest.raises(AnalyzerError, match="total timeout after 2s"):
        run_typescript_analyzer(
            tmp_path,
            [changed],
            AnalyzerConfig(script_path=str(script), timeout_seconds=2),
        )


def test_typescript_analyzer_prioritizes_critical_policy_risk_before_medium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "analyze.js"
    script.write_text("console.log('{}')\n", encoding="utf-8")
    medium = ChangedFile(
        old_path="src/medium.ts",
        new_path="src/medium.ts",
        language="typescript",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind=kind,
                severity=RiskSeverity.MEDIUM,
                reason="Medium-risk boundary changed.",
                file="src/medium.ts",
            )
            for kind in ("validation", "persistence", "public_api")
        ],
    )
    critical = ChangedFile(
        old_path="src/critical.ts",
        new_path="src/critical.ts",
        language="typescript",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="policy:money",
                severity=RiskSeverity.CRITICAL,
                reason="Settlement policy changed.",
                file="src/critical.ts",
            )
        ],
    )
    seen_shards: list[list[str]] = []
    clock = [0.0]

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")
    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", lambda: clock[0])

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
        clock[0] = 2.1
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
        [medium, critical],
        AnalyzerConfig(script_path=str(script), timeout_seconds=2, changed_file_shard_size=1),
    )

    assert result is not None
    assert seen_shards == [["src/critical.ts"]]
    assert result.failed_files == ["src/medium.ts"]


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
    clock = [0.0]
    shard_durations = iter([2.1, 2.0, 0.0])

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")
    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", lambda: clock[0])

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
        clock[0] += next(shard_durations)
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
    clock = [0.0]
    shard_durations = iter([2.1, 2.0, 2.0, 2.0])

    monkeypatch.setattr("apex_ray.analyzers.typescript.shutil.which", lambda name: "/usr/bin/node")
    monkeypatch.setattr("apex_ray.analyzers.typescript.time.monotonic", lambda: clock[0])

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
        clock[0] += next(shard_durations)
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

    message = str(exc.value)
    assert "TypeScript analyzer" in message
    assert "timed out after" in message or "total timeout after" in message
