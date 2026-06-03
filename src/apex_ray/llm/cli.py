import json
import shutil
from pathlib import Path

from apex_ray.llm.errors import LLMProviderError


def build_codex_command(
    codex_path: str,
    schema_path: Path,
    output_path: Path,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    command = [
        codex_path,
        "--ask-for-approval",
        "never",
    ]
    if effort:
        if effort == "max":
            raise LLMProviderError("Codex CLI does not support effort 'max'; use low, medium, high, or xhigh.")
        command.extend(["--config", f'model_reasoning_effort="{effort}"'])
    command.extend(
        [
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
    )
    if model:
        command.extend(["--model", model])
    command.append("-")
    return command


def build_claude_command(
    claude_path: str,
    schema: dict[str, object],
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    command = [
        claude_path,
        "--print",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--tools",
        "",
    ]
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    return command


def resolve_codex_path(codex_path: str, repo_root: Path | None = None) -> str:
    return resolve_cli_path(codex_path, repo_root, display_name="Codex CLI")


def resolve_claude_path(claude_path: str, repo_root: Path | None = None) -> str:
    return resolve_cli_path(claude_path, repo_root, display_name="Claude Code CLI")


def resolve_cli_path(cli_path: str, repo_root: Path | None, *, display_name: str) -> str:
    configured_path = Path(cli_path).expanduser()
    if "/" in cli_path or "\\" in cli_path or configured_path.is_absolute():
        if not configured_path.is_absolute() and repo_root is not None:
            configured_path = repo_root / configured_path
        resolved_path = configured_path.resolve()
        if not resolved_path.exists():
            raise LLMProviderError(f"{display_name} not found: {cli_path}")
        return str(resolved_path)

    resolved = shutil.which(cli_path)
    if not resolved:
        raise LLMProviderError(f"{display_name} not found: {cli_path}")
    return resolved


def claude_result_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise LLMProviderError("Claude Code CLI did not write an output message.")
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(raw, dict):
        if raw.get("is_error"):
            message = raw.get("result") or raw.get("error") or stripped
            raise LLMProviderError(f"Claude Code CLI returned an error: {message}")
        subtype = str(raw.get("subtype") or "")
        if subtype.startswith("error_"):
            message = raw.get("error") or raw.get("result") or subtype
            raise LLMProviderError(f"Claude Code CLI structured output failed: {subtype}: {message}")
        if "structured_output" in raw:
            return json.dumps(raw["structured_output"])
        result = raw.get("result")
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return json.dumps(result)
        if "findings" in raw or "decisions" in raw:
            return json.dumps(raw)
    return stripped
