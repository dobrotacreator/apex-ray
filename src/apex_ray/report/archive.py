import gzip
import hashlib
import json
import re
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from apex_ray.models import ReportsConfig

_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


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

    archive_root = _resolve_archive_root(root, config.archive_dir).resolve(strict=False)
    timestamp = _archive_timestamp(created_at or datetime.now(UTC))
    run_component = _validate_run_id(run_id) if run_id is not None else uuid.uuid4().hex[:8]
    archive_id = f"{timestamp}-{run_component}"
    run_dir = _unique_run_dir(archive_root, archive_id)
    try:
        run_dir.resolve(strict=False).relative_to(archive_root)
    except ValueError as exc:
        raise ValueError("run_id must keep the archive directory inside archive_root") from exc
    run_dir.mkdir(parents=True)

    written: list[dict[str, str | int]] = []
    used_names = {_artifact_name_key("manifest.json")}
    for artifact in artifacts:
        content = artifact.content.encode("utf-8")
        compress = config.compression == "gzip" or (
            config.compression == "auto" and len(content) >= config.compression_min_bytes
        )
        artifact_name = _unique_artifact_name(artifact.path.name, compress=compress, used_names=used_names)
        used_names.add(_artifact_name_key(artifact_name))
        artifact_path = run_dir / artifact_name
        if compress:
            artifact_path.write_bytes(gzip.compress(content, compresslevel=6, mtime=0))
        else:
            artifact_path.write_bytes(content)
        source_path, source_scope, source_id = _manifest_source(root, artifact.path)
        written.append(
            {
                "file": artifact_path.name,
                "source_path": source_path,
                "source_scope": source_scope,
                "source_id": source_id,
                "encoding": "gzip" if compress else "identity",
                "original_bytes": len(content),
                "stored_bytes": artifact_path.stat().st_size,
            }
        )

    manifest = {
        "schema_version": "archive-manifest/v2",
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


def _validate_run_id(run_id: str) -> str:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(
            "run_id must be 1-64 ASCII letters, digits, dots, underscores, or hyphens "
            "and must start with a letter or digit"
        )
    return run_id


def _manifest_source(root: Path, source_path: Path) -> tuple[str, str, str]:
    absolute = source_path.resolve(strict=False)
    try:
        relative = absolute.relative_to(root.resolve(strict=False)).as_posix()
        return relative, "repository", f"repository:{relative}"
    except ValueError:
        digest = hashlib.sha256(absolute.as_posix().encode("utf-8")).hexdigest()[:20]
        return source_path.name, "external", f"external:{digest}"


def _unique_artifact_name(
    source_name: str,
    *,
    compress: bool,
    used_names: set[str],
) -> str:
    suffixes = "".join(Path(source_name).suffixes)
    stem = source_name[: -len(suffixes)] if suffixes else source_name
    gzip_suffix = ".gz" if compress else ""
    candidate = f"{source_name}{gzip_suffix}"
    index = 2
    while _artifact_name_key(candidate) in used_names:
        candidate = f"{stem}-{index}{suffixes}{gzip_suffix}"
        index += 1
    return candidate


def _artifact_name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


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
