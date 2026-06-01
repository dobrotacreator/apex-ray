import json
from pathlib import Path

from apex_ray.models import (
    ContextPack,
    DiffStats,
    DiffSummary,
    FileKind,
    LLMRun,
    ProjectProfile,
    ReviewConfig,
    TargetMode,
)
from apex_ray.report import build_report
from apex_ray.telemetry import append_review_telemetry, load_review_telemetry, render_review_telemetry_summary


def test_review_telemetry_round_trip(tmp_path: Path) -> None:
    config = ReviewConfig()
    config.llm.enabled = True
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        config,
        DiffSummary(target_mode=TargetMode.WORKTREE, stats=DiffStats(files_changed=1, additions=3)),
        context_packs=[ContextPack(id="src/cart.ts#file", file="src/cart.ts", file_kind=FileKind.SOURCE)],
        llm_runs=[
            LLMRun(
                provider="fake",
                context_pack_id="src/cart.ts#file",
                status="ok",
                duration_ms=9,
                input_chars=400,
                estimated_input_tokens=100,
                actual_input_tokens=80,
                actual_output_tokens=20,
                actual_total_tokens=100,
                estimated_saved_input_tokens=25,
                usage_source="unit",
                cache_hits=1,
            )
        ],
    )
    telemetry_path = tmp_path / ".apex-ray" / "telemetry" / "review-runs.jsonl"

    append_review_telemetry(
        report,
        telemetry_path,
        source_repo=tmp_path,
        duration_ms=25,
        output_path=tmp_path / "review.md",
        json_output_path=tmp_path / "review.json",
    )
    entries = load_review_telemetry(telemetry_path)
    summary = render_review_telemetry_summary(entries)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["target_mode"] == "worktree"
    assert entry["duration_ms"] == 25
    assert entry["files_changed"] == 1
    assert entry["llm_estimated_input_tokens"] == 100
    assert entry["llm_actual_total_tokens"] == 100
    assert entry["llm_estimated_saved_input_tokens"] == 25
    assert entry["llm_usage_sources"] == ["unit"]
    assert entry["llm_cache_hits"] == 1
    assert entry["pack_status_counts"] == {"reviewed_deep": 1}
    assert "Latest LLM tokens: `~100`" in summary
    assert "Latest actual LLM tokens: `100`" in summary


def test_review_telemetry_jsonl_is_compact(tmp_path: Path) -> None:
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, stats=DiffStats(files_changed=0)),
    )
    telemetry_path = tmp_path / "review-runs.jsonl"

    append_review_telemetry(report, telemetry_path, source_repo=tmp_path, duration_ms=1)

    line = telemetry_path.read_text(encoding="utf-8").strip()
    assert "\n" not in line
    assert json.loads(line)["context_packs_count"] == 0
