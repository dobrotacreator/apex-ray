from pathlib import Path

import pytest

from apex_ray.analyzers.dart import mapping as dart_mapping_module
from apex_ray.analyzers.dart.mapping import (
    DartDocumentSymbol,
    DartReferenceSourceReader,
    analyzer_reference_from_lsp_location,
    analyzer_references_from_lsp_locations,
    changed_document_symbols,
    collect_analyzer_references_from_lsp_locations,
    collect_document_symbols,
    flatten_document_symbols,
)


def _range(start_line: int, end_line: int, *, end_character: int = 1) -> dict[str, object]:
    return {
        "start": {"line": start_line, "character": 0},
        "end": {"line": end_line, "character": end_character},
    }


def test_flatten_document_symbols_preserves_nested_ranges_and_privacy() -> None:
    payload = [
        {
            "name": "ResourceWidget",
            "detail": "class ResourceWidget extends StatefulWidget",
            "kind": 5,
            "range": _range(0, 12, end_character=0),
            "selectionRange": _range(0, 0, end_character=14),
            "children": [
                {
                    "name": "build",
                    "detail": "Widget build(BuildContext context)",
                    "kind": 6,
                    "range": _range(4, 9, end_character=0),
                    "selectionRange": _range(4, 4, end_character=5),
                },
                {
                    "name": "_reset",
                    "kind": 6,
                    "range": _range(10, 10),
                    "selectionRange": _range(10, 10),
                },
            ],
        }
    ]

    symbols = flatten_document_symbols(payload, uri="file:///repo/lib/resource.dart")

    assert [(item.symbol.name, item.symbol.kind) for item in symbols] == [
        ("ResourceWidget", "class"),
        ("ResourceWidget.build", "method"),
        ("ResourceWidget._reset", "method"),
    ]
    assert symbols[0].symbol.start_line == 1
    assert symbols[0].symbol.end_line == 12
    assert symbols[1].symbol.start_line == 5
    assert symbols[1].symbol.end_line == 9
    assert symbols[0].symbol.signature == "class ResourceWidget extends StatefulWidget"
    assert symbols[0].symbol.exported is True
    assert symbols[2].symbol.exported is False
    assert symbols[1].position == {"line": 4, "character": 0}


def test_flatten_document_symbols_accepts_symbol_information() -> None:
    payload = [
        {
            "name": "loadResource",
            "kind": 12,
            "location": {
                "uri": "file:///repo/lib/resource.dart",
                "range": _range(7, 9),
            },
            "containerName": "ResourceService",
        }
    ]

    symbols = flatten_document_symbols(payload, uri="file:///repo/lib/resource.dart")

    assert symbols == [
        DartDocumentSymbol(
            symbol=symbols[0].symbol,
            uri="file:///repo/lib/resource.dart",
            position={"line": 7, "character": 0},
        )
    ]
    assert symbols[0].symbol.name == "ResourceService.loadResource"
    assert symbols[0].symbol.kind == "function"


def test_collect_document_symbols_stops_before_materializing_the_full_payload() -> None:
    payload = [
        {
            "name": f"symbol{index}",
            "kind": 12,
            "range": _range(index, index),
            "selectionRange": _range(index, index),
        }
        for index in range(100)
    ]

    collected = collect_document_symbols(payload, uri="file:///repo/lib/resource.dart", limit=3)

    assert [item.symbol.name for item in collected.symbols] == ["symbol0", "symbol1", "symbol2"]
    assert collected.truncated is True


@pytest.mark.parametrize(
    ("limit", "expected_count", "expected_truncated"),
    [(32, 32, True), (2_000, 2_000, False)],
)
def test_collect_document_symbols_iteratively_handles_deep_nesting(
    limit: int,
    expected_count: int,
    expected_truncated: bool,
) -> None:
    node: dict[str, object] = {
        "name": "symbol1999",
        "kind": 6,
        "range": _range(1_999, 1_999),
        "selectionRange": _range(1_999, 1_999),
    }
    for index in reversed(range(1_999)):
        node = {
            "name": f"symbol{index}",
            "kind": 6,
            "range": _range(index, index),
            "selectionRange": _range(index, index),
            "children": [node],
        }

    collected = collect_document_symbols([node], uri="file:///repo/lib/deep.dart", limit=limit)

    assert len(collected.symbols) == expected_count
    assert collected.symbols[0].symbol.name == "symbol0"
    assert collected.symbols[-1].symbol.name.endswith(f"symbol{expected_count - 1}")
    assert max(len(item.symbol.name) for item in collected.symbols) <= 1_000
    assert sum(len(item.symbol.name) for item in collected.symbols) <= expected_count * 1_000
    assert collected.truncated is expected_truncated


