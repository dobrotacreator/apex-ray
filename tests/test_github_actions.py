from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION_DIR = ROOT / ".github" / "actions" / "apex-ray-review"
ACTION_PATH = ACTION_DIR / "action.yml"
HELPER_PATH = ACTION_DIR / "prepare.py"
DOCS_PATH = ROOT / "docs" / "github-actions.md"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("apex_ray_action_prepare", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repository(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "ci@example.invalid")
    _git(root, "config", "user.name", "CI Test")
    config = root / ".apex-ray" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """\
review:
  rule_paths:
    - .apex-ray/rules
  memory:
    enabled: true
  analyzer:
    script_path: scripts/untrusted-analyzer.sh
    index_cache_enabled: true
  llm:
    enabled: true
    provider: openai_api
    cache_enabled: true
  telemetry:
    enabled: true
  reports:
    archive: true
  triage:
    enabled: true
  reviewers:
    - id: security
      focus: Security boundaries.
    - id: finance
      focus: Financial invariants.
""",
        encoding="utf-8",
    )
    (root / "app.ts").write_text("export const value = 1;\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD")


def _commit_config(root: Path, text: str) -> str:
    config = root / ".apex-ray" / "config.yml"
    config.write_text(text, encoding="utf-8")
    _git(root, "add", str(config.relative_to(root)))
    _git(root, "commit", "-qm", "update config")
    return _git(root, "rev-parse", "HEAD")


def _pull_request_event(base_sha: str, *, fork: bool = False) -> dict[str, Any]:
    return {
        "pull_request": {
            "base": {
                "sha": base_sha,
                "repo": {"full_name": "owner/repository"},
            },
            "head": {
                "sha": "1" * 40,
                "repo": {"full_name": "fork/repository" if fork else "owner/repository"},
            },
        }
    }


def _plan_options(
    helper: ModuleType,
    *,
    workspace: Path,
    runner_temp: Path,
    event: dict[str, Any],
    trust_pr_config: bool = False,
) -> Any:
    return helper.PlanOptions(
        workspace=workspace,
        runner_temp=runner_temp,
        event=event,
        repository="owner/repository",
        actor="external-contributor",
        config_path=".apex-ray/config.yml",
        reviewers="security, finance\nsecurity",
        llm="true",
        base="",
        markdown_output=".apex-ray/ci/review.md",
        json_output=".apex-ray/ci/review.json",
        sarif_output=".apex-ray/ci/review.sarif",
        trust_pr_config=trust_pr_config,
        artifact_name="apex-ray-security",
        sarif_category="apex-ray-security",
        fail_on_quality_gate=True,
    )


def test_action_uses_pinned_dependencies_and_has_no_secret_inputs() -> None:
    action = yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))

    assert action["runs"]["using"] == "composite"
    input_names = set(action["inputs"])
    assert not input_names & {
        "api-key",
        "api-key-env",
        "token",
        "secret",
        "base-url",
        "base-url-env",
    }

    uses = [step["uses"] for step in action["runs"]["steps"] if "uses" in step]
    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) for value in uses)

    checkout = next(step for step in action["runs"]["steps"] if step.get("id") == "checkout")
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False

    assert action["inputs"]["fail-on-quality-gate"]["default"] == "true"
    assert action["outputs"]["quality-gate-status"]["value"] == "${{ steps.finalize.outputs.quality-gate-status }}"

    upload_sarif = next(step for step in action["runs"]["steps"] if step.get("id") == "upload-sarif")
    assert upload_sarif["continue-on-error"] is True
    assert "untrusted-pr" in upload_sarif["if"]
    assert "steps.review.outcome == 'success'" in upload_sarif["if"]

    finalize = next(step for step in action["runs"]["steps"] if step.get("id") == "finalize")
    assert "steps.review.outcome == 'success'" in finalize["if"]

    serialized = ACTION_PATH.read_text(encoding="utf-8")
    assert "pull_request_target" not in serialized


