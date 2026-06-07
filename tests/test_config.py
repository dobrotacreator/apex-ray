from pathlib import Path

import pytest

from apex_ray import git
from apex_ray.config import (
    ConfigError,
    ensure_apex_gitignore,
    find_local_config,
    init_config,
    init_project,
    load_config,
)


def test_load_config_defaults_when_missing(tmp_path: Path) -> None:
    config, path = load_config(tmp_path)

    assert path is None
    assert config.base == "main"
    assert "**/generated/**" in config.ignore


def test_init_config_creates_default_file(tmp_path: Path) -> None:
    path = init_config(tmp_path)
    config, loaded_path = load_config(tmp_path)

    assert path == loaded_path
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "max_changed_snippets" not in text
    assert config.base == "main"
    assert config.analyzer.index_cache_enabled is True
    assert config.analyzer.index_cache_dir is None
    assert config.analyzer.changed_file_shard_size == 40
    assert config.analyzer.adaptive_sharding is True
    assert config.analyzer.large_change_shard_size == 4
    assert config.context.max_pack_chars == 40000
    assert config.context.max_reference_snippets == 8
    assert config.rule_paths == [".apex-ray/rules"]
    assert config.memory.paths == [".apex-ray/memory"]
    assert config.memory.max_cards_per_pack == 4
    assert config.local_data.root == "git_common"
    assert config.llm.profiles == {}
    assert config.llm.enabled is True
    assert config.llm.effort == "medium"
    assert config.llm.max_packs == 64
    assert config.llm.max_deep_packs == 48
    assert config.llm.max_input_tokens == 300_000
    assert config.llm.cache_dir == "${local_data}/cache/llm"
    assert config.telemetry.enabled is True
    assert config.telemetry.path == "${local_data}/telemetry/review-runs.jsonl"
    assert config.reports.archive is True
    assert config.reports.archive_dir == "${local_data}/reports/runs"
    assert config.reports.retention == 20
    assert config.triage.enabled is True
    assert config.triage.state_path == "${local_data}/triage/suppressions.json"
    assert config.triage.events_path == "${local_data}/triage/events.jsonl"
    assert config.triage.default_expiry_days == 14
    assert config.triage.max_active_suppressions == 200
    assert config.triage.events_retention_days == 90
    assert config.gates.pre_push.progress == "auto"
    assert config.gates.pre_push.progress_interval_seconds == 5.0
    assert config.gates.pre_push.incremental_retry.enabled is False
    assert config.gates.pre_push.incremental_retry.state_path == ".apex-ray/reports/pre-push-state.json"


