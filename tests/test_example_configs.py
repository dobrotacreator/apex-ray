import tomllib
from pathlib import Path

import pytest

from apex_ray.config import load_config

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


def test_repository_self_review_config_loads_without_local_overrides() -> None:
    config_path = REPO_ROOT / ".apex-ray" / "config.yml"

    config, loaded_path = load_config(REPO_ROOT, config_path)

    assert loaded_path == config_path
    assert config.llm.max_packs < 96
    assert config.llm.max_deep_packs is not None
    assert config.llm.max_deep_packs < 64
    assert {reviewer.id for reviewer in config.reviewers} >= {
        "correctness",
        "security",
        "typescript",
    }


def test_source_distribution_includes_examples_and_self_review_config() -> None:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = set(document["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])

    assert {"/examples", "/.apex-ray/config.yml"} <= includes