def test_document_symbol_strings_are_bounded_and_preserve_the_leaf_name() -> None:
    leaf = "leafMethod"
    payload = [
        {
            "name": leaf,
            "detail": "x" * 20_000,
            "kind": 6,
            "location": {
                "uri": "file:///repo/lib/resource.dart",
                "range": _range(1, 1),
            },
            "containerName": "Container" * 2_000,
        }
    ]

    symbol = flatten_document_symbols(payload, uri="file:///repo/lib/resource.dart")[0].symbol

    assert len(symbol.name) <= 1_000
    assert symbol.name.endswith(f".{leaf}")
    assert len(symbol.signature) <= 1_000


def test_changed_document_symbols_uses_added_and_deleted_only_anchor_ranges() -> None:
    symbols = flatten_document_symbols(
        [
            {
                "name": "first",
                "kind": 12,
                "range": _range(1, 3),
                "selectionRange": _range(1, 1),
            },
            {
                "name": "second",
                "kind": 12,
                "range": _range(8, 12),
                "selectionRange": _range(8, 8),
            },
        ],
        uri="file:///repo/lib/resource.dart",
    )

    assert [item.symbol.name for item in changed_document_symbols(symbols, [(10, 10)])] == ["second"]


def test_analyzer_reference_from_lsp_location_rejects_external_and_reads_repo_line(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lib" / "référence.dart"
    source.parent.mkdir()
    source.write_text("void first() {}\nvoid consume() => first();\n", encoding="utf-8")
    location = {
        "uri": source.as_uri(),
        "range": _range(1, 1, end_character=7),
    }

    reference = analyzer_reference_from_lsp_location(tmp_path, location, kind="call")

    assert reference is not None
    assert reference.file == "lib/référence.dart"
    assert reference.line == 2
    assert reference.end_line == 2
    assert reference.text == "void consume() => first();"
    assert (
        analyzer_reference_from_lsp_location(
            tmp_path,
            {"uri": (tmp_path.parent / "outside.dart").as_uri(), "range": _range(0, 0)},
            kind="call",
        )
        is None
    )


def test_analyzer_reference_decodes_file_uri_exactly_once(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "percent%20literal.dart"
    misleading = tmp_path / "lib" / "percent literal.dart"
    source.parent.mkdir()
    source.write_text("void intended() {}\n", encoding="utf-8")
    misleading.write_text("void wrong() {}\n", encoding="utf-8")

    reference = analyzer_reference_from_lsp_location(
        tmp_path,
        {"uri": source.as_uri(), "range": _range(0, 0)},
        kind="call",
    )

    assert reference is not None
    assert reference.file == "lib/percent%20literal.dart"
    assert reference.text == "void intended() {}"


def test_analyzer_reference_from_location_link_uses_target_range(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "contract.dart"
    source.parent.mkdir()
    source.write_text("abstract interface class Contract {}\n", encoding="utf-8")

    reference = analyzer_reference_from_lsp_location(
        tmp_path,
        {
            "targetUri": source.as_uri(),
            "targetRange": _range(0, 0, end_character=33),
        },
        kind="contract",
    )

    assert reference is not None
    assert reference.file == "lib/contract.dart"
    assert reference.kind == "contract"


def test_reference_collection_is_bounded_deduplicated_and_filters_generated(tmp_path: Path) -> None:
    source = tmp_path / "test" / "resource_test.dart"
    generated = tmp_path / "lib" / "resource.g.dart"
    source.parent.mkdir()
    generated.parent.mkdir()
    source.write_text("void testResource() {}\n", encoding="utf-8")
    generated.write_text("void generatedConsumer() {}\n", encoding="utf-8")
    source_location = {"uri": source.as_uri(), "range": _range(0, 0)}

    references = analyzer_references_from_lsp_locations(
        tmp_path,
        [
            source_location,
            source_location,
            {"uri": generated.as_uri(), "range": _range(0, 0)},
            {"uri": "https://example.invalid/source.dart", "range": _range(0, 0)},
        ],
        kind="call",
        limit=1,
        exclude=lambda path: path.endswith(".g.dart"),
    )

    assert [(reference.file, reference.line) for reference in references] == [("test/resource_test.dart", 1)]

    collected = collect_analyzer_references_from_lsp_locations(
        tmp_path,
        [source_location, source_location, {"uri": generated.as_uri(), "range": _range(0, 0)}],
        kind="call",
        limit=1,
        exclude=lambda path: path.endswith(".g.dart"),
    )
    assert collected.references == references
    assert collected.excluded_count == 1


def test_reference_collection_reads_only_retained_locations_after_deterministic_sort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("".join(f"reference at line {line}\n" for line in range(1, 5_001)), encoding="utf-8")
    bounded_reads: list[Path] = []
    original_read = dart_mapping_module._read_bounded_repo_text

    def counted_read(root: Path, path: Path, *, max_bytes: int, deadline: float | None) -> str | None:
        bounded_reads.append(path)
        return original_read(root, path, max_bytes=max_bytes, deadline=deadline)

    monkeypatch.setattr(dart_mapping_module, "_read_bounded_repo_text", counted_read)
    payload = [{"uri": source.as_uri(), "range": _range(line, line)} for line in reversed(range(4_976, 5_000))]
    reader = DartReferenceSourceReader(tmp_path)

    collected = collect_analyzer_references_from_lsp_locations(
        tmp_path,
        payload,
        kind="read",
        limit=24,
        reader=reader,
    )

    assert [reference.line for reference in collected.references] == list(range(4_977, 5_001))
    assert collected.references[-1].text == "reference at line 5000"
    assert bounded_reads == [source]
    assert reader.files_read == 1
    assert collected.partial is False


def test_reference_source_reader_uses_sparse_line_index_for_newline_heavy_source(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "newline_heavy.dart"
    source.parent.mkdir()
    source.write_text("\n" * 100_000 + "void tail() {}\n", encoding="utf-8")
    reader = DartReferenceSourceReader(tmp_path)

    collected = collect_analyzer_references_from_lsp_locations(
        tmp_path,
        [{"uri": source.as_uri(), "range": _range(100_000, 100_000)}],
        kind="read",
        limit=1,
        reader=reader,
    )

    assert [reference.text for reference in collected.references] == ["void tail() {}"]
    assert reader.line_index_entries <= 1 + 100_001 // dart_mapping_module.DART_REFERENCE_LINE_CHECKPOINT_INTERVAL
    assert reader.line_index_entries < 1_000


def test_reference_source_reader_rejects_oversized_and_symlinked_files(tmp_path: Path) -> None:
    oversized = tmp_path / "lib" / "oversized.dart"
    target = tmp_path / "lib" / "target.dart"
    symlink = tmp_path / "lib" / "linked.dart"
    oversized.parent.mkdir()
    oversized.write_text("x" * 65, encoding="utf-8")
    target.write_text("void target() {}\n", encoding="utf-8")
    try:
        symlink.symlink_to(target)
    except OSError as exc:  # pragma: no cover - platform permission fallback
        pytest.skip(f"symlinks are unavailable: {exc}")
    reader = DartReferenceSourceReader(tmp_path, max_file_bytes=64, max_total_source_bytes=128)

    oversized_collection = collect_analyzer_references_from_lsp_locations(
        tmp_path,
        [{"uri": oversized.as_uri(), "range": _range(0, 0)}],
        kind="read",
        limit=1,
        reader=reader,
    )
    symlink_collection = collect_analyzer_references_from_lsp_locations(
        tmp_path,
        [{"uri": symlink.as_uri(), "range": _range(0, 0)}],
        kind="read",
        limit=1,
        reader=reader,
    )

    assert [reference.text for reference in oversized_collection.references] == [""]
    assert oversized_collection.partial is True
    assert [reference.text for reference in symlink_collection.references] == [""]
    assert symlink_collection.partial is True
    assert reader.files_read == 0
    assert reader.skipped_files == 2
    assert reader.source_bytes_read == 0


def test_reference_source_reader_enforces_aggregate_byte_cap(tmp_path: Path) -> None:
    first = tmp_path / "lib" / "a.dart"
    second = tmp_path / "lib" / "b.dart"
    first.parent.mkdir()
    first.write_text("void first() {}\n", encoding="utf-8")
    second.write_text("void second() {}\n", encoding="utf-8")
    reader = DartReferenceSourceReader(tmp_path, max_file_bytes=64, max_total_source_bytes=24)

    collected = collect_analyzer_references_from_lsp_locations(
        tmp_path,
        [
            {"uri": second.as_uri(), "range": _range(0, 0)},
            {"uri": first.as_uri(), "range": _range(0, 0)},
        ],
        kind="read",
        limit=2,
        reader=reader,
    )

    assert [(reference.file, reference.text) for reference in collected.references] == [
        ("lib/a.dart", "void first() {}"),
        ("lib/b.dart", ""),
    ]
    assert collected.partial is True
    assert reader.files_read == 1
    assert reader.skipped_files == 1
    assert reader.source_bytes_read == len(b"void first() {}\n")


def test_reference_collection_stops_parsing_when_run_deadline_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lib" / "resource.dart"
    source.parent.mkdir()
    source.write_text("void resource() {}\n", encoding="utf-8")
    clock = [0.0]
    candidate_calls = 0
    original_candidate = dart_mapping_module._reference_candidate_from_lsp_location

    def timed_candidate(root: Path, payload: object, *, kind: str):
        nonlocal candidate_calls
        candidate_calls += 1
        candidate = original_candidate(root, payload, kind=kind)
        clock[0] = 2.0
        return candidate

    monkeypatch.setattr(dart_mapping_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dart_mapping_module, "_reference_candidate_from_lsp_location", timed_candidate)
    reader = DartReferenceSourceReader(tmp_path, deadline=1.0)

    collected = collect_analyzer_references_from_lsp_locations(
        tmp_path,
        [
            {"uri": source.as_uri(), "range": _range(0, 0)},
            {"uri": source.as_uri(), "range": _range(1, 1)},
        ],
        kind="read",
        limit=2,
        reader=reader,
    )

    assert candidate_calls == 1
    assert collected.references == []
    assert collected.partial is True
    assert reader.files_read == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_reference_collection_output_and_generated_count_are_deterministic(
    tmp_path: Path,
    reverse: bool,
) -> None:
    first = tmp_path / "lib" / "a.dart"
    second = tmp_path / "lib" / "b.dart"
    generated = tmp_path / "lib" / "resource.g.dart"
    first.parent.mkdir()
    first.write_text("void first() {}\n", encoding="utf-8")
    second.write_text("void second() {}\n", encoding="utf-8")
    generated.write_text("void generated() {}\n", encoding="utf-8")
    payload = [
        {"uri": generated.as_uri(), "range": _range(0, 0)},
        {"uri": second.as_uri(), "range": _range(0, 0)},
        {"uri": first.as_uri(), "range": _range(0, 0)},
        {"uri": generated.as_uri(), "range": _range(0, 0)},
    ]
    if reverse:
        payload.reverse()

    collected = collect_analyzer_references_from_lsp_locations(
        tmp_path,
        payload,
        kind="read",
        limit=2,
        exclude=lambda path: path.endswith(".g.dart"),
        reader=DartReferenceSourceReader(tmp_path),
    )

    assert [(reference.file, reference.text) for reference in collected.references] == [
        ("lib/a.dart", "void first() {}"),
        ("lib/b.dart", "void second() {}"),
    ]
    assert collected.excluded_count == 2
    assert collected.partial is False
