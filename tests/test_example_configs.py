import tomllib
from pathlib import Path

import pytest
import yaml

from apex_ray.classify import classify_diff
from apex_ray.config import load_config
from apex_ray.diff import parse_unified_diff
from apex_ray.llm.routing import review_config_for_pack
from apex_ray.models import ChangedFile, ContextPack, FileKind, TargetMode
from apex_ray.reviewers import llm_config_for_reviewer, reviewer_matches_pack
from apex_ray.risk import apply_project_risk_policy
from apex_ray.rules import load_rule_file, match_rules_for_pack

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIGS = (
    "typescript-balanced.yml",
    "typescript-security.yml",
    "typescript-fintech.yml",
    "github-actions-api.yml",
)


@pytest.mark.parametrize("filename", EXAMPLE_CONFIGS)
def test_example_config_loads_through_public_config_api(
    tmp_path: Path,
    filename: str,
) -> None:
    config_path = REPO_ROOT / "examples" / "configs" / filename

    config, loaded_path = load_config(tmp_path, config_path)

    assert loaded_path == config_path
    if config.languages:
        assert {"typescript", "javascript"} <= set(config.languages)
    assert config.llm.enabled is True
    assert config.llm.max_deep_packs is not None
    assert config.llm.max_deep_packs <= config.llm.max_packs
    assert config.reviewers


def test_typescript_security_example_inherits_portable_model_defaults(
    tmp_path: Path,
) -> None:
    config_path = REPO_ROOT / "examples" / "configs" / "typescript-security.yml"

    config, _loaded_path = load_config(tmp_path, config_path)

    assert config.llm.model is None
    assert config.llm.profiles
    assert all(profile.model is None for profile in config.llm.profiles.values())


def test_repository_self_review_config_loads_without_local_overrides() -> None:
    config_path = REPO_ROOT / ".apex-ray" / "config.yml"

    config, loaded_path = load_config(REPO_ROOT, config_path)

    assert loaded_path == config_path
    assert config.base == "origin/main"
    assert config.gates.pre_push.fetch_base is True
    assert config.llm.max_packs < 96
    assert config.llm.max_deep_packs is not None
    assert config.llm.max_deep_packs < 64
    assert {reviewer.id for reviewer in config.reviewers} >= {
        "correctness",
        "security",
        "typescript",
    }


def test_repository_self_hosting_hook_runs_locked_source_runtime_directly() -> None:
    hook_path = REPO_ROOT / "lefthook.yml"
    if not hook_path.exists():
        pytest.skip("tracked self-hosting hook is not included in the source distribution")
    hook_config = yaml.safe_load(hook_path.read_text(encoding="utf-8"))

    command = hook_config["pre-push"]["commands"]["apex-ray-review"]["run"]

    assert command == "uv run --locked apex-ray gate pre-push"
    assert not (REPO_ROOT / "scripts" / "apex-ray-pre-push.sh").exists()


def test_repository_source_runtime_marker_forces_lf_checkouts() -> None:
    attributes_path = REPO_ROOT / ".gitattributes"
    if not attributes_path.exists():
        pytest.skip("tracked checkout attributes are not included in the source distribution")

    assert ".apex-ray/runtime text eol=lf" in attributes_path.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("path", "file_kind", "policy_id", "reviewer_id"),
    [
        (
            "tests/test_github_actions.py",
            FileKind.TEST,
            "provider-and-ci-trust-boundary-tests",
            "security",
        ),
        (
            "analyzer-runtimes/typescript/test/analyzer.test.ts",
            FileKind.TEST,
            "typescript-analyzer-core",
            "typescript",
        ),
        (
            "analyzer-runtimes/typescript/package.json",
            FileKind.DEPENDENCY,
            "typescript-analyzer-core",
            "typescript",
        ),
        (
            "tests/test_report.py",
            FileKind.TEST,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            ".apex-ray/config.yml",
            FileKind.CONFIG,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "tests/test_report_archive.py",
            FileKind.TEST,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "tests/test_invocation.py",
            FileKind.TEST,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "tests/test_repository_runtime.py",
            FileKind.TEST,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "tests/test_local_data.py",
            FileKind.TEST,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "tests/test_sarif.py",
            FileKind.TEST,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "tests/test_telemetry.py",
            FileKind.TEST,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "src/apex_ray/context/packs.py",
            FileKind.SOURCE,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "src/apex_ray/pipeline/selection.py",
            FileKind.SOURCE,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "src/apex_ray/llm/routing.py",
            FileKind.SOURCE,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "src/apex_ray/memory.py",
            FileKind.SOURCE,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "src/apex_ray/rules.py",
            FileKind.SOURCE,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "src/apex_ray/repository_runtime.py",
            FileKind.SOURCE,
            "configuration-and-report-contract",
            "compatibility",
        ),
        (
            "tests/test_gates.py",
            FileKind.TEST,
            "gate-state-test-contract",
            "compatibility",
        ),
        (
            "research/telemetry.zip",
            FileKind.UNKNOWN,
            "private-derived-artifact",
            "security",
        ),
        (
            ".env.production.local",
            FileKind.UNKNOWN,
            "credential-bearing-artifact",
            "security",
        ),
        (
            ".env.production",
            FileKind.UNKNOWN,
            "credential-bearing-artifact",
            "security",
        ),
        (
            "certs/prod.PEM",
            FileKind.UNKNOWN,
            "credential-bearing-artifact",
            "security",
        ),
        (
            "certs/prod.KEY",
            FileKind.UNKNOWN,
            "credential-bearing-artifact",
            "security",
        ),
        (
            ".Env.production",
            FileKind.UNKNOWN,
            "credential-bearing-artifact",
            "security",
        ),
        (
            "certs/prod.Pem",
            FileKind.UNKNOWN,
            "credential-bearing-artifact",
            "security",
        ),
        (
            "certs/prod.Key",
            FileKind.UNKNOWN,
            "credential-bearing-artifact",
            "security",
        ),
    ],
)
def test_repository_policy_routes_core_tests_and_private_artifacts_to_specialists(
    path: str,
    file_kind: FileKind,
    policy_id: str,
    reviewer_id: str,
) -> None:
    config, _loaded_path = load_config(REPO_ROOT, REPO_ROOT / ".apex-ray/config.yml")
    changed_file = ChangedFile(
        old_path=path,
        new_path=path,
        file_kind=file_kind,
    )

    apply_project_risk_policy(changed_file, config.risk)

    pack = ContextPack(
        id=f"{path}#file",
        file=path,
        file_kind=file_kind,
        risk_signals=changed_file.risk_signals,
    )
    signal = next(signal for signal in pack.risk_signals if signal.kind == f"policy:{policy_id}")
    reviewer = next(reviewer for reviewer in config.reviewers if reviewer.id == reviewer_id)
    reviewer_config = llm_config_for_reviewer(config.llm, reviewer)
    routed, profile, reason = review_config_for_pack(reviewer_config, pack)

    assert set(signal.reviewer_tags).intersection(reviewer.risk_tags)
    assert reviewer_matches_pack(reviewer, pack)
    assert profile == "strong"
    assert routed.model == "gpt-5.6-sol"
    assert reason == f"escalated:strong:risk:policy:{policy_id}"


