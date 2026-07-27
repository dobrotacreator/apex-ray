from pathlib import Path

from apex_ray.models import AnalyzerReference, CodeSnippet, ContextPack, ReviewRule
from apex_ray.rules import load_rule_file, match_rules_for_pack, rule_match_for_prompt

ROOT = Path(__file__).resolve().parents[1]


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


def test_private_artifact_rule_allows_curated_eval_inputs_but_blocks_generated_runs() -> None:
    rule = load_rule_file(ROOT / ".apex-ray/rules/private-artifact-files.md")

    allowed = [
        ContextPack(id="label", file=".apex-ray/eval/labels/pr-1.yml"),
        ContextPack(id="case", file=".apex-ray/evals/cases/pr-1.json"),
        ContextPack(id="env-example", file=".env.example"),
        ContextPack(id="env-production-example", file=".env.production.example"),
        ContextPack(id="mixed-env-example", file=".Env.production.Example"),
    ]
    private = [
        ContextPack(id="telemetry", file=".apex-ray/eval/telemetry/pr-eval-runs.jsonl"),
        ContextPack(id="legacy-run", file=".apex-ray/eval/runs/latest/report.json"),
        ContextPack(id="run", file=".apex-ray/evals/runs/latest/report.json"),
        ContextPack(id="root-env", file=".env"),
        ContextPack(id="root-local-env", file=".env.production.local"),
        ContextPack(id="root-production-env", file=".env.production"),
        ContextPack(id="mixed-env", file=".Env.production"),
        ContextPack(id="root-pem", file="client.pem"),
        ContextPack(id="root-key", file="client.key"),
        ContextPack(id="upper-pem", file="certs/client.PEM"),
        ContextPack(id="upper-key", file="certs/client.KEY"),
        ContextPack(id="mixed-pem", file="certs/client.Pem"),
        ContextPack(id="mixed-key", file="certs/client.Key"),
    ]

    assert all(match_rules_for_pack(pack, [rule]) == [] for pack in allowed)
    assert all([match.id for match in match_rules_for_pack(pack, [rule])] == [rule.id] for pack in private)
