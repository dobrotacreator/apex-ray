import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from apex_ray import __version__, git
from apex_ray.cli import app
from apex_ray.cli.common import atomic_write_text
from apex_ray.diff import parse_unified_diff
from apex_ray.discovery import DiscoveryError, DiscoveryTimeoutError
from apex_ray.findings import finding_fingerprint
from apex_ray.llm.cache import REVIEW_PROMPT_VERSION
from apex_ray.llm.providers import FakeLLMProvider
from apex_ray.models import (
    AnalyzerFile,
    AnalyzerResult,
    AnalyzerSymbol,
    ContextPack,
    DiffStats,
    DiffSummary,
    FileKind,
    Finding,
    FindingConfidence,
    FindingResolutionStatus,
    FindingSeverity,
    FindingVerification,
    LLMCoverageTodo,
    LLMReviewerCoverageSummary,
    LLMRun,
    ProjectProfile,
    ReviewConfig,
    ReviewerConfig,
    ReviewInputSnapshot,
    ReviewReport,
    TargetMode,
)
from apex_ray.pipeline.snapshot import capture_review_input_snapshot, validate_review_input_snapshot
from apex_ray.report import build_report

runner = CliRunner()
FIXTURE_DIR = Path(__file__).parent / "fixtures"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_RICH_FRAME_CHARS = str.maketrans({ord(char): " " for char in "\u2500\u2502\u256d\u256e\u2570\u256f"})
_GATE_HEAD_1 = "1" * 40
_GATE_HEAD_2 = "2" * 40
_GATE_MERGE_BASE = "a" * 40


def test_atomic_write_removes_partial_temporary_file_when_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "review.json"
    output.write_text("old report\n", encoding="utf-8")
    original_write_text = Path.write_text

    def failing_write_text(path: Path, _content: str, *, encoding: str) -> int:
        original_write_text(path, "partial", encoding=encoding)
        raise OSError("simulated write failure")

    monkeypatch.setattr(Path, "write_text", failing_write_text)

    with pytest.raises(OSError, match="simulated write failure"):
        atomic_write_text(output, "new report\n")

    assert output.read_text(encoding="utf-8") == "old report\n"
    assert not list(tmp_path.glob(".review.json.*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")
def test_atomic_write_preserves_existing_output_permissions(tmp_path: Path) -> None:
    output = tmp_path / "review.json"
    output.write_text("old report\n", encoding="utf-8")
    output.chmod(0o664)

    atomic_write_text(output, "new report\n")

    assert output.read_text(encoding="utf-8") == "new report\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o664


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask semantics")
def test_atomic_write_new_output_honors_process_umask(tmp_path: Path) -> None:
    output = tmp_path / "review.json"
    previous_umask = os.umask(0o027)
    try:
        atomic_write_text(output, "new report\n")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(output.stat().st_mode) == 0o640


def _plain_cli_output(output: str) -> str:
    return " ".join(_ANSI_RE.sub("", output).translate(_RICH_FRAME_CHARS).split())


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"], catch_exceptions=False)

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_init_creates_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"], catch_exceptions=False)

    assert result.exit_code == 0
    assert (tmp_path / ".apex-ray" / "config.yml").exists()
    assert (tmp_path / ".apex-ray" / ".gitignore").exists()
    assert (tmp_path / "lefthook.yml").exists()
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert (tmp_path / ".apex-ray" / "skills" / "apex-ray" / "SKILL.md").exists()
    assert (tmp_path / ".apex-ray" / "skills" / "apex-ray-improve" / "SKILL.md").exists()
    assert (tmp_path / ".agents" / "skills" / "apex-ray" / "SKILL.md").exists()
    assert (tmp_path / ".agents" / "skills" / "apex-ray-improve" / "SKILL.md").exists()
    assert not (tmp_path / ".codex").exists()
    assert (tmp_path / ".claude" / "skills" / "apex-ray" / "SKILL.md").exists()
    assert (tmp_path / ".claude" / "skills" / "apex-ray-improve" / "SKILL.md").exists()
    assert "apex-ray-review" in (tmp_path / "lefthook.yml").read_text(encoding="utf-8")
    assert "apex-ray gate pre-push" in (tmp_path / "lefthook.yml").read_text(encoding="utf-8")
    assert "--no-llm" not in (tmp_path / "lefthook.yml").read_text(encoding="utf-8")
    config_text = (tmp_path / ".apex-ray" / "config.yml").read_text(encoding="utf-8")
    assert "max_packs: 48" in config_text
    assert "max_deep_packs: 16" in config_text
    assert "max_input_tokens: 180000" in config_text
    assert "jobs: 2" in config_text
    assert "auto_followup_max_pack_reviews: 16" in config_text
    assert "progress: auto" in config_text
    assert "Next: inspect and commit Apex Ray setup files" in result.stdout


def test_init_can_skip_hooks_and_agent_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--hooks", "none", "--agent-files", "none"], catch_exceptions=False)

    assert result.exit_code == 0
    assert (tmp_path / ".apex-ray" / "config.yml").exists()
    assert not (tmp_path / "lefthook.yml").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".apex-ray" / "skills").exists()


def test_init_can_skip_agent_skill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--no-agent-skill"], catch_exceptions=False)

    assert result.exit_code == 0
    assert (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".apex-ray" / "skills").exists()
    assert "$apex-ray" not in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_init_refresh_agent_artifacts_dry_run_then_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    init_result = runner.invoke(app, ["init"], catch_exceptions=False)
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text(
        "<!-- APEX_RAY_START -->\n## Apex Ray\n\nOld instructions.\n<!-- APEX_RAY_END -->\n",
        encoding="utf-8",
    )

    dry_run = runner.invoke(app, ["init", "--refresh-agent-artifacts", "--dry-run"], catch_exceptions=False)
    refresh = runner.invoke(app, ["init", "--refresh-agent-artifacts"], catch_exceptions=False)

    assert init_result.exit_code == 0
    assert dry_run.exit_code == 0
    assert "would refresh" in dry_run.stdout
    assert str(agents_path) in dry_run.stdout
    assert refresh.exit_code == 0
    assert "refreshed" in refresh.stdout
    assert "Old instructions" not in agents_path.read_text(encoding="utf-8")
    assert "apex-ray-agent-artifacts: version=" in agents_path.read_text(encoding="utf-8")


def test_doctor_reports_local_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".apex-ray").mkdir()
    (tmp_path / ".apex-ray" / "config.yml").write_text("review:\n", encoding="utf-8")
    (tmp_path / ".apex-ray" / "config.local.yml").write_text("review:\n  llm:\n    jobs: 2\n", encoding="utf-8")

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 0
    assert f"- Local config: {tmp_path / '.apex-ray' / 'config.local.yml'}" in result.stdout
    assert "- Python analyzer: built in" in result.stdout
    assert "- Python analyzer available: true" in result.stdout
    assert "- Agent artifacts: not found" in result.stdout
    assert "- Go available:" in result.stdout
    assert "- Go analyzer:" in result.stdout
    assert "- Go analyzer available:" in result.stdout


def test_doctor_reports_discovery_error_without_traceback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_discovery(*_args, **_kwargs):
        raise DiscoveryError(
            "Project file discovery failed: Git inventory exceeded its safety limit; "
            "remove generated or untracked files before retrying."
        )

    monkeypatch.setattr("apex_ray.cli.main.discover_project", fail_discovery)

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 2
    plain_output = _plain_cli_output(result.output)
    assert "Git inventory exceeded its safety limit" in plain_output
    assert "Traceback" not in plain_output


def test_doctor_passes_configured_timeout_to_project_discovery(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".apex-ray").mkdir()
    (tmp_path / ".apex-ray" / "config.yml").write_text(
        "review:\n  analyzer:\n    timeout_seconds: 17\n",
        encoding="utf-8",
    )
    seen_timeout: float | None = None

    def capture_discovery(
        root: Path,
        ignored_patterns: list[str] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> ProjectProfile:
        del ignored_patterns
        nonlocal seen_timeout
        seen_timeout = timeout_seconds
        return ProjectProfile(root=str(root), is_git_repo=False)

    monkeypatch.setattr("apex_ray.cli.main.discover_project", capture_discovery)

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 0
    assert seen_timeout == 17


def test_doctor_reports_root_probe_timeout_without_traceback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apex_ray.cli.main.discover_repo_root",
        lambda _cwd: (_ for _ in ()).throw(
            DiscoveryTimeoutError("Project file discovery timed out while locating the Git repository root.")
        ),
    )

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 2
    plain_output = _plain_cli_output(result.output)
    assert "timed out while locating the Git repository root" in plain_output
    assert "Traceback" not in plain_output


