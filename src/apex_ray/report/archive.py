import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from apex_ray.models import ReportsConfig


@dataclass(frozen=True)
class ReportArtifact:
    path: Path
    content: str


def archive_report_artifacts(
    root: Path,
    config: ReportsConfig,
    artifacts: list[ReportArtifact],
    *,
    created_at: datetime | None = None,
    run_id: str | None = None,
) -> Path | None:
    if not config.archive or not artifacts:
        return None

    archive_root = _resolve_archive_root(root, config.archive_dir)
    timestamp = _archive_timestamp(created_at or datetime.now(UTC))
    archive_id = f"{timestamp}-{run_id or uuid.uuid4().hex[:8]}"
    run_dir = _unique_run_dir(archive_root, archive_id)
    run_dir.mkdir(parents=True)

    written: list[dict[str, str]] = []
    for artifact in artifacts:
        artifact_path = run_dir / artifact.path.name
        artifact_path.write_text(artifact.content, encoding="utf-8")
        written.append(
            {
                "file": artifact_path.name,
                "source_path": str(artifact.path),
            }
        )

    manifest = {
        "archive_id": run_dir.name,
        "created_at": datetime.now(UTC).isoformat(),
        "files": written,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    _prune_archives(archive_root, config.retention)
    return run_dir


def _resolve_archive_root(root: Path, archive_dir: str) -> Path:
    configured = Path(archive_dir).expanduser()
    if configured.is_absolute():
        return configured
    return root / configured


def _archive_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _unique_run_dir(archive_root: Path, archive_id: str) -> Path:
    candidate = archive_root / archive_id
    if not candidate.exists():
        return candidate
    for index in range(2, 100):
        candidate = archive_root / f"{archive_id}-{index}"
        if not candidate.exists():
            return candidate
    return archive_root / f"{archive_id}-{uuid.uuid4().hex}"


def _prune_archives(archive_root: Path, retention: int | None) -> None:
    if retention is None:
        return
    run_dirs = sorted(path for path in archive_root.iterdir() if path.is_dir())
    stale_dirs = run_dirs[: max(0, len(run_dirs) - retention)]
    for path in stale_dirs:
        shutil.rmtree(path)