def test_action_builds_its_pinned_source_with_hash_locked_dependencies() -> None:
    action = yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))
    steps = action["runs"]["steps"]
    setup_python = next(step for step in steps if step["name"] == "Set up Python")
    setup_node = next(step for step in steps if step["name"] == "Set up Node.js for TypeScript analysis")
    setup_uv = next(step for step in steps if step["name"] == "Set up uv")
    runtime = next(step for step in steps if step["name"] == "Prepare locked Apex Ray runtime")

    assert re.fullmatch(r"3\.14\.\d+", setup_python["with"]["python-version"])
    assert re.fullmatch(r"24\.\d+\.\d+", setup_node["with"]["node-version"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", setup_uv["with"]["version"])
    assert setup_uv["with"]["enable-cache"] is False
    assert setup_uv["with"]["working-directory"] == "${{ github.action_path }}/../../.."

    command = runtime["run"]
    assert 'cd "$APEX_RAY_TYPESCRIPT_RUNTIME"' in command
    assert "npm ci --ignore-scripts --no-audit --no-fund" in command
    assert "npm run build" in command
    assert 'uv sync --locked --no-dev --no-install-project --project "$APEX_RAY_SOURCE"' in command
    assert runtime["env"]["APEX_RAY_SOURCE"] == "${{ github.action_path }}/../../.."
    assert runtime["env"]["PYTHONPATH"] == "${{ github.action_path }}/../../../src"
    assert runtime["env"]["UV_PROJECT_ENVIRONMENT"] == "${{ runner.temp }}/apex-ray-action-venv"
    for step_id in ("prepare", "review", "finalize"):
        step = next(candidate for candidate in steps if candidate.get("id") == step_id)
        assert step["env"]["PYTHONPATH"] == "${{ github.action_path }}/../../../src"
        assert step["env"]["PYTHONNOUSERSITE"] == "1"

    serialized = ACTION_PATH.read_text(encoding="utf-8")
    locked_helper_prefix = (
        'uv run --locked --no-dev --no-sync --project "$GITHUB_ACTION_PATH/../../.." '
        'python -P -s "$GITHUB_ACTION_PATH/prepare.py"'
    )
    assert serialized.count(locked_helper_prefix) == 3
    assert "--no-editable" not in serialized
    assert "uv pip install" not in serialized
    assert "apex-ray==" not in serialized


@pytest.mark.parametrize(
    "config_text",
    [
        pytest.param(
            """\
review:
  llm:
    enabled: true
    provider: codex_cli
    codex_path: scripts/codex-from-base.sh
""",
            id="root-codex",
        ),
        pytest.param(
            """\
review:
  llm:
    enabled: true
    provider: claude_code_cli
    claude_path: scripts/claude-from-base.sh
""",
            id="root-claude",
        ),
        pytest.param(
            """\
review:
  llm:
    enabled: true
    provider: openai_api
    profiles:
      security:
        provider: codex_cli
        codex_path: scripts/codex-profile.sh
  reviewers:
    - id: security
      profile: security
""",
            id="reviewer-codex-profile",
        ),
        pytest.param(
            """\
review:
  llm:
    enabled: true
    provider: anthropic_api
    profiles:
      strong:
        provider: claude_code_cli
        claude_path: scripts/claude-profile.sh
    routing:
      review_profile: strong
""",
            id="routing-claude-profile",
        ),
    ],
)
def test_restricted_pr_config_rejects_cli_provider_routes_when_llm_can_run(
    tmp_path: Path,
    config_text: str,
) -> None:
    helper = _load_helper()
    workspace = tmp_path / "repo"
    runner_temp = tmp_path / "runner"
    workspace.mkdir()
    runner_temp.mkdir()
    _init_repository(workspace)
    base_sha = _commit_config(workspace, config_text)

    with pytest.raises(ValueError, match="Restricted pull-request config cannot enable CLI LLM provider routes"):
        helper.create_plan(
            _plan_options(
                helper,
                workspace=workspace,
                runner_temp=runner_temp,
                event=_pull_request_event(base_sha),
            )
        )


def test_fork_with_cli_base_config_is_forced_to_static_review(tmp_path: Path) -> None:
    helper = _load_helper()
    workspace = tmp_path / "repo"
    runner_temp = tmp_path / "runner"
    workspace.mkdir()
    runner_temp.mkdir()
    _init_repository(workspace)
    base_sha = _commit_config(
        workspace,
        """\
review:
  llm:
    enabled: true
    provider: codex_cli
    codex_path: scripts/codex-from-base.sh
""",
    )

    plan = helper.create_plan(
        _plan_options(
            helper,
            workspace=workspace,
            runner_temp=runner_temp,
            event=_pull_request_event(base_sha, fork=True),
        )
    )

    assert plan["untrusted_pr"] is True
    assert plan["llm_mode"] == "disabled-untrusted-pr"
    assert "--no-llm" in plan["args"]
    assert "--llm" not in plan["args"]


def test_restricted_auto_llm_rejects_the_implicit_default_cli_provider(tmp_path: Path) -> None:
    helper = _load_helper()
    workspace = tmp_path / "repo"
    runner_temp = tmp_path / "runner"
    workspace.mkdir()
    runner_temp.mkdir()
    _init_repository(workspace)
    base_sha = _commit_config(
        workspace,
        """\
review:
  llm:
    enabled: true
""",
    )
    options = _plan_options(
        helper,
        workspace=workspace,
        runner_temp=runner_temp,
        event=_pull_request_event(base_sha),
    )._replace(llm="auto")

    with pytest.raises(ValueError, match=r"review\.llm\.provider \(codex_cli\)"):
        helper.create_plan(options)


def test_fork_plan_uses_sanitized_base_config_and_forces_no_llm(tmp_path: Path) -> None:
    helper = _load_helper()
    workspace = tmp_path / "repo"
    runner_temp = tmp_path / "runner"
    workspace.mkdir()
    runner_temp.mkdir()
    base_sha = _init_repository(workspace)
    config = workspace / ".apex-ray" / "config.yml"
    config.write_text(
        """\
review:
  analyzer:
    script_path: scripts/changed-in-pr.sh
    index_cache_enabled: false
    index_cache_dir: ../../untrusted-cache
    refresh_index_cache: true
  llm:
    enabled: true
    provider: openai_compatible
    api:
      base_url: https://attacker.invalid/v1
""",
        encoding="utf-8",
    )
    event = {
        "pull_request": {
            "base": {
                "sha": base_sha,
                "repo": {"full_name": "owner/repository"},
            },
            "head": {
                "sha": "1" * 40,
                "repo": {"full_name": "fork/repository"},
            },
        }
    }

    plan = helper.create_plan(
        _plan_options(
            helper,
            workspace=workspace,
            runner_temp=runner_temp,
            event=event,
            trust_pr_config=True,
        )
    )

    assert plan["untrusted_pr"] is True
    assert plan["config_source"] == "restricted-base"
    assert plan["llm_mode"] == "disabled-untrusted-pr"
    assert "--no-llm" in plan["args"]
    assert plan["args"][plan["args"].index("--base") + 1] == base_sha
    assert [plan["args"][index + 1] for index, value in enumerate(plan["args"]) if value == "--reviewer"] == [
        "security",
        "finance",
    ]

    safe_config = yaml.safe_load(Path(plan["config_path"]).read_text(encoding="utf-8"))
    review = safe_config["review"]
    assert review["rule_paths"] == []
    assert review["memory"]["enabled"] is False
    assert review["analyzer"]["script_path"] is None
    assert review["analyzer"]["index_cache_enabled"] is True
    assert review["analyzer"]["index_cache_dir"] == str(
        runner_temp / "apex-ray-ci" / "local-data" / "cache" / "analyzer" / "typescript"
    )
    assert review["analyzer"]["refresh_index_cache"] is False
    assert review["llm"]["provider"] == "openai_api"
    assert review["llm"]["cache_enabled"] is False
    assert review["telemetry"]["enabled"] is False
    assert review["reports"]["archive"] is False
    assert review["triage"]["enabled"] is False
    assert "attacker.invalid" not in Path(plan["config_path"]).read_text(encoding="utf-8")


def test_same_repository_pr_can_explicitly_trust_head_config(tmp_path: Path) -> None:
    helper = _load_helper()
    workspace = tmp_path / "repo"
    runner_temp = tmp_path / "runner"
    workspace.mkdir()
    runner_temp.mkdir()
    base_sha = _init_repository(workspace)
    event = {
        "pull_request": {
            "base": {
                "sha": base_sha,
                "repo": {"full_name": "owner/repository"},
            },
            "head": {
                "sha": "2" * 40,
                "repo": {"full_name": "owner/repository"},
            },
        }
    }

    plan = helper.create_plan(
        _plan_options(
            helper,
            workspace=workspace,
            runner_temp=runner_temp,
            event=event,
            trust_pr_config=True,
        )
    )

    assert plan["untrusted_pr"] is False
    assert plan["config_source"] == "head"
    assert plan["config_path"] == str(workspace / ".apex-ray" / "config.yml")
    assert "--llm" in plan["args"]
    assert "--no-llm" not in plan["args"]


def test_fork_plan_executes_static_review_and_finalizes_sarif(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    workspace = tmp_path / "repo"
    runner_temp = tmp_path / "runner"
    workspace.mkdir()
    runner_temp.mkdir()
    base_sha = _init_repository(workspace)
    (workspace / "app.ts").write_text(
        "export const value = Number.parseInt('2', 10);\n",
        encoding="utf-8",
    )
    _git(workspace, "add", "app.ts")
    _git(workspace, "commit", "-qm", "change")
    head_sha = _git(workspace, "rev-parse", "HEAD")
    event = {
        "pull_request": {
            "base": {
                "sha": base_sha,
                "repo": {"full_name": "owner/repository"},
            },
            "head": {
                "sha": head_sha,
                "repo": {"full_name": "fork/repository"},
            },
        }
    }
    options = _plan_options(
        helper,
        workspace=workspace,
        runner_temp=runner_temp,
        event=event,
    )._replace(reviewers="security")
    plan = helper.create_plan(options)
    plan_path = runner_temp / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    assert plan["args"][plan["args"].index("--sarif") + 1] == plan["sarif_path"]
    assert helper._run(plan_path) == 0

    github_output = tmp_path / "github-output"
    github_summary = tmp_path / "github-summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(github_summary))
    assert helper._finalize(plan_path) == 0

    sarif = json.loads(Path(plan["sarif_path"]).read_text(encoding="utf-8"))
    assert sarif["version"] == "2.1.0"
    assert "sarif-ready=true" in github_output.read_text(encoding="utf-8")
    assert "disabled-untrusted-pr" in github_summary.read_text(encoding="utf-8")


def test_run_removes_stale_outputs_before_invoking_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    workspace = tmp_path / "repo"
    runner_temp = tmp_path / "runner"
    workspace.mkdir()
    runner_temp.mkdir()
    _init_repository(workspace)
    plan = helper.create_plan(
        _plan_options(
            helper,
            workspace=workspace,
            runner_temp=runner_temp,
            event={},
        )._replace(reviewers="", llm="false")
    )
    plan_path = runner_temp / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    outputs = [
        Path(plan["markdown_path"]),
        Path(plan["json_path"]),
        Path(plan["sarif_path"]),
    ]
    for output in outputs:
        output.write_text("stale", encoding="utf-8")

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        assert not any(output.exists() for output in outputs)
        command = args[0]
        assert isinstance(command, list)
        assert command[:12] == [
            "uv",
            "run",
            "--locked",
            "--no-dev",
            "--no-sync",
            "--project",
            str(ROOT),
            "python",
            "-P",
            "-s",
            "-c",
            (
                "import os, apex_ray; "
                "apex_ray.__version__ = os.environ['APEX_RAY_SOURCE_VERSION']; "
                "from apex_ray.cli import app; app()"
            ),
        ]
        assert command[12:] == plan["args"][1:]
        runtime_env = kwargs["env"]
        assert isinstance(runtime_env, dict)
        assert runtime_env["PYTHONPATH"] == str(ROOT / "src")
        assert runtime_env["PYTHONNOUSERSITE"] == "1"
        expected_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        assert runtime_env["APEX_RAY_SOURCE_VERSION"] == expected_version
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    assert helper._run(plan_path) == 7
    assert not any(output.exists() for output in outputs)


def _write_finalization_plan(
    root: Path,
    *,
    quality_gate_status: str,
    fail_on_quality_gate: bool,
    include_critical_finding: bool = False,
) -> Path:
    json_path = root / "review.json"
    sarif_path = root / "review.sarif"
    findings: list[dict[str, Any]] = []
    if include_critical_finding:
        findings.append(
            {
                "title": "Critical issue",
                "severity": "critical",
                "confidence": "high",
                "file": "src/payment.ts",
                "line": 10,
                "failure_mode": "A transaction may be duplicated.",
                "evidence": "The retry path lacks an idempotency key.",
                "suggested_fix": "Persist an idempotency key.",
                "suggested_test": "Retry the same transaction.",
            }
        )
    json_path.write_text(
        json.dumps(
            {
                "project": {"root": str(root), "is_git_repo": True},
                "config": {},
                "diff": {"target_mode": "patch"},
                "summary": {},
                "llm_coverage": {
                    "quality_gate_status": quality_gate_status,
                    "quality_gate_reasons": (
                        ["Required reviewer security did not complete."] if quality_gate_status == "fail" else []
                    ),
                    "total_context_packs": 1,
                    "reviewed_context_packs": 1,
                },
                "findings": findings,
                "generated_at": "2026-07-26T00:00:00Z",
                "version": "test",
            }
        ),
        encoding="utf-8",
    )
    sarif_path.write_text("{}\n", encoding="utf-8")
    plan_path = root / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "json_path": str(json_path),
                "sarif_path": str(sarif_path),
                "reviewers": ["security"],
                "llm_mode": "configured",
                "markdown_output": ".apex-ray/ci/review.md",
                "sarif_output": ".apex-ray/ci/review.sarif",
                "fail_on_quality_gate": fail_on_quality_gate,
            }
        ),
        encoding="utf-8",
    )
    return plan_path


