import shutil
import uuid
from pathlib import Path

import yaml

from apex_ray import git
from apex_ray.benchmark.errors import BenchmarkError
from apex_ray.benchmark.models import CaptureResult, ExpectedContext
from apex_ray.diff import parse_unified_diff
from apex_ray.models import AnalyzerReference, ContextPack, FileStatus, LLMProviderName, ReviewConfig, TargetMode
from apex_ray.pipeline import run_review_pipeline

CONFIG_FILES = (
    "package.json",
    "tsconfig.json",
    "jsconfig.json",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
)
LOCAL_CONFIG_FILES = ("package.json", "tsconfig.json", "jsconfig.json")


def capture_benchmark_case(
    source_repo: Path,
    output_dir: Path,
    name: str,
    target_mode: TargetMode,
    base: str | None = None,
    expected_title_contains: str | None = None,
    expected_file: str | None = None,
    llm: bool = False,
    provider: LLMProviderName = LLMProviderName.CODEX_CLI,
    verify: bool = True,
    overwrite: bool = False,
) -> CaptureResult:
    repo_root = git.repo_root(source_repo) or source_repo.resolve()
    if not git.is_git_repo(repo_root):
        raise BenchmarkError(f"Source repo is not a git repository: {source_repo}")
    write_dir, replace_output = _capture_output_dir(output_dir, overwrite=overwrite)

    diff_text = _diff_for_capture(repo_root, target_mode, base)
    diff_summary = parse_unified_diff(diff_text, target_mode)
    if not diff_summary.files:
        raise BenchmarkError("No changed files found to capture.")
    context_paths, expected_context, context_warnings = _capture_context(repo_root, diff_text, target_mode, base)

    repo_dir = write_dir / "repo"
    diff_path = write_dir / "change.diff"
    case_path = write_dir / "case.yml"
    copied_files: list[str] = []
    warnings: list[str] = [*context_warnings]
    try:
        repo_dir.mkdir(parents=True, exist_ok=True)

        for config_file in CONFIG_FILES:
            copied_files.extend(_copy_if_exists(repo_root, repo_dir, config_file))

        for changed_file in diff_summary.files:
            if changed_file.status == FileStatus.DELETED or changed_file.new_path is None:
                warnings.append(f"Skipped deleted file: {changed_file.old_path}")
                continue
            copied_files.extend(_copy_file_with_local_configs(repo_root, repo_dir, changed_file.new_path))

        for context_path in sorted(context_paths):
            copied_files.extend(_copy_file_with_local_configs(repo_root, repo_dir, context_path))

        diff_path.write_text(diff_text, encoding="utf-8")
        case = _captured_case_dict(
            name=name,
            llm=llm,
            provider=provider,
            verify=verify,
            expected_title_contains=expected_title_contains,
            expected_file=expected_file,
            expected_context=expected_context,
        )
        case_path.write_text(yaml.safe_dump(case, sort_keys=False), encoding="utf-8")
        if replace_output:
            _replace_output_directory(write_dir, output_dir)
    except Exception:
        if replace_output:
            shutil.rmtree(write_dir, ignore_errors=True)
        raise

    return CaptureResult(
        output_dir=str(output_dir),
        case_path=str(output_dir / "case.yml"),
        diff_path=str(output_dir / "change.diff"),
        repo_dir=str(output_dir / "repo"),
        copied_files=sorted(set(copied_files)),
        warnings=warnings,
    )


def _capture_output_dir(output_dir: Path, *, overwrite: bool) -> tuple[Path, bool]:
    if not output_dir.exists() or not any(output_dir.iterdir()):
        return output_dir, False
    if not overwrite:
        raise BenchmarkError(f"Output directory is not empty: {output_dir}")
    return output_dir.with_name(f".{output_dir.name}.{uuid.uuid4().hex}.tmp"), True


def _replace_output_directory(source: Path, destination: Path) -> None:
    if destination.exists():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    source.replace(destination)


