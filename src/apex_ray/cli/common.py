from pathlib import Path

import typer

from apex_ray.config import ensure_apex_gitignore


def ensure_distinct_outputs(output: Path, json_output: Path, html_output: Path | None = None) -> None:
    outputs = [("Markdown", output), ("JSON", json_output)]
    if html_output is not None:
        outputs.append(("HTML", html_output))
    seen: dict[Path, str] = {}
    for label, path in outputs:
        resolved = path.resolve()
        existing = seen.get(resolved)
        if existing:
            raise typer.BadParameter(f"{existing} and {label} output paths must be different.")
        seen[resolved] = label


def resolve_output_path(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def ensure_apex_ignore_for_outputs(root: Path, *paths: Path | None) -> None:
    apex_dir = (root / ".apex-ray").resolve()
    for path in paths:
        if path is not None and path.resolve().is_relative_to(apex_dir):
            ensure_apex_gitignore(root)
            return
