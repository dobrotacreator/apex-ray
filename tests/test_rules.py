from apex_ray.models import AnalyzerReference, CodeSnippet, ContextPack, ReviewRule
from apex_ray.rules import match_rules_for_pack


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