def _capture_context(
    repo_root: Path,
    diff_text: str,
    target_mode: TargetMode,
    base: str | None,
) -> tuple[set[str], list[ExpectedContext], list[str]]:
    config = ReviewConfig()
    config.llm.enabled = False
    config.analyzer.index_cache_enabled = False
    report = run_review_pipeline(
        repo_root,
        diff_text,
        target_mode,
        config,
        base=base if target_mode == TargetMode.BASE else None,
    )

    paths: set[str] = set()
    expected_context: list[ExpectedContext] = []
    for pack in report.context_packs:
        paths.add(pack.file)
        paths.update(pack.related_tests)
        paths.update(reference.file for reference in pack.references)
        paths.update(callee.file for callee in pack.callees)
        paths.update(contract.file for contract in pack.contracts)
        paths.update(reference.file for reference in pack.metadata)
        paths.update(snippet.file for snippet in pack.changed_snippets)
        paths.update(snippet.file for snippet in pack.reference_snippets)
        paths.update(snippet.file for snippet in pack.callee_snippets)
        paths.update(snippet.file for snippet in pack.contract_snippets)
        paths.update(snippet.file for snippet in pack.metadata_snippets)
        paths.update(snippet.file for snippet in pack.related_test_snippets)
        captured_expectation = _captured_expected_context(pack)
        if captured_expectation:
            expected_context.append(captured_expectation)

    warnings = [*report.diff.warnings]
    for result in report.analyzer_results:
        warnings.extend(result.warnings)
    return paths, expected_context, warnings


def _captured_expected_context(pack: ContextPack) -> ExpectedContext | None:
    reference = next((reference for reference in pack.references if reference.kind != "import"), None)
    contract = next(iter(pack.contracts), None)
    metadata = next(iter(pack.metadata), None)
    related_test = pack.related_tests[0] if pack.related_tests else None
    if reference is None and contract is None and metadata is None and related_test is None:
        return None
    return ExpectedContext(
        pack_file=pack.file,
        related_test=related_test,
        reference_file=reference.file if reference else None,
        reference_kind=reference.kind if reference else None,
        reference_text_contains=_reference_text_marker(pack, reference) if reference else None,
        contract_file=contract.file if contract else None,
        contract_kind=contract.kind if contract else None,
        contract_text_contains=contract.text if contract else None,
        metadata_file=metadata.file if metadata else None,
        metadata_kind=metadata.kind if metadata else None,
        metadata_text_contains=metadata.text if metadata else None,
    )


def _reference_text_marker(pack: ContextPack, reference: AnalyzerReference) -> str | None:
    for symbol in [pack.symbol, *pack.symbols]:
        if symbol and symbol.name in reference.text:
            return symbol.name
    return reference.text


def _diff_for_capture(repo_root: Path, target_mode: TargetMode, base: str | None) -> str:
    if target_mode == TargetMode.WORKTREE:
        return git.diff_worktree(repo_root)
    if target_mode == TargetMode.STAGED:
        return git.diff_staged(repo_root)
    if target_mode == TargetMode.BASE:
        return git.diff_base(repo_root, base or "main")
    raise BenchmarkError("Patch mode is not supported for capture; use worktree, staged, or base.")


def _copy_if_exists(repo_root: Path, repo_dir: Path, rel_path: str) -> list[str]:
    source = (repo_root / rel_path).resolve()
    try:
        source.relative_to(repo_root.resolve())
    except ValueError:
        return []
    if not source.exists() or not source.is_file():
        return []
    target = repo_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return [rel_path]


def _copy_file_with_local_configs(repo_root: Path, repo_dir: Path, rel_path: str) -> list[str]:
    copied = _copy_if_exists(repo_root, repo_dir, rel_path)
    if not copied:
        return []
    copied.extend(_copy_local_config_ancestors(repo_root, repo_dir, rel_path))
    return copied


def _copy_local_config_ancestors(repo_root: Path, repo_dir: Path, rel_path: str) -> list[str]:
    copied: list[str] = []
    current = Path(rel_path).parent
    while True:
        for config_file in LOCAL_CONFIG_FILES:
            candidate = str(current / config_file) if str(current) != "." else config_file
            copied.extend(_copy_if_exists(repo_root, repo_dir, candidate))
        if str(current) == ".":
            break
        current = current.parent
    return copied


def _captured_case_dict(
    name: str,
    llm: bool,
    provider: LLMProviderName,
    verify: bool,
    expected_title_contains: str | None,
    expected_file: str | None,
    expected_context: list[ExpectedContext],
) -> dict[str, object]:
    expected = []
    if expected_title_contains or expected_file:
        expected.append(
            {
                key: value
                for key, value in {
                    "file": expected_file,
                    "title_contains": expected_title_contains,
                }.items()
                if value
            }
        )
    return {
        "name": name,
        "repo": "repo",
        "diff": "change.diff",
        "llm": llm,
        "provider": provider.value,
        "verify": verify,
        "expected": expected,
        "expected_context": [item.model_dump(mode="json", exclude_none=True) for item in expected_context],
    }
