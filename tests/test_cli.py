import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from apex_ray import __version__
from apex_ray.cli import app
from apex_ray.diff import parse_unified_diff
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
    ProjectProfile,
    ReviewConfig,
    TargetMode,
)
from apex_ray.report import build_report

runner = CliRunner()
FIXTURE_DIR = Path(__file__).parent / "fixtures"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_RICH_FRAME_CHARS = str.maketrans({ord(char): " " for char in "\u2500\u2502\u256d\u256e\u2570\u256f"})


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
    assert "max_packs: 64" in config_text
    assert "max_deep_packs: 48" in config_text
    assert "max_input_tokens: 300000" in config_text
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
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

    def fake_run_git(args, cwd, check=True):
        if args[:1] == ["show"] and args[1].startswith("HEAD:"):
            path = args[1].removeprefix("HEAD:")
            stdout = head_source.get(path, "")
            return subprocess.CompletedProcess(args, 0 if path in head_source else 1, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="unexpected git call")

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
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

    monkeypatch.setattr("apex_ray.cli.gate.git.repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("apex_ray.cli.gate.git.is_git_repo", lambda _root: True)
    monkeypatch.setattr("apex_ray.cli.gate.git.rev_parse", lambda _root, _ref: next(heads))
    monkeypatch.setattr("apex_ray.cli.gate.git.merge_base", lambda _root, _base, _head: "base-1")
    monkeypatch.setattr("apex_ray.cli.gate.git.object_exists", lambda _root, _ref: True)
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
