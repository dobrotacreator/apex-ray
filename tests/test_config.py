from pathlib import Path

import pytest

from apex_ray.config import ConfigError, find_local_config, init_config, init_project, load_config


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
    assert config.analyzer.large_change_shard_size == 8
    assert config.context.max_pack_chars == 40000
    assert config.context.max_reference_snippets == 8
    assert config.rule_paths == [".apex-ray/rules"]
    assert config.memory.paths == [".apex-ray/memory"]
    assert config.memory.max_cards_per_pack == 4
    assert config.llm.profiles == {}
    assert config.llm.enabled is False
    assert config.telemetry.enabled is False
    assert config.telemetry.path == ".apex-ray/telemetry/review-runs.jsonl"


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
    assert ".apex-ray/reports/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "apex-ray-review" in (tmp_path / "lefthook.yml").read_text(encoding="utf-8")
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert "--no-llm" in (tmp_path / "lefthook.yml").read_text(encoding="utf-8")
    agents_text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "review --continue-from .apex-ray/reports/review.json --residual-priority p0 --no-llm" in agents_text
    assert "Add `--llm` only when" in agents_text


def test_init_project_is_idempotent_for_existing_agent_files(tmp_path: Path) -> None:
    init_project(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text("custom\n", encoding="utf-8")

    init_project(tmp_path)

    assert agents.read_text(encoding="utf-8") == "custom\n"


def test_init_project_refuses_to_replace_existing_git_hook_without_force(tmp_path: Path) -> None:
    hook = tmp_path / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="pre-push hook already exists"):
        init_project(tmp_path, hooks="git")

    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho existing\n"


def test_init_project_can_replace_existing_git_hook_with_force(tmp_path: Path) -> None:
    hook = tmp_path / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")

    init_project(tmp_path, hooks="git", overwrite=True)

    assert "apex-ray review" in hook.read_text(encoding="utf-8")


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
        "        codex_path: tools/codex\n"
        "      strong:\n"
        "        provider: claude_code_cli\n"
        "        model: claude-strong\n"
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
    assert config.llm.profiles["cheap"].codex_path == "tools/codex"
    assert config.llm.profiles["strong"].provider == "claude_code_cli"
    assert config.llm.profiles["strong"].model == "claude-strong"
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