def test_doctor_reports_outdated_agent_artifacts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"], catch_exceptions=False)
    (tmp_path / "AGENTS.md").write_text(
        "<!-- APEX_RAY_START -->\n## Apex Ray\n\nOld instructions.\n<!-- APEX_RAY_END -->\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "- Agent artifacts: outdated" in result.stdout
    assert "Run: apex-ray init --refresh-agent-artifacts" in result.stdout


def test_telemetry_summary_uses_configured_local_data_path(tmp_path: Path, monkeypatch) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir()
    config.write_text(
        "review:\n"
        "  local_data:\n"
        "    root: git_common\n"
        "  telemetry:\n"
        "    path: ${local_data}/telemetry/review-runs.jsonl\n",
        encoding="utf-8",
    )
    telemetry_path = tmp_path / ".git" / "apex-ray" / "telemetry" / "review-runs.jsonl"
    telemetry_path.parent.mkdir(parents=True)
    telemetry_path.write_text(
        json.dumps(
            {
                "created_at": "2026-06-01T00:00:00Z",
                "run_id": "unit",
                "target_mode": "worktree",
                "findings_count": 0,
                "coverage_ratio": 1.0,
                "high_risk_coverage_ratio": 1.0,
                "partial_severity": "none",
                "llm_estimated_input_tokens": 0,
                "duration_ms": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["telemetry-summary"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "- Runs: `1`" in result.stdout
    assert "unit" in result.stdout


def test_benchmark_help_uses_generic_analyzer_cache_wording() -> None:
    result = runner.invoke(app, ["benchmark", "--help"], catch_exceptions=False)
    plain_output = _plain_cli_output(result.stdout)

    assert result.exit_code == 0
    assert "Use analyzer repo index caches." in plain_output
    assert "TS/JS analyzer repo index cache" not in plain_output


def test_memory_lint_loads_repo_memory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    memory_dir = tmp_path / ".apex-ray" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "cart-total.md").write_text(
        "---\nid: cart-total\nkind: invariant\n---\nCart totals must include quantity.\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["memory", "lint"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "- Loaded cards: 1" in result.stdout
    assert "cart-total" in result.stdout


def test_memory_suggest_writes_cards_from_report(tmp_path: Path) -> None:
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[
            Finding(
                title="Cart total ignores quantity",
                severity=FindingSeverity.HIGH,
                confidence=FindingConfidence.HIGH,
                file="src/cart.ts",
                line=6,
                failure_mode="The cart total undercharges multi-quantity items.",
                evidence="The diff returns item.price without item.quantity.",
                suggested_fix="Restore price * quantity.",
                suggested_test="Add a multi-quantity cart total case.",
            )
        ],
    )
    report_path = tmp_path / "review.json"
    output = tmp_path / "memory-suggestions.md"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    result = runner.invoke(
        app,
        ["memory", "suggest", "--from-report", str(report_path), "--output", str(output), "--include-unverified"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Wrote" in result.stdout
    text = output.read_text(encoding="utf-8")
    assert "id: cart-total-ignores-quantity" in text
    assert "The cart total undercharges" in text


def test_findings_list_prints_fingerprints_from_report(tmp_path: Path) -> None:
    finding = Finding(
        title="Cart total ignores quantity",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/cart.ts",
        line=6,
        failure_mode="The cart total undercharges multi-quantity items.",
        evidence="The diff returns item.price without item.quantity.",
        suggested_fix="Restore price * quantity.",
        suggested_test="Add a multi-quantity cart total case.",
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        findings=[finding],
    )
    report_path = tmp_path / "review.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    result = runner.invoke(app, ["findings", "list", "--from-report", str(report_path)], catch_exceptions=False)

    assert result.exit_code == 0
    assert finding_fingerprint(finding) in result.stdout
    assert "Cart total ignores quantity" in result.stdout


def test_review_patch_writes_markdown_and_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    output = tmp_path / "review.md"
    json_output = tmp_path / "review.json"
    html_output = tmp_path / "review.html"

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--output",
            str(output),
            "--json",
            str(json_output),
            "--html",
            str(html_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Wrote" in result.stdout
    assert "# Apex Ray Review" in output.read_text(encoding="utf-8")
    assert '"files_changed": 3' in json_output.read_text(encoding="utf-8")
    assert "<h1>Apex Ray Review</h1>" in html_output.read_text(encoding="utf-8")
    assert not (tmp_path / ".apex-ray").exists()


def test_review_warns_for_outdated_agent_artifacts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text(
        "<!-- APEX_RAY_START -->\n## Apex Ray\n\nOld instructions.\n<!-- APEX_RAY_END -->\n",
        encoding="utf-8",
    )
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")

    result = runner.invoke(
        app,
        ["review", "--diff", str(patch)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Wrote" in result.stdout
    assert "Warning: Apex Ray agent artifacts are outdated" in result.stderr
    assert "apex-ray init --refresh-agent-artifacts" in result.stderr


def test_review_patch_defaults_to_apex_reports_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")

    result = runner.invoke(app, ["review", "--diff", str(patch)], catch_exceptions=False)

    output = tmp_path / ".apex-ray" / "reports" / "review.md"
    json_output = tmp_path / ".apex-ray" / "reports" / "review.json"
    assert result.exit_code == 0
    assert output.exists()
    assert json_output.exists()
    assert "reports/" in (tmp_path / ".apex-ray" / ".gitignore").read_text(encoding="utf-8")
    assert not (tmp_path / "review.md").exists()
    assert not (tmp_path / "review.json").exists()


def test_review_reports_discovery_error_without_traceback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")

    def fail_discovery(*_args, **_kwargs):
        raise DiscoveryError(
            "Project file discovery failed: Git inventory exceeded its safety limit; "
            "remove generated or untracked files before retrying."
        )

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fail_discovery)

    result = runner.invoke(
        app,
        ["review", "--diff", str(patch)],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    plain_output = _plain_cli_output(result.output)
    assert "Git inventory exceeded its safety limit" in plain_output
    assert "Traceback" not in plain_output
    assert not (tmp_path / ".apex-ray" / "reports" / "review.json").exists()


def test_review_reports_root_probe_timeout_without_traceback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apex_ray.cli.main.discover_repo_root",
        lambda _cwd: (_ for _ in ()).throw(
            DiscoveryTimeoutError("Project file discovery timed out while locating the Git repository root.")
        ),
    )

    result = runner.invoke(app, ["review", "--worktree"], catch_exceptions=False)

    assert result.exit_code == 2
    plain_output = _plain_cli_output(result.output)
    assert "timed out while locating the Git repository root" in plain_output
    assert "Traceback" not in plain_output
    assert not (tmp_path / ".apex-ray" / "reports" / "review.json").exists()


def test_review_patch_defaults_to_repo_reports_dir_from_subdir(tmp_path: Path, monkeypatch) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subdir = tmp_path / "src"
    subdir.mkdir()
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(subdir)

    result = runner.invoke(app, ["review", "--diff", str(patch)], catch_exceptions=False)

    output = tmp_path / ".apex-ray" / "reports" / "review.md"
    json_output = tmp_path / ".apex-ray" / "reports" / "review.json"
    assert result.exit_code == 0
    assert output.exists()
    assert json_output.exists()
    assert not (subdir / ".apex-ray").exists()


def test_review_worktree_uses_git_common_local_data_for_telemetry_and_archives(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    config = repo / ".apex-ray" / "config.yml"
    config.parent.mkdir()
    config.write_text(
        "review:\n"
        "  local_data:\n"
        "    root: git_common\n"
        "  llm:\n"
        "    enabled: false\n"
        "  telemetry:\n"
        "    enabled: true\n"
        "    path: ${local_data}/telemetry/review-runs.jsonl\n"
        "  reports:\n"
        "    archive: true\n"
        "    archive_dir: ${local_data}/reports/runs\n",
        encoding="utf-8",
    )
    (config.parent / ".gitignore").write_text("reports/\n", encoding="utf-8")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "worktree", "add", str(worktree), "-b", "feature"], cwd=repo, check=True)
    (worktree / "app.py").write_text("value = 2\n", encoding="utf-8")
    monkeypatch.chdir(worktree)

    result = runner.invoke(app, ["review", "--worktree", "--no-llm"], catch_exceptions=False)

    shared_root = repo / ".git" / "apex-ray"
    assert result.exit_code == 0
    assert (worktree / ".apex-ray" / "reports" / "review.md").exists()
    assert (worktree / ".apex-ray" / "reports" / "review.json").exists()
    persisted = ReviewReport.model_validate_json(
        (worktree / ".apex-ray" / "reports" / "review.json").read_text(encoding="utf-8")
    )
    assert persisted.input_snapshot is not None
    assert (
        validate_review_input_snapshot(
            persisted.input_snapshot,
            worktree,
            expected_target_mode=persisted.diff.target_mode,
            expected_base_ref=persisted.diff.base,
        )
        == "current"
    )
    assert (shared_root / "telemetry" / "review-runs.jsonl").exists()
    archive_dirs = list((shared_root / "reports" / "runs").iterdir())
    assert len(archive_dirs) == 1
    assert (archive_dirs[0] / "review.md").exists()
    assert (archive_dirs[0] / "review.json").exists()
    assert not (worktree / ".apex-ray" / "telemetry").exists()


def test_review_patch_archives_reports_when_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "review:\n  reports:\n    archive: true\n    retention: 5\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--output",
            ".apex-ray/reports/review.md",
            "--json",
            ".apex-ray/reports/review.json",
        ],
        catch_exceptions=False,
    )

    archive_dirs = list((tmp_path / ".apex-ray" / "reports" / "runs").iterdir())
    assert result.exit_code == 0
    assert "Archived report:" in result.stdout
    assert len(archive_dirs) == 1
    assert (archive_dirs[0] / "review.md").exists()
    assert (archive_dirs[0] / "review.json").exists()
    assert (archive_dirs[0] / "manifest.json").exists()


def test_gate_pre_push_blocks_high_verified_finding(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        line=84,
        failure_mode="The changed query can return another tenant's order.",
        evidence="The diff removes tenantId from the lookup predicate.",
        suggested_fix="Restore the tenantId predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
        context_pack_id="src/orders.ts#getOrder:1",
    )

    def fake_run_review_pipeline(*args, **kwargs):
        config = args[3]
        return build_report(
            ProjectProfile(root=str(tmp_path), is_git_repo=True),
            config,
            DiffSummary(target_mode=TargetMode.BASE, base="main", stats=DiffStats(files_changed=1)),
            findings=[finding],
            verifications=[
                FindingVerification(
                    finding=finding,
                    approved=True,
                    confidence=FindingConfidence.HIGH,
                    reason="Concrete diff-caused issue.",
                )
            ],
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: "diff --git a/src/orders.ts b/src/orders.ts\n"
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    result = runner.invoke(app, ["gate", "pre-push"])

    assert result.exit_code == 1
    assert "APEX RAY GATE: BLOCKED" in result.stdout
    assert "Missing tenant predicate" in result.stdout
    assert "After fixing, commit the changes and run git push again." in result.stdout
    assert (tmp_path / ".apex-ray" / "reports" / "pre-push.json").exists()


def test_gate_pre_push_reports_discovery_error_without_traceback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_discovery(*_args, **_kwargs):
        raise DiscoveryError(
            "Project discovery timed out while reading the Git inventory; "
            "increase review.analyzer.timeout_seconds before retrying."
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.diff_base", lambda _root, _base: "")
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fail_discovery)

    result = runner.invoke(
        app,
        ["gate", "pre-push"],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    plain_output = _plain_cli_output(result.output)
    assert "Project discovery timed out" in plain_output
    assert "Traceback" not in plain_output
    assert not (tmp_path / ".apex-ray" / "reports" / "pre-push.json").exists()


def test_gate_pre_push_reports_root_probe_timeout_without_traceback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apex_ray.cli.gate.discover_repo_root",
        lambda _cwd: (_ for _ in ()).throw(
            DiscoveryTimeoutError("Project file discovery timed out while locating the Git repository root.")
        ),
    )

    result = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert result.exit_code == 2
    plain_output = _plain_cli_output(result.output)
    assert "timed out while locating the Git repository root" in plain_output
    assert "Traceback" not in plain_output
    assert not (tmp_path / ".apex-ray" / "reports" / "pre-push.json").exists()


def test_findings_suppress_unblocks_matching_pre_push_finding(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        line=84,
        failure_mode="The changed query can return another tenant's order.",
        evidence="The diff removes tenantId from the lookup predicate.",
        suggested_fix="Restore the tenantId predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
        context_pack_id="src/orders.ts#getOrder:1",
    )
    pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
        diff_snippet=[
            "@@ -83,1 +83,1 @@",
            "-  return orders.find({ id, tenantId });",
            "+  return orders.find({ id });",
        ],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.BASE, base="main", stats=DiffStats(files_changed=1)),
        context_packs=[pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Concrete diff-caused issue.",
            )
        ],
    )
    report_path = tmp_path / ".apex-ray" / "reports" / "pre-push.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    fingerprint = finding_fingerprint(finding)

    suppress = runner.invoke(
        app,
        [
            "findings",
            "suppress",
            fingerprint,
            "--from-report",
            str(report_path),
            "--reason",
            "The repository layer already applies tenant scoping before this helper.",
        ],
        catch_exceptions=False,
    )

    assert suppress.exit_code == 0
    assert f"Suppressed {fingerprint}" in suppress.stdout
    suppression_list = runner.invoke(app, ["findings", "suppressions"], catch_exceptions=False)
    assert suppression_list.exit_code == 0
    assert fingerprint in suppression_list.stdout
    assert "The repository layer already applies tenant scoping" in suppression_list.stdout
    assert (tmp_path / ".apex-ray" / "triage" / "suppressions.json").exists()
    assert (tmp_path / ".apex-ray" / "triage" / "events.jsonl").exists()
    assert "triage/" in (tmp_path / ".apex-ray" / ".gitignore").read_text(encoding="utf-8")

    def fake_run_review_pipeline(*args, **kwargs):
        return report

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: "diff --git a/src/orders.ts b/src/orders.ts\n"
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    gate = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert gate.exit_code == 0
    assert "APEX RAY GATE: PASSED" in gate.stdout
    assert "Suppressed findings: 1" in gate.stdout
    assert fingerprint in gate.stdout


def test_findings_suppress_scopes_incremental_report_to_configured_base(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        line=84,
        failure_mode="The changed query can return another tenant's order.",
        evidence="The diff removes tenantId from the lookup predicate.",
        suggested_fix="Restore the tenantId predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
        context_pack_id="src/orders.ts#getOrder:1",
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(base="release"),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            base="0123456789abcdef..HEAD",
            stats=DiffStats(files_changed=1),
        ),
        context_packs=[ContextPack(id=finding.context_pack_id, file=finding.file)],
        findings=[finding],
    )
    report_path = tmp_path / ".apex-ray" / "reports" / "pre-push.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "findings",
            "suppress",
            finding_fingerprint(finding),
            "--from-report",
            str(report_path),
            "--reason",
            "Confirmed false positive.",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    state = json.loads((tmp_path / ".apex-ray" / "triage" / "suppressions.json").read_text(encoding="utf-8"))
    assert state["suppressions"][0]["target_base_ref"] == "release"


def test_gate_pre_push_reports_stale_suppression_details(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text("review:\n  reports:\n    archive: true\n", encoding="utf-8")
    finding = Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        line=84,
        failure_mode="The changed query can return another tenant's order.",
        evidence="The diff removes tenantId from the lookup predicate.",
        suggested_fix="Restore the tenantId predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
        context_pack_id="src/orders.ts#getOrder:1",
    )
    original_pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
        diff_snippet=[
            "@@ -83,1 +83,1 @@",
            "-  return orders.find({ id, tenantId });",
            "+  return orders.find({ id });",
        ],
    )
    changed_pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
        diff_snippet=[
            "@@ -83,1 +83,1 @@",
            "-  return orders.find({ id, tenantId });",
            "+  return orders.find({ id, accountId });",
        ],
    )
    original_report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.BASE, base="main", stats=DiffStats(files_changed=1)),
        context_packs=[original_pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Concrete diff-caused issue.",
            )
        ],
    )
    changed_report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.BASE, base="main", stats=DiffStats(files_changed=1)),
        context_packs=[changed_pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Concrete diff-caused issue.",
            )
        ],
    )
    report_path = tmp_path / ".apex-ray" / "reports" / "pre-push.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(original_report.model_dump_json(indent=2), encoding="utf-8")
    fingerprint = finding_fingerprint(finding)
    suppress = runner.invoke(
        app,
        [
            "findings",
            "suppress",
            fingerprint,
            "--from-report",
            str(report_path),
            "--reason",
            "The repository layer already applies tenant scoping before this helper.",
        ],
        catch_exceptions=False,
    )

    def fake_run_review_pipeline(*args, **kwargs):
        return changed_report

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: "diff --git a/src/orders.ts b/src/orders.ts\n"
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    gate = runner.invoke(app, ["gate", "pre-push"])
    markdown = (tmp_path / ".apex-ray" / "reports" / "pre-push.md").read_text(encoding="utf-8")
    archive_dirs = list((tmp_path / ".apex-ray" / "reports" / "runs").iterdir())
    triage_snapshot = json.loads((archive_dirs[0] / "pre-push-triage.json").read_text(encoding="utf-8"))

    assert suppress.exit_code == 0
    assert gate.exit_code == 1
    assert "Stale suppressions removed: 1" in gate.stdout
    assert "Prior reason: The repository layer already applies tenant scoping" in gate.stdout
    assert "Re-check the current finding" in gate.stdout
    assert "### Local Triage" in markdown
    assert "Stale suppressions requiring review" in markdown
    assert "The repository layer already applies tenant scoping" in markdown
    assert triage_snapshot["stale_suppressions_count"] == 1
    assert triage_snapshot["stale_suppressions"][0]["finding_fingerprint"] == fingerprint
    assert "tenant scoping" in triage_snapshot["stale_suppressions"][0]["prior_reason"]


