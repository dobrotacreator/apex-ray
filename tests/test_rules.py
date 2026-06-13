from pathlib import Path

from apex_ray.models import AnalyzerReference, CodeSnippet, ContextPack, ReviewRule
from apex_ray.rules import load_rule_file, match_rules_for_pack, rule_match_for_prompt


def test_rule_paths_match_changed_file_not_context_paths() -> None:
    pack = ContextPack(
        id="src/cart.ts#diff",
        file="src/cart.ts",
        references=[
            AnalyzerReference(file="src/admin.ts", line=10, text="calculateTotal(items)", kind="call"),
        ],
        reference_snippets=[
            CodeSnippet(file="src/admin.ts", start_line=8, end_line=12, code="calculateTotal(items)\n"),
        ],
    )
    rule = ReviewRule(id="admin-only", paths=["src/admin.ts"])

    assert match_rules_for_pack(pack, [rule]) == []


def test_rule_context_paths_match_supplied_context() -> None:
    pack = ContextPack(
        id="src/cart.ts#diff",
        file="src/cart.ts",
        references=[
            AnalyzerReference(file="src/admin.ts", line=10, text="calculateTotal(items)", kind="call"),
        ],
    )
    rule = ReviewRule(id="admin-context", context_paths=["src/admin.ts"])

    assert [match.id for match in match_rules_for_pack(pack, [rule])] == ["admin-context"]


def test_rule_resolution_surfaces_are_loaded_and_carried_to_matches(tmp_path: Path) -> None:
    rule_path = tmp_path / "schema-migration-contracts.md"
    rule_path.write_text(
        """---
id: schema-migration-contracts
title: Keep schemas and migrations aligned
resolution_surfaces:
  - apps/api/src/database/**
  - apps/migrator/migrations/**
---
Schema changes require migrations.
""",
        encoding="utf-8",
    )

    rule = load_rule_file(rule_path)
    match = rule_match_for_prompt(rule)

    assert rule.resolution_surfaces == ["apps/api/src/database/**", "apps/migrator/migrations/**"]
    assert match.resolution_surfaces == rule.resolution_surfaces
