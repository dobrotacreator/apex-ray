import re
from functools import lru_cache
from pathlib import Path

from apex_ray.models import AnalyzerReference, AnalyzerSymbol, ChangedFile, CodeSnippet, ContextConfig

TEST_ANCHOR_MIN_LENGTH = 4
TEST_ANCHOR_LIMIT = 32


def test_anchor_terms(changed_file: ChangedFile, symbols: list[AnalyzerSymbol]) -> list[str]:
    terms: list[str] = []
    for hunk in changed_file.hunks:
        for line in hunk.lines:
            if line.kind not in {"add", "delete"}:
                continue
            terms.extend(_quoted_terms(line.content))
    for symbol in symbols:
        terms.append(symbol.name)
        if ":" in symbol.name:
            terms.append(symbol.name.rsplit(":", 1)[-1])

    seen: set[str] = set()
    unique_terms: list[str] = []
    for term in terms:
        normalized = term.strip()
        if len(normalized) < TEST_ANCHOR_MIN_LENGTH or normalized in seen:
            continue
        seen.add(normalized)
        unique_terms.append(normalized)
        if len(unique_terms) >= TEST_ANCHOR_LIMIT:
            break
    return unique_terms


def changed_symbol_snippets(
    repo_root: Path | None,
    file_path: str,
    symbols: list[AnalyzerSymbol],
    config: ContextConfig,
) -> list[CodeSnippet]:
    if repo_root is None:
        return []

    snippets: list[CodeSnippet] = []
    seen_ranges: set[tuple[str, int, int]] = set()
    sorted_symbols = sorted(symbols, key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name))
    for symbol in sorted_symbols:
        snippet = snippet_for_line_range(
            repo_root,
            file_path,
            symbol.start_line,
            symbol.end_line,
            max_lines=config.max_changed_snippet_lines,
        )
        if not snippet:
            continue
        range_key = (snippet.file, snippet.start_line, snippet.end_line)
        if range_key in seen_ranges:
            continue
        seen_ranges.add(range_key)
        snippets.append(snippet)
        if len(snippets) >= config.max_changed_snippets:
            break
    return snippets


def changed_hunk_snippets(
    repo_root: Path | None,
    changed_file: ChangedFile,
    config: ContextConfig,
) -> list[CodeSnippet]:
    if repo_root is None:
        return []

    snippets: list[CodeSnippet] = []
    seen_ranges: set[tuple[str, int, int]] = set()
    for hunk in changed_file.hunks:
        start_line = max(1, hunk.new_start - config.hunk_context_lines)
        hunk_end = hunk.new_start + max(hunk.new_lines - 1, 0)
        end_line = hunk_end + config.hunk_context_lines
        snippet = snippet_for_line_range(
            repo_root,
            changed_file.path,
            start_line,
            end_line,
            max_lines=config.max_changed_snippet_lines,
        )
        if not snippet:
            continue
        range_key = (snippet.file, snippet.start_line, snippet.end_line)
        if range_key in seen_ranges:
            continue
        seen_ranges.add(range_key)
        snippets.append(snippet)
        if len(snippets) >= config.max_hunk_snippets:
            break
    return snippets


def reference_snippets(
    repo_root: Path | None,
    references: list[AnalyzerReference],
    config: ContextConfig,
    excluded_files: set[str] | None = None,
) -> list[CodeSnippet]:
    if repo_root is None:
        return []
    excluded_files = excluded_files or set()
    snippets: list[CodeSnippet] = []
    seen: set[tuple[str, int]] = set()
    seen_ranges: set[tuple[str, int, int]] = set()
    for reference in references:
        if reference.file in excluded_files or is_import_reference(reference):
            continue
        key = (reference.file, reference.line)
        if key in seen:
            continue
        seen.add(key)
        start_line = leading_comment_start(repo_root, reference.file, reference.line)
        if reference.end_line and reference.end_line > reference.line:
            snippet = snippet_for_line_range(
                repo_root,
                reference.file,
                start_line,
                reference.end_line,
                max_lines=config.max_changed_snippet_lines,
            )
        elif start_line < reference.line:
            snippet = snippet_for_line_range(
                repo_root,
                reference.file,
                start_line,
                reference.line,
                max_lines=config.max_changed_snippet_lines,
            )
        else:
            snippet = snippet_around_line(
                repo_root, reference.file, reference.line, config.reference_snippet_context_lines
            )
        if snippet:
            range_key = (snippet.file, snippet.start_line, snippet.end_line)
            if range_key in seen_ranges:
                continue
            seen_ranges.add(range_key)
            snippets.append(snippet)
        if len(snippets) >= config.max_reference_snippets:
            break
    return snippets


def callee_snippets(
    repo_root: Path | None,
    callees: list[AnalyzerReference],
    config: ContextConfig,
) -> list[CodeSnippet]:
    return reference_snippets(repo_root, callees, config)


def contract_snippets(
    repo_root: Path | None,
    contracts: list[AnalyzerReference],
    config: ContextConfig,
) -> list[CodeSnippet]:
    if repo_root is None:
        return []
    snippets: list[CodeSnippet] = []
    seen: set[tuple[str, int]] = set()
    seen_ranges: set[tuple[str, int, int]] = set()
    for contract in contracts:
        key = (contract.file, contract.line)
        if key in seen:
            continue
        seen.add(key)
        if contract.end_line and contract.end_line > contract.line:
            snippet = snippet_for_line_range(
                repo_root,
                contract.file,
                contract.line,
                contract.end_line,
                max_lines=config.max_changed_snippet_lines,
            )
        else:
            snippet = snippet_around_line(
                repo_root, contract.file, contract.line, config.reference_snippet_context_lines
            )
        if snippet:
            range_key = (snippet.file, snippet.start_line, snippet.end_line)
            if range_key in seen_ranges:
                continue
            seen_ranges.add(range_key)
            snippets.append(snippet)
        if len(snippets) >= config.max_reference_snippets:
            break
    return snippets


