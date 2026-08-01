import fnmatch


def path_matches_any(path: str, patterns: list[str]) -> bool:
    normalized = _normalize_path(path)
    for pattern in patterns:
        normalized_pattern = _normalize_path(pattern)
        if normalized == normalized_pattern:
            return True
        if any(fnmatch.fnmatchcase(normalized, variant) for variant in _glob_variants(normalized_pattern)):
            return True
    return False


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").removeprefix("./")


def _glob_variants(pattern: str) -> set[str]:
    variants = {pattern}
    pending = [pattern]
    while pending:
        candidate = pending.pop()
        if candidate.startswith("**/"):
            shortened = candidate[3:]
            if shortened not in variants:
                variants.add(shortened)
                pending.append(shortened)
        marker = "/**/"
        start = candidate.find(marker)
        if start >= 0:
            shortened = f"{candidate[:start]}/{candidate[start + len(marker) :]}"
            if shortened not in variants:
                variants.add(shortened)
                pending.append(shortened)
    return variants