def test_load_config_parses_analyzer_shard_size(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text(
        "review:\n  analyzer:\n    changed_file_shard_size: 7\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert config.analyzer.changed_file_shard_size == 7


def test_load_config_parses_review_telemetry(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text(
        "review:\n  telemetry:\n    enabled: true\n    path: .apex-ray/telemetry/custom.jsonl\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert config.telemetry.enabled is True
    assert config.telemetry.path == ".apex-ray/telemetry/custom.jsonl"


def test_load_config_parses_local_data(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text(
        "review:\n  local_data:\n    root: git_common\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert config.local_data.root == "git_common"


def test_load_config_parses_report_archive(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text(
        "review:\n  reports:\n    archive: true\n    archive_dir: .apex-ray/reports/archive\n    retention: 7\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert config.reports.archive is True
    assert config.reports.archive_dir == ".apex-ray/reports/archive"
    assert config.reports.retention == 7


def test_load_config_parses_triage(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text(
        "review:\n"
        "  triage:\n"
        "    enabled: true\n"
        "    state_path: .apex-ray/triage/custom-suppressions.json\n"
        "    events_path: .apex-ray/triage/custom-events.jsonl\n"
        "    default_expiry_days: 30\n"
        "    max_active_suppressions: 25\n"
        "    events_retention_days: 120\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert config.triage.enabled is True
    assert config.triage.state_path == ".apex-ray/triage/custom-suppressions.json"
    assert config.triage.events_path == ".apex-ray/triage/custom-events.jsonl"
    assert config.triage.default_expiry_days == 30
    assert config.triage.max_active_suppressions == 25
    assert config.triage.events_retention_days == 120


def test_load_config_parses_pre_push_incremental_retry(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text(
        "review:\n"
        "  gates:\n"
        "    pre_push:\n"
        "      incremental_retry:\n"
        "        enabled: true\n"
        "        state_path: .apex-ray/reports/custom-pre-push-state.json\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert config.gates.pre_push.incremental_retry.enabled is True
    assert config.gates.pre_push.incremental_retry.state_path == ".apex-ray/reports/custom-pre-push-state.json"


def test_load_config_merges_local_override_after_shared_config(tmp_path: Path) -> None:
    shared = tmp_path / ".apex-ray" / "config.yml"
    local = tmp_path / ".apex-ray" / "config.local.yml"
    shared.parent.mkdir()
    shared.write_text(
        "review:\n"
        "  ignore:\n"
        "    - shared/**\n"
        "  llm:\n"
        "    enabled: true\n"
        "    jobs: 1\n"
        "    profiles:\n"
        "      cheap:\n"
        "        model: cheap-shared\n"
        "      strong:\n"
        "        model: strong-shared\n",
        encoding="utf-8",
    )
    local.write_text(
        "review:\n"
        "  ignore:\n"
        "    - local/**\n"
        "  llm:\n"
        "    jobs: 3\n"
        "    profiles:\n"
        "      cheap:\n"
        "        model: cheap-local\n",
        encoding="utf-8",
    )

    config, path = load_config(tmp_path)

    assert path == shared
    assert find_local_config(tmp_path) == local
    assert config.ignore == ["local/**"]
    assert config.llm.enabled is True
    assert config.llm.jobs == 3
    assert config.llm.profiles["cheap"].model == "cheap-local"
    assert config.llm.profiles["strong"].model == "strong-shared"


def test_load_config_explicit_path_ignores_local_override(tmp_path: Path) -> None:
    shared = tmp_path / ".apex-ray" / "config.yml"
    local = tmp_path / ".apex-ray" / "config.local.yml"
    shared.parent.mkdir()
    shared.write_text("review:\n  llm:\n    jobs: 1\n", encoding="utf-8")
    local.write_text("review:\n  llm:\n    jobs: 4\n", encoding="utf-8")

    config, path = load_config(tmp_path, shared)

    assert path == shared
    assert config.llm.jobs == 1


def test_local_override_errors_report_local_path(tmp_path: Path) -> None:
    shared = tmp_path / ".apex-ray" / "config.yml"
    local = tmp_path / ".apex-ray" / "config.local.yml"
    shared.parent.mkdir()
    shared.write_text("review:\n", encoding="utf-8")
    local.write_text("review:\n  llm:\n    jobs: 0\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)

    assert str(local) in str(exc.value)
    assert "jobs" in str(exc.value)


def test_init_project_creates_team_setup_files(tmp_path: Path) -> None:
    written = init_project(tmp_path)

    assert tmp_path / ".apex-ray" / "config.yml" in written
    assert (tmp_path / ".apex-ray" / "rules").is_dir()
    assert (tmp_path / ".apex-ray" / "memory").is_dir()
    assert (tmp_path / ".apex-ray" / "reports").is_dir()
    assert "config.local.yml" in (tmp_path / ".apex-ray" / ".gitignore").read_text(encoding="utf-8")
    assert not (tmp_path / ".gitignore").exists()
    assert "apex-ray-review" in (tmp_path / "lefthook.yml").read_text(encoding="utf-8")
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert (tmp_path / ".apex-ray" / "skills" / "apex-ray" / "SKILL.md").exists()
    assert (tmp_path / ".apex-ray" / "skills" / "apex-ray-improve" / "SKILL.md").exists()
    assert (tmp_path / ".agents" / "skills" / "apex-ray" / "SKILL.md").exists()
    assert (tmp_path / ".agents" / "skills" / "apex-ray-improve" / "SKILL.md").exists()
    assert not (tmp_path / ".codex").exists()
    assert (tmp_path / ".claude" / "skills" / "apex-ray" / "SKILL.md").exists()
    assert (tmp_path / ".claude" / "skills" / "apex-ray-improve" / "SKILL.md").exists()
    assert not (tmp_path / ".claude" / ".gitignore").exists()
    lefthook_text = (tmp_path / "lefthook.yml").read_text(encoding="utf-8")
    assert "no_tty: true" in lefthook_text
    assert "follow: true" in lefthook_text
    assert "apex-ray gate pre-push" in lefthook_text
    assert "--base" not in lefthook_text
    assert "--no-llm" not in lefthook_text
    agents_text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "APEX_RAY_START" in agents_text
    assert "LLM analysis can be long-running and may appear idle" in agents_text
    assert "do not proactively run `apex-ray review` or `apex-ray gate pre-push`" in agents_text
    assert "pre-push incremental retry state remains the source of truth" in agents_text
    assert "Do not bypass the configured pre-push gate by default" in agents_text
    assert "$apex-ray" in agents_text
    assert "$apex-ray-improve" in agents_text
    skill_text = (tmp_path / ".apex-ray" / "skills" / "apex-ray" / "SKILL.md").read_text(encoding="utf-8")
    assert "do not proactively run `apex-ray review` or `apex-ray gate pre-push`" in skill_text
    assert "apex-ray review --continue-from .apex-ray/reports/review.json" in skill_text
    assert "Do not bypass the configured pre-push gate by default" in skill_text
    assert "Use `--no-llm` or `.apex-ray/config.local.yml`" in skill_text
    improve_skill_text = (tmp_path / ".apex-ray" / "skills" / "apex-ray-improve" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "recommendation-only improvements" in improve_skill_text
    assert "Greptile comments" in improve_skill_text


def test_init_project_codex_agent_files_use_agents_skill_directory(tmp_path: Path) -> None:
    init_project(tmp_path, hooks="none", agent_files="codex")

    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / ".apex-ray" / "skills" / "apex-ray" / "SKILL.md").exists()
    assert (tmp_path / ".agents" / "skills" / "apex-ray" / "SKILL.md").exists()
    assert (tmp_path / ".agents" / "skills" / "apex-ray-improve" / "SKILL.md").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".claude").exists()


def test_init_project_deprecates_update_gitignore_without_touching_root(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="root .gitignore"):
        init_project(tmp_path, update_gitignore=True, hooks="none", agent_files="none")

    assert (tmp_path / ".apex-ray" / ".gitignore").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_init_project_scoped_gitignore_covers_apex_local_artifacts(tmp_path: Path) -> None:
    git.run_git(["init"], cwd=tmp_path)
    init_project(tmp_path)
    apex_gitignore_text = (tmp_path / ".apex-ray" / ".gitignore").read_text(encoding="utf-8")

    ignored_paths = [
        ".apex-ray/config.local.yml",
        ".apex-ray/cache/example",
        ".apex-ray/telemetry/example.jsonl",
        ".apex-ray/reports/review.json",
        ".apex-ray/triage/suppressions.json",
        ".apex-ray/eval/runs/run.json",
        ".apex-ray/evals/runs/run.json",
    ]
    for relative_path in ignored_paths:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("local\n", encoding="utf-8")

        ignored = git.run_git(["check-ignore", "-v", relative_path], cwd=tmp_path, check=False)

        assert ignored.returncode == 0, relative_path
        assert ".apex-ray/.gitignore" in ignored.stdout, relative_path

    assert "review.md" not in apex_gitignore_text
    assert "review.json" not in apex_gitignore_text
    assert git.run_git(["check-ignore", "review.json"], cwd=tmp_path, check=False).returncode == 1


def test_init_project_extends_existing_apex_gitignore(tmp_path: Path) -> None:
    apex_gitignore = tmp_path / ".apex-ray" / ".gitignore"
    apex_gitignore.parent.mkdir()
    apex_gitignore.write_text("custom-apex\n", encoding="utf-8")

    init_project(tmp_path)

    assert "custom-apex" in apex_gitignore.read_text(encoding="utf-8")
    assert "reports/" in apex_gitignore.read_text(encoding="utf-8")
    assert "triage/" in apex_gitignore.read_text(encoding="utf-8")


def test_ensure_apex_gitignore_preserves_custom_entries_on_overwrite(tmp_path: Path) -> None:
    apex_gitignore = tmp_path / ".apex-ray" / ".gitignore"
    apex_gitignore.parent.mkdir()
    apex_gitignore.write_text("custom-apex\ncache/\n", encoding="utf-8")

    written = ensure_apex_gitignore(tmp_path, overwrite=True)

    text = apex_gitignore.read_text(encoding="utf-8")
    assert written == apex_gitignore
    assert "custom-apex\n" in text
    assert "cache/\n" in text
    assert "reports/\n" in text
    assert "triage/\n" in text
    assert "config.local.yml\n" in text


def test_ensure_apex_gitignore_rejects_external_symlink(tmp_path: Path) -> None:
    apex_dir = tmp_path / ".apex-ray"
    apex_dir.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-gitignore"
    outside.write_text("outside\n", encoding="utf-8")
    (apex_dir / ".gitignore").symlink_to(outside)

    with pytest.raises(ConfigError, match="outside the repository"):
        ensure_apex_gitignore(tmp_path)

    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_ensure_apex_gitignore_rejects_external_parent_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-apex"
    outside.mkdir()
    (tmp_path / ".apex-ray").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError, match="outside the repository"):
        ensure_apex_gitignore(tmp_path)

    assert not (outside / ".gitignore").exists()


def test_init_project_appends_to_existing_agent_files(tmp_path: Path) -> None:
    init_project(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text("custom\n", encoding="utf-8")

    init_project(tmp_path)

    text = agents.read_text(encoding="utf-8")
    assert text.startswith("custom\n")
    assert "APEX_RAY_START" in text
    assert "$apex-ray" in text
    assert "$apex-ray-improve" in text


def test_init_project_updates_existing_claude_symlink_to_agents_once(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("custom\n", encoding="utf-8")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").symlink_to("../AGENTS.md")

    init_project(tmp_path)

    text = agents.read_text(encoding="utf-8")
    assert text.startswith("custom\n")
    assert text.count("APEX_RAY_START") == 1
    assert (claude_dir / "CLAUDE.md").is_symlink()


def test_init_project_prefers_existing_root_claude_file(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("claude root\n", encoding="utf-8")

    init_project(tmp_path)

    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.startswith("claude root\n")
    assert "APEX_RAY_START" in text
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()


def test_init_project_updates_root_claude_symlink_to_agents(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("custom\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")

    init_project(tmp_path)

    text = agents.read_text(encoding="utf-8")
    assert text.startswith("custom\n")
    assert text.count("APEX_RAY_START") == 1
    assert (tmp_path / "CLAUDE.md").is_symlink()
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()


def test_init_project_reports_only_changed_files_on_second_run(tmp_path: Path) -> None:
    init_project(tmp_path)

    written = init_project(tmp_path)

    assert written == []


def test_init_project_validates_options_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Unsupported hooks value"):
        init_project(tmp_path, hooks="banana")

    assert not (tmp_path / ".apex-ray").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_init_project_detects_current_branch_as_default_base(tmp_path: Path) -> None:
    git.run_git(["init", "--initial-branch=master"], cwd=tmp_path)

    init_project(tmp_path)

    assert "base: master" in (tmp_path / ".apex-ray" / "config.yml").read_text(encoding="utf-8")


def test_init_project_does_not_use_feature_branch_as_default_base(tmp_path: Path) -> None:
    git.run_git(["init", "--initial-branch=develop"], cwd=tmp_path)
    git.run_git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    git.run_git(["config", "user.name", "Test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    git.run_git(["add", "README.md"], cwd=tmp_path)
    git.run_git(["commit", "-m", "initial"], cwd=tmp_path)
    git.run_git(["checkout", "-b", "feature/review"], cwd=tmp_path)

    init_project(tmp_path)

    assert "base: main" in (tmp_path / ".apex-ray" / "config.yml").read_text(encoding="utf-8")
    assert "base: feature/review" not in (tmp_path / ".apex-ray" / "config.yml").read_text(encoding="utf-8")


def test_init_project_leaves_existing_root_gitignore_untouched(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("# Apex Ray\n.apex-ray/reports/\n", encoding="utf-8")

    init_project(tmp_path)

    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "# Apex Ray\n.apex-ray/reports/\n"


def test_init_project_can_skip_agent_skill_files(tmp_path: Path) -> None:
    init_project(tmp_path, agent_skill=False)

    agents_text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "LLM analysis can be long-running and may appear idle" in agents_text
    assert "For manual Apex Ray runs" in agents_text
    assert "do not proactively run `apex-ray review` or `apex-ray gate pre-push`" in agents_text
    assert "$apex-ray" not in agents_text
    assert not (tmp_path / ".apex-ray" / "skills").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".claude" / "skills").exists()


def test_init_project_refuses_to_replace_existing_git_hook_without_force(tmp_path: Path) -> None:
    git.run_git(["init"], cwd=tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="pre-push hook already exists"):
        init_project(tmp_path, hooks="git")

    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho existing\n"


def test_init_project_can_replace_existing_git_hook_with_force(tmp_path: Path) -> None:
    git.run_git(["init"], cwd=tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")

    init_project(tmp_path, hooks="git", overwrite=True)

    assert "apex-ray gate pre-push" in hook.read_text(encoding="utf-8")


def test_init_project_refuses_to_rewrite_existing_lefthook_without_force(tmp_path: Path) -> None:
    (tmp_path / "lefthook.yml").write_text(
        "pre-push:\n  commands:\n    test:\n      run: |\n        npm test\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Lefthook config already exists"):
        init_project(tmp_path)

    assert "run: |\n        npm test" in (tmp_path / "lefthook.yml").read_text(encoding="utf-8")
    assert not (tmp_path / ".apex-ray").exists()


def test_init_project_preserves_agent_symlink_on_force(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("custom\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")

    init_project(tmp_path, overwrite=True)

    assert (tmp_path / "CLAUDE.md").is_symlink()
    assert "APEX_RAY_START" in target.read_text(encoding="utf-8")


def test_init_project_rejects_agent_symlink_outside_repo(tmp_path: Path) -> None:
    outside = tmp_path.with_name(f"{tmp_path.name}-outside-agents.md")
    outside.write_text("external\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to(outside)

    with pytest.raises(ConfigError, match="outside the repository"):
        init_project(tmp_path)

    assert outside.read_text(encoding="utf-8") == "external\n"
    assert not (tmp_path / ".apex-ray").exists()


def test_load_config_loads_markdown_rule_files(tmp_path: Path) -> None:
    config_path = tmp_path / ".apex-ray" / "config.yml"
    rules_dir = tmp_path / ".apex-ray" / "rules"
    rules_dir.mkdir(parents=True)
    config_path.write_text(
        "review:\n  rule_paths:\n    - .apex-ray/rules\n",
        encoding="utf-8",
    )
    (rules_dir / "corebank-env.md").write_text(
        "---\n"
        "id: corebank-env\n"
        "title: Preserve CoreBank env options\n"
        "severity: high\n"
        "mode: strict\n"
        "paths:\n"
        "  - apps/api/src/common/corebank/**\n"
        "triggers:\n"
        "  text:\n"
        "    - buildCoreBankHttpClientOptions\n"
        "---\n"
        "Env-derived timeout and log options must survive optional URL/key fields.\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert len(config.rule_definitions) == 1
    rule = config.rule_definitions[0]
    assert rule.id == "corebank-env"
    assert rule.severity == "high"
    assert rule.mode == "strict"
    assert rule.triggers.text == ["buildCoreBankHttpClientOptions"]
    assert rule.source_path == ".apex-ray/rules/corebank-env.md"
    assert "Env-derived timeout" in rule.body


def test_load_config_loads_markdown_memory_files(tmp_path: Path) -> None:
    config_path = tmp_path / ".apex-ray" / "config.yml"
    memory_dir = tmp_path / ".apex-ray" / "memory"
    memory_dir.mkdir(parents=True)
    config_path.write_text(
        "review:\n  memory:\n    paths:\n      - .apex-ray/memory\n",
        encoding="utf-8",
    )
    (memory_dir / "cart-total.md").write_text(
        "---\n"
        "id: cart-total\n"
        "title: Preserve cart totals\n"
        "kind: invariant\n"
        "severity: high\n"
        "paths:\n"
        "  - src/cart.ts\n"
        "triggers:\n"
        "  symbols:\n"
        "    - calculateTotal\n"
        "---\n"
        "Quantity multiplication is a project invariant.\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert len(config.memory_definitions) == 1
    card = config.memory_definitions[0]
    assert card.id == "cart-total"
    assert card.kind == "invariant"
    assert card.severity == "high"
    assert card.triggers.symbols == ["calculateTotal"]
    assert card.source_path == ".apex-ray/memory/cart-total.md"
    assert "Quantity multiplication" in card.body


def test_load_config_parses_llm_profiles_and_routing(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text(
        "review:\n"
        "  llm:\n"
        "    profiles:\n"
        "      cheap:\n"
        "        provider: codex_cli\n"
        "        model: codex-cheap\n"
        "        effort: low\n"
        "        codex_path: tools/codex\n"
        "      strong:\n"
        "        provider: claude_code_cli\n"
        "        model: claude-strong\n"
        "        effort: high\n"
        "        claude_path: tools/claude\n"
        "    routing:\n"
        "      review_profile: cheap\n"
        "      verify_profile: strong\n"
        "      escalated_review_profile: strong\n"
        "      escalate_review_when:\n"
        "        risk:\n"
        "          - auth\n"
        "        exclude_file_kind:\n"
        "          - test\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert config.llm.profiles["cheap"].model == "codex-cheap"
    assert config.llm.profiles["cheap"].effort == "low"
    assert config.llm.profiles["cheap"].codex_path == "tools/codex"
    assert config.llm.profiles["strong"].provider == "claude_code_cli"
    assert config.llm.profiles["strong"].model == "claude-strong"
    assert config.llm.profiles["strong"].effort == "high"
    assert config.llm.profiles["strong"].claude_path == "tools/claude"
    assert config.llm.routing.review_profile == "cheap"
    assert config.llm.routing.verify_profile == "strong"
    assert config.llm.routing.escalated_review_profile == "strong"
    assert config.llm.routing.escalate_review_when.risk == ["auth"]
    assert config.llm.routing.escalate_review_when.exclude_file_kind == ["test"]


def test_invalid_config_reports_path(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text("review:\n  rules: not-a-list\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)

    assert str(path) in str(exc.value)
    assert "rules" in str(exc.value)


def test_invalid_context_limit_reports_path(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text("review:\n  context:\n    max_pack_chars: 0\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)

    assert str(path) in str(exc.value)
    assert "max_pack_chars" in str(exc.value)


def test_unreadable_config_reports_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text("review:\n", encoding="utf-8")

    def raise_os_error(self: Path, encoding: str) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", raise_os_error)

    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)

    assert str(path) in str(exc.value)
    assert "Unable to read config file" in str(exc.value)


def test_unknown_config_key_reports_path(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text("review:\n  llm:\n    timout_seconds: 1\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)

    assert str(path) in str(exc.value)
    assert "timout_seconds" in str(exc.value)


def test_invalid_llm_timeout_reports_path(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text("review:\n  llm:\n    timeout_seconds: 0\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)

    assert str(path) in str(exc.value)
    assert "timeout_seconds" in str(exc.value)


def test_unknown_routing_profile_reports_path(tmp_path: Path) -> None:
    path = tmp_path / ".apex-ray" / "config.yml"
    path.parent.mkdir()
    path.write_text("review:\n  llm:\n    routing:\n      review_profile: cheap\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)

    assert str(path) in str(exc.value)
    assert "unknown profile 'cheap'" in str(exc.value)


def test_invalid_rule_frontmatter_reports_config_path(tmp_path: Path) -> None:
    config_path = tmp_path / ".apex-ray" / "config.yml"
    rules_dir = tmp_path / ".apex-ray" / "rules"
    rules_dir.mkdir(parents=True)
    config_path.write_text("review:\n  rule_paths:\n    - .apex-ray/rules\n", encoding="utf-8")
    (rules_dir / "broken.md").write_text("---\nfoo: [\n---\nbody\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)

    assert str(config_path) in str(exc.value)
    assert "Invalid YAML frontmatter" in str(exc.value)


def test_invalid_memory_frontmatter_reports_config_path(tmp_path: Path) -> None:
    config_path = tmp_path / ".apex-ray" / "config.yml"
    memory_dir = tmp_path / ".apex-ray" / "memory"
    memory_dir.mkdir(parents=True)
    config_path.write_text("review:\n  memory:\n    paths:\n      - .apex-ray/memory\n", encoding="utf-8")
    (memory_dir / "broken.md").write_text("---\nfoo: [\n---\nbody\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)

    assert str(config_path) in str(exc.value)
    assert "Invalid YAML frontmatter" in str(exc.value)
