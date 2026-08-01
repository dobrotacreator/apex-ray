import gzip
import json
import unicodedata
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

import apex_ray.report.loading as report_loading_module
from apex_ray.findings import active_verifications, unresolved_verifications
from apex_ray.models import (
    ContextPack,
    DiffSummary,
    Finding,
    FindingConfidence,
    FindingSeverity,
    FindingVerification,
    LLMRun,
    ProjectProfile,
    ReportsConfig,
    ReviewConfig,
    TargetMode,
)
from apex_ray.report import (
    ReportArtifact,
    ReviewReportLoadError,
    archive_report_artifacts,
    build_report,
    load_review_report,
)


def _gzip_with_optional_headers(payload: bytes) -> bytes:
    header = bytearray(b"\x1f\x8b\x08\x1e" + b"\0" * 4 + b"\0\xff")
    extra = b"ARAY"
    header.extend(len(extra).to_bytes(2, "little"))
    header.extend(extra)
    header.extend(b"review.json\0")
    header.extend(b"Apex Ray archive\0")
    header.extend((zlib.crc32(header) & 0xFFFF).to_bytes(2, "little"))
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    body = compressor.compress(payload) + compressor.flush()
    trailer = (zlib.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "little")
    trailer += (len(payload) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(header) + body + trailer


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


def test_archive_report_artifacts_resolves_relative_source_paths_from_repository_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    run_dir = archive_report_artifacts(
        root,
        ReportsConfig(archive=True, archive_dir=".apex-ray/reports/runs"),
        [
            ReportArtifact(
                Path(".apex-ray/reports/review.json"),
                "{}",
            )
        ],
        run_id="relative-source",
    )

    assert run_dir is not None
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["source_path"] == ".apex-ray/reports/review.json"
    assert manifest["files"][0]["source_scope"] == "repository"
    assert manifest["files"][0]["source_id"] == "repository:.apex-ray/reports/review.json"


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


@pytest.mark.parametrize(
    "run_id",
    [
        "../escape",
        "nested/run",
        r"nested\run",
        "/absolute",
        r"C:\absolute",
        "..",
        ".hidden",
        "x" * 65,
    ],
)
def test_archive_report_artifacts_rejects_unsafe_run_ids(
    tmp_path: Path,
    run_id: str,
) -> None:
    with pytest.raises(ValueError, match="run_id"):
        archive_report_artifacts(
            tmp_path,
            ReportsConfig(archive=True, archive_dir=".apex-ray/reports/runs"),
            [ReportArtifact(tmp_path / "review.json", "{}")],
            run_id=run_id,
        )

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
    assert manifest["schema_version"] == "archive-manifest/v2"
    assert manifest["files"][0]["source_path"] == ".apex-ray/reports/review.json"
    assert manifest["files"][0]["source_scope"] == "repository"
    assert manifest["files"][0]["source_id"] == "repository:.apex-ray/reports/review.json"
    assert str(tmp_path) not in (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert manifest["files"][0]["stored_bytes"] < manifest["files"][0]["original_bytes"]


def test_archive_report_artifacts_keeps_same_named_external_sources_distinct(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    first_source = tmp_path / "external-a" / "review.json"
    second_source = tmp_path / "external-b" / "review.json"

    run_dir = archive_report_artifacts(
        root,
        ReportsConfig(
            archive=True,
            archive_dir=".apex-ray/reports/runs",
            compression="gzip",
        ),
        [
            ReportArtifact(first_source, '{"source":"first"}'),
            ReportArtifact(second_source, '{"source":"second"}'),
        ],
        run_id="external-collision",
    )

    assert run_dir is not None
    manifest_text = (run_dir / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["schema_version"] == "archive-manifest/v2"
    assert [entry["file"] for entry in manifest["files"]] == [
        "review.json.gz",
        "review-2.json.gz",
    ]
    assert [entry["source_path"] for entry in manifest["files"]] == [
        "review.json",
        "review.json",
    ]
    assert {entry["source_scope"] for entry in manifest["files"]} == {"external"}
    assert len({entry["source_id"] for entry in manifest["files"]}) == 2
    assert all(entry["source_id"].startswith("external:") for entry in manifest["files"])
    assert gzip.decompress((run_dir / "review.json.gz").read_bytes()) == b'{"source":"first"}'
    assert gzip.decompress((run_dir / "review-2.json.gz").read_bytes()) == b'{"source":"second"}'
    assert str(tmp_path) not in manifest_text


def test_archive_report_artifacts_reserves_manifest_filename(tmp_path: Path) -> None:
    run_dir = archive_report_artifacts(
        tmp_path,
        ReportsConfig(archive=True, archive_dir=".apex-ray/reports/runs"),
        [ReportArtifact(tmp_path / "manifest.json", '{"artifact":true}')],
        run_id="reserved-manifest",
    )

    assert run_dir is not None
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["file"] == "manifest-2.json"
    assert (run_dir / "manifest-2.json").read_text(encoding="utf-8") == '{"artifact":true}'


@pytest.mark.parametrize("compression", ["none", "gzip"])
def test_archive_report_artifacts_keeps_unicode_equivalent_names_distinct(
    tmp_path: Path,
    compression: Literal["none", "gzip"],
) -> None:
    first_name = "\u00e9.json"
    second_name = "e\u0301.json"

    run_dir = archive_report_artifacts(
        tmp_path,
        ReportsConfig(
            archive=True,
            archive_dir=".apex-ray/reports/runs",
            compression=compression,
        ),
        [
            ReportArtifact(tmp_path / first_name, '{"source":"first"}'),
            ReportArtifact(tmp_path / second_name, '{"source":"second"}'),
        ],
        run_id=f"unicode-{compression}",
    )

    assert run_dir is not None
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    stored_names = [entry["file"] for entry in manifest["files"]]
    normalized_names = {unicodedata.normalize("NFC", name).casefold() for name in stored_names}
    assert len(normalized_names) == 2
    stored_contents = []
    for stored_name in stored_names:
        content = (run_dir / stored_name).read_bytes()
        stored_contents.append(gzip.decompress(content) if compression == "gzip" else content)
    assert stored_contents == [b'{"source":"first"}', b'{"source":"second"}']


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


@pytest.mark.parametrize(
    ("reason", "include_failed_run", "expected_status"),
    [
        ("Verifier failed for this finding: temporary outage", True, "failed_provider"),
        ("Missing context pack: src/auth.ts#authorize:1", False, "missing_context_pack"),
    ],
)
def test_load_review_report_migrates_legacy_verifier_outages_to_unresolved(
    tmp_path: Path,
    reason: str,
    include_failed_run: bool,
    expected_status: str,
) -> None:
    pack = ContextPack(id="src/auth.ts#authorize:1", file="src/auth.ts")
    finding = Finding(
        title="Authorization bypass",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=pack.file,
        failure_mode="A transfer bypasses account ownership authorization.",
        evidence="The changed branch dispatches before the ownership check.",
        suggested_fix="Check ownership before dispatch.",
        suggested_test="Reject a cross-account transfer.",
        context_pack_id=pack.id,
    )
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
        context_packs=[pack],
        findings=[finding],
        verifications=[
            FindingVerification(
                finding=finding,
                approved=False,
                confidence=FindingConfidence.LOW,
                reason=reason,
            )
        ],
        llm_runs=(
            [
                LLMRun(
                    kind="verify",
                    provider="fake",
                    context_pack_id=pack.id,
                    status="failed_provider",
                    duration_ms=1,
                )
            ]
            if include_failed_run
            else []
        ),
    )
    payload = report.model_dump(mode="json")
    payload["verifications"][0].pop("superseded")
    payload["verifications"][0].pop("superseded_reason")
    path = tmp_path / "legacy-review.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_review_report(path)

    assert active_verifications(loaded.verifications) == []
    assert unresolved_verifications(loaded.verifications) == loaded.verifications
    assert loaded.verifications[0].superseded_reason == (
        f"Verification run did not complete successfully ({expected_status})."
    )


def test_load_review_report_accepts_bounded_optional_gzip_headers(tmp_path: Path) -> None:
    report = build_report(
        ProjectProfile(root="/repo", is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH),
    )
    path = tmp_path / "review.json.gz"
    path.write_bytes(_gzip_with_optional_headers(report.model_dump_json().encode()))

    loaded = load_review_report(path)

    assert loaded.project == report.project
    assert loaded.diff == report.diff


@pytest.mark.parametrize("compressed", [False, True])
def test_load_review_report_rejects_payloads_over_the_bounded_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compressed: bool,
) -> None:
    path = tmp_path / ("review.json.gz" if compressed else "review.json")
    payload = b'{"padding":"' + (b"x" * 512) + b'"}'
    path.write_bytes(gzip.compress(payload, mtime=0) if compressed else payload)
    monkeypatch.setattr(report_loading_module, "_MAX_REPORT_BYTES", 128)

    with pytest.raises(ReviewReportLoadError, match="exceeds 128 bytes"):
        load_review_report(path)


def test_load_review_report_bounds_concatenated_gzip_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "review.json.gz"
    path.write_bytes(b"".join(gzip.compress(b"", mtime=0) for _ in range(128)) + gzip.compress(b"{}", mtime=0))
    monkeypatch.setattr(report_loading_module, "_MAX_REPORT_BYTES", 128)

    with pytest.raises(
        ReviewReportLoadError,
        match=r"compressed Apex Ray report .* exceeds 1152 bytes",
    ):
        load_review_report(path)


def test_load_review_report_rejects_multiple_gzip_members(tmp_path: Path) -> None:
    path = tmp_path / "review.json.gz"
    path.write_bytes(gzip.compress(b"{}") + gzip.compress(b""))

    with pytest.raises(
        ReviewReportLoadError,
        match=r"gzip Apex Ray report .* contains trailing data",
    ):
        load_review_report(path)


def test_load_review_report_rejects_oversized_gzip_header(tmp_path: Path) -> None:
    path = tmp_path / "review.json.gz"
    path.write_bytes(b"\x1f\x8b\x08\x08" + b"\0" * 6 + b"a" * (64 * 1024) + b"\0")

    with pytest.raises(
        ReviewReportLoadError,
        match=r"gzip header .* exceeds 65536 bytes",
    ):
        load_review_report(path)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\x1f\x8b\x08\xe0" + b"\0" * 6, "reserved header flags"),
        (b"\x1f\x8b\x08\x04" + b"\0" * 6 + b"\x04\0ab", "truncated header"),
        (b"\x1f\x8b\x08\x08" + b"\0" * 6 + b"name", "truncated header"),
    ],
)
def test_load_review_report_rejects_malformed_gzip_headers(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    path = tmp_path / "review.json.gz"
    path.write_bytes(payload)

    with pytest.raises(ReviewReportLoadError, match=message):
        load_review_report(path)


def test_load_review_report_rejects_corrupt_gzip_checksum(tmp_path: Path) -> None:
    path = tmp_path / "review.json.gz"
    payload = bytearray(gzip.compress(b"{}", mtime=0))
    payload[-8] ^= 0xFF
    path.write_bytes(payload)

    with pytest.raises(ReviewReportLoadError, match="Invalid gzip Apex Ray report"):
        load_review_report(path)


def test_load_review_report_rejects_pathologically_nested_json(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text("[" * 1_000_000 + "0" + "]" * 1_000_000, encoding="utf-8")

    with pytest.raises(ReviewReportLoadError, match="Invalid JSON in Apex Ray report"):
        load_review_report(path)
