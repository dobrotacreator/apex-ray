from dataclasses import dataclass

from .constants import (
    DART_DIRECTIVE_CHAR_LIMIT,
    DART_DIRECTIVE_LIMIT,
    DART_DIRECTIVE_SOURCE_CHAR_LIMIT,
)


@dataclass(frozen=True, slots=True)
class DartDirective:
    kind: str
    target: str
    line: int
    end_line: int
    conditional_targets: tuple[str, ...] = ()


def parse_dart_directives(
    source: str,
    *,
    max_source_chars: int = DART_DIRECTIVE_SOURCE_CHAR_LIMIT,
    max_directives: int = DART_DIRECTIVE_LIMIT,
    max_directive_chars: int = DART_DIRECTIVE_CHAR_LIMIT,
) -> list[DartDirective]:
    """Scan top-level Dart URI and part directives with deterministic bounds.

    The scanner understands Dart comments and string forms but deliberately
    does not try to parse arbitrary Dart expressions.
    """

    if max_source_chars <= 0 or max_directives <= 0 or max_directive_chars <= 0:
        return []
    text = source[:max_source_chars]
    directives: list[DartDirective] = []
    index = 0
    while index < len(text) and len(directives) < max_directives:
        index = _skip_space_and_comments(text, index)
        if index >= len(text):
            break
        string_end = _skip_string(text, index)
        if string_end is not None:
            index = string_end
            continue
        identifier, end = _read_identifier(text, index)
        if identifier not in {"import", "export", "part"}:
            index = end if end > index else index + 1
            continue
        statement_end = _find_statement_end(text, index, max_directive_chars)
        if statement_end is None:
            index = end
            continue
        statement = text[index:statement_end]
        directive = _parse_statement(identifier, statement, text.count("\n", 0, index) + 1)
        if directive is not None:
            directives.append(directive)
        index = statement_end
    return directives


def _parse_statement(keyword: str, statement: str, line: int) -> DartDirective | None:
    end_line = line + statement.count("\n")
    cursor = len(keyword)
    if keyword == "part":
        cursor = _skip_space_and_comments(statement, cursor)
        identifier, after_identifier = _read_identifier(statement, cursor)
        if identifier == "of":
            cursor = _skip_space_and_comments(statement, after_identifier)
            strings = _string_literals(statement[cursor:])
            if strings:
                target = strings[0]
            else:
                target = _part_of_identifier(statement[cursor:])
            if target:
                return DartDirective("part-of", target, line, end_line)
            return None

    strings = _string_literals(statement[cursor:])
    if not strings:
        return None
    conditional = tuple(strings[1:]) if keyword == "import" else ()
    return DartDirective(keyword, strings[0], line, end_line, conditional)


def _find_statement_end(text: str, start: int, limit: int) -> int | None:
    index = start
    ceiling = min(len(text), start + limit)
    while index < ceiling:
        if text.startswith("//", index):
            newline = text.find("\n", index + 2, ceiling)
            index = ceiling if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            index = _skip_block_comment(text, index, ceiling)
            continue
        string_end = _skip_string(text, index, ceiling)
        if string_end is not None:
            index = string_end
            continue
        if text[index] == ";":
            return index + 1
        index += 1
    return None


def _string_literals(text: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            index = _skip_block_comment(text, index, len(text))
            continue
        parsed = _parse_string(text, index)
        if parsed is not None:
            value, index = parsed
            values.append(value)
            continue
        index += 1
    return values


def _parse_string(text: str, start: int) -> tuple[str, int] | None:
    index = start
    raw = False
    if index < len(text) and text[index] in "rR" and index + 1 < len(text) and text[index + 1] in "'\"":
        raw = True
        index += 1
    if index >= len(text) or text[index] not in "'\"":
        return None
    quote = text[index]
    triple = text.startswith(quote * 3, index)
    delimiter = quote * (3 if triple else 1)
    body_start = index + len(delimiter)
    cursor = body_start
    while cursor < len(text):
        if text.startswith(delimiter, cursor):
            body = text[body_start:cursor]
            return (body if raw else _decode_escapes(body), cursor + len(delimiter))
        if not raw and text[cursor] == "\\":
            cursor += 2
        else:
            cursor += 1
    return None


def _skip_string(text: str, start: int, ceiling: int | None = None) -> int | None:
    parsed = _parse_string(text[:ceiling] if ceiling is not None else text, start)
    return parsed[1] if parsed is not None else None


def _decode_escapes(body: str) -> str:
    result: list[str] = []
    index = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
    while index < len(body):
        if body[index] != "\\" or index + 1 >= len(body):
            result.append(body[index])
            index += 1
            continue
        marker = body[index + 1]
        if marker == "u":
            decoded, consumed = _decode_unicode_escape(body, index + 2)
            if decoded is not None:
                result.append(decoded)
                index = consumed
                continue
        result.append(escapes.get(marker, marker))
        index += 2
    return "".join(result)


def _decode_unicode_escape(body: str, start: int) -> tuple[str | None, int]:
    if start < len(body) and body[start] == "{":
        close = body.find("}", start + 1, min(len(body), start + 8))
        digits = body[start + 1 : close] if close >= 0 else ""
        consumed = close + 1
    else:
        digits = body[start : start + 4]
        consumed = start + 4
    if not digits or any(character not in "0123456789abcdefABCDEF" for character in digits):
        return None, start
    try:
        return chr(int(digits, 16)), consumed
    except ValueError:
        return None, start


def _part_of_identifier(text: str) -> str:
    before_semicolon = text.split(";", maxsplit=1)[0].strip()
    compact = "".join(before_semicolon.split())
    if not compact:
        return ""
    pieces = compact.split(".")
    if all(
        piece and (piece[0].isalpha() or piece[0] == "_") and all(c.isalnum() or c == "_" for c in piece)
        for piece in pieces
    ):
        return compact
    return ""


def _skip_space_and_comments(text: str, start: int) -> int:
    index = start
    while index < len(text):
        if text[index].isspace():
            index += 1
        elif text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
        elif text.startswith("/*", index):
            index = _skip_block_comment(text, index, len(text))
        else:
            break
    return index


def _skip_block_comment(text: str, start: int, ceiling: int) -> int:
    depth = 1
    index = start + 2
    while index < ceiling and depth:
        if text.startswith("/*", index):
            depth += 1
            index += 2
        elif text.startswith("*/", index):
            depth -= 1
            index += 2
        else:
            index += 1
    return index


def _read_identifier(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or not (text[start].isalpha() or text[start] in "_$"):
        return "", start
    end = start + 1
    while end < len(text) and (text[end].isalnum() or text[end] in "_$"):
        end += 1
    return text[start:end], end