def metadata_snippets(
    repo_root: Path | None,
    metadata: list[AnalyzerReference],
    config: ContextConfig,
) -> list[CodeSnippet]:
    return reference_snippets(repo_root, metadata, config)


def is_import_reference(reference: AnalyzerReference) -> bool:
    return reference.kind == "import" or reference.text.lstrip().startswith("import ")


def test_snippets(
    repo_root: Path | None,
    test_paths: list[str],
    config: ContextConfig,
    anchors: list[str] | None = None,
) -> list[CodeSnippet]:
    if repo_root is None:
        return []
    snippets: list[CodeSnippet] = []
    for test_path in test_paths[: config.max_related_test_snippets]:
        snippet = anchored_test_snippet(repo_root, test_path, anchors or [], config.max_related_test_snippet_lines)
        if snippet is None:
            snippet = snippet_for_file_start(repo_root, test_path, config.max_related_test_snippet_lines)
        if snippet:
            snippets.append(snippet)
    return snippets


def anchored_test_snippet(
    repo_root: Path,
    rel_path: str,
    anchors: list[str],
    max_lines: int,
) -> CodeSnippet | None:
    if not anchors:
        return None
    lines = read_lines(repo_root, rel_path)
    if not lines:
        return None
    for index, line in enumerate(lines, start=1):
        for anchor in anchors:
            if anchor in line:
                return snippet_for_line_window(repo_root, rel_path, index, max_lines)
    return None


def leading_comment_start(repo_root: Path, rel_path: str, line: int) -> int:
    lines = read_lines(repo_root, rel_path)
    if not lines or line <= 1:
        return line
    index = line - 2
    stripped = lines[index].strip()
    if stripped.endswith("*/"):
        while index >= 0:
            current = lines[index].strip()
            if current.startswith("/*"):
                return index + 1
            index -= 1
    if stripped.startswith("//"):
        while index >= 0 and lines[index].strip().startswith("//"):
            index -= 1
        return index + 2
    return line


def snippet_around_line(repo_root: Path, rel_path: str, line: int, context_lines: int) -> CodeSnippet | None:
    lines = read_lines(repo_root, rel_path)
    if not lines or line < 1:
        return None
    start_line = max(1, line - context_lines)
    end_line = min(len(lines), line + context_lines)
    return CodeSnippet(
        file=rel_path,
        start_line=start_line,
        end_line=end_line,
        code="".join(lines[start_line - 1 : end_line]),
    )


def snippet_for_line_range(
    repo_root: Path,
    rel_path: str,
    start_line: int,
    end_line: int,
    max_lines: int,
) -> CodeSnippet | None:
    lines = read_lines(repo_root, rel_path)
    if not lines or start_line < 1 or end_line < start_line:
        return None

    bounded_start = min(start_line, len(lines))
    bounded_end = min(end_line, len(lines), bounded_start + max_lines - 1)
    return CodeSnippet(
        file=rel_path,
        start_line=bounded_start,
        end_line=bounded_end,
        code="".join(lines[bounded_start - 1 : bounded_end]),
    )


def snippet_for_line_window(repo_root: Path, rel_path: str, line: int, max_lines: int) -> CodeSnippet | None:
    lines = read_lines(repo_root, rel_path)
    if not lines or line < 1:
        return None
    before = max(0, (max_lines - 1) // 2)
    start_line = max(1, line - before)
    end_line = min(len(lines), start_line + max_lines - 1)
    return CodeSnippet(
        file=rel_path,
        start_line=start_line,
        end_line=end_line,
        code="".join(lines[start_line - 1 : end_line]),
    )


def snippet_for_file_start(repo_root: Path, rel_path: str, max_lines: int) -> CodeSnippet | None:
    lines = read_lines(repo_root, rel_path)
    if not lines:
        return None
    end_line = min(len(lines), max_lines)
    return CodeSnippet(
        file=rel_path,
        start_line=1,
        end_line=end_line,
        code="".join(lines[:end_line]),
    )


def read_lines(repo_root: Path, rel_path: str) -> list[str]:
    path = resolve_repo_path(repo_root, rel_path)
    if path is None:
        return []
    try:
        stat = path.stat()
    except OSError:
        return []
    if not path.is_file():
        return []
    return list(_read_lines_cached(str(path), stat.st_mtime_ns, stat.st_size))


def resolve_repo_path(repo_root: Path, rel_path: str) -> Path | None:
    candidate = Path(rel_path)
    if candidate.is_absolute():
        return None

    resolved_root = repo_root.resolve()
    resolved_path = (resolved_root / candidate).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved_path


def _quoted_terms(text: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r"""["'`]([^"'`]{4,160})["'`]""", text)]


@lru_cache(maxsize=4096)
def _read_lines_cached(path: str, mtime_ns: int, size: int) -> tuple[str, ...]:
    try:
        return tuple(Path(path).read_text(encoding="utf-8").splitlines(keepends=True))
    except OSError:
        return ()
    except UnicodeDecodeError:
        return ()
