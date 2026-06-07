from pathlib import Path

from apex_ray import git
from apex_ray.models import LocalDataConfig, ReviewConfig

LOCAL_DATA_TOKEN = "${local_data}"
GIT_COMMON_ROOT = "git_common"


class LocalDataPathError(ValueError):
    pass


def resolve_local_data_root(repo_root: Path, config: LocalDataConfig) -> Path:
    if config.root == GIT_COMMON_ROOT:
        common_dir = git.common_dir(repo_root)
        if common_dir is not None:
            return common_dir / "apex-ray"
        return repo_root / ".apex-ray"

    root = Path(config.root).expanduser()
    if root.is_absolute():
        return root
    return repo_root / root


def resolve_config_path(repo_root: Path, local_data: LocalDataConfig, value: str | Path) -> Path:
    text = str(value)
    if LOCAL_DATA_TOKEN in text:
        if text == LOCAL_DATA_TOKEN:
            return resolve_local_data_root(repo_root, local_data)
        for separator in ("/", "\\"):
            prefix = f"{LOCAL_DATA_TOKEN}{separator}"
            if text.startswith(prefix):
                suffix = text[len(prefix) :]
                return resolve_local_data_root(repo_root, local_data) / Path(suffix)
        raise LocalDataPathError(f"{LOCAL_DATA_TOKEN} must be the first path segment: {text}")

    path = Path(text).expanduser()
    if path.is_absolute():
        return path
    return repo_root / path


def resolve_runtime_config_paths(repo_root: Path, config: ReviewConfig) -> ReviewConfig:
    effective = config.model_copy(deep=True)
    if effective.llm.cache_dir:
        effective.llm.cache_dir = str(resolve_config_path(repo_root, effective.local_data, effective.llm.cache_dir))
    if effective.reports.archive_dir:
        effective.reports.archive_dir = str(
            resolve_config_path(repo_root, effective.local_data, effective.reports.archive_dir)
        )
    return effective
