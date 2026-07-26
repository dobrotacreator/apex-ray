import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from apex_ray.models import DiffSummary, ProjectProfile, ReportsConfig, ReviewConfig, TargetMode
from apex_ray.report import (
    ReportArtifact,
    archive_report_artifacts,
    build_report,
    load_review_report,
)


def test_archive_report_artifacts_writes_run_directory(tmp_path: Path) -> None:
    config = ReportsConfig(archive=True, archive_dir=".apex-ray/reports/runs", retention=20)
    markdown = tmp_path / ".apex-ray" / "reports" / "review.md"
    json_report = tmp_path / ".apex-ray" / "reports" / "review.json"

    run_dir = archive_report_artifacts(
        tmp_path,
        config,
        [
            ReportArtifact(markdown, "# report\n"),
            ReportArtifact(json_report, '{"ok": true}\n'),
        ],
        created_at=datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
        run_id="unit",
    )

    assert run_dir == tmp_path / ".apex-ray" / "reports" / "runs" / "20260601T123000Z-unit"
    assert (run_dir / "review.md").read_text(encoding="utf-8") == "# report\n"
    assert (run_dir / "review.json").read_text(encoding="utf-8") == '{"ok": true}\n'
    assert "review.md" in (run_dir / "manifest.json").read_text(encoding="utf-8")


def test_archive_report_artifacts_prunes_old_runs(tmp_path: Path) -> None:
    archive_root = tmp_path / ".apex-ray" / "reports" / "runs"
    for name in ("20260101T000000Z-old", "20260201T000000Z-old"):
        run_dir = archive_root / name
        run_dir.mkdir(parents=True)
        (run_dir / "review.json").write_text("{}", encoding="utf-8")
    config = ReportsConfig(archive=True, archive_dir=".apex-ray/reports/runs", retention=2)

    run_dir = archive_report_artifacts(
        tmp_path,
        config,
        [ReportArtifact(tmp_path / "review.json", "{}")],
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
        run_id="new",
    )

    assert run_dir is not None
    assert sorted(path.name for path in archive_root.iterdir()) == [
        "20260201T000000Z-old",
        "20260301T000000Z-new",
    ]


def test_archive_report_artifacts_returns_none_when_disabled(tmp_path: Path) -> None:
    run_dir = archive_report_artifacts(
        tmp_path,
        ReportsConfig(archive=False),
        [ReportArtifact(tmp_path / "review.json", "{}")],
    )

    assert run_dir is None
    assert not (tmp_path / ".apex-ray").exists()


def test_archive_report_artifacts_compresses_large_content_without_absolute_source_paths(
    tmp_path: Path,
) -> None:
    config = ReportsConfig(
        archive=True,
        archive_dir=".apex-ray/reports/runs",
        compression="auto",
        compression_min_bytes=100,
    )
    report_path = tmp_path / ".apex-ray" / "reports" / "review.json"
    content = '{"context":"' + ("repeated-source-context-" * 200) + '"}'

    run_dir = archive_report_artifacts(
        tmp_path,
        config,
        [ReportArtifact(report_path, content)],
        run_id="compressed",
    )

    assert run_dir is not None
    compressed_path = run_dir / "review.json.gz"
    assert gzip.decompress(compressed_path.read_bytes()).decode("utf-8") == content
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["encoding"] == "gzip"
    assert manifest["files"][0]["source_path"] == ".apex-ray/reports/review.json"
    assert str(tmp_path) not in (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert manifest["files"][0]["stored_bytes"] < manifest["files"][0]["original_bytes"]


def test_load_review_report_reads_gzip_archive_artifact(tmp_path: Path) -> None:
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    run_dir = archive_report_artifacts(
        tmp_path,
        ReportsConfig(
            archive=True,
            archive_dir=".apex-ray/reports/runs",
            compression="gzip",
        ),
        [ReportArtifact(tmp_path / "review.json", report.model_dump_json())],
        run_id="gzip-load",
    )

    assert run_dir is not None
    loaded = load_review_report(run_dir / "review.json.gz")

    assert loaded.project == report.project
    assert loaded.diff == report.diff
