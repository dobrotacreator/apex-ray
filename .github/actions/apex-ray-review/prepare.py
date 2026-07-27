#!/usr/bin/env python3
"""Runtime helper for the Apex Ray composite GitHub Action."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, NamedTuple

_REVIEWER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$")
_CLI_LLM_PROVIDERS = {"codex_cli", "claude_code_cli"}
_QUALITY_GATE_STATUSES = {"disabled", "pass", "warn", "fail"}
_ZERO_SHA = "0" * 40
_MAX_ANNOTATIONS = 50
_MAX_ANNOTATION_CHARS = 4_000


class PlanOptions(NamedTuple):
    workspace: Path
    runner_temp: Path
    event: dict[str, Any]
    repository: str
    actor: str
    config_path: str
    reviewers: str
    llm: str
    base: str
    markdown_output: str
    json_output: str
    sarif_output: str
    trust_pr_config: bool
    artifact_name: str
    sarif_category: str
    fail_on_quality_gate: bool


def create_plan(options: PlanOptions) -> dict[str, Any]:
    """Create a safe, serializable invocation plan."""

    workspace = options.workspace.resolve()
    runner_temp = options.runner_temp.resolve()
    _require_isolated_action_runtime(workspace, _action_root())
    runner_temp.mkdir(parents=True, exist_ok=True)
    requested_config = _safe_repo_path(options.config_path, label="Config path")
    reviewers = parse_reviewers(options.reviewers)
    llm = _choice(options.llm, {"auto", "true", "false"}, label="LLM mode")
    artifact_name = _safe_artifact_value(options.artifact_name, label="Artifact name")
    sarif_category = _safe_artifact_value(
        options.sarif_category,
        label="SARIF category",
    )

    markdown_path = safe_workspace_path(
        workspace,
        options.markdown_output,
        label="Markdown output",
    )
    json_path = safe_workspace_path(
        workspace,
        options.json_output,
        label="JSON output",
    )
    sarif_path = safe_workspace_path(
        workspace,
        options.sarif_output,
        label="SARIF output",
    )
    if len({markdown_path, json_path, sarif_path}) != 3:
        raise ValueError("Markdown, JSON, and SARIF outputs must use different paths.")
    for output_path in (markdown_path, json_path, sarif_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)

    pull_request = options.event.get("pull_request")
    pr_data = pull_request if isinstance(pull_request, dict) else None
    untrusted_pr = _is_untrusted_pull_request(
        pr_data,
        repository=options.repository,
        actor=options.actor,
    )
    use_head_config = bool(pr_data is not None and options.trust_pr_config and not untrusted_pr)
    # INPUT_BASE selects only the analyzed diff. Restricted configuration has a
    # separate, fail-closed trust root supplied by the pull-request event.
    config_base_sha = (
        "" if pr_data is None or use_head_config else _resolve_pull_request_config_base(workspace, pr_data)
    )
    analysis_base_ref = options.base.strip() or _event_base_ref(options.event)
    base_sha = _resolve_commit(workspace, analysis_base_ref) if analysis_base_ref else ""

    if pr_data is not None:
        if use_head_config:
            head_config = safe_workspace_path(
                workspace,
                requested_config,
                label="Config path",
            )
            raw_config = head_config.read_text(encoding="utf-8") if head_config.is_file() else None
        else:
            raw_config = _read_git_file(workspace, config_base_sha, requested_config)
        config_document = _load_config_document(raw_config)
        safe_config = _sanitize_config(config_document, runner_temp)
        if not untrusted_pr and llm != "false":
            _reject_cli_routes_when_llm_can_run(
                safe_config,
                force_enabled=llm == "true",
            )
        config_path = runner_temp / "apex-ray-ci" / "config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            _yaml().safe_dump(
                safe_config,
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        if raw_config is None:
            config_source = "restricted-defaults"
        else:
            config_source = "restricted-head" if use_head_config else "restricted-base"
    else:
        head_config = safe_workspace_path(
            workspace,
            requested_config,
            label="Config path",
        )
        config_path = head_config if head_config.is_file() else None
        config_source = "head" if config_path is not None else "defaults"

    args = [
        "apex-ray",
        "review",
        "--output",
        str(markdown_path),
        "--json",
        str(json_path),
        "--sarif",
        str(sarif_path),
        "--no-telemetry",
    ]
    if config_path is not None:
        args.extend(["--config", str(config_path)])
    if base_sha:
        args.extend(["--base", base_sha])
    for reviewer in reviewers:
        args.extend(["--reviewer", reviewer])

    if untrusted_pr:
        args.append("--no-llm")
        llm_mode = "disabled-untrusted-pr"
    elif llm == "true":
        args.append("--llm")
        llm_mode = "forced"
    elif llm == "false":
        args.append("--no-llm")
        llm_mode = "disabled-input"
    else:
        llm_mode = "configured"

    return {
        "schema_version": 1,
        "workspace": str(workspace),
        "config_path": str(config_path) if config_path is not None else "",
        "config_source": config_source,
        "base_sha": base_sha,
        "config_base_sha": config_base_sha,
        "untrusted_pr": untrusted_pr,
        "llm_mode": llm_mode,
        "reviewers": reviewers,
        "markdown_path": str(markdown_path),
        "markdown_output": options.markdown_output,
        "json_path": str(json_path),
        "json_output": options.json_output,
        "sarif_path": str(sarif_path),
        "sarif_output": options.sarif_output,
        "artifact_name": artifact_name,
        "sarif_category": sarif_category,
        "fail_on_quality_gate": options.fail_on_quality_gate,
        "args": args,
    }


def parse_reviewers(value: str) -> list[str]:
    reviewers: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,\n]", value):
        reviewer = item.strip()
        if not reviewer:
            continue
        if not _REVIEWER_RE.fullmatch(reviewer):
            raise ValueError(
                "Reviewer ids must start with a lowercase letter or digit and "
                "contain only lowercase letters, digits, '.', '_', or '-'."
            )
        if reviewer not in seen:
            reviewers.append(reviewer)
            seen.add(reviewer)
    return reviewers


def safe_workspace_path(workspace: Path, value: str, *, label: str) -> Path:
    """Resolve a workspace-relative path without permitting escapes or links."""

    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or "\\" in value
        or PureWindowsPath(value).is_absolute()
    ):
        raise ValueError(f"{label} must be a workspace-relative path.")
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"{label} must be a workspace-relative path.")
    resolved_workspace = workspace.resolve()
    candidate = resolved_workspace.joinpath(*relative.parts)
    current = resolved_workspace
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise ValueError(f"{label} must not contain symbolic links.")
    if not candidate.resolve().is_relative_to(resolved_workspace):
        raise ValueError(f"{label} must be a workspace-relative path.")
    return candidate


def _action_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _require_isolated_action_runtime(workspace: Path, action_root: Path) -> None:
    resolved_workspace = workspace.resolve()
    resolved_action_root = action_root.resolve()
    if resolved_action_root.is_relative_to(resolved_workspace) or resolved_workspace.is_relative_to(
        resolved_action_root
    ):
        raise ValueError("The locked Apex Ray Action runtime must resolve outside the repository under review.")


def _safe_repo_path(value: str, *, label: str) -> str:
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or "\\" in value
        or PureWindowsPath(value).is_absolute()
    ):
        raise ValueError(f"{label} must be a workspace-relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"{label} must be a workspace-relative path.")
    return path.as_posix()


def _safe_artifact_value(value: str, *, label: str) -> str:
    if not _ARTIFACT_RE.fullmatch(value):
        raise ValueError(f"{label} must use only letters, digits, '.', '_', or '-' and be at most 128 characters.")
    return value


def _choice(value: str, choices: set[str], *, label: str) -> str:
    normalized = value.strip().lower()
    if normalized not in choices:
        raise ValueError(f"{label} must be one of: {', '.join(sorted(choices))}.")
    return normalized


def _event_base_ref(event: dict[str, Any]) -> str:
    pull_request = event.get("pull_request")
    if isinstance(pull_request, dict):
        base = pull_request.get("base")
        if isinstance(base, dict) and isinstance(base.get("sha"), str):
            return base["sha"]
    before = event.get("before")
    if isinstance(before, str) and before and before != _ZERO_SHA:
        return before
    return ""


def _resolve_pull_request_config_base(
    workspace: Path,
    pull_request: dict[str, Any],
) -> str:
    base_sha = _nested_string(pull_request, "base", "sha")
    if not re.fullmatch(r"[0-9a-f]{40,64}", base_sha):
        raise ValueError("Pull-request event must include a valid base commit SHA to load restricted configuration.")
    try:
        return _resolve_commit(workspace, base_sha)
    except ValueError as exc:
        raise ValueError("Unable to resolve the pull-request base commit SHA for restricted configuration.") from exc


def _is_untrusted_pull_request(
    pull_request: dict[str, Any] | None,
    *,
    repository: str,
    actor: str,
) -> bool:
    if pull_request is None:
        return False
    if actor == "dependabot[bot]":
        return True
    base_name = _nested_string(pull_request, "base", "repo", "full_name")
    head_name = _nested_string(pull_request, "head", "repo", "full_name")
    trusted_base = base_name or repository
    return not head_name or head_name.casefold() != trusted_base.casefold()


def _nested_string(data: dict[str, Any], *keys: str) -> str:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return value if isinstance(value, str) else ""


def _resolve_commit(workspace: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Unable to resolve review base ref: {ref}")
    sha = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise ValueError(f"Git returned an invalid commit id for review base: {ref}")
    return sha


def _read_git_file(workspace: Path, commit: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _load_config_document(raw_config: str | None) -> dict[str, Any]:
    if raw_config is None:
        return {"review": {}}
    yaml_module = _yaml()
    try:
        loaded = yaml_module.safe_load(raw_config)
    except yaml_module.YAMLError as exc:
        raise ValueError(f"The trusted base Apex Ray config is invalid YAML: {exc}") from exc
    if loaded is None:
        if yaml_module.compose(raw_config) is None:
            loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("The trusted base Apex Ray config must be a mapping.")
    review = loaded.get("review", {})
    if review is None:
        review = {}
    if not isinstance(review, dict):
        raise ValueError("The trusted base Apex Ray review config must be a mapping.")
    return deepcopy(loaded)


def _sanitize_config(
    document: dict[str, Any],
    runner_temp: Path,
) -> dict[str, Any]:
    safe_document = deepcopy(document)
    review_value = safe_document.get("review", {})
    review = review_value if isinstance(review_value, dict) else {}
    safe_document["review"] = review
    local_data_root = runner_temp / "apex-ray-ci" / "local-data"

    review["rule_paths"] = []
    memory = _mapping(review, "memory")
    memory.update({"enabled": False, "paths": []})

    analyzer = _mapping(review, "analyzer")
    analyzer.update(
        {
            "script_path": None,
            "index_cache_enabled": True,
            "index_cache_dir": str(local_data_root / "cache" / "analyzer" / "typescript"),
            "refresh_index_cache": False,
        }
    )
    llm = _mapping(review, "llm")
    llm.update(
        {
            "cache_enabled": False,
            "cache_dir": None,
            "refresh_cache": False,
        }
    )
    telemetry = _mapping(review, "telemetry")
    telemetry.update(
        {
            "enabled": False,
            "path": "${local_data}/telemetry/review-runs.jsonl",
        }
    )
    reports = _mapping(review, "reports")
    reports.update(
        {
            "archive": False,
            "archive_dir": "${local_data}/reports/runs",
        }
    )
    triage = _mapping(review, "triage")
    triage.update(
        {
            "enabled": False,
            "state_path": "${local_data}/triage/suppressions.json",
            "events_path": "${local_data}/triage/events.jsonl",
        }
    )
    review["local_data"] = {"root": str(local_data_root)}
    return safe_document


def _reject_cli_routes_when_llm_can_run(
    document: dict[str, Any],
    *,
    force_enabled: bool,
) -> None:
    from apex_ray.models import ReviewConfig

    review = document.get("review", {})
    if not isinstance(review, dict):
        raise ValueError("The trusted base Apex Ray review config must be a mapping.")
    config = ReviewConfig.model_validate(review)
    if not (force_enabled or config.llm.enabled):
        return

    routes: list[str] = []
    root_provider = str(config.llm.provider)
    if root_provider in _CLI_LLM_PROVIDERS:
        routes.append(f"review.llm.provider ({root_provider})")
    for profile_name, profile in sorted(config.llm.profiles.items()):
        provider = str(profile.provider or config.llm.provider)
        if provider in _CLI_LLM_PROVIDERS:
            routes.append(f"review.llm.profiles.{profile_name} ({provider})")
    if routes:
        raise ValueError(
            "Restricted pull-request config cannot enable CLI LLM provider routes: "
            f"{', '.join(routes)}. Use an API provider or disable LLM review for this job."
        )


def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key, {})
    mapping = deepcopy(value) if isinstance(value, dict) else {}
    parent[key] = mapping
    return mapping


def _yaml() -> Any:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyYAML is unavailable. Install the matching Apex Ray package before planning a review."
        ) from exc
    return yaml


def _load_event(path: str) -> dict[str, Any]:
    if not path:
        return {}
    event_path = Path(path)
    loaded = json.loads(event_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("GitHub event payload must be a JSON object.")
    return loaded


def _parse_bool(value: str, *, label: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{label} must be true or false.")
    return normalized == "true"


def _write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    if "\n" in name or "\r" in name or "\n" in value or "\r" in value:
        raise ValueError("GitHub Action outputs must be single-line values.")
    with Path(output_path).open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def _write_step_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines))
        stream.write("\n")


def _workflow_command_escape(value: str, *, property_value: bool = False) -> str:
    sanitized = "".join(
        character if character in {"\r", "\n", "\t"} or ord(character) >= 32 else "\N{REPLACEMENT CHARACTER}"
        for character in value
    )
    escaped = sanitized.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        escaped = escaped.replace(":", "%3A").replace(",", "%2C")
    return escaped


def _safe_annotation_file(value: str) -> str | None:
    normalized = value.replace("\\", "/")
    if any(ord(character) < 32 for character in normalized):
        return None
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _emit_finding_annotations(findings: list[Any]) -> None:
    command_by_severity = {
        "critical": "error",
        "high": "error",
        "medium": "warning",
        "low": "notice",
    }
    emitted = 0
    for finding in findings:
        if emitted >= _MAX_ANNOTATIONS:
            break
        line = finding.line
        file = _safe_annotation_file(str(finding.file))
        if file is None or not isinstance(line, int) or isinstance(line, bool) or line < 1:
            continue
        severity = str(finding.severity)
        command = command_by_severity.get(severity, "warning")
        title = _workflow_command_escape(
            f"Apex Ray {severity} finding",
            property_value=True,
        )
        message = f"{finding.title}: {finding.failure_mode}"[:_MAX_ANNOTATION_CHARS]
        print(
            f"::{command} "
            f"file={_workflow_command_escape(file, property_value=True)},"
            f"line={line},title={title}::"
            f"{_workflow_command_escape(message)}"
        )
        emitted += 1


def _write_plan_outputs(plan: dict[str, Any], plan_path: Path) -> None:
    outputs = {
        "plan": str(plan_path),
        "repository-path": str(plan["workspace"]),
        "config-source": str(plan["config_source"]),
        "untrusted-pr": str(plan["untrusted_pr"]).lower(),
        "llm-mode": str(plan["llm_mode"]),
        "reviewers": ",".join(plan["reviewers"]),
        "markdown-output": str(plan["markdown_output"]),
        "markdown-path": str(plan["markdown_path"]),
        "json-output": str(plan["json_output"]),
        "json-path": str(plan["json_path"]),
        "sarif-output": str(plan["sarif_output"]),
        "sarif-path": str(plan["sarif_path"]),
        "artifact-name": str(plan["artifact_name"]),
        "sarif-category": str(plan["sarif_category"]),
        "fail-on-quality-gate": str(plan["fail_on_quality_gate"]).lower(),
    }
    for name, value in outputs.items():
        _write_output(name, value)


def _read_plan(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
        raise ValueError("Invalid Apex Ray Action plan.")
    return loaded


def _plan_from_environment() -> int:
    workspace = Path(os.environ["APEX_RAY_REPOSITORY_PATH"])
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    options = PlanOptions(
        workspace=workspace,
        runner_temp=runner_temp,
        event=_load_event(os.environ.get("GITHUB_EVENT_PATH", "")),
        repository=os.environ.get("GITHUB_REPOSITORY", ""),
        actor=os.environ.get("GITHUB_ACTOR", ""),
        config_path=os.environ["INPUT_CONFIG_PATH"],
        reviewers=os.environ.get("INPUT_REVIEWERS", ""),
        llm=os.environ["INPUT_LLM"],
        base=os.environ.get("INPUT_BASE", ""),
        markdown_output=os.environ["INPUT_MARKDOWN_OUTPUT"],
        json_output=os.environ["INPUT_JSON_OUTPUT"],
        sarif_output=os.environ["INPUT_SARIF_OUTPUT"],
        trust_pr_config=_parse_bool(
            os.environ["INPUT_TRUST_PR_CONFIG"],
            label="Trust PR config",
        ),
        artifact_name=os.environ["INPUT_ARTIFACT_NAME"],
        sarif_category=os.environ["INPUT_SARIF_CATEGORY"],
        fail_on_quality_gate=_parse_bool(
            os.environ["INPUT_FAIL_ON_QUALITY_GATE"],
            label="Fail on quality gate",
        ),
    )
    plan = create_plan(options)
    plan_path = runner_temp / "apex-ray-ci" / "plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    plan_path.chmod(0o600)
    _write_plan_outputs(plan, plan_path)
    return 0


def _run(plan_path: Path) -> int:
    plan = _read_plan(plan_path)
    args = plan.get("args")
    workspace = plan.get("workspace")
    if (
        not isinstance(args, list)
        or not args
        or args[0] != "apex-ray"
        or not all(isinstance(item, str) for item in args)
        or not isinstance(workspace, str)
    ):
        raise ValueError("Invalid Apex Ray Action command plan.")
    workspace_path = Path(workspace)
    action_root = _action_root()
    _require_isolated_action_runtime(workspace_path, action_root)
    for key, label in (
        ("markdown_output", "Markdown output"),
        ("json_output", "JSON output"),
        ("sarif_output", "SARIF output"),
    ):
        output = plan.get(key)
        if not isinstance(output, str):
            raise ValueError("Invalid Apex Ray Action output plan.")
        output_path = safe_workspace_path(workspace_path, output, label=label)
        if output_path.is_dir():
            raise ValueError(f"{label} must be a file path.")
        output_path.unlink(missing_ok=True)
    for required_path in (
        action_root / "pyproject.toml",
        action_root / "uv.lock",
        action_root / "src" / "apex_ray" / "__init__.py",
    ):
        if not required_path.is_file():
            raise RuntimeError(f"Locked Apex Ray Action source is missing {required_path.name}.")
    command = [
        "uv",
        "run",
        "--locked",
        "--no-dev",
        "--no-sync",
        "--project",
        str(action_root),
        "python",
        "-P",
        "-s",
        "-c",
        (
            "import os, apex_ray; "
            "apex_ray.__version__ = os.environ['APEX_RAY_SOURCE_VERSION']; "
            "from apex_ray.cli import app; app()"
        ),
        *args[1:],
    ]
    runtime_env = os.environ.copy()
    source_metadata = tomllib.loads((action_root / "pyproject.toml").read_text(encoding="utf-8"))
    source_version = source_metadata.get("project", {}).get("version")
    if not isinstance(source_version, str) or not _VERSION_RE.fullmatch(source_version):
        raise RuntimeError("Locked Apex Ray Action source has an invalid project version.")
    runtime_env["APEX_RAY_SOURCE_VERSION"] = source_version
    runtime_env["PYTHONNOUSERSITE"] = "1"
    runtime_env["PYTHONPATH"] = str(action_root / "src")
    return subprocess.run(
        command,
        cwd=workspace,
        check=False,
        env=runtime_env,
    ).returncode


def _finalize(plan_path: Path) -> int:
    from apex_ray.report import load_review_report

    plan = _read_plan(plan_path)
    json_path = Path(str(plan["json_path"]))
    sarif_path = Path(str(plan["sarif_path"]))
    if not json_path.is_file():
        _write_output("sarif-ready", "false")
        _write_step_summary(
            [
                "## Apex Ray review",
                "",
                "The review did not produce a JSON report. Inspect the review step logs.",
                "",
            ]
        )
        return 1

    report = load_review_report(json_path)
    if not sarif_path.is_file():
        _write_output("sarif-ready", "false")
        _write_step_summary(
            [
                "## Apex Ray review",
                "",
                "The review did not produce a SARIF report. Inspect the review step logs.",
                "",
            ]
        )
        return 1
    counts = Counter(str(finding.severity) for finding in report.findings)
    reviewer_text = ", ".join(plan["reviewers"]) or "configured reviewers"
    coverage = report.llm_coverage
    quality_gate_status = str(coverage.quality_gate_status)
    if quality_gate_status not in _QUALITY_GATE_STATUSES:
        raise ValueError(f"Invalid coverage quality gate status: {quality_gate_status!r}")
    fail_on_quality_gate = plan.get("fail_on_quality_gate", True)
    if not isinstance(fail_on_quality_gate, bool):
        raise ValueError("Invalid Apex Ray Action quality gate policy.")
    quality_gate_failed = fail_on_quality_gate and quality_gate_status == "fail"
    reviewer_statuses = {
        reviewer.reviewer_id: reviewer.status
        for reviewer in sorted(coverage.reviewers, key=lambda reviewer: reviewer.reviewer_id)
    }
    partial_coverage = coverage.partial_severity != "none"
    _emit_finding_annotations(report.findings)
    _write_step_summary(
        [
            "## Apex Ray review",
            "",
            f"- Reviewers: `{reviewer_text}`",
            f"- LLM mode: `{plan['llm_mode']}`",
            f"- Findings: `{len(report.findings)}` "
            f"(critical `{counts['critical']}`, high `{counts['high']}`, "
            f"medium `{counts['medium']}`, low `{counts['low']}`)",
            f"- Coverage quality gate: `{quality_gate_status}`",
            f"- Context coverage: `{coverage.reviewed_context_packs}` / `{coverage.total_context_packs}` packs",
            f"- Markdown artifact: `{plan['markdown_output']}`",
            f"- SARIF artifact: `{plan['sarif_output']}`",
            "",
        ]
    )
    outputs = {
        "findings-count": str(len(report.findings)),
        "critical-findings-count": str(counts["critical"]),
        "high-findings-count": str(counts["high"]),
        "medium-findings-count": str(counts["medium"]),
        "low-findings-count": str(counts["low"]),
        "partial-coverage": str(partial_coverage).lower(),
        "partial-coverage-severity": str(coverage.partial_severity),
        "reviewer-statuses": json.dumps(
            reviewer_statuses,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "quality-gate-status": quality_gate_status,
        "gate-outcome": "fail" if quality_gate_failed else "pass",
        "sarif-ready": "true",
    }
    for name, value in outputs.items():
        _write_output(name, value)
    return 1 if quality_gate_failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("plan_path", type=Path)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("plan_path", type=Path)
    args = parser.parse_args()

    if args.command == "plan":
        return _plan_from_environment()
    if args.command == "run":
        return _run(args.plan_path)
    if args.command == "finalize":
        return _finalize(args.plan_path)
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        title = _workflow_command_escape(
            "Apex Ray Action configuration",
            property_value=True,
        )
        print(
            f"::error title={title}::{_workflow_command_escape(str(exc))}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