@pytest.mark.parametrize(
    ("quality_gate_status", "fail_on_quality_gate", "include_critical_finding", "expected"),
    [
        pytest.param("fail", True, False, 1, id="required-reviewer-failure-blocks"),
        pytest.param("fail", False, False, 0, id="explicit-quality-gate-opt-out"),
        pytest.param("pass", True, True, 0, id="ordinary-findings-do-not-block"),
    ],
)
def test_finalize_uses_coverage_quality_gate_as_ci_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    quality_gate_status: str,
    fail_on_quality_gate: bool,
    include_critical_finding: bool,
    expected: int,
) -> None:
    helper = _load_helper()
    github_output = tmp_path / "github-output"
    github_summary = tmp_path / "github-summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(github_summary))
    plan_path = _write_finalization_plan(
        tmp_path,
        quality_gate_status=quality_gate_status,
        fail_on_quality_gate=fail_on_quality_gate,
        include_critical_finding=include_critical_finding,
    )

    assert helper._finalize(plan_path) == expected

    outputs = github_output.read_text(encoding="utf-8")
    assert f"quality-gate-status={quality_gate_status}\n" in outputs
    assert "sarif-ready=true\n" in outputs
    summary = github_summary.read_text(encoding="utf-8")
    assert f"Coverage quality gate: `{quality_gate_status}`" in summary
    if include_critical_finding:
        assert "critical `1`" in summary