def test_repository_private_artifact_policy_does_not_flag_telemetry_implementation() -> None:
    config, _loaded_path = load_config(REPO_ROOT, REPO_ROOT / ".apex-ray/config.yml")
    changed_file = ChangedFile(
        old_path="src/apex_ray/telemetry.py",
        new_path="src/apex_ray/telemetry.py",
        file_kind=FileKind.SOURCE,
    )

    apply_project_risk_policy(changed_file, config.risk)

    assert all(signal.kind != "policy:private-derived-artifact" for signal in changed_file.risk_signals)


@pytest.mark.parametrize(
    "path",
    [
        ".env.production.EXAMPLE",
        ".ENV.production.example",
        ".Env.production.Example",
    ],
)
def test_repository_credential_policy_allows_case_insensitive_example_suffixes(path: str) -> None:
    config, _loaded_path = load_config(REPO_ROOT, REPO_ROOT / ".apex-ray/config.yml")
    changed_file = ChangedFile(
        old_path=path,
        new_path=path,
        file_kind=FileKind.UNKNOWN,
    )

    apply_project_risk_policy(changed_file, config.risk)

    pack = ContextPack(
        id=f"{path}#file",
        file=path,
        file_kind=changed_file.file_kind,
        risk_signals=changed_file.risk_signals,
    )
    private_rule = load_rule_file(REPO_ROOT / ".apex-ray/rules/private-artifact-files.md")

    assert all(signal.kind != "policy:credential-bearing-artifact" for signal in changed_file.risk_signals)
    assert match_rules_for_pack(pack, [private_rule]) == []


@pytest.mark.parametrize(
    "path",
    [
        ".apex-ray/eval/runs/latest/report.json",
        ".apex-ray/evals/runs/latest/report.json",
    ],
)
def test_repository_config_reviews_force_added_private_run_artifacts(path: str) -> None:
    config, _loaded_path = load_config(REPO_ROOT, REPO_ROOT / ".apex-ray/config.yml")
    summary = parse_unified_diff(
        (
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            "@@ -0,0 +1 @@\n"
            '+{"private": true}\n'
        ),
        target_mode=TargetMode.PATCH,
    )

    classified = classify_diff(summary, ignore_patterns=config.ignore, risk=config.risk)
    changed_file = classified.files[0]
    private_rule = load_rule_file(REPO_ROOT / ".apex-ray/rules/private-artifact-files.md")
    pack = ContextPack(
        id=f"{path}#file",
        file=path,
        file_kind=changed_file.file_kind,
        risk_signals=changed_file.risk_signals,
    )
    security = next(reviewer for reviewer in config.reviewers if reviewer.id == "security")

    assert changed_file.is_ignored is False
    assert [rule.id for rule in match_rules_for_pack(pack, [private_rule])] == ["private-artifact-files"]
    assert reviewer_matches_pack(security, pack)


def test_source_distribution_includes_examples_and_self_review_config() -> None:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = set(document["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])

    assert {"/examples", "/.apex-ray/config.yml"} <= includes
