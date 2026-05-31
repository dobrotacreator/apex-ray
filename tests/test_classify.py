from pathlib import Path

from apex_ray.classify import classify_diff, detect_file_kind, detect_language
from apex_ray.diff import parse_unified_diff
from apex_ray.models import FileKind, TargetMode


def test_detect_file_kind() -> None:
    assert detect_file_kind("src/app.test.ts") == FileKind.TEST
    assert detect_file_kind("docs/usage.md") == FileKind.DOCS
    assert detect_file_kind("db/migrations/001_create_users.sql") == FileKind.MIGRATION
    assert detect_file_kind("schema.prisma") == FileKind.SCHEMA
    assert detect_file_kind("package-lock.json") == FileKind.LOCKFILE


def test_detect_language() -> None:
    assert detect_language("src/app.ts") == "typescript"
    assert detect_language("src/app.py") == "python"
    assert detect_language("unknown.file") == "unknown"


def test_classify_diff_adds_risk_signals() -> None:
    text = Path("tests/fixtures/sample.diff").read_text(encoding="utf-8")
    summary = parse_unified_diff(text, target_mode=TargetMode.PATCH)

    classified = classify_diff(summary, ignore_patterns=["docs/**"])

    auth_file = classified.files[0]
    assert auth_file.file_kind == FileKind.SOURCE
    assert auth_file.language == "python"
    assert {signal.kind for signal in auth_file.risk_signals} >= {"auth", "test_gap"}

    docs_file = classified.files[1]
    assert docs_file.is_ignored is True
    assert docs_file.ignore_reason == "Matched ignore pattern: docs/**"
    assert classified.stats.ignored_files == 1


def test_classify_diff_does_not_match_risk_keywords_inside_identifiers() -> None:
    summary = parse_unified_diff(
        "diff --git a/.apex-ray/config.yml b/.apex-ray/config.yml\n"
        "--- a/.apex-ray/config.yml\n"
        "+++ b/.apex-ray/config.yml\n"
        "@@ -1 +1 @@\n"
        "-max_input_tokens: 1000\n"
        "+max_input_tokens: 2000\n",
        target_mode=TargetMode.PATCH,
    )

    classified = classify_diff(summary, ignore_patterns=[])

    assert {signal.kind for signal in classified.files[0].risk_signals} == set()