def test_finalize_fails_when_current_review_has_no_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "json_path": str(tmp_path / "missing.json"),
                "sarif_path": str(tmp_path / "review.sarif"),
            }
        ),
        encoding="utf-8",
    )
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    assert helper._finalize(plan_path) == 1
    assert github_output.read_text(encoding="utf-8") == "sarif-ready=false\n"


@pytest.mark.parametrize(
    "value",
    [
        "../review.json",
        "/tmp/review.json",
        "reports/../../review.json",
        "reports/review.json\nother",
        r"C:\tmp\review.json",
    ],
)
def test_action_rejects_output_paths_outside_workspace(
    tmp_path: Path,
    value: str,
) -> None:
    helper = _load_helper()

    with pytest.raises(ValueError, match="workspace-relative"):
        helper.safe_workspace_path(tmp_path, value, label="JSON output")


def test_action_rejects_overlapping_report_outputs(tmp_path: Path) -> None:
    helper = _load_helper()
    workspace = tmp_path / "repo"
    runner_temp = tmp_path / "runner"
    workspace.mkdir()
    runner_temp.mkdir()
    _init_repository(workspace)

    options = _plan_options(
        helper,
        workspace=workspace,
        runner_temp=runner_temp,
        event={},
    )._replace(
        reviewers="",
        markdown_output=".apex-ray/ci/report.json",
        json_output=".apex-ray/ci/report.json",
    )

    with pytest.raises(ValueError, match="different paths"):
        helper.create_plan(options)


def test_documented_workflows_are_valid_yaml_and_avoid_pull_request_target() -> None:
    docs = DOCS_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", docs, flags=re.DOTALL)

    assert blocks
    assert all(yaml.safe_load(block) is not None for block in blocks)
    assert "pull_request_target" not in docs
    assert "security-events: write" in docs
    assert "concurrency:" in docs
    assert "fail-on-quality-gate" in docs
    assert "`uv.lock`" in docs
    assert "required: true" not in docs
