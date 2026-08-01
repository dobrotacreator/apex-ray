from pathlib import Path

import pytest

from apex_ray.classify import classify_diff, detect_file_kind, detect_language
from apex_ray.diff import parse_unified_diff
from apex_ray.models import FileKind, RiskConfig, RiskRule, RiskSeverity, TargetMode
from apex_ray.risk import risk_signal_score


@pytest.mark.parametrize(
    "path",
    [
        "src/api.generated.ts",
        "src/api.generated.tsx",
        "src/api.generated.mts",
        "src/api.generated.cts",
        "src/api.generated.js",
        "src/api.generated.jsx",
        "src/api.generated.mjs",
        "src/api.generated.cjs",
        "src/api.generated.d.ts",
        "src/api.generated.d.mts",
        "src/api.generated.d.cts",
    ],
)
def test_detect_file_kind_recognizes_generated_ts_js_extensions(path: str) -> None:
    assert detect_file_kind(path) == FileKind.GENERATED


def test_detect_file_kind() -> None:
    assert detect_file_kind("src/app.test.ts") == FileKind.TEST
    assert detect_file_kind("internal/auth/service_test.go") == FileKind.TEST
    assert detect_file_kind("docs/usage.md") == FileKind.DOCS
    assert detect_file_kind("db/migrations/001_create_users.sql") == FileKind.MIGRATION
    assert detect_file_kind("schema.prisma") == FileKind.SCHEMA
    assert detect_file_kind("package-lock.json") == FileKind.LOCKFILE


def test_detect_language() -> None:
    assert detect_language("src/app.ts") == "typescript"
    assert detect_language("src/app.mts") == "typescript"
    assert detect_language("src/app.cts") == "typescript"
    assert detect_language("src/app.py") == "python"
    assert detect_language("internal/auth/service.go") == "go"
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


def test_classify_diff_applies_localized_project_risk_policy() -> None:
    summary = parse_unified_diff(
        "diff --git a/src/settlement/quote.ts b/src/settlement/quote.ts\n"
        "--- a/src/settlement/quote.ts\n"
        "+++ b/src/settlement/quote.ts\n"
        "@@ -1,2 +1,2 @@\n"
        "-return amount;\n"
        "+return applyRounding(amount, currency);\n",
        target_mode=TargetMode.PATCH,
    )
    risk = RiskConfig(
        built_in_enabled=False,
        rules=[
            RiskRule(
                id="settlement-boundary",
                severity="critical",
                score=97,
                paths=["src/settlement/**"],
                languages=["typescript"],
                file_kinds=["source"],
                statuses=["modified"],
                text=["rounding"],
                categories=["financial"],
                reviewer_tags=["finance"],
                guidance="Check value preservation.",
            )
        ],
    )

    classified = classify_diff(summary, ignore_patterns=[], risk=risk)

    signals = classified.files[0].risk_signals
    assert len(signals) == 1
    signal = signals[0]
    assert signal.kind == "policy:settlement-boundary"
    assert signal.severity == "critical"
    assert signal.score == 97
    assert signal.source == "project"
    assert signal.rule_id == "settlement-boundary"
    assert signal.line == 1
    assert signal.categories == ["financial"]
    assert signal.reviewer_tags == ["finance"]
    assert signal.guidance == "Check value preservation."
    assert classified.files[0].hunks[0].risk_signals == signals


def test_project_risk_text_rule_deduplicates_matches_at_same_current_anchor() -> None:
    summary = parse_unified_diff(
        "diff --git a/src/payments/transfer.ts b/src/payments/transfer.ts\n"
        "--- a/src/payments/transfer.ts\n"
        "+++ b/src/payments/transfer.ts\n"
        "@@ -1,3 +1 @@\n"
        "-transfer(primary);\n"
        "-transfer(secondary);\n"
        "-transfer(tertiary);\n"
        "+transferSafely([primary, secondary, tertiary]);\n",
        target_mode=TargetMode.PATCH,
    )
    risk = RiskConfig(
        built_in_enabled=False,
        rules=[
            RiskRule(
                id="money-movement",
                severity=RiskSeverity.CRITICAL,
                text=["transfer"],
            )
        ],
    )

    classified = classify_diff(summary, ignore_patterns=[], risk=risk)

    signals = classified.files[0].risk_signals
    assert [(signal.kind, signal.line) for signal in signals] == [("policy:money-movement", 1)]
    assert classified.files[0].hunks[0].risk_signals == signals


def test_project_risk_glob_matches_zero_directory_double_star() -> None:
    summary = parse_unified_diff(
        "diff --git a/src/auth.test.ts b/src/auth.test.ts\n"
        "--- a/src/auth.test.ts\n"
        "+++ b/src/auth.test.ts\n"
        "@@ -1 +1 @@\n"
        "-expect(allowed).toBe(false);\n"
        "+expect(allowed).toBe(true);\n",
        target_mode=TargetMode.PATCH,
    )
    risk = RiskConfig(
        built_in_enabled=False,
        rules=[
            RiskRule(
                id="test-policy",
                paths=["src/**/*.test.ts"],
                severity=RiskSeverity.HIGH,
            )
        ],
    )

    classified = classify_diff(summary, ignore_patterns=[], risk=risk)

    assert [signal.kind for signal in classified.files[0].risk_signals] == ["policy:test-policy"]


def test_project_risk_rule_can_explicitly_assign_zero_priority_score() -> None:
    summary = parse_unified_diff(
        "diff --git a/docs/runbook.md b/docs/runbook.md\n"
        "--- a/docs/runbook.md\n"
        "+++ b/docs/runbook.md\n"
        "@@ -1 +1 @@\n"
        "-Old wording\n"
        "+New wording\n",
        target_mode=TargetMode.PATCH,
    )
    risk = RiskConfig(
        built_in_enabled=False,
        rules=[
            RiskRule(
                id="routine-docs",
                severity=RiskSeverity.LOW,
                score=0,
                paths=["docs/**"],
            )
        ],
    )

    classified = classify_diff(summary, ignore_patterns=[], risk=risk)

    signal = classified.files[0].risk_signals[0]
    assert risk_signal_score(signal) == 0