def test_gate_pre_push_archives_reports_when_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "review:\n  reports:\n    archive: true\n",
        encoding="utf-8",
    )

    def fake_run_review_pipeline(*args, **kwargs):
        config = args[3]
        return build_report(
            ProjectProfile(root=str(tmp_path), is_git_repo=True),
            config,
            DiffSummary(target_mode=TargetMode.BASE, base="main", stats=DiffStats(files_changed=1)),
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: "diff --git a/src/orders.ts b/src/orders.ts\n"
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    result = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    archive_dirs = list((tmp_path / ".apex-ray" / "reports" / "runs").iterdir())
    assert result.exit_code == 0
    assert "Archived report:" in result.stdout
    assert len(archive_dirs) == 1
    assert (archive_dirs[0] / "pre-push.md").exists()
    assert (archive_dirs[0] / "pre-push.json").exists()
    assert (archive_dirs[0] / "pre-push-triage.json").exists()


def test_gate_pre_push_emits_progress_to_stderr(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "review:\n  gates:\n    pre_push:\n      progress: always\n      progress_interval_seconds: 0\n",
        encoding="utf-8",
    )

    def fake_run_review_pipeline(*args, **kwargs):
        progress = kwargs["progress"]
        progress.event("pipeline progress", force=True)
        config = args[3]
        return build_report(
            ProjectProfile(root=str(tmp_path), is_git_repo=True),
            config,
            DiffSummary(target_mode=TargetMode.BASE, base="main", stats=DiffStats(files_changed=1)),
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: "diff --git a/src/orders.ts b/src/orders.ts\n"
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    result = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "APEX RAY GATE: PASSED" in result.stdout
    assert "pipeline progress" not in result.stdout
    assert "apex-ray: reading diff main...HEAD" in result.stderr
    assert "apex-ray: pipeline progress" in result.stderr


def test_gate_pre_push_warns_for_outdated_agent_artifacts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        "<!-- APEX_RAY_START -->\n## Apex Ray\n\nOld instructions.\n<!-- APEX_RAY_END -->\n",
        encoding="utf-8",
    )

    def fake_run_review_pipeline(*args, **kwargs):
        config = args[3]
        return build_report(
            ProjectProfile(root=str(tmp_path), is_git_repo=True),
            config,
            DiffSummary(target_mode=TargetMode.BASE, base="main", stats=DiffStats(files_changed=1)),
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: "diff --git a/src/orders.ts b/src/orders.ts\n"
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    result = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "APEX RAY GATE: PASSED" in result.stdout
    assert "Warning: Apex Ray agent artifacts are outdated" in result.stderr
    assert "apex-ray init --refresh-agent-artifacts" in result.stderr


def test_gate_pre_push_does_not_block_unverified_finding_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    finding = Finding(
        title="Unverified high issue",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.MEDIUM,
        file="src/orders.ts",
        failure_mode="Potential issue.",
        evidence="Candidate evidence.",
        suggested_fix="Investigate.",
        suggested_test="Add a regression test.",
    )

    def fake_run_review_pipeline(*args, **kwargs):
        config = args[3]
        return build_report(
            ProjectProfile(root=str(tmp_path), is_git_repo=True),
            config,
            DiffSummary(target_mode=TargetMode.BASE, base="main", stats=DiffStats(files_changed=1)),
            findings=[finding],
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: "diff --git a/src/orders.ts b/src/orders.ts\n"
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    result = runner.invoke(app, ["gate", "pre-push"])

    assert result.exit_code == 0
    assert "APEX RAY GATE: PASSED" in result.stdout
    assert "Findings: 1" in result.stdout


def test_gate_pre_push_blocks_critical_partial_coverage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    def fake_run_review_pipeline(*args, **kwargs):
        config = args[3]
        report = build_report(
            ProjectProfile(root=str(tmp_path), is_git_repo=True),
            config,
            DiffSummary(target_mode=TargetMode.BASE, base="main", stats=DiffStats(files_changed=1)),
        )
        report.llm_coverage.partial_severity = "critical"
        report.llm_coverage.partial_reasons = ["1 unreviewed P0 context pack(s)"]
        return report

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: "diff --git a/src/orders.ts b/src/orders.ts\n"
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    result = runner.invoke(app, ["gate", "pre-push"])

    assert result.exit_code == 1
    assert "Partial coverage is critical" in result.stdout


def test_gate_pre_push_incremental_retry_reviews_previous_head_delta(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_incremental_gate_config(tmp_path)
    diff_calls: list[str] = []
    heads = iter(["head-1", "head-2"])

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        diff_calls.append(diff_text)
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            parse_unified_diff(diff_text, target_mode=target_mode, base=kwargs.get("base")),
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: _diff_for("src/orders.ts", "old", "full")
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda _root, old, new: _diff_for("src/orders.ts", old, new),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    first = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert diff_calls == [_diff_for("src/orders.ts", "old", "full"), _diff_for("src/orders.ts", "head-1", "HEAD")]
    assert "Mode: incremental" in second.stdout


def test_gate_pre_push_rejects_incremental_state_from_divergent_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_incremental_gate_config(tmp_path)
    diff_calls: list[str] = []
    heads = iter([_GATE_HEAD_1, _GATE_HEAD_2])

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        diff_calls.append(diff_text)
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            parse_unified_diff(diff_text, target_mode=target_mode, base=kwargs.get("base")),
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: _GATE_MERGE_BASE)
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.is_ancestor",
        lambda _root, _ancestor, _descendant: False,
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for("src/orders.ts", "old", "full"),
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda *_args: pytest.fail("divergent state must not select an incremental range"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    first = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    state = json.loads((tmp_path / ".apex-ray" / "reports" / "pre-push-state.json").read_text(encoding="utf-8"))
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert diff_calls == [
        _diff_for("src/orders.ts", "old", "full"),
        _diff_for("src/orders.ts", "old", "full"),
    ]
    assert "Mode: full" in second.stdout
    assert "previous gate HEAD is not an ancestor of current HEAD" in second.stdout
    assert state["head_sha"] == _GATE_HEAD_2


@pytest.mark.parametrize(
    ("fetch_config", "expected_events"),
    [
        (
            "  gates:\n    pre_push:\n      fetch_base: true\n",
            ["fetch:origin/main", "diff:origin/main"],
        ),
        ("", ["diff:origin/main"]),
    ],
)
def test_gate_pre_push_fetch_base_is_explicit_and_precedes_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_config: str,
    expected_events: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".apex-ray" / "config.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        f"review:\n  base: origin/main\n{fetch_config}",
        encoding="utf-8",
    )
    events: list[str] = []

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            parse_unified_diff(diff_text, target_mode=target_mode, base=kwargs.get("base")),
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.fetch_remote_tracking_ref",
        lambda _root, base: events.append(f"fetch:{base}"),
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, base: events.append(f"diff:{base}") or _diff_for("src/orders.ts", "old", "new"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    result = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert result.exit_code == 0
    assert events == expected_events


def test_gate_pre_push_fetch_base_failure_is_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".apex-ray" / "config.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "review:\n  base: origin/main\n  gates:\n    pre_push:\n      fetch_base: true\n",
        encoding="utf-8",
    )

    def fail_fetch(_root: Path, _base: str) -> None:
        raise git.GitError(["fetch"], "network unavailable", 1)

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.fetch_remote_tracking_ref", fail_fetch)
    monkeypatch.setattr(
        "apex_ray.cli.gate.run_review_pipeline",
        lambda *_args, **_kwargs: pytest.fail("review must not start after a configured fetch failure"),
    )

    result = runner.invoke(app, ["gate", "pre-push"])

    assert result.exit_code == 2
    assert "network unavailable" in _plain_cli_output(result.output)


def test_gate_pre_push_incremental_retry_tracks_reviewer_set_not_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
review:
  reviewers:
    - id: security
      paths: [src/auth/**]
    - id: finance
      paths: [src/payments/**]
  gates:
    pre_push:
      incremental_retry:
        enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    diff_calls: list[str] = []
    heads = iter(["head-1", "head-2", "head-3", "head-4"])

    def fake_run_review_pipeline(root, diff_text, target_mode, review_config, **kwargs):
        diff_calls.append(diff_text)
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            review_config,
            parse_unified_diff(diff_text, target_mode=target_mode, base=kwargs.get("base")),
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for("src/orders.ts", "old", "full"),
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda _root, old, new: _diff_for("src/orders.ts", old, new),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    first = runner.invoke(app, ["gate", "pre-push", "--reviewer", "security"], catch_exceptions=False)
    second = runner.invoke(app, ["gate", "pre-push", "--reviewer", "finance"], catch_exceptions=False)
    third = runner.invoke(
        app,
        ["gate", "pre-push", "--reviewer", "security", "--reviewer", "finance"],
        catch_exceptions=False,
    )
    fourth = runner.invoke(
        app,
        ["gate", "pre-push", "--reviewer", "finance", "--reviewer", "security"],
        catch_exceptions=False,
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert third.exit_code == 0
    assert fourth.exit_code == 0
    assert diff_calls == [
        _diff_for("src/orders.ts", "old", "full"),
        _diff_for("src/orders.ts", "old", "full"),
        _diff_for("src/orders.ts", "old", "full"),
        _diff_for("src/orders.ts", "head-3", "HEAD"),
    ]
    assert "Fallback reason: review config, reviewer scope," in second.stdout
    assert "Mode: incremental" in fourth.stdout


def test_gate_pre_push_auto_followup_preserves_reviewer_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
review:
  reviewers:
    - id: security
      focus: Security boundaries.
  gates:
    pre_push:
      auto_followup_p0: true
      auto_followup_p0_max_pack_reviews: 3
""".lstrip(),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_run_review_pipeline(root, _diff_text, _target_mode, review_config, **kwargs):
        seen["initial_reviewer_ids"] = kwargs["reviewer_ids"]
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            review_config,
            DiffSummary(target_mode=TargetMode.BASE, stats=DiffStats(files_changed=1)),
        )
        report.llm_coverage.partial_severity = "critical"
        return report

    def fake_continue(report, **kwargs):
        seen["followup_reviewer_ids"] = kwargs["reviewer_ids"]
        seen["max_pack_reviews"] = kwargs["max_pack_reviews"]
        report.llm_coverage.partial_severity = "none"
        return report, [object()]

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for("src/auth.ts", "old", "new"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", fake_continue)

    result = runner.invoke(
        app,
        ["gate", "pre-push", "--reviewer", "security"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen == {
        "initial_reviewer_ids": ["security"],
        "followup_reviewer_ids": ["security"],
        "max_pack_reviews": 3,
    }


def test_gate_pre_push_legacy_auto_followup_only_attempts_critical_partial(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
review:
  gates:
    pre_push:
      auto_followup_p0: true
      fail_on_partial_severity: major
""".lstrip(),
        encoding="utf-8",
    )
    continuation_calls = 0

    def fake_run_review_pipeline(root, _diff_text, _target_mode, review_config, **_kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            review_config,
            DiffSummary(target_mode=TargetMode.BASE, stats=DiffStats(files_changed=1)),
        )
        report.llm_coverage.enabled = True
        report.llm_coverage.partial_severity = "major"
        report.llm_coverage.partial_reasons = ["A P0 verifier retry remains."]
        return report

    def fake_continue(report, **_kwargs):
        nonlocal continuation_calls
        continuation_calls += 1
        return report, []

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for("src/auth.ts", "old", "new"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", fake_continue)

    result = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert result.exit_code == 1
    assert continuation_calls == 0


def test_gate_pre_push_generalized_followup_upgrades_shallow_high_risk_pack(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
review:
  gates:
    pre_push:
      auto_followup: true
      auto_followup_max_pack_reviews: 5
      fail_on_partial_severity: major
""".lstrip(),
        encoding="utf-8",
    )
    pack_id = "src/payments.ts#capture:1"
    seen: dict[str, object] = {}

    def fake_run_review_pipeline(root, _diff_text, _target_mode, review_config, **_kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            review_config,
            DiffSummary(target_mode=TargetMode.BASE, stats=DiffStats(files_changed=1)),
        )
        report.llm_coverage.partial_severity = "major"
        report.llm_coverage.shallow_only_high_risk_context_pack_ids = [pack_id]
        return report

    def fake_continue(report, **kwargs):
        seen.update(kwargs)
        report.llm_runs.append(
            LLMRun(
                provider="fake",
                context_pack_id=pack_id,
                status="ok",
                duration_ms=1,
            )
        )
        report.llm_coverage.partial_severity = "none"
        return report, [object()]

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for("src/payments.ts", "old", "new"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", fake_continue)

    result = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert result.exit_code == 0
    assert seen["residual_priorities"] == {"p0", "p1"}
    assert seen["pack_ids"] == {pack_id}
    assert seen["only_unreviewed"] is True
    assert seen["force_review_pack_ids"] == {pack_id}
    assert seen["review_depth"] == "deep"
    assert seen["max_pack_reviews"] == 5


def test_gate_pre_push_generalized_verifier_retry_does_not_force_primary_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
review:
  llm:
    enabled: true
    provider: fake
  reviewers:
    - id: security
      required: true
      verify: true
  gates:
    pre_push:
      auto_followup: true
      fail_on_partial_severity: none
""".lstrip(),
        encoding="utf-8",
    )
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts", file_kind=FileKind.SOURCE)
    seen: dict[str, object] = {}

    def fake_run_review_pipeline(root, _diff_text, _target_mode, review_config, **_kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            review_config,
            DiffSummary(target_mode=TargetMode.BASE, stats=DiffStats(files_changed=1)),
            context_packs=[pack],
        )
        report.llm_coverage.quality_gate_status = "fail"
        report.llm_coverage.quality_gate_reasons = ["Required verifier failed."]
        report.llm_coverage.partial_severity = "critical"
        report.llm_coverage.reviewers[0].status = "fail"
        report.llm_coverage.coverage_todos = [
            LLMCoverageTodo(
                context_pack_id=pack.id,
                file=pack.file,
                file_kind=pack.file_kind,
                reviewer_id="security",
                priority="p0",
                reason="Reviewer security has an active failed verification run.",
            )
        ]
        return report

    def fake_continue(report, **kwargs):
        seen.update(kwargs)
        report.llm_runs.append(
            LLMRun(
                kind="verify",
                provider="fake",
                reviewer_id="security",
                context_pack_id=pack.id,
                status="ok",
                duration_ms=1,
            )
        )
        report.llm_coverage.quality_gate_status = "pass"
        report.llm_coverage.quality_gate_reasons = []
        report.llm_coverage.partial_severity = "none"
        report.llm_coverage.reviewers[0].status = "pass"
        report.llm_coverage.coverage_todos = []
        return report, []

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for(pack.file, "old", "new"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", fake_continue)

    result = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert result.exit_code == 0
    assert seen["pack_ids"] == {pack.id}
    assert seen["force_review_pack_ids"] == set()
    assert seen["reviewer_ids"] == ["security"]


def test_gate_pre_push_generalized_followup_reports_actionable_no_eligible_debt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
review:
  gates:
    pre_push:
      auto_followup: true
      fail_on_partial_severity: major
""".lstrip(),
        encoding="utf-8",
    )

    def fake_run_review_pipeline(root, _diff_text, _target_mode, review_config, **_kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            review_config,
            DiffSummary(target_mode=TargetMode.BASE, stats=DiffStats(files_changed=1)),
        )
        report.llm_coverage.partial_severity = "major"
        report.llm_coverage.partial_reasons = ["legacy report lacks structured coverage debt"]
        return report

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for("src/payments.ts", "old", "new"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr(
        "apex_ray.cli.gate.continue_review_from_report",
        lambda report, **_kwargs: (report, []),
    )

    result = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "Auto-followup made no progress" in result.stdout
    assert "suggested_command" in result.stdout


def test_gate_pre_push_rejects_head_mutation_during_review_before_publishing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    app_path = tmp_path / "app.py"
    app_path.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-qb", "feature"], cwd=tmp_path, check=True)
    app_path.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "feature"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)

    def fake_pipeline(root, diff_text, target_mode, config, **kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(target_mode=target_mode, base=kwargs["base"]),
            input_snapshot=capture_review_input_snapshot(
                root,
                diff_text,
                target_mode,
                base_ref=kwargs["base"],
            ),
        )
        app_path.write_text("value = 3\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "concurrent change"], cwd=tmp_path, check=True)
        return report

    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_pipeline)

    result = runner.invoke(app, ["gate", "pre-push", "--base", "main"], catch_exceptions=False)

    assert result.exit_code == 2
    assert "HEAD changed" in _plain_cli_output(result.output)
    assert not (tmp_path / ".apex-ray" / "reports" / "pre-push.json").exists()


def test_gate_pre_push_manual_continuation_clears_incremental_coverage_debt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".apex-ray" / "config.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
review:
  llm:
    enabled: true
    provider: fake
  reviewers:
    - id: security
      focus: Authorization boundaries.
      paths: [src/auth/**]
      required: true
      verify: false
  gates:
    pre_push:
      auto_followup_p0: false
      incremental_retry:
        enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    packs = [
        ContextPack(
            id="src/auth/session.ts#authorize:1",
            file="src/auth/session.ts",
            file_kind=FileKind.SOURCE,
        ),
        ContextPack(
            id="src/auth/token.ts#verify:1",
            file="src/auth/token.ts",
            file_kind=FileKind.SOURCE,
        ),
    ]
    pipeline_calls = 0
    continuation_calls = 0

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **_kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(
                target_mode=TargetMode.BASE,
                base="main",
                stats=DiffStats(files_changed=1),
            ),
            context_packs=packs,
            input_snapshot=_gate_input_snapshot(
                diff_text,
                target_mode,
                base_ref="main",
                head_sha=_GATE_HEAD_1,
                merge_base_sha=_GATE_MERGE_BASE,
            ),
        )
        report.llm_coverage.enabled = True
        report.llm_coverage.partial_severity = "critical"
        report.llm_coverage.partial_reasons = ["2 required reviewer assignments remain"]
        report.llm_coverage.residual_risk_p0_context_pack_ids = [pack.id for pack in packs]
        report.llm_coverage.unreviewed_context_pack_ids = [pack.id for pack in packs]
        report.llm_coverage.coverage_todos = [
            LLMCoverageTodo(
                context_pack_id=pack.id,
                file=pack.file,
                reviewer_id="security",
                priority="p0",
            )
            for pack in packs
        ]
        return report

    def fake_manual_continue(report, **_kwargs):
        nonlocal continuation_calls
        continuation_calls += 1
        reviewed = packs[:continuation_calls]
        remaining = packs[continuation_calls:]
        report.llm_coverage.partial_severity = "critical" if remaining else "none"
        report.llm_coverage.partial_reasons = (
            [f"{len(remaining)} required reviewer assignment(s) remain"] if remaining else []
        )
        report.llm_coverage.quality_gate_status = "fail" if remaining else "pass"
        report.llm_coverage.quality_gate_reasons = (
            [f"{len(remaining)} required reviewer assignment(s) remain"] if remaining else []
        )
        report.llm_coverage.residual_risk_p0_context_pack_ids = [pack.id for pack in remaining]
        report.llm_coverage.reviewed_context_pack_ids = [pack.id for pack in reviewed]
        report.llm_coverage.unreviewed_context_pack_ids = [pack.id for pack in remaining]
        reviewer_coverage = report.llm_coverage.reviewers[0]
        reviewer_coverage.status = "fail" if remaining else "pass"
        reviewer_coverage.reasons = [f"{len(remaining)} required reviewer assignment(s) remain"] if remaining else []
        reviewer_coverage.matching_context_pack_ids = [pack.id for pack in packs]
        reviewer_coverage.selected_context_pack_ids = [pack.id for pack in packs]
        reviewer_coverage.reviewed_context_pack_ids = [pack.id for pack in reviewed]
        reviewer_coverage.matching_context_packs = len(packs)
        reviewer_coverage.selected_context_packs = len(packs)
        reviewer_coverage.reviewed_context_packs = len(reviewed)
        report.llm_coverage.coverage_todos = [
            LLMCoverageTodo(
                context_pack_id=pack.id,
                file=pack.file,
                reviewer_id="security",
                priority="p0",
            )
            for pack in remaining
        ]
        report.generated_at += timedelta(microseconds=1)
        return report, [reviewed[-1]]

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: _GATE_HEAD_1)
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: _GATE_MERGE_BASE)
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for(packs[0].file, "old", "new"),
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("coverage retry should resume the report")),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.main.continue_review_from_report", fake_manual_continue)

    first = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    report_path = tmp_path / ".apex-ray" / "reports" / "pre-push.json"
    blocked_report = json.loads(report_path.read_text(encoding="utf-8"))
    suggested = blocked_report["llm_coverage"]["coverage_todos"][0]["suggested_command"]

    assert first.exit_code == 1
    assert f"--json {shlex.quote(str(report_path))}" in suggested

    first_continuation = runner.invoke(app, shlex.split(suggested)[1:], catch_exceptions=False)
    continued_report = json.loads(report_path.read_text(encoding="utf-8"))
    second_suggested = continued_report["llm_coverage"]["coverage_todos"][0]["suggested_command"]

    assert first_continuation.exit_code == 0
    assert f"--json {shlex.quote(str(report_path))}" in second_suggested

    second_continuation = runner.invoke(app, shlex.split(second_suggested)[1:], catch_exceptions=False)
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    state = json.loads((tmp_path / ".apex-ray" / "reports" / "pre-push-state.json").read_text(encoding="utf-8"))

    assert second_continuation.exit_code == 0
    assert second.exit_code == 0
    assert pipeline_calls == 1
    assert continuation_calls == 2
    assert state["coverage_debt"]["quality_gate_failed"] is False
    assert state["coverage_debt"]["partial_blocked"] is False


def test_gate_pre_push_bounded_coverage_resume_preserves_pending_head_delta(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".apex-ray" / "config.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
review:
  llm:
    enabled: true
    provider: fake
  gates:
    pre_push:
      auto_followup_p0: true
      auto_followup_p0_max_pack_reviews: 1
      incremental_retry:
        enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    pack = ContextPack(
        id="src/payments/ledger.ts#post:1",
        file="src/payments/ledger.ts",
        file_kind=FileKind.SOURCE,
    )
    heads = iter([_GATE_HEAD_1, _GATE_HEAD_2, _GATE_HEAD_2])
    pipeline_calls = 0
    continuation_calls = 0
    range_calls: list[tuple[str, str]] = []

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        report_head = _GATE_HEAD_1 if pipeline_calls == 1 else _GATE_HEAD_2
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(
                target_mode=target_mode,
                base=kwargs.get("base"),
                stats=DiffStats(files_changed=1),
            ),
            context_packs=[pack] if pipeline_calls == 1 else [],
            input_snapshot=_gate_input_snapshot(
                diff_text,
                target_mode,
                base_ref=kwargs.get("base"),
                head_sha=report_head,
                merge_base_sha=_GATE_MERGE_BASE if target_mode == TargetMode.BASE else None,
                range_start_sha=_GATE_HEAD_1 if target_mode == TargetMode.PATCH else None,
            ),
        )
        if pipeline_calls == 1:
            report.llm_coverage.enabled = True
            report.llm_coverage.partial_severity = "critical"
            report.llm_coverage.partial_reasons = ["1 residual P0 assignment remains"]
            report.llm_coverage.residual_risk_p0_context_pack_ids = [pack.id]
            report.llm_coverage.unreviewed_context_pack_ids = [pack.id]
            report.llm_coverage.coverage_todos = [
                LLMCoverageTodo(
                    context_pack_id=pack.id,
                    file=pack.file,
                    priority="p0",
                )
            ]
        return report

    def fake_continue(report, **kwargs):
        nonlocal continuation_calls
        continuation_calls += 1
        assert kwargs["max_pack_reviews"] == 1
        report.generated_at += timedelta(microseconds=1)
        if continuation_calls == 2:
            report.llm_coverage.partial_severity = "none"
            report.llm_coverage.partial_reasons = []
            report.llm_coverage.residual_risk_p0_context_pack_ids = []
            report.llm_coverage.reviewed_context_pack_ids = [pack.id]
            report.llm_coverage.unreviewed_context_pack_ids = []
            report.llm_coverage.coverage_todos = []
        return report, [pack]

    def fake_diff_range(_root, old: str, new: str) -> str:
        range_calls.append((old, new))
        return _diff_for("src/payments/new-entry.ts", "old", "new")

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: _GATE_MERGE_BASE)
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for(pack.file, "old", "new"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.git.diff_range", fake_diff_range)
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", fake_continue)
    monkeypatch.setattr("apex_ray.cli.gate.validate_review_input_snapshot", lambda *_args, **_kwargs: "current")

    first = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    pipeline_calls_after_resume = pipeline_calls
    state_after_resume = json.loads(
        (tmp_path / ".apex-ray" / "reports" / "pre-push-state.json").read_text(encoding="utf-8")
    )
    third = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert [first.exit_code, second.exit_code, third.exit_code] == [1, 1, 0]
    assert "Mode: coverage-resume" in second.stdout
    assert "New commits are pending review" in second.stdout
    assert pipeline_calls_after_resume == 1
    assert state_after_resume["head_sha"] == _GATE_HEAD_1
    assert state_after_resume["coverage_debt"]["partial_blocked"] is False
    assert pipeline_calls == 2
    assert continuation_calls == 2
    assert range_calls == [(_GATE_HEAD_1, "HEAD")]


@pytest.mark.parametrize(
    ("auto_followup_p0", "auto_followup", "expected_continuation_calls"),
    [(True, False, 2), (False, False, 0), (False, True, 2)],
)
def test_gate_pre_push_no_progress_coverage_resume_refreshes_pending_head(
    tmp_path: Path,
    monkeypatch,
    auto_followup_p0: bool,
    auto_followup: bool,
    expected_continuation_calls: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".apex-ray" / "config.yml"
    config_path.parent.mkdir(parents=True)
    auto_followup_config = (
        "      auto_followup: true\n      auto_followup_max_pack_reviews: 1\n" if auto_followup else ""
    )
    config_path.write_text(
        f"""
review:
  llm:
    enabled: true
    provider: fake
  gates:
    pre_push:
      auto_followup_p0: {str(auto_followup_p0).lower()}
      auto_followup_p0_max_pack_reviews: 1
{auto_followup_config}      incremental_retry:
        enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    p1_pack = ContextPack(
        id="src/service.ts#run:1",
        file="src/service.ts",
        file_kind=FileKind.SOURCE,
    )
    heads = iter([_GATE_HEAD_1, _GATE_HEAD_2])
    pipeline_calls = 0
    continuation_calls = 0
    base_diff_calls = 0

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        report_head = _GATE_HEAD_1 if pipeline_calls == 1 else _GATE_HEAD_2
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(
                target_mode=target_mode,
                base=kwargs.get("base"),
                stats=DiffStats(files_changed=1),
            ),
            context_packs=[p1_pack] if pipeline_calls == 1 else [],
            input_snapshot=_gate_input_snapshot(
                diff_text,
                target_mode,
                base_ref=kwargs.get("base"),
                head_sha=report_head,
                merge_base_sha=_GATE_MERGE_BASE,
            ),
        )
        if pipeline_calls == 1:
            report.llm_coverage.enabled = True
            report.llm_coverage.quality_gate_status = "fail"
            report.llm_coverage.quality_gate_reasons = ["required P1 reviewer assignment remains"]
            report.llm_coverage.partial_severity = "critical"
            report.llm_coverage.partial_reasons = ["required P1 reviewer assignment remains"]
            report.llm_coverage.residual_risk_p1_context_pack_ids = [p1_pack.id]
            report.llm_coverage.unreviewed_context_pack_ids = [p1_pack.id]
            report.llm_coverage.coverage_todos = [
                LLMCoverageTodo(
                    context_pack_id=p1_pack.id,
                    file=p1_pack.file,
                    priority="p1",
                )
            ]
        return report

    def fake_continue(report, **_kwargs):
        nonlocal continuation_calls
        continuation_calls += 1
        return report, []

    def fake_diff_base(_root, _base):
        nonlocal base_diff_calls
        base_diff_calls += 1
        return _diff_for(p1_pack.file, "old", "new")

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: _GATE_MERGE_BASE)
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.diff_base", fake_diff_base)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a no-progress coverage retry must refresh the full current range")
        ),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", fake_continue)
    monkeypatch.setattr("apex_ray.cli.gate.validate_review_input_snapshot", lambda *_args, **_kwargs: "current")

    first = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    state = json.loads((tmp_path / ".apex-ray" / "reports" / "pre-push-state.json").read_text(encoding="utf-8"))
    refreshed_report = json.loads((tmp_path / ".apex-ray" / "reports" / "pre-push.json").read_text(encoding="utf-8"))

    assert [first.exit_code, second.exit_code] == [1, 0]
    assert pipeline_calls == 2
    assert continuation_calls == expected_continuation_calls
    assert base_diff_calls == 2
    assert "New commits are pending review" not in second.stdout
    assert "coverage retry made no progress while newer commits were pending" in second.stdout
    assert "Auto-followup made no progress" not in second.stdout
    assert state["head_sha"] == _GATE_HEAD_2
    assert state["coverage_debt"]["quality_gate_failed"] is False
    assert state["coverage_debt"]["partial_blocked"] is False
    assert refreshed_report["input_snapshot"]["head_sha"] == _GATE_HEAD_2


def test_gate_pre_push_coverage_resume_keeps_older_carried_blocker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_incremental_gate_config(tmp_path, llm_enabled=True)
    finding = _blocking_finding()
    finding_pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
    )
    delta_pack = ContextPack(
        id="src/other.ts#change:1",
        file="src/other.ts",
        file_kind=FileKind.SOURCE,
    )
    heads = iter([_GATE_HEAD_1, _GATE_HEAD_2, _GATE_HEAD_2])
    pipeline_calls = 0
    continuation_calls = 0

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        if pipeline_calls == 1:
            return _gate_report(
                root,
                config,
                diff_text,
                target_mode,
                kwargs.get("base"),
                finding,
                [finding_pack],
                [
                    LLMRun(
                        provider="fake",
                        context_pack_id=finding_pack.id,
                        status="ok",
                        duration_ms=1,
                    )
                ],
            )
        report = _gate_report(
            root,
            config,
            diff_text,
            target_mode,
            kwargs.get("base"),
            None,
            [delta_pack],
        )
        report.input_snapshot = _gate_input_snapshot(
            diff_text,
            target_mode,
            base_ref=kwargs.get("base"),
            head_sha=_GATE_HEAD_2,
            range_start_sha=_GATE_HEAD_1,
        )
        report.llm_coverage.partial_severity = "critical"
        report.llm_coverage.partial_reasons = ["1 residual P0 assignment remains"]
        report.llm_coverage.residual_risk_p0_context_pack_ids = [delta_pack.id]
        report.llm_coverage.unreviewed_context_pack_ids = [delta_pack.id]
        report.llm_coverage.coverage_todos = [
            LLMCoverageTodo(
                context_pack_id=delta_pack.id,
                file=delta_pack.file,
                priority="p0",
            )
        ]
        return report

    def fake_continue(report, **_kwargs):
        nonlocal continuation_calls
        continuation_calls += 1
        report.generated_at += timedelta(microseconds=1)
        if continuation_calls == 2:
            report.llm_coverage.partial_severity = "none"
            report.llm_coverage.partial_reasons = []
            report.llm_coverage.residual_risk_p0_context_pack_ids = []
            report.llm_coverage.reviewed_context_pack_ids = [delta_pack.id]
            report.llm_coverage.unreviewed_context_pack_ids = []
            report.llm_coverage.coverage_todos = []
        return report, [delta_pack]

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: _GATE_MERGE_BASE)
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for(finding.file, "tenant lookup", "unscoped lookup"),
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda _root, _old, _new: _diff_for(delta_pack.file, "old", "new"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", fake_continue)
    monkeypatch.setattr("apex_ray.cli.gate.validate_review_input_snapshot", lambda *_args, **_kwargs: "current")

    first = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    third = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    state = json.loads((tmp_path / ".apex-ray" / "reports" / "pre-push-state.json").read_text(encoding="utf-8"))

    assert [first.exit_code, second.exit_code, third.exit_code] == [1, 1, 1]
    assert "Mode: coverage-resume" in third.stdout
    assert "Missing tenant predicate" in third.stdout
    assert state["coverage_debt"]["partial_blocked"] is False
    assert [item["finding"]["title"] for item in state["active_findings"]] == ["Missing tenant predicate"]


def test_gate_pre_push_preserves_unchanged_noncritical_quality_debt_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".apex-ray" / "config.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
review:
  llm:
    enabled: true
    provider: fake
  gates:
    pre_push:
      auto_followup_p0: false
      incremental_retry:
        enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    pack = ContextPack(id="src/service.ts#run:1", file="src/service.ts")
    pipeline_calls = 0

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **_kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(
                target_mode=TargetMode.BASE,
                base="main",
                stats=DiffStats(files_changed=1),
            ),
            context_packs=[pack],
            input_snapshot=_gate_input_snapshot(
                diff_text,
                target_mode,
                base_ref="main",
                head_sha=_GATE_HEAD_1,
                merge_base_sha=_GATE_MERGE_BASE,
            ),
        )
        report.llm_coverage.quality_gate_status = "fail"
        report.llm_coverage.quality_gate_reasons = ["required reviewer failed"]
        report.llm_coverage.partial_severity = "major"
        report.llm_coverage.partial_reasons = ["review retry required"]
        report.llm_coverage.coverage_todos = [
            LLMCoverageTodo(
                context_pack_id=pack.id,
                file=pack.file,
                priority="p1",
            )
        ]
        return report

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: _GATE_HEAD_1)
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: _GATE_MERGE_BASE)
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base",
        lambda _root, _base: _diff_for(pack.file, "old", "new"),
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("quality debt report must be preserved")),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)

    first = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert [first.exit_code, second.exit_code] == [1, 1]
    assert "Mode: coverage-resume" in second.stdout
    assert pipeline_calls == 1


def test_gate_pre_push_missing_coverage_report_falls_back_to_full_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".apex-ray" / "config.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
review:
  llm:
    enabled: true
    provider: fake
  gates:
    pre_push:
      auto_followup_p0: false
      incremental_retry:
        enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    pack = ContextPack(id="src/service.ts#run:1", file="src/service.ts")
    finding = _blocking_finding()
    pipeline_calls = 0
    base_diff_calls = 0

    def fake_run_review_pipeline(root, _diff_text, _target_mode, config, **_kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(
                target_mode=TargetMode.BASE,
                base="main",
                stats=DiffStats(files_changed=1),
            ),
            context_packs=[pack],
            findings=[finding] if pipeline_calls == 1 else [],
            verifications=[
                FindingVerification(
                    finding=finding,
                    approved=True,
                    confidence=FindingConfidence.HIGH,
                    reason="Concrete diff-caused issue.",
                )
            ]
            if pipeline_calls == 1
            else [],
        )
        if pipeline_calls == 1:
            report.llm_coverage.partial_severity = "critical"
            report.llm_coverage.partial_reasons = ["1 residual P0 assignment remains"]
            report.llm_coverage.coverage_todos = [
                LLMCoverageTodo(
                    context_pack_id=pack.id,
                    file=pack.file,
                    priority="p0",
                )
            ]
        else:
            report.llm_coverage.quality_gate_status = "pass"
            report.llm_coverage.quality_gate_reasons = []
            report.llm_coverage.partial_severity = "none"
            report.llm_coverage.partial_reasons = []
            report.llm_coverage.reviewed_context_pack_ids = [pack.id]
            report.llm_coverage.unreviewed_context_pack_ids = []
            report.llm_coverage.coverage_todos = []
        return report

    def fake_diff_base(_root, _base):
        nonlocal base_diff_calls
        base_diff_calls += 1
        return _diff_for(pack.file, "old", "new")

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: "head-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.diff_base", fake_diff_base)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("missing debt report requires full fallback")),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr(
        "apex_ray.cli.gate.resolve_carried_findings",
        lambda carried_findings, *_args, **_kwargs: carried_findings,
    )

    first = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    (tmp_path / ".apex-ray" / "reports" / "pre-push.json").unlink()
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    state = json.loads((tmp_path / ".apex-ray" / "reports" / "pre-push-state.json").read_text(encoding="utf-8"))

    assert [first.exit_code, second.exit_code] == [1, 1]
    assert "Mode: full" in second.stdout
    assert "coverage-debt report is unavailable" in second.stdout
    assert "Missing tenant predicate" in second.stdout
    assert [item["finding"]["title"] for item in state["active_findings"]] == ["Missing tenant predicate"]
    assert pipeline_calls == 2
    assert base_diff_calls == 2


def test_gate_pre_push_incremental_retry_carries_blocker_when_unrelated_delta(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_incremental_gate_config(tmp_path)
    finding = _blocking_finding()
    heads = iter(["head-1", "head-2"])
    diff_texts: list[str] = []

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        diff_texts.append(diff_text)
        report_finding = finding if len(diff_texts) == 1 else None
        return _gate_report(root, config, diff_text, target_mode, kwargs.get("base"), report_finding)

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: _diff_for("src/orders.ts", "old", "full")
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda _root, _old, _new: _diff_for("src/other.ts", "before", "after"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    first = runner.invoke(app, ["gate", "pre-push"])
    second = runner.invoke(app, ["gate", "pre-push"])

    assert first.exit_code == 1
    assert second.exit_code == 1
    assert "Still blocking carried findings: 1" in second.stdout
    assert "Missing tenant predicate" in second.stdout


def test_gate_pre_push_incremental_retry_drops_stale_carried_blocker_when_evidence_is_gone(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_incremental_gate_config(tmp_path)
    finding = _blocking_finding()
    old_changed_line = "const order = findOrder({ id: orderId });"
    fixed_line = "const order = findOrder({ id: orderId, tenantId });"
    source_file = tmp_path / "src" / "orders.ts"
    source_file.parent.mkdir()
    source_file.write_text(f"{old_changed_line}\n", encoding="utf-8")
    head_source = {"src/orders.ts": f"{old_changed_line}\n"}
    context_pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
        file_kind=FileKind.SOURCE,
        diff_snippet=[
            "@@ -1 +1 @@",
            "-const order = findOrder({ id: orderId, tenantId });",
            f"+{old_changed_line}",
        ],
    )
    heads = iter(["head-1", "head-2"])
    run_count = 0

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        nonlocal run_count
        run_count += 1
        report_finding = finding if run_count == 1 else None
        return _gate_report(
            root,
            config,
            diff_text,
            target_mode,
            kwargs.get("base"),
            report_finding,
            context_packs=[context_pack] if report_finding is not None else [],
        )

    def fake_run_git(args, cwd, check=True, *, errors="replace"):
        assert errors in {"replace", "strict"}
        if args[:1] == ["show"] and args[1].startswith("HEAD:"):
            path = args[1].removeprefix("HEAD:")
            stdout = head_source.get(path, "")
            return subprocess.CompletedProcess(args, 0 if path in head_source else 1, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="unexpected git call")

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: _diff_for("src/orders.ts", "old", "full")
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda _root, _old, _new: _diff_for("src/other.ts", "before", "after"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))
    monkeypatch.setattr("apex_ray.gate_retry.git.run_git", fake_run_git)

    first = runner.invoke(app, ["gate", "pre-push"])
    head_source["src/orders.ts"] = f"{fixed_line}\n"
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    state = json.loads((tmp_path / ".apex-ray" / "reports" / "pre-push-state.json").read_text(encoding="utf-8"))

    assert first.exit_code == 1
    assert second.exit_code == 0
    assert "Resolved carried findings: 1" in second.stdout
    assert state["active_findings"] == []


def test_gate_pre_push_incremental_retry_resolved_carried_blocker_passes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_incremental_gate_config(tmp_path)
    finding = _blocking_finding()
    heads = iter(["head-1", "head-2"])
    run_count = 0

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        nonlocal run_count
        run_count += 1
        report_finding = finding if run_count == 1 else None
        return _gate_report(root, config, diff_text, target_mode, kwargs.get("base"), report_finding)

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: _diff_for("src/orders.ts", "old", "full")
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda _root, _old, _new: _diff_for("src/orders.ts", "before", "after"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))
    monkeypatch.setattr("apex_ray.cli.gate.resolve_carried_findings", lambda *args, **kwargs: [])

    first = runner.invoke(app, ["gate", "pre-push"])
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert first.exit_code == 1
    assert second.exit_code == 0
    assert "Resolved carried findings: 1" in second.stdout


def test_gate_pre_push_incremental_retry_drops_carried_blocker_after_pack_reviewed_clean(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_incremental_gate_config(tmp_path, llm_enabled=True)
    finding = _blocking_finding()
    pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
        diff_snippet=["@@ -84,1 +84,1 @@", "-  query({ id, tenantId })", "+  query({ id })"],
    )
    heads = iter(["head-1", "head-2"])
    run_count = 0
    resolver = FakeLLMProvider(resolution_statuses=[FindingResolutionStatus.RESOLVED])

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        nonlocal run_count
        run_count += 1
        report_finding = finding if run_count == 1 else None
        return _gate_report(
            root,
            config,
            diff_text,
            target_mode,
            kwargs.get("base"),
            report_finding,
            [pack],
            llm_runs=[
                LLMRun(
                    provider="fake",
                    context_pack_id=finding.context_pack_id,
                    status="ok",
                    duration_ms=1,
                    findings_count=1 if report_finding is not None else 0,
                )
            ],
        )

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: _diff_for("src/orders.ts", "old", "full")
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda _root, _old, _new: _diff_for("src/orders.ts", "before", "after"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))
    monkeypatch.setattr("apex_ray.cli.gate.provider_from_config", lambda _config: resolver)

    first = runner.invoke(app, ["gate", "pre-push"])
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)
    state = json.loads((tmp_path / ".apex-ray" / "reports" / "pre-push-state.json").read_text(encoding="utf-8"))

    assert first.exit_code == 1
    assert second.exit_code == 0
    assert "Resolved carried findings: 1" in second.stdout
    assert state["active_findings"] == []
    assert resolver.resolved_finding_titles == ["Missing tenant predicate"]


def test_gate_pre_push_incremental_retry_suppresses_carried_finding(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_incremental_gate_config(tmp_path)
    finding = _blocking_finding()
    pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
        diff_snippet=["@@ -84,1 +84,1 @@", "-  query({ id, tenantId })", "+  query({ id })"],
    )
    heads = iter(["head-1", "head-2"])
    run_count = 0

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        nonlocal run_count
        run_count += 1
        report_finding = finding if run_count == 1 else None
        return _gate_report(root, config, diff_text, target_mode, kwargs.get("base"), report_finding, [pack])

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: _diff_for("src/orders.ts", "old", "full")
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda _root, _old, _new: _diff_for("src/orders.ts", "before", "after"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    first = runner.invoke(app, ["gate", "pre-push"])
    suppress = runner.invoke(
        app,
        [
            "findings",
            "suppress",
            finding_fingerprint(finding),
            "--from-report",
            str(tmp_path / ".apex-ray" / "reports" / "pre-push.json"),
            "--reason",
            "The repository layer already applies tenant scoping before this helper.",
        ],
        catch_exceptions=False,
    )
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert first.exit_code == 1
    assert suppress.exit_code == 0
    assert second.exit_code == 0
    assert "Suppressed findings: 1" in second.stdout
    assert "Carried blocking findings" not in second.stdout


def test_gate_pre_push_incremental_retry_uncertain_resolution_blocks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_incremental_gate_config(tmp_path)
    finding = _blocking_finding()
    heads = iter(["head-1", "head-2"])
    run_count = 0

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        nonlocal run_count
        run_count += 1
        report_finding = finding if run_count == 1 else None
        return _gate_report(root, config, diff_text, target_mode, kwargs.get("base"), report_finding)

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: _diff_for("src/orders.ts", "old", "full")
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda _root, _old, _new: _diff_for("src/orders.ts", "before", "after"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    first = runner.invoke(app, ["gate", "pre-push"])
    second = runner.invoke(app, ["gate", "pre-push"])

    assert first.exit_code == 1
    assert second.exit_code == 1
    assert "Uncertain carried findings: 1" in second.stdout
    assert "Missing tenant predicate" in second.stdout


def test_gate_pre_push_incremental_retry_uses_resolution_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_incremental_gate_config(tmp_path, llm_enabled=True)
    finding = _blocking_finding()
    provider = FakeLLMProvider(resolution_statuses=[FindingResolutionStatus.RESOLVED])
    heads = iter(["head-1", "head-2"])
    run_count = 0

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        nonlocal run_count
        run_count += 1
        report_finding = finding if run_count == 1 else None
        return _gate_report(root, config, diff_text, target_mode, kwargs.get("base"), report_finding)

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd, **_kwargs: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_ancestor", lambda _root, _ancestor, _descendant: True)
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_base", lambda _root, _base: _diff_for("src/orders.ts", "old", "full")
    )
    monkeypatch.setattr(
        "apex_ray.cli.gate.git.diff_range",
        lambda _root, _old, _new: _diff_for("src/orders.ts", "before", "after"),
    )
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))
    monkeypatch.setattr("apex_ray.cli.gate.provider_from_config", lambda _config: provider)

    first = runner.invoke(app, ["gate", "pre-push"])
    second = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert first.exit_code == 1
    assert second.exit_code == 0
    assert provider.resolved_finding_titles == ["Missing tenant predicate"]


def _write_incremental_gate_config(root: Path, *, llm_enabled: bool = False) -> None:
    config = root / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    llm_text = "  llm:\n    enabled: true\n    provider: fake\n" if llm_enabled else ""
    config.write_text(
        f"review:\n{llm_text}  gates:\n    pre_push:\n      incremental_retry:\n        enabled: true\n",
        encoding="utf-8",
    )


def _diff_for(path: str, old_value: str, new_value: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old_value}\n+{new_value}\n"


def _gate_input_snapshot(
    diff_text: str,
    target_mode: TargetMode,
    *,
    base_ref: str | None,
    head_sha: str,
    merge_base_sha: str | None = None,
    range_start_sha: str | None = None,
) -> ReviewInputSnapshot:
    return ReviewInputSnapshot(
        target_mode=target_mode,
        base_ref=base_ref,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        range_start_sha=range_start_sha,
        diff_sha256=hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
    )


def _blocking_finding() -> Finding:
    return Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        line=84,
        failure_mode="The changed query can return another tenant's order.",
        evidence="The diff removes tenantId from the lookup predicate.",
        suggested_fix="Restore the tenantId predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
        context_pack_id="src/orders.ts#getOrder:1",
    )


def _gate_report(
    root: Path,
    config: ReviewConfig,
    diff_text: str,
    target_mode: TargetMode,
    base: str | None,
    finding: Finding | None,
    context_packs: list[ContextPack] | None = None,
    llm_runs: list[LLMRun] | None = None,
):
    findings = [finding] if finding is not None else []
    verifications = (
        [
            FindingVerification(
                finding=finding,
                approved=True,
                confidence=FindingConfidence.HIGH,
                reason="Concrete diff-caused issue.",
            )
        ]
        if finding is not None
        else []
    )
    return build_report(
        ProjectProfile(root=str(root), is_git_repo=True),
        config,
        parse_unified_diff(diff_text, target_mode=target_mode, base=base),
        context_packs=context_packs,
        findings=findings,
        verifications=verifications,
        llm_runs=llm_runs,
    )


def test_review_patch_reports_explicit_config_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config = tmp_path / "replay-config.yml"
    config.write_text("review:\n  ignore: []\n", encoding="utf-8")
    output = tmp_path / "review.md"
    json_output = tmp_path / "review.json"

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--config",
            str(config),
            "--output",
            str(output),
            "--json",
            str(json_output),
        ],
        catch_exceptions=False,
    )

    data = json.loads(json_output.read_text(encoding="utf-8"))
    assert result.exit_code == 0
    assert data["project"]["config_path"] == str(config)
    assert f"- Config: `{config}`" in output.read_text(encoding="utf-8")


def test_review_json_preserves_portable_local_data_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config = tmp_path / "portable-config.yml"
    config.write_text(
        """
review:
  local_data:
    root: .apex-ray/private
  llm:
    enabled: false
    cache_dir: ${local_data}/cache/llm
  reports:
    archive: false
    archive_dir: ${local_data}/reports/runs
  triage:
    enabled: false
    state_path: ${local_data}/triage/suppressions.json
    events_path: ${local_data}/triage/events.jsonl
""",
        encoding="utf-8",
    )
    json_output = tmp_path / "review.json"

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--config",
            str(config),
            "--output",
            str(tmp_path / "review.md"),
            "--json",
            str(json_output),
        ],
        catch_exceptions=False,
    )

    report_config = json.loads(json_output.read_text(encoding="utf-8"))["config"]
    assert result.exit_code == 0
    assert report_config["llm"]["cache_dir"] == "${local_data}/cache/llm"
    assert report_config["reports"]["archive_dir"] == "${local_data}/reports/runs"
    assert report_config["triage"]["state_path"] == "${local_data}/triage/suppressions.json"
    assert report_config["triage"]["events_path"] == "${local_data}/triage/events.jsonl"
    assert str(tmp_path) not in json.dumps(report_config)


def test_gate_pre_push_json_preserves_portable_local_data_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
review:
  local_data:
    root: .apex-ray/private
  llm:
    enabled: false
    cache_dir: ${local_data}/cache/llm
  reports:
    archive: false
    archive_dir: ${local_data}/reports/runs
  triage:
    enabled: false
    state_path: ${local_data}/triage/suppressions.json
    events_path: ${local_data}/triage/events.jsonl
""",
        encoding="utf-8",
    )

    def fake_run_review_pipeline(*args, **_kwargs):
        runtime_config = args[3]
        assert Path(runtime_config.llm.cache_dir).is_absolute()
        return build_report(
            ProjectProfile(root=str(tmp_path), is_git_repo=True),
            runtime_config,
            DiffSummary(target_mode=TargetMode.BASE, base="main", stats=DiffStats(files_changed=1)),
        )

    monkeypatch.setattr("apex_ray.cli.gate.discover_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.diff_base", lambda _root, _base: "")
    monkeypatch.setattr("apex_ray.cli.gate.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.gate.continue_review_from_report", lambda report, **_kwargs: (report, []))

    result = runner.invoke(app, ["gate", "pre-push"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    report_path = tmp_path / ".apex-ray" / "reports" / "pre-push.json"
    report_config = json.loads(report_path.read_text(encoding="utf-8"))["config"]
    assert report_config["llm"]["cache_dir"] == "${local_data}/cache/llm"
    assert report_config["reports"]["archive_dir"] == "${local_data}/reports/runs"
    assert report_config["triage"]["state_path"] == "${local_data}/triage/suppressions.json"
    assert report_config["triage"]["events_path"] == "${local_data}/triage/events.jsonl"
    assert str(tmp_path) not in json.dumps(report_config)


def test_review_passes_repeatable_reviewer_selection_to_pipeline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config_path = tmp_path / "reviewers.yml"
    config_path.write_text(
        """
review:
  reviewers:
    - id: security
      focus: Security boundaries.
    - id: finance
      focus: Financial correctness.
""",
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_run_review_pipeline(root, diff_text, target_mode, config, **kwargs):
        seen["reviewer_ids"] = kwargs.get("reviewer_ids")
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=False),
            config,
            parse_unified_diff(diff_text, target_mode=target_mode),
        )

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_run_review_pipeline)

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--config",
            str(config_path),
            "--reviewer",
            "finance",
            "--reviewer",
            "security",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen["reviewer_ids"] == ["finance", "security"]


def test_review_rejects_base_with_diff(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")

    result = runner.invoke(app, ["review", "--base", "main", "--diff", str(patch)])

    assert result.exit_code != 0
    assert "Use only one review target" in result.output


def test_review_rejects_same_markdown_and_json_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    output = tmp_path / "review.out"

    result = runner.invoke(app, ["review", "--diff", str(patch), "--output", str(output), "--json", str(output)])

    assert result.exit_code != 0
    assert "output paths must be different" in result.output


def test_review_rejects_same_markdown_and_html_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    output = tmp_path / "review.out"

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--output",
            str(output),
            "--json",
            str(tmp_path / "review.json"),
            "--html",
            str(output),
        ],
    )

    assert result.exit_code != 0
    assert "Markdown and HTML output paths must be different" in result.output


def test_review_writes_sarif_and_rejects_duplicate_output_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    sarif_output = tmp_path / "review.sarif"

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--output",
            str(tmp_path / "review.md"),
            "--json",
            str(tmp_path / "review.json"),
            "--sarif",
            str(sarif_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(sarif_output.read_text(encoding="utf-8"))["version"] == "2.1.0"
    duplicate = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--output",
            str(tmp_path / "duplicate.md"),
            "--json",
            str(tmp_path / "duplicate.json"),
            "--sarif",
            str(tmp_path / "duplicate.json"),
        ],
    )
    assert duplicate.exit_code != 0
    assert "JSON and SARIF output paths must be different" in duplicate.output


def test_review_continue_from_respects_configured_llm_default(tmp_path: Path, monkeypatch) -> None:
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
    )
    report_path = tmp_path / "review.json"
    output = tmp_path / "continued.md"
    json_output = tmp_path / "continued.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    seen: dict[str, bool] = {}

    def fake_continue(*args, **kwargs):
        seen["llm_enabled"] = kwargs["config"].llm.enabled
        return report, [object()]

    monkeypatch.setattr("apex_ray.cli.main.continue_review_from_report", fake_continue)

    result = runner.invoke(
        app,
        ["review", "--continue-from", str(report_path), "--output", str(output), "--json", str(json_output)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen["llm_enabled"] is False


def test_review_continue_from_can_enable_llm_explicitly(tmp_path: Path, monkeypatch) -> None:
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
    )
    report_path = tmp_path / "review.json"
    output = tmp_path / "continued.md"
    json_output = tmp_path / "continued.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    seen: dict[str, bool] = {}

    def fake_continue(*args, **kwargs):
        seen["llm_enabled"] = kwargs["config"].llm.enabled
        return report, [object()]

    monkeypatch.setattr("apex_ray.cli.main.continue_review_from_report", fake_continue)

    result = runner.invoke(
        app,
        [
            "review",
            "--continue-from",
            str(report_path),
            "--llm",
            "--output",
            str(output),
            "--json",
            str(json_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen["llm_enabled"] is True


def test_review_continue_from_honors_effective_reviewer_pack_cap(tmp_path: Path, monkeypatch) -> None:
    config = ReviewConfig(
        reviewers=[
            ReviewerConfig(id="correctness", max_packs=7),
            ReviewerConfig(id="security", max_packs=3),
        ]
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
    )
    report_path = tmp_path / "review.json"
    output = tmp_path / "continued.md"
    json_output = tmp_path / "continued.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_continue(*args, **kwargs):
        seen["max_pack_reviews"] = kwargs.get("max_pack_reviews")
        seen["respect_config_budgets"] = kwargs.get("respect_config_budgets")
        return report, [object()]

    monkeypatch.setattr("apex_ray.cli.main.continue_review_from_report", fake_continue)

    result = runner.invoke(
        app,
        [
            "review",
            "--continue-from",
            str(report_path),
            "--llm",
            "--reviewer",
            "correctness",
            "--llm-max-packs",
            "2",
            "--output",
            str(output),
            "--json",
            str(json_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen == {"max_pack_reviews": 2, "respect_config_budgets": True}


def test_review_continue_from_handles_config_with_no_enabled_reviewers(tmp_path: Path, monkeypatch) -> None:
    config = ReviewConfig(reviewers=[ReviewerConfig(id="disabled", enabled=False)])
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    report_path = tmp_path / "review.json"
    output = tmp_path / "continued.md"
    json_output = tmp_path / "continued.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_continue(*args, **kwargs):
        seen["max_pack_reviews"] = kwargs.get("max_pack_reviews")
        return report, []

    monkeypatch.setattr("apex_ray.cli.main.continue_review_from_report", fake_continue)

    result = runner.invoke(
        app,
        [
            "review",
            "--continue-from",
            str(report_path),
            "--llm",
            "--output",
            str(output),
            "--json",
            str(json_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen == {"max_pack_reviews": None}


def test_review_rejects_stale_base_snapshot_before_continuation(tmp_path: Path, monkeypatch) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-qb", "feature"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "feature"], cwd=tmp_path, check=True)
    diff_text = subprocess.run(
        ["git", "diff", "main...HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.BASE, base="main"),
        input_snapshot=capture_review_input_snapshot(
            tmp_path,
            diff_text,
            TargetMode.BASE,
            base_ref="main",
        ),
    )
    report_path = tmp_path / "review.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    original = report_path.read_text(encoding="utf-8")
    (tmp_path / "app.py").write_text("value = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "new head"], cwd=tmp_path, check=True)
    monkeypatch.setattr(
        "apex_ray.cli.main.continue_review_from_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stale report must not run")),
    )

    result = runner.invoke(
        app,
        ["review", "--continue-from", str(report_path), "--llm", "--json", str(report_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert "HEAD changed" in _plain_cli_output(result.output)
    assert report_path.read_text(encoding="utf-8") == original


def test_review_rejects_worktree_mutation_during_pipeline_before_publishing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    app_path = tmp_path / "app.py"
    app_path.write_text("value = 1\n", encoding="utf-8")
    apex_dir = tmp_path / ".apex-ray"
    apex_dir.mkdir()
    (apex_dir / ".gitignore").write_text("reports/\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py", ".apex-ray/.gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("value = 2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fake_pipeline(root, diff_text, target_mode, config, **_kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(target_mode=target_mode),
            input_snapshot=capture_review_input_snapshot(root, diff_text, target_mode),
        )
        app_path.write_text("value = 3\n", encoding="utf-8")
        return report

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        ["review", "--worktree", "--no-llm", "--no-analyzer-cache"],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert "worktree diff changed" in _plain_cli_output(result.output)
    assert not (tmp_path / ".apex-ray" / "reports" / "review.json").exists()


def test_review_worktree_requires_prepared_ignored_outputs_without_mutating_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    app_path = tmp_path / "app.py"
    app_path.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("value = 2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apex_ray.cli.main.run_review_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe output must fail preflight")),
    )

    result = runner.invoke(
        app,
        ["review", "--worktree", "--no-llm", "--no-analyzer-cache"],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert "run apex-ray init and commit .apex-ray/.gitignore" in _plain_cli_output(result.output)
    assert not (tmp_path / ".apex-ray").exists()


def test_review_worktree_rejects_unignored_in_repo_output_before_pipeline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    app_path = tmp_path / "app.py"
    app_path.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("value = 2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apex_ray.cli.main.run_review_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe output must fail preflight")),
    )

    result = runner.invoke(
        app,
        [
            "review",
            "--worktree",
            "--no-llm",
            "--output",
            str(tmp_path / "review.md"),
            "--json",
            str(tmp_path / "review.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert "must be outside the repository or Git-ignored" in _plain_cli_output(result.output)


def test_review_worktree_rejects_git_index_output_without_mutating_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    app_path = tmp_path / "app.py"
    app_path.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("value = 2\n", encoding="utf-8")
    index_path = tmp_path / ".git" / "index"
    original_index = index_path.read_bytes()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apex_ray.cli.main.run_review_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Git metadata output must fail preflight")),
    )

    result = runner.invoke(
        app,
        [
            "review",
            "--worktree",
            "--no-llm",
            "--no-analyzer-cache",
            "--output",
            str(tmp_path.parent / f"{tmp_path.name}-review.md"),
            "--json",
            str(index_path),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert "must be outside the repository or Git-ignored" in _plain_cli_output(result.output)
    assert index_path.read_bytes() == original_index


@pytest.mark.parametrize(
    ("config_body", "unsafe_path"),
    [
        (
            "  reports:\n    archive: true\n    archive_dir: unsafe-archive\n",
            "unsafe-archive",
        ),
        (
            "  telemetry:\n    enabled: true\n    path: unsafe-telemetry/review-runs.jsonl\n",
            "unsafe-telemetry/review-runs.jsonl",
        ),
    ],
)
def test_review_worktree_rejects_unignored_archive_and_telemetry_before_pipeline(
    tmp_path: Path,
    monkeypatch,
    config_body: str,
    unsafe_path: str,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    apex_dir = tmp_path / ".apex-ray"
    apex_dir.mkdir()
    (apex_dir / "config.yml").write_text(
        f"review:\n  llm:\n    enabled: false\n{config_body}",
        encoding="utf-8",
    )
    (apex_dir / ".gitignore").write_text("reports/\n", encoding="utf-8")
    app_path = tmp_path / "app.py"
    app_path.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("value = 2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "apex_ray.cli.main.run_review_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe side effect must fail preflight")),
    )

    result = runner.invoke(
        app,
        ["review", "--worktree", "--no-llm", "--no-analyzer-cache"],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    plain_output = _plain_cli_output(result.output)
    assert unsafe_path in plain_output
    assert "must be outside the repository or Git-ignored" in plain_output


@pytest.mark.parametrize(
    ("arguments", "environment", "unsafe_path"),
    [
        (
            ["--llm", "--llm-provider", "fake", "--cache-dir", "unsafe-llm-cache"],
            {},
            "unsafe-llm-cache",
        ),
        (
            ["--no-llm", "--analyzer-cache-dir", "unsafe-analyzer-cache"],
            {},
            "unsafe-analyzer-cache",
        ),
        (
            ["--no-llm"],
            {"APEX_RAY_CACHE_HOME": "unsafe-env-cache"},
            "unsafe-env-cache",
        ),
        (
            ["--no-llm"],
            {"XDG_CACHE_HOME": "unsafe-xdg-cache"},
            "unsafe-xdg-cache",
        ),
    ],
)
def test_review_worktree_rejects_unignored_cache_side_effects_before_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    environment: dict[str, str],
    unsafe_path: str,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    app_path = tmp_path / "app.py"
    app_path.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("value = 2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in ("APEX_RAY_CACHE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "apex_ray.cli.main.run_review_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe cache must fail preflight")),
    )
    external_root = tmp_path.parent / f"{tmp_path.name}-reports"

    result = runner.invoke(
        app,
        [
            "review",
            "--worktree",
            *arguments,
            "--output",
            str(external_root / "review.md"),
            "--json",
            str(external_root / "review.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    plain_output = _plain_cli_output(result.output)
    assert unsafe_path in plain_output
    assert "must be outside the repository or Git-ignored" in plain_output
    assert not (tmp_path / unsafe_path).exists()


def test_review_worktree_accepts_uncreated_directory_only_ignored_cache_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(
        "/ignored-llm/\n/ignored-analyzer/\n/ignored-archive/\n",
        encoding="utf-8",
    )
    apex_dir = tmp_path / ".apex-ray"
    apex_dir.mkdir()
    (apex_dir / "config.yml").write_text(
        "review:\n  reports:\n    archive: true\n    archive_dir: ignored-archive\n",
        encoding="utf-8",
    )
    app_path = tmp_path / "app.ts"
    app_path.write_text("export const value = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", ".apex-ray/config.yml", "app.ts"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("export const value = 2;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    called = False

    def fake_pipeline(root, diff_text, target_mode, config, **_kwargs):
        nonlocal called
        called = True
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(target_mode=target_mode),
            input_snapshot=capture_review_input_snapshot(root, diff_text, target_mode),
        )

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)
    external_root = tmp_path.parent / f"{tmp_path.name}-reports"

    result = runner.invoke(
        app,
        [
            "review",
            "--worktree",
            "--llm",
            "--llm-provider",
            "fake",
            "--cache-dir",
            "ignored-llm",
            "--analyzer-cache-dir",
            "ignored-analyzer",
            "--output",
            str(external_root / "review.md"),
            "--json",
            str(external_root / "review.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert called is True


def test_review_worktree_accepts_ignored_default_analyzer_leaf_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("/ignored-cache/repos/\n", encoding="utf-8")
    app_path = tmp_path / "app.ts"
    app_path.write_text("export const value = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "app.ts"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("export const value = 2;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APEX_RAY_CACHE_HOME", "ignored-cache")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    called = False

    def fake_pipeline(root, diff_text, target_mode, config, **_kwargs):
        nonlocal called
        called = True
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(target_mode=target_mode),
            input_snapshot=capture_review_input_snapshot(root, diff_text, target_mode),
        )

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)
    external_root = tmp_path.parent / f"{tmp_path.name}-reports"

    result = runner.invoke(
        app,
        [
            "review",
            "--worktree",
            "--no-llm",
            "--output",
            str(external_root / "review.md"),
            "--json",
            str(external_root / "review.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert called is True


def test_review_worktree_preserves_analyzer_specific_relative_cache_roots(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    repo_hash = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()[:16]
    (tmp_path / ".gitignore").write_text(
        (f"/rel-cache/repos/{repo_hash}/typescript/\n/nested/rel-cache/repos/{repo_hash}/dart/\n"),
        encoding="utf-8",
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".keep").write_text("\n", encoding="utf-8")
    app_path = tmp_path / "app.ts"
    app_path.write_text("export const value = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "nested/.keep", "app.ts"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("export const value = 2;\n", encoding="utf-8")
    monkeypatch.chdir(nested)
    monkeypatch.setenv("APEX_RAY_CACHE_HOME", "rel-cache")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    called = False

    def fake_pipeline(root, diff_text, target_mode, config, **_kwargs):
        nonlocal called
        called = True
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(target_mode=target_mode),
            input_snapshot=capture_review_input_snapshot(root, diff_text, target_mode),
        )

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)
    external_reports = tmp_path_factory.mktemp("external-reports")

    result = runner.invoke(
        app,
        [
            "review",
            "--worktree",
            "--no-llm",
            "--output",
            str(external_reports / "review.md"),
            "--json",
            str(external_reports / "review.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert called is True


def test_review_worktree_preflights_node_temp_cache_fallback(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    app_path = tmp_path / "app.ts"
    app_path.write_text("export const value = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.ts"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("export const value = 2;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APEX_RAY_CACHE_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", "")
    monkeypatch.setenv("TMPDIR", "unsafe-tmp")
    called = False

    def fake_pipeline(root, diff_text, target_mode, config, **_kwargs):
        nonlocal called
        called = True
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(target_mode=target_mode),
            input_snapshot=capture_review_input_snapshot(root, diff_text, target_mode),
        )

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)
    external_reports = tmp_path_factory.mktemp("external-reports")

    result = runner.invoke(
        app,
        [
            "review",
            "--worktree",
            "--no-llm",
            "--output",
            str(external_reports / "review.md"),
            "--json",
            str(external_reports / "review.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert called is False
    assert "unsafe-tmp" in _plain_cli_output(result.output)


def test_review_worktree_rejects_external_analyzer_leaf_symlink_into_worktree(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    app_path = tmp_path / "app.ts"
    app_path.write_text("export const value = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.ts"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("export const value = 2;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    external_cache_home = tmp_path_factory.mktemp("external-analyzer-cache")
    repo_hash = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()[:16]
    cache_parent = external_cache_home / "repos" / repo_hash
    cache_parent.mkdir(parents=True)
    unsafe_target = tmp_path / "unsafe-cache-target"
    unsafe_target.mkdir()
    (cache_parent / "typescript").symlink_to(unsafe_target, target_is_directory=True)
    monkeypatch.setenv("APEX_RAY_CACHE_HOME", str(external_cache_home))
    monkeypatch.setattr(
        "apex_ray.cli.main.run_review_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe cache must fail preflight")),
    )
    external_reports = tmp_path_factory.mktemp("external-reports")

    result = runner.invoke(
        app,
        [
            "review",
            "--worktree",
            "--no-llm",
            "--output",
            str(external_reports / "review.md"),
            "--json",
            str(external_reports / "review.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert "must be outside the repository or Git-ignored" in _plain_cli_output(result.output)
    assert list(unsafe_target.iterdir()) == []


@pytest.mark.parametrize(
    ("arguments", "environment"),
    [
        (["--llm", "--llm-provider", "fake", "--no-analyzer-cache"], {}),
        (
            [
                "--llm",
                "--llm-provider",
                "fake",
                "--cache-dir",
                "unsafe-llm-cache",
                "--no-cache",
                "--no-analyzer-cache",
            ],
            {},
        ),
        (["--no-llm", "--no-analyzer-cache"], {"APEX_RAY_CACHE_HOME": "unsafe-analyzer-cache"}),
    ],
)
def test_review_worktree_does_not_preflight_disabled_cache_side_effects(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    environment: dict[str, str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    app_path = tmp_path / "app.ts"
    app_path.write_text("export const value = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.ts"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("export const value = 2;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in ("APEX_RAY_CACHE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    called = False

    def fake_pipeline(root, diff_text, target_mode, config, **_kwargs):
        nonlocal called
        called = True
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=True),
            config,
            DiffSummary(target_mode=target_mode),
            input_snapshot=capture_review_input_snapshot(root, diff_text, target_mode),
        )

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)
    external_reports = tmp_path_factory.mktemp("external-reports")

    result = runner.invoke(
        app,
        [
            "review",
            "--worktree",
            *arguments,
            "--output",
            str(external_reports / "review.md"),
            "--json",
            str(external_reports / "review.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert called is True


def test_review_continuation_does_not_preflight_unused_analyzer_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "cli@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("/reports/\n", encoding="utf-8")
    app_path = tmp_path / "app.py"
    app_path.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    app_path.write_text("value = 2\n", encoding="utf-8")
    diff_text = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    reports = tmp_path / "reports"
    reports.mkdir()
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.WORKTREE),
        input_snapshot=capture_review_input_snapshot(tmp_path, diff_text, TargetMode.WORKTREE),
    )
    report_path = reports / "prior.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APEX_RAY_CACHE_HOME", "unsafe-analyzer-cache")
    called = False

    def fake_continue(*_args, **_kwargs):
        nonlocal called
        called = True
        return report, [object()]

    monkeypatch.setattr("apex_ray.cli.main.continue_review_from_report", fake_continue)

    result = runner.invoke(
        app,
        [
            "review",
            "--continue-from",
            str(report_path),
            "--llm",
            "--no-cache",
            "--output",
            str(reports / "continued.md"),
            "--json",
            str(reports / "continued.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert called is True


def test_review_completion_rejects_legacy_report_without_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    report_path = tmp_path / "legacy.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr(
        "apex_ray.cli.main.continue_review_until_complete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy report must not drain")),
    )

    result = runner.invoke(
        app,
        ["review", "--continue-from", str(report_path), "--llm", "--until-complete"],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert "requires a report with a review-input snapshot" in _plain_cli_output(result.output)


def test_review_llm_max_packs_overrides_root_and_every_reviewer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
review:
  reviewers:
    - id: correctness
      max_packs: 80
    - id: security
      max_packs: 12
""".lstrip(),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_pipeline(root, _diff, _mode, config, **_kwargs):
        seen["root"] = config.llm.max_packs
        seen["reviewers"] = [reviewer.max_packs for reviewer in config.reviewers]
        return build_report(
            ProjectProfile(root=str(root), is_git_repo=False),
            config,
            DiffSummary(target_mode=TargetMode.PATCH),
        )

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--config",
            str(config_path),
            "--llm-max-packs",
            "5",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen == {"root": 5, "reviewers": [5, 5]}


def test_review_partial_summary_is_explicit_without_changing_default_exit_code(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
review:
  reviewers:
    - id: correctness
    - id: security
""".lstrip(),
        encoding="utf-8",
    )

    def fake_pipeline(root, _diff, _mode, config, **_kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=False),
            config,
            DiffSummary(target_mode=TargetMode.PATCH),
        )
        report.llm_coverage.enabled = True
        report.llm_coverage.total_context_packs = 10
        report.llm_coverage.reviewed_context_packs = 4
        report.llm_coverage.unreviewed_context_packs = 6
        report.llm_coverage.partial_severity = "minor"
        return report

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)
    markdown_output = tmp_path / "custom-review.md"
    json_output = tmp_path / "custom-review.json"
    html_output = tmp_path / "custom-review.html"
    sarif_output = tmp_path / "custom-review.sarif"

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--config",
            str(config_path),
            "--output",
            str(markdown_output),
            "--json",
            str(json_output),
            "--html",
            str(html_output),
            "--sarif",
            str(sarif_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "APEX RAY REVIEW: PARTIAL COVERAGE" in result.stdout
    assert "Review coverage: PARTIAL - 4/10 unique context packs (40.0%)" in result.stdout
    assert "Findings: 0 in reviewed scope" in result.stdout
    assert "Continue reviewer correctness:" in result.stdout
    assert "--reviewer correctness" in result.stdout
    assert "Continue reviewer security:" in result.stdout
    assert "--reviewer security" in result.stdout
    assert f"--output {markdown_output}" in result.stdout
    assert f"--json {json_output}" in result.stdout
    assert f"--html {html_output}" in result.stdout
    assert f"--sarif {sarif_output}" in result.stdout


def test_review_partial_summary_targets_the_reviewer_that_owns_assignment_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
review:
  reviewers:
    - id: correctness
      required: true
    - id: security
""".lstrip(),
        encoding="utf-8",
    )
    pack_id = "src/auth.ts#authorize:1"

    def fake_pipeline(root, _diff, _mode, config, **_kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=False),
            config,
            DiffSummary(target_mode=TargetMode.PATCH),
        )
        report.llm_coverage.enabled = True
        report.llm_coverage.total_context_packs = 1
        report.llm_coverage.reviewed_context_packs = 1
        report.llm_coverage.reviewers = [
            LLMReviewerCoverageSummary(
                reviewer_id="correctness",
                required=True,
                status="pass",
                matching_context_packs=1,
                reviewed_context_packs=1,
                matching_context_pack_ids=[pack_id],
                reviewed_context_pack_ids=[pack_id],
            ),
            LLMReviewerCoverageSummary(
                reviewer_id="security",
                status="warn",
                matching_context_packs=1,
                reviewed_context_packs=0,
                matching_context_pack_ids=[pack_id],
            ),
        ]
        report.llm_coverage.coverage_todos = [
            LLMCoverageTodo(
                context_pack_id=pack_id,
                file="src/auth.ts",
                reviewer_id="security",
                priority="p0",
            )
        ]
        return report

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        ["review", "--diff", str(patch), "--config", str(config_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "APEX RAY REVIEW: PARTIAL COVERAGE" in result.stdout
    assert "Continue reviewer security:" in result.stdout
    assert "--reviewer security" in result.stdout
    assert "Continue reviewer correctness:" not in result.stdout


def test_review_partial_summary_routes_depth_debt_to_a_matching_reviewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
review:
  reviewers:
    - id: baseline
      required: true
    - id: security
""".lstrip(),
        encoding="utf-8",
    )
    pack_id = "src/auth.ts#authorize:1"

    def fake_pipeline(root, _diff, _mode, config, **_kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=False),
            config,
            DiffSummary(target_mode=TargetMode.PATCH),
        )
        report.llm_coverage.enabled = True
        report.llm_coverage.total_context_packs = 1
        report.llm_coverage.reviewed_context_packs = 1
        report.llm_coverage.partial_severity = "major"
        report.llm_coverage.shallow_only_high_risk_context_pack_ids = [pack_id]
        report.llm_coverage.reviewers = [
            LLMReviewerCoverageSummary(reviewer_id="baseline", required=True, status="not_applicable"),
            LLMReviewerCoverageSummary(
                reviewer_id="security",
                status="pass",
                matching_context_packs=1,
                reviewed_context_packs=1,
                matching_context_pack_ids=[pack_id],
                reviewed_context_pack_ids=[pack_id],
            ),
        ]
        return report

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)

    result = runner.invoke(
        app,
        ["review", "--diff", str(patch), "--config", str(config_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Continue reviewer security:" in result.stdout
    assert "--reviewer security" in result.stdout
    assert "Continue reviewer baseline:" not in result.stdout


def test_review_until_complete_rejects_configured_disabled_llm_before_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "review:\n  llm:\n    enabled: false\n",
        encoding="utf-8",
    )

    def unexpected_pipeline(*_args, **_kwargs):
        pytest.fail("completion with disabled LLM must fail before the review pipeline")

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", unexpected_pipeline)

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--config",
            str(config_path),
            "--until-complete",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert "Coverage completion requires --llm or review.llm.enabled: true." in _plain_cli_output(result.output)


def test_review_until_complete_uses_the_single_required_reviewer_and_writes_reports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
review:
  llm:
    enabled: true
    provider: fake
  reviewers:
    - id: correctness
      required: true
    - id: security
""".lstrip(),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_pipeline(root, _diff, _mode, config, **_kwargs):
        seen["initial_reviewer_ids"] = _kwargs["reviewer_ids"]
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=False),
            config,
            DiffSummary(target_mode=TargetMode.PATCH),
        )
        report.llm_coverage.enabled = True
        report.llm_coverage.total_context_packs = 2
        report.llm_coverage.unreviewed_context_packs = 2
        report.llm_coverage.partial_severity = "minor"
        return report

    def fake_drain(report, **kwargs):
        seen.update(kwargs)
        kwargs["on_batch"](report, 1)
        seen["intermediate_json_exists"] = json_output.exists()
        seen["intermediate_completion"] = ReviewReport.model_validate_json(
            json_output.read_text(encoding="utf-8")
        ).llm_coverage.completion_status
        report.llm_coverage.reviewed_context_packs = 2
        report.llm_coverage.unreviewed_context_packs = 0
        report.llm_coverage.partial_severity = "none"
        return SimpleNamespace(report=report, complete=True, batches=2, stop_reason="complete")

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)
    monkeypatch.setattr("apex_ray.cli.main.continue_review_until_complete", fake_drain)
    json_output = tmp_path / "review.json"

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--config",
            str(config_path),
            "--until-complete",
            "--strict-coverage",
            "--followup-max-pack-reviews",
            "3",
            "--max-followup-passes",
            "4",
            "--json",
            str(json_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen["initial_reviewer_ids"] == ["correctness"]
    assert seen["reviewer_ids"] == ["correctness"]
    assert seen["batch_size"] == 3
    assert seen["max_batches"] == 4
    assert seen["intermediate_json_exists"] is True
    assert seen["intermediate_completion"] == "partial"
    assert json_output.exists()
    persisted = ReviewReport.model_validate_json(json_output.read_text(encoding="utf-8"))
    assert persisted.coverage_completion is not None
    assert persisted.coverage_completion.status == "complete"
    assert persisted.coverage_completion.reviewer_ids == ["correctness"]
    assert persisted.coverage_completion.batches == 2
    assert "Coverage completion: COMPLETE after 2 follow-up batch(es)." in result.stdout


def test_review_strict_coverage_fails_only_after_writing_incomplete_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")

    def fake_pipeline(root, _diff, _mode, config, **_kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=False),
            config,
            DiffSummary(target_mode=TargetMode.PATCH),
        )
        report.llm_coverage.enabled = True
        report.llm_coverage.total_context_packs = 1
        report.llm_coverage.unreviewed_context_packs = 1
        report.llm_coverage.partial_severity = "minor"
        return report

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_pipeline)
    monkeypatch.setattr(
        "apex_ray.cli.main.continue_review_until_complete",
        lambda report, **_kwargs: SimpleNamespace(
            report=report,
            complete=False,
            batches=1,
            stop_reason="no_progress",
        ),
    )
    json_output = tmp_path / "review.json"

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--llm",
            "--strict-coverage",
            "--json",
            str(json_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert json_output.exists()
    persisted = ReviewReport.model_validate_json(json_output.read_text(encoding="utf-8"))
    assert persisted.coverage_completion is not None
    assert persisted.coverage_completion.status == "incomplete"
    assert persisted.coverage_completion.stop_reason == "no_progress"
    assert "Coverage completion: INCOMPLETE (no_progress)." in result.stdout


def test_review_continue_from_preserves_explicit_config_and_repeatable_reviewers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prior = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
    )
    report_path = tmp_path / "review.json"
    report_path.write_text(prior.model_dump_json(indent=2), encoding="utf-8")
    config_path = tmp_path / "continued.yml"
    config_path.write_text(
        """
review:
  reviewers:
    - id: security
      focus: Authorization boundaries.
    - id: finance
      focus: Financial correctness.
""",
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_continue(*args, **kwargs):
        seen["configured_reviewers"] = [item.id for item in kwargs["config"].reviewers]
        seen["reviewer_ids"] = kwargs["reviewer_ids"]
        return prior, [object()]

    monkeypatch.setattr("apex_ray.cli.main.continue_review_from_report", fake_continue)

    result = runner.invoke(
        app,
        [
            "review",
            "--continue-from",
            str(report_path),
            "--config",
            str(config_path),
            "--reviewer",
            "finance",
            "--reviewer",
            "security",
            "--output",
            str(tmp_path / "continued.md"),
            "--json",
            str(tmp_path / "continued.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen == {
        "configured_reviewers": ["security", "finance"],
        "reviewer_ids": ["finance", "security"],
    }


def test_review_continue_from_resolves_explicit_config_against_report_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    caller_root = tmp_path / "caller"
    project_root = tmp_path / "project"
    caller_root.mkdir()
    rules_dir = project_root / ".apex-ray" / "rules"
    rules_dir.mkdir(parents=True)
    prior = build_report(
        ProjectProfile(root=str(project_root), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
    )
    report_path = tmp_path / "review.json"
    report_path.write_text(prior.model_dump_json(indent=2), encoding="utf-8")
    config_path = project_root / ".apex-ray" / "config.yml"
    config_path.write_text(
        "review:\n  rule_paths:\n    - .apex-ray/rules\n",
        encoding="utf-8",
    )
    (rules_dir / "continuation-root.md").write_text(
        "---\n"
        "id: continuation-root\n"
        "paths:\n"
        "  - src/**\n"
        "---\n"
        "Resolve continuation policy against the report repository.\n",
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_continue(*args, **kwargs):
        seen["root"] = kwargs["repo_root"]
        seen["rules"] = [rule.id for rule in kwargs["config"].rule_definitions]
        return prior, [object()]

    monkeypatch.setattr("apex_ray.cli.main.discover_repo_root", lambda _cwd: caller_root)
    monkeypatch.setattr("apex_ray.cli.main.continue_review_from_report", fake_continue)

    result = runner.invoke(
        app,
        [
            "review",
            "--continue-from",
            str(report_path),
            "--config",
            str(config_path),
            "--output",
            str(tmp_path / "continued.md"),
            "--json",
            str(tmp_path / "continued.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen == {
        "root": project_root,
        "rules": ["continuation-root"],
    }


def test_review_auto_followup_preserves_explicit_reviewer_scope(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch = tmp_path / "sample.diff"
    patch.write_text((FIXTURE_DIR / "sample.diff").read_text(encoding="utf-8"), encoding="utf-8")
    config_path = tmp_path / "reviewers.yml"
    config_path.write_text(
        """
review:
  reviewers:
    - id: security
      focus: Authorization boundaries.
""",
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_run_review_pipeline(root, _diff_text, _target_mode, config, **_kwargs):
        report = build_report(
            ProjectProfile(root=str(root), is_git_repo=False),
            config,
            DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        )
        report.llm_coverage.partial_severity = "critical"
        return report

    def fake_continue(report, **kwargs):
        seen["reviewer_ids"] = kwargs["reviewer_ids"]
        seen["max_pack_reviews"] = kwargs["max_pack_reviews"]
        return report, [object()]

    monkeypatch.setattr("apex_ray.cli.main.run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr("apex_ray.cli.main.continue_review_from_report", fake_continue)

    result = runner.invoke(
        app,
        [
            "review",
            "--diff",
            str(patch),
            "--config",
            str(config_path),
            "--reviewer",
            "security",
            "--auto-followup",
            "--auto-followup-max-pack-reviews",
            "3",
            "--output",
            str(tmp_path / "review.md"),
            "--json",
            str(tmp_path / "review.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen == {
        "reviewer_ids": ["security"],
        "max_pack_reviews": 3,
    }


def test_review_continue_from_accepts_legacy_context_pack_symbols_without_line_ranges(
    tmp_path: Path, monkeypatch
) -> None:
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=1)),
        analyzer_results=[
            AnalyzerResult(
                language="typescript",
                projectRoot=str(tmp_path),
                tsconfigPath=None,
                files=[
                    AnalyzerFile(
                        path="src/service.ts",
                        symbols=[
                            AnalyzerSymbol(name="request", kind="function", startLine=12, endLine=14),
                        ],
                        changedSymbols=[
                            AnalyzerSymbol(name="request", kind="function", startLine=12, endLine=14),
                        ],
                    )
                ],
                indexCache=None,
            )
        ],
        context_packs=[
            ContextPack(
                id="src/service.ts#request:7",
                file="src/service.ts",
                changed_lines=[(12, 14)],
                symbol=AnalyzerSymbol(name="request", kind="function", startLine=12, endLine=14),
                symbols=[
                    AnalyzerSymbol(name="request", kind="function", startLine=12, endLine=14),
                    AnalyzerSymbol(name="helper", kind="function", startLine=20, endLine=22),
                ],
            )
        ],
    )
    report_data = json.loads(report.model_dump_json(indent=2))
    analyzer_file = report_data["analyzer_results"][0]["files"][0]
    for symbol in [*analyzer_file["symbols"], *analyzer_file["changed_symbols"]]:
        symbol.pop("start_line", None)
        symbol.pop("end_line", None)
        symbol.pop("startLine", None)
        symbol.pop("endLine", None)
    pack = report_data["context_packs"][0]
    for symbol in [pack["symbol"], *pack["symbols"]]:
        symbol.pop("start_line", None)
        symbol.pop("end_line", None)
        symbol.pop("startLine", None)
        symbol.pop("endLine", None)
    report_path = tmp_path / "legacy-review.json"
    output = tmp_path / "continued.md"
    json_output = tmp_path / "continued.json"
    report_path.write_text(json.dumps(report_data), encoding="utf-8")
    seen: dict[str, int | None] = {}

    def fake_continue(prior_report, *args, **kwargs):
        loaded_pack = prior_report.context_packs[0]
        seen["symbol_start_line"] = loaded_pack.symbol.start_line if loaded_pack.symbol else None
        seen["symbol_end_line"] = loaded_pack.symbol.end_line if loaded_pack.symbol else None
        seen["secondary_start_line"] = loaded_pack.symbols[1].start_line
        seen["analyzer_symbol_start_line"] = prior_report.analyzer_results[0].files[0].symbols[0].start_line
        return prior_report, [object()]

    monkeypatch.setattr("apex_ray.cli.main.continue_review_from_report", fake_continue)

    result = runner.invoke(
        app,
        [
            "review",
            "--continue-from",
            str(report_path),
            "--llm",
            "--only-pack",
            "src/service.ts#request:7",
            "--output",
            str(output),
            "--json",
            str(json_output),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert seen == {
        "symbol_start_line": 12,
        "symbol_end_line": 14,
        "secondary_start_line": 12,
        "analyzer_symbol_start_line": 1,
    }


def test_review_patch_can_run_fake_llm(tmp_path: Path, monkeypatch, built_ts_analyzer: None) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = FIXTURE_DIR / "ts_project"
    for source in fixture.rglob("*"):
        if source.is_file():
            relative_source = source.relative_to(fixture)
            if ".apex-ray" in relative_source.parts:
                continue
            target = tmp_path / relative_source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    config = tmp_path / ".apex-ray" / "config.yml"
    config.parent.mkdir()
    config.write_text(
        """
review:
  llm:
    enabled: true
    provider: fake
""",
        encoding="utf-8",
    )
    output = tmp_path / "review.md"
    json_output = tmp_path / "review.json"

    result = runner.invoke(
        app,
        ["review", "--diff", str(tmp_path / "cart.diff"), "--output", str(output), "--json", str(json_output)],
        catch_exceptions=False,
    )

    data = json.loads(json_output.read_text(encoding="utf-8"))
    assert result.exit_code == 0
    assert data["context_packs"]
    assert data["llm_runs"][0]["provider"] == "fake"
    assert data["llm_runs"][0]["prompt_version"] == REVIEW_PROMPT_VERSION
    assert "No LLM findings reported." in output.read_text(encoding="utf-8")


def test_eval_run_prs_cli_passes_options_to_runner(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run_pr_eval_cases(**kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(
            matched_greptile_findings_total=2,
            greptile_findings_total=3,
            extra_apex_findings_total=1,
            failed=False,
            partial=0,
        )

    monkeypatch.setattr("apex_ray.cli.eval.run_pr_eval_cases", fake_run_pr_eval_cases)

    result = runner.invoke(
        app,
        [
            "eval",
            "run-prs",
            "--repo",
            str(tmp_path / "repo"),
            "--cases",
            str(tmp_path / "cases"),
            "--output",
            str(tmp_path / "run"),
            "--pr",
            "12",
            "--llm",
            "--llm-provider",
            "fake",
            "--llm-model",
            "fake-strong",
            "--verify",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--telemetry-path",
            str(tmp_path / "telemetry.jsonl"),
            "--case-jobs",
            "2",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Matched Greptile findings: 2/3; extra Apex findings: 1" in result.stdout
    assert seen["source_repo"] == tmp_path / "repo"
    assert seen["cases_dir"] == tmp_path / "cases"
    assert seen["output_dir"] == tmp_path / "run"
    assert seen["pr_numbers"] == [12]
    assert seen["llm_enabled"] is True
    assert seen["provider_override"] == "fake"
    assert seen["model_override"] == "fake-strong"
    assert seen["verify_override"] is True
    assert seen["cache_dir"] == tmp_path / "cache"
    assert seen["telemetry_path"] == tmp_path / "telemetry.jsonl"
    assert seen["case_jobs"] == 2


def test_eval_run_prs_cli_fails_on_partial_by_default(tmp_path: Path, monkeypatch) -> None:
    def fake_run_pr_eval_cases(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            matched_greptile_findings_total=1,
            greptile_findings_total=1,
            extra_apex_findings_total=0,
            failed=0,
            partial=1,
        )

    monkeypatch.setattr("apex_ray.cli.eval.run_pr_eval_cases", fake_run_pr_eval_cases)

    result = runner.invoke(
        app,
        [
            "eval",
            "run-prs",
            "--repo",
            str(tmp_path / "repo"),
            "--cases",
            str(tmp_path / "cases"),
            "--output",
            str(tmp_path / "run"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Partial PR eval cases: 1" in result.stdout


def test_eval_run_prs_cli_rejects_conflicting_llm_flags(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "eval",
            "run-prs",
            "--repo",
            str(tmp_path / "repo"),
            "--cases",
            str(tmp_path / "cases"),
            "--output",
            str(tmp_path / "run"),
            "--llm",
            "--no-llm",
        ],
    )

    assert result.exit_code != 0
    assert "Use only one of --llm or --no-llm" in _plain_cli_output(result.output)
