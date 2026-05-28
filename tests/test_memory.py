from pathlib import Path

import pytest

from apex_ray.memory import (
    MemoryError,
    load_memory_cards,
    memory_cards_for_audience,
    pack_prompt_payload,
    select_memory_for_pack,
)
from apex_ray.models import (
    AnalyzerReference,
    AnalyzerSymbol,
    ContextPack,
    MemoryCard,
    MemoryConfig,
)


def make_pack() -> ContextPack:
    return ContextPack(
        id="src/cart.ts#calculateTotal:1",
        file="src/cart.ts",
        diff_snippet=["@@ -5,1 +5,1 @@", "-  return item.price;", "+  return item.price * item.quantity;"],
        symbol=AnalyzerSymbol(
            name="calculateTotal",
            kind="function",
            startLine=5,
            endLine=8,
            exported=True,
            signature="(items: CartItem[]): number",
        ),
        symbols=[
            AnalyzerSymbol(
                name="calculateTotal",
                kind="function",
                startLine=5,
                endLine=8,
                exported=True,
                signature="(items: CartItem[]): number",
            )
        ],
    )


def test_default_memory_path_missing_is_ignored(tmp_path: Path) -> None:
    assert load_memory_cards(tmp_path, [".apex-ray/memory"]) == []


def test_load_memory_cards_from_markdown(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".apex-ray" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "cart-total.md").write_text(
        "---\n"
        "id: cart-total\n"
        "title: Preserve cart totals\n"
        "kind: invariant\n"
        "severity: high\n"
        "paths:\n"
        "  - src/cart.ts\n"
        "triggers:\n"
        "  symbols:\n"
        "    - calculateTotal\n"
        "---\n"
        "Quantity multiplication is a product invariant.\n",
        encoding="utf-8",
    )

    cards = load_memory_cards(tmp_path, [".apex-ray/memory"])

    assert len(cards) == 1
    assert cards[0].id == "cart-total"
    assert cards[0].kind == "invariant"
    assert cards[0].source_path == ".apex-ray/memory/cart-total.md"
    assert "Quantity multiplication" in cards[0].body


def test_load_memory_cards_rejects_duplicate_ids(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".apex-ray" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "first.md").write_text("---\nid: duplicate\n---\nFirst\n", encoding="utf-8")
    (memory_dir / "second.md").write_text("---\nid: duplicate\n---\nSecond\n", encoding="utf-8")

    with pytest.raises(MemoryError) as exc:
        load_memory_cards(tmp_path, [".apex-ray/memory"])

    assert "Duplicate memory card id 'duplicate'" in str(exc.value)


def test_select_memory_prioritizes_relevance_before_severity() -> None:
    matches, omissions = select_memory_for_pack(
        make_pack(),
        [
            MemoryCard(
                id="global-critical",
                title="Global critical",
                kind="invariant",
                severity="critical",
                body="Useful only as broad background.",
            ),
            MemoryCard(
                id="cart-path",
                title="Cart path",
                kind="bug_pattern",
                severity="low",
                paths=["src/cart.ts"],
                body="Cart-specific historical issue.",
            ),
        ],
        MemoryConfig(max_cards_per_pack=1),
        base_chars=10000,
    )

    assert [match.id for match in matches] == ["cart-path"]
    assert [omission.id for omission in omissions] == ["global-critical"]
    assert omissions[0].reason == "memory card count budget exceeded"


def test_memory_prompt_payload_filters_by_audience() -> None:
    matches, _ = select_memory_for_pack(
        make_pack(),
        [
            MemoryCard(
                id="known-fp",
                title="Known false positive",
                kind="false_positive",
                triggers={"symbols": ["calculateTotal"]},
                body="Do not reject safe cart rounding guards.",
            ),
            MemoryCard(
                id="cart-invariant",
                title="Cart invariant",
                kind="invariant",
                triggers={"symbols": ["calculateTotal"]},
                body="Cart totals must include quantity.",
            ),
        ],
        MemoryConfig(),
        base_chars=10000,
    )
    pack = make_pack().model_copy(update={"memory_matches": matches})

    assert [match.id for match in memory_cards_for_audience(pack, "review")] == ["cart-invariant"]
    assert [match.id for match in memory_cards_for_audience(pack, "verify")] == ["cart-invariant", "known-fp"]
    assert "known-fp" not in str(pack_prompt_payload(pack, "review"))
    assert "known-fp" in str(pack_prompt_payload(pack, "verify"))


def test_memory_prompt_payload_uses_compact_symbol_metadata() -> None:
    reference = AnalyzerReference(
        file="src/checkout.ts",
        line=12,
        text="total: calculateTotal(items)",
        kind="call",
    )
    symbol = AnalyzerSymbol(
        name="calculateTotal",
        kind="function",
        startLine=5,
        endLine=8,
        exported=True,
        signature="(items: CartItem[]): number",
        references=[reference],
        callees=[reference],
        contracts=[reference],
        metadata=[reference],
    )
    pack = make_pack().model_copy(
        update={
            "symbol": symbol,
            "symbols": [symbol],
            "references": [reference],
        }
    )

    payload = pack_prompt_payload(pack, "review")

    assert payload["symbol"] == {
        "name": "calculateTotal",
        "kind": "function",
        "start_line": 5,
        "end_line": 8,
        "exported": True,
        "signature": "(items: CartItem[]): number",
    }
    assert payload["symbols"] == [payload["symbol"]]
    assert "references" not in payload["symbols"][0]
    assert payload["references"] == [
        {
            "file": "src/checkout.ts",
            "line": 12,
            "end_line": None,
            "text": "total: calculateTotal(items)",
            "kind": "call",
        }
    ]
