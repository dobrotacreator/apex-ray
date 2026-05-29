import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from apex_ray.cli import app
from apex_ray.llm_cache import REVIEW_PROMPT_VERSION
from apex_ray.models import (
    DiffStats,
    DiffSummary,
    Finding,
    FindingConfidence,
    FindingSeverity,
    ProjectProfile,
    ReviewConfig,
    TargetMode,
)
from apex_ray.report import build_report

runner = CliRunner()
FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_init_creates_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"], catch_exceptions=False)

    assert result.exit_code == 0
    assert (tmp_path / ".apex-ray" / "config.yml").exists()
    assert (tmp_path / ".apex-ray" / ".gitignore").exists()
    assert (tmp_path / "lefthook.yml").exists()
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert "apex-ray-review" in (tmp_path / "lefthook.yml").read_text(encoding="utf-8")
    assert "--no-llm" in (tmp_path / "lefthook.yml").read_text(encoding="utf-8")


def test_init_can_skip_hooks_and_agent_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--hooks", "none", "--agent-files", "none"], catch_exceptions=False)

    assert result.exit_code == 0
    assert (tmp_path / ".apex-ray" / "config.yml").exists()
    assert not (tmp_path / "lefthook.yml").exists()
    assert not (tmp_path / "AGENTS.md").exists()


def test_doctor_reports_local_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".apex-ray").mkdir()
    (tmp_path / ".apex-ray" / "config.yml").write_text("review:\n", encoding="utf-8")
    (tmp_path / ".apex-ray" / "config.local.yml").write_text("review:\n  llm:\n    jobs: 2\n", encoding="utf-8")

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert result.exit_code == 0
    assert f"- Local config: {tmp_path / '.apex-ray' / 'config.local.yml'}" in result.stdout


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
        ["memory", "suggest", "--from-report", str(report_path), "--output", str(output)],
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

    monkeypatch.setattr("apex_ray.cli.continue_review_from_report", fake_continue)

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

    monkeypatch.setattr("apex_ray.cli.continue_review_from_report", fake_continue)

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
        )

    monkeypatch.setattr("apex_ray.cli_eval.run_pr_eval_cases", fake_run_pr_eval_cases)

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
    assert "Use only one of --llm or --no-llm" in result.output
