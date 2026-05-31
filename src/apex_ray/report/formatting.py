from collections import Counter


def summarize_notes(notes: list[str], limit: int = 8) -> list[str]:
    if not notes:
        return []
    counts = Counter(notes)
    ordered = []
    seen = set()
    for note in notes:
        if note in seen:
            continue
        seen.add(note)
        count = counts[note]
        ordered.append(f"{note} (x{count})" if count > 1 else note)
    if len(ordered) <= limit:
        return ordered
    hidden = len(ordered) - limit
    return [*ordered[:limit], f"... {hidden} more note types"]


def format_list(values: list[str]) -> str:
    if not values:
        return "`none`"
    return ", ".join(f"`{value}`" for value in values)
