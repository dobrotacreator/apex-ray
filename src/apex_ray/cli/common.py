from pathlib import Path

import typer


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
