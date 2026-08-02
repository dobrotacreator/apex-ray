import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from apex_ray.models import DartAnalyzerConfig

DartToolchainSource = Literal[
    "configured",
    "project-fvm",
    "path",
    "fvm",
    "flutter-sibling",
    "unavailable",
]


@dataclass(slots=True)
class DartToolchainResolution:
    command: list[str]
    source: DartToolchainSource
    version: str | None = None
    error: str | None = None
    remediation: str | None = None


DartCommandResolution = DartToolchainResolution


def resolve_dart_command(
    repo_root: Path,
    configured_command: Sequence[str] | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> DartToolchainResolution | None:
    """Resolve a Dart command without executing it or mutating the project."""

    configured = _validated_command(configured_command)
    if configured:
        return DartToolchainResolution(command=configured, source="configured")

    local_dart = _first_executable(repo_root / ".fvm" / "flutter_sdk" / "bin", "dart")
    if local_dart is not None:
        return DartToolchainResolution(command=[str(local_dart)], source="project-fvm")

    path_dart = which("dart")
    if path_dart:
        return DartToolchainResolution(command=[path_dart], source="path")

    path_fvm = which("fvm")
    if path_fvm:
        return DartToolchainResolution(command=[path_fvm, "dart"], source="fvm")

    flutter = which("flutter")
    if flutter:
        sibling = _unambiguous_flutter_dart(Path(flutter))
        if sibling is not None:
            return DartToolchainResolution(command=[str(sibling)], source="flutter-sibling")
    return None


def resolve_dart_toolchain(
    repo_root: Path,
    config: DartAnalyzerConfig | None = None,
    *,
    probe_version: bool = True,
    timeout_seconds: float = 2.0,
    which: Callable[[str], str | None] = shutil.which,
) -> DartToolchainResolution:
    """Resolve the project Dart SDK and optionally make a short version probe.

    Resolution failures are returned as data so ``doctor`` and analyzer
    fallback paths can offer the same actionable remediation.
    """

    configured_command = config.command if config is not None else None
    resolution = resolve_dart_command(repo_root, configured_command, which=which)
    if resolution is None:
        return DartToolchainResolution(
            command=[],
            source="unavailable",
            error="No Dart SDK command could be resolved for this project.",
            remediation=(
                "Install Flutter or Dart, configure review.analyzer.dart.command, or select the project's FVM SDK."
            ),
        )
    if probe_version:
        resolution.version, probe_error = _probe_dart_version(resolution.command, repo_root, timeout_seconds)
        if probe_error is not None:
            configured = "configured " if resolution.source == "configured" else ""
            resolution.error = f"Unable to run {configured}Dart SDK command {resolution.command[0]!r}: {probe_error}"
            resolution.remediation = (
                "Fix review.analyzer.dart.command and ensure it selects an executable Dart SDK."
                if resolution.source == "configured"
                else "Ensure the selected Dart or Flutter SDK is executable, then run apex-ray doctor again."
            )
        elif config is not None and not config.plugins:
            plugin_probe_error = _probe_disabled_analyzer_plugins(
                resolution.command,
                repo_root,
                timeout_seconds,
            )
            if plugin_probe_error is not None:
                resolution.error = f"Selected Dart SDK cannot disable analyzer plugins: {plugin_probe_error}"
                resolution.remediation = (
                    "Upgrade the selected Dart or Flutter SDK, or use plugins: true only for a trusted checkout."
                )
    return resolution


def _validated_command(command: Sequence[str] | None) -> list[str]:
    if command is None:
        return []
    if isinstance(command, str | bytes):
        raise ValueError("Dart command must be an argument list, not a shell string")
    result = list(command)
    if any(not isinstance(argument, str) or not argument.strip() or "\x00" in argument for argument in result):
        raise ValueError("Dart command arguments must be non-empty strings without NUL bytes")
    return result


def _first_executable(directory: Path, basename: str) -> Path | None:
    for name in (basename, f"{basename}.exe", f"{basename}.bat", f"{basename}.cmd"):
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _unambiguous_flutter_dart(flutter: Path) -> Path | None:
    parent_candidates = [flutter.parent]
    try:
        resolved_parent = flutter.resolve(strict=True).parent
    except OSError:
        resolved_parent = flutter.parent
    if resolved_parent != flutter.parent:
        parent_candidates.append(resolved_parent)

    candidates: dict[Path, Path] = {}
    for parent in parent_candidates:
        dart = _first_executable(parent, "dart")
        if dart is None:
            continue
        try:
            identity = dart.resolve(strict=True)
        except OSError:
            identity = dart.absolute()
        candidates[identity] = dart
    if len(candidates) != 1:
        return None
    return next(iter(candidates.values()))


def _probe_dart_version(
    command: list[str],
    repo_root: Path,
    timeout_seconds: float,
) -> tuple[str | None, str | None]:
    try:
        completed = subprocess.run(
            [*command, "--version"],
            cwd=repo_root,
            capture_output=True,
            check=False,
            text=True,
            timeout=max(0.1, timeout_seconds),
        )
    except subprocess.TimeoutExpired:
        return None, f"version probe exceeded {max(0.1, timeout_seconds):g}s"
    except OSError as exc:
        return None, str(exc)
    except subprocess.SubprocessError as exc:
        return None, f"version probe failed ({exc})"
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        detail = f": {output.splitlines()[0][:512]}" if output else ""
        return None, f"version probe exited with code {completed.returncode}{detail}"
    if not output:
        return None, "version probe produced no output"
    version = output.splitlines()[0][:512]
    prefix = "Dart SDK version:"
    if version.startswith(prefix):
        version = version.removeprefix(prefix).strip()
    return version or None, None if version else "version probe produced no version"


def _probe_disabled_analyzer_plugins(
    command: list[str],
    repo_root: Path,
    timeout_seconds: float,
) -> str | None:
    timeout = max(0.1, timeout_seconds)
    try:
        completed = subprocess.run(
            [*command, "language-server", "--no-plugins", "--help"],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"capability probe exceeded {timeout:g}s"
    except OSError as exc:
        return str(exc)
    except subprocess.SubprocessError as exc:
        return f"capability probe failed ({exc})"
    if completed.returncode == 0:
        return None
    output = (completed.stderr or completed.stdout).strip()
    detail = f": {output.splitlines()[0][:512]}" if output else ""
    return f"capability probe exited with code {completed.returncode}{detail}"
