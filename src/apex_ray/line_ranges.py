from collections.abc import Iterable


def merge_line_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((min(start, end), max(start, end)) for start, end in ranges if start > 0 and end > 0)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end + 1:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def line_range_count(ranges: list[tuple[int, int]]) -> int:
    return sum(max(0, end - start + 1) for start, end in ranges)


def subtract_line_ranges(
    ranges: list[tuple[int, int]],
    covered_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    remaining: list[tuple[int, int]] = []
    for start, end in ranges:
        fragments = [(start, end)]
        for covered_start, covered_end in covered_ranges:
            next_fragments: list[tuple[int, int]] = []
            for fragment_start, fragment_end in fragments:
                if covered_end < fragment_start or covered_start > fragment_end:
                    next_fragments.append((fragment_start, fragment_end))
                    continue
                if fragment_start < covered_start:
                    next_fragments.append((fragment_start, covered_start - 1))
                if covered_end < fragment_end:
                    next_fragments.append((covered_end + 1, fragment_end))
            fragments = next_fragments
            if not fragments:
                break
        remaining.extend(fragments)
    return merge_line_ranges(remaining)
