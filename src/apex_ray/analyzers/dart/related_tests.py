import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from .constants import (
    DART_RELATED_TEST_FILE_LIMIT,
    DART_RELATED_TEST_FILE_SIZE_LIMIT,
    DART_SCAN_IGNORED_DIRS,
)
from .directives import DartDirective, parse_dart_directives
from .generated import is_generated_dart_path

_PACKAGE_NAME_RE = re.compile(r"(?m)^name:\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:#.*)?$")
_IDENTIFIER_RUN_RE = re.compile(r"[\w$]+")
_FRAMEWORK_TEST_MARKERS = ("testWidgets(", "goldenTest(", "blocTest<", "IntegrationTestWidgetsFlutterBinding")
DART_RELATED_TEST_TOTAL_SOURCE_BYTE_LIMIT = 32 * 1024 * 1024
DART_RELATED_TEST_IDENTIFIER_MEMORY_LIMIT = 32 * 1024 * 1024
_IDENTIFIER_ENTRY_ESTIMATED_OVERHEAD = 96


@dataclass(frozen=True, slots=True)
class DartRelatedTestCandidate:
    path: str
    directives: tuple[DartDirective, ...] = ()
    symbol_evidence: frozenset[str] | None = None
    has_framework_registration: bool = False


@dataclass(frozen=True, slots=True)
class DartRelatedTestIndex:
    repo_root: Path
    package_roots: tuple[tuple[Path, str], ...] = ()
    tests: tuple[DartRelatedTestCandidate, ...] = ()
    source_bytes_read: int = 0
    identifier_memory_bytes: int = 0
    files_read: int = 0
    skipped_text_files: int = 0
    truncated: bool = False
    deadline: float | None = None


@dataclass(slots=True)
class _BoundedTextReader:
    max_file_bytes: int
    max_total_source_bytes: int
    deadline: float | None = None
    source_bytes_read: int = 0
    files_read: int = 0
    skipped_text_files: int = 0
    truncated: bool = False

    def read(self, path: Path) -> str | None:
        if self._deadline_expired():
            return None
        try:
            size = path.stat().st_size
        except OSError:
            self.skipped_text_files += 1
            return None
        if self._deadline_expired():
            return None
        if size > self.max_file_bytes:
            self.skipped_text_files += 1
            return None
        remaining = self.max_total_source_bytes - self.source_bytes_read
        if size > remaining:
            self.skipped_text_files += 1
            self.truncated = True
            return None
        if self._deadline_expired():
            return None
        text = _read_bounded_text(path, min(self.max_file_bytes, remaining))
        if text is None:
            self.skipped_text_files += 1
            return None
        encoded_size = len(text.encode("utf-8"))
        if encoded_size > remaining:
            self.skipped_text_files += 1
            self.truncated = True
            return None
        self.source_bytes_read += size
        self.files_read += 1
        self._deadline_expired()
        return text

    def _deadline_expired(self) -> bool:
        if not _deadline_expired(self.deadline):
            return False
        self.truncated = True
        return True


def build_dart_related_test_index(
    repo_root: Path,
    *,
    candidate_paths: Iterable[str] | None = None,
    max_files: int = DART_RELATED_TEST_FILE_LIMIT,
    max_file_bytes: int = DART_RELATED_TEST_FILE_SIZE_LIMIT,
    max_total_source_bytes: int = DART_RELATED_TEST_TOTAL_SOURCE_BYTE_LIMIT,
    max_identifier_memory_bytes: int = DART_RELATED_TEST_IDENTIFIER_MEMORY_LIMIT,
    deadline: float | None = None,
) -> DartRelatedTestIndex:
    """Build immutable package/test evidence once for a Dart analyzer run.

    ``deadline`` is an absolute ``time.monotonic()`` value. Expiry returns the
    bounded evidence collected so far and marks the index as truncated.
    """

    root = repo_root.resolve()
    if (
        max_files <= 0
        or max_file_bytes <= 0
        or max_total_source_bytes <= 0
        or max_identifier_memory_bytes < 0
        or _deadline_expired(deadline)
    ):
        return DartRelatedTestIndex(repo_root=root, truncated=True, deadline=deadline)

    inventory: tuple[str, ...] | None = None
    inventory_truncated = False
    if candidate_paths is not None:
        inventory, inventory_truncated = _collect_inventory(candidate_paths, deadline=deadline)
    if inventory is None:
        manifest_paths, manifests_truncated = _discover_manifest_paths(
            root,
            max_files=max_files,
            deadline=deadline,
        )
        test_paths, tests_truncated = _discover_test_paths(
            root,
            max_files=max_files,
            deadline=deadline,
        )
    else:
        manifest_paths, manifests_truncated = _inventory_manifest_paths(
            root,
            inventory,
            max_files=max_files,
            deadline=deadline,
        )
        test_paths, tests_truncated = _inventory_test_paths(
            root,
            inventory,
            max_files=max_files,
            deadline=deadline,
        )

    reader = _BoundedTextReader(
        max_file_bytes=max_file_bytes,
        max_total_source_bytes=max_total_source_bytes,
        deadline=deadline,
    )
    deadline_truncated = inventory_truncated
    package_roots: list[tuple[Path, str]] = []
    for relative in manifest_paths:
        if _deadline_expired(deadline):
            deadline_truncated = True
            break
        manifest = _repo_file(root, relative)
        if manifest is None:
            continue
        text = reader.read(manifest)
        if _deadline_expired(deadline):
            deadline_truncated = True
            break
        match = _PACKAGE_NAME_RE.search(text or "")
        if match:
            package_roots.append((manifest.parent.resolve(), match.group(1)))
        if _deadline_expired(deadline):
            deadline_truncated = True
            break
    package_roots.sort(key=lambda item: (-len(item[0].parts), item[0].as_posix()))

    tests: list[DartRelatedTestCandidate] = []
    identifier_memory_bytes = 0
    for relative in test_paths:
        if _deadline_expired(deadline):
            deadline_truncated = True
            break
        candidate = _repo_file(root, relative)
        if candidate is None:
            continue
        text = reader.read(candidate)
        if _deadline_expired(deadline):
            deadline_truncated = True
            break
        directives = tuple(parse_dart_directives(text)) if text is not None else ()
        parse_expired = _deadline_expired(deadline)
        symbol_evidence: frozenset[str] | None = None
        identifier_truncated = False
        if text is not None and not parse_expired:
            symbol_evidence, indexed_bytes, identifier_truncated = _index_symbol_evidence(
                text,
                max_memory_bytes=max_identifier_memory_bytes - identifier_memory_bytes,
                deadline=deadline,
            )
            identifier_memory_bytes += indexed_bytes
        evidence_expired = _deadline_expired(deadline)
        has_framework_registration = (
            not evidence_expired and text is not None and any(marker in text for marker in _FRAMEWORK_TEST_MARKERS)
        )
        framework_expired = _deadline_expired(deadline)
        tests.append(
            DartRelatedTestCandidate(
                path=relative,
                directives=directives,
                symbol_evidence=symbol_evidence,
                has_framework_registration=has_framework_registration and not framework_expired,
            )
        )
        if identifier_truncated:
            deadline_truncated = True
        if parse_expired or evidence_expired or framework_expired:
            deadline_truncated = True
            break

    return DartRelatedTestIndex(
        repo_root=root,
        package_roots=tuple(package_roots),
        tests=tuple(tests),
        source_bytes_read=reader.source_bytes_read,
        identifier_memory_bytes=identifier_memory_bytes,
        files_read=reader.files_read,
        skipped_text_files=reader.skipped_text_files,
        truncated=(manifests_truncated or tests_truncated or reader.truncated or deadline_truncated),
        deadline=deadline,
    )


def rank_related_dart_tests(
    repo_root: Path,
    source_path: str,
    *,
    semantic_references: Iterable[str] = (),
    symbol_names: Iterable[str] = (),
    candidate_paths: Iterable[str] | None = None,
    limit: int = 12,
    max_files: int = DART_RELATED_TEST_FILE_LIMIT,
    max_file_bytes: int = DART_RELATED_TEST_FILE_SIZE_LIMIT,
    max_total_source_bytes: int = DART_RELATED_TEST_TOTAL_SOURCE_BYTE_LIMIT,
    index: DartRelatedTestIndex | None = None,
) -> list[str]:
    """Rank existing repository Dart tests using semantics before naming.

    Package discovery is graph-free, so local package cycles cannot prevent a
    cross-package test from being selected.
    """

    if limit <= 0 or max_files <= 0 or max_file_bytes <= 0 or max_total_source_bytes <= 0:
        return []
    root = repo_root.resolve()
    if index is not None:
        if index.repo_root != root:
            raise ValueError("Dart related-test index belongs to a different repository root")
        if _deadline_expired(index.deadline):
            return []
    source = _repo_file(root, source_path)
    if source is None or source.suffix.casefold() != ".dart" or _is_test_path(source.relative_to(root)):
        return []
    source_relative = source.relative_to(root).as_posix()
    if is_generated_dart_path(source_relative):
        return []

    if index is None:
        index = build_dart_related_test_index(
            root,
            candidate_paths=candidate_paths,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_source_bytes=max_total_source_bytes,
        )

    package_names = index.package_roots
    source_package = _containing_package(source, package_names)
    source_uri = _package_uri(source, source_package)
    indexed_paths = {candidate.path for candidate in index.tests}
    semantic = {reference.replace("\\", "/") for reference in semantic_references} & indexed_paths
    names = {name for name in symbol_names if name and _is_identifier(name)}

    ranked: list[tuple[int, str]] = []
    for indexed_test in index.tests:
        if _deadline_expired(index.deadline):
            break
        relative = indexed_test.path
        candidate = root / relative
        score = 1_000 if relative in semantic else 0
        candidate_package = _containing_package(candidate, package_names)
        if candidate_package is not None and source_package is not None and candidate_package[0] == source_package[0]:
            score += 50
        score += _convention_score(source, candidate, source_package)

        if _directly_imports_source(candidate, indexed_test.directives, source, source_uri, root):
            score += 600
        if names and indexed_test.symbol_evidence is not None and not names.isdisjoint(indexed_test.symbol_evidence):
            score += 40
        if score and indexed_test.has_framework_registration:
            score += 10
        if score:
            if "integration_test" in Path(relative).parts:
                score += 5
            ranked.append((score, relative))

    return [path for _, path in sorted(ranked, key=lambda item: (-item[0], item[1]))[:limit]]


def _index_symbol_evidence(
    source: str,
    *,
    max_memory_bytes: int,
    deadline: float | None,
) -> tuple[frozenset[str] | None, int, bool]:
    """Precompute exact ``\\b<identifier>\\b`` matches within a memory budget."""

    if max_memory_bytes <= 0:
        return None, 0, True
    evidence: set[str] = set()
    estimated_bytes = 0

    def add(value: str) -> bool:
        nonlocal estimated_bytes
        if value in evidence:
            return True
        value_bytes = len(value.encode("utf-8")) + _IDENTIFIER_ENTRY_ESTIMATED_OVERHEAD
        if value_bytes > max_memory_bytes - estimated_bytes:
            return False
        evidence.add(value)
        estimated_bytes += value_bytes
        return True

    for run_number, match in enumerate(_IDENTIFIER_RUN_RE.finditer(source)):
        if run_number % 256 == 0 and _deadline_expired(deadline):
            return None, 0, True
        run = match.group(0)
        if "$" not in run:
            if _is_identifier(run) and not add(run):
                return None, 0, True
            continue

        # Python's ``\\b`` is a transition between ``\\w`` and ``\\W``.
        # Dart permits '$' in identifiers, so every boundary-delimited slice
        # of a mixed ``[\\w$]+`` run is a possible match for the old regex.
        boundaries: list[int] = []
        if run[0] != "$":
            boundaries.append(0)
        boundaries.extend(offset for offset in range(1, len(run)) if (run[offset - 1] == "$") != (run[offset] == "$"))
        if run[-1] != "$":
            boundaries.append(len(run))
        for start_number, start in enumerate(boundaries):
            for end in boundaries[start_number + 1 :]:
                value = run[start:end]
                if _is_identifier(value) and not add(value):
                    return None, 0, True
            if _deadline_expired(deadline):
                return None, 0, True

    if _deadline_expired(deadline):
        return None, 0, True
    return frozenset(evidence), estimated_bytes, False


def _collect_inventory(
    candidate_paths: Iterable[str],
    *,
    deadline: float | None,
) -> tuple[tuple[str, ...], bool]:
    paths: list[str] = []
    for raw_path in candidate_paths:
        if _deadline_expired(deadline):
            return tuple(paths), True
        paths.append(str(raw_path))
    return tuple(paths), _deadline_expired(deadline)


def _discover_test_paths(
    root: Path,
    *,
    max_files: int,
    deadline: float | None,
) -> tuple[list[str], bool]:
    paths: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        if _deadline_expired(deadline):
            return paths, True
        dirnames[:] = sorted(name for name in dirnames if name not in DART_SCAN_IGNORED_DIRS)
        for filename in sorted(filenames):
            if _deadline_expired(deadline):
                return paths, True
            path = Path(directory) / filename
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if _is_test_path(relative) and not is_generated_dart_path(relative):
                paths.append(relative.as_posix())
                if len(paths) > max_files:
                    return paths[:max_files], True
    return paths, False


def _inventory_test_paths(
    root: Path,
    candidate_paths: Iterable[str],
    *,
    max_files: int,
    deadline: float | None,
) -> tuple[list[str], bool]:
    relevant: list[str] = []
    normalized_paths: set[str] = set()
    for raw_path in candidate_paths:
        if _deadline_expired(deadline):
            return relevant, True
        normalized = str(raw_path).replace("\\", "/")
        if normalized and _is_test_path(Path(normalized)) and not is_generated_dart_path(normalized):
            normalized_paths.add(normalized)
    for normalized in sorted(normalized_paths):
        if _deadline_expired(deadline):
            return relevant, True
        relative = _validated_test_path(root, normalized)
        if relative is None:
            continue
        relevant.append(relative)
        if len(relevant) > max_files:
            return relevant[:max_files], True
    return relevant, False


def _inventory_manifest_paths(
    root: Path,
    candidate_paths: Iterable[str],
    *,
    max_files: int,
    deadline: float | None,
) -> tuple[list[str], bool]:
    manifests: list[str] = []
    normalized_paths: set[str] = set()
    for raw_path in candidate_paths:
        if _deadline_expired(deadline):
            return manifests, True
        normalized = str(raw_path).replace("\\", "/")
        if normalized and Path(normalized).name in {"pubspec.yaml", "pubspec.yml"}:
            normalized_paths.add(normalized)
    for normalized in sorted(normalized_paths):
        if _deadline_expired(deadline):
            return manifests, True
        manifest = _repo_file(root, normalized)
        if manifest is None or manifest.name not in {"pubspec.yaml", "pubspec.yml"}:
            continue
        manifests.append(manifest.relative_to(root).as_posix())
        if len(manifests) > max_files:
            return manifests[:max_files], True
    return manifests, False


def _discover_manifest_paths(
    root: Path,
    *,
    max_files: int,
    deadline: float | None,
) -> tuple[list[str], bool]:
    manifests: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        if _deadline_expired(deadline):
            return manifests, True
        dirnames[:] = sorted(name for name in dirnames if name not in DART_SCAN_IGNORED_DIRS)
        for filename in sorted(filenames):
            if _deadline_expired(deadline):
                return manifests, True
            if filename not in {"pubspec.yaml", "pubspec.yml"}:
                continue
            manifest = Path(directory) / filename
            try:
                manifests.append(manifest.relative_to(root).as_posix())
            except ValueError:
                continue
            if len(manifests) > max_files:
                return manifests[:max_files], True
    return manifests, False


def _containing_package(path: Path, packages: Iterable[tuple[Path, str]]) -> tuple[Path, str] | None:
    resolved = path.resolve()
    for package in packages:
        try:
            resolved.relative_to(package[0])
        except ValueError:
            continue
        return package
    return None


def _package_uri(source: Path, package: tuple[Path, str] | None) -> str | None:
    if package is None:
        return None
    try:
        relative = source.relative_to(package[0] / "lib")
    except ValueError:
        return None
    return f"package:{package[1]}/{relative.as_posix()}"


def _directly_imports_source(
    candidate: Path,
    directives: tuple[DartDirective, ...],
    source: Path,
    source_uri: str | None,
    root: Path,
) -> bool:
    for directive in directives:
        if directive.kind != "import":
            continue
        for target in (directive.target, *directive.conditional_targets):
            if source_uri is not None and target == source_uri:
                return True
            if target.startswith(("dart:", "package:")):
                continue
            target_path = target.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
            try:
                resolved = (candidate.parent / target_path).resolve()
                resolved.relative_to(root)
            except OSError, ValueError:
                continue
            if resolved == source:
                return True
    return False


def _convention_score(
    source: Path,
    candidate: Path,
    source_package: tuple[Path, str] | None,
) -> int:
    expected_name = f"{source.stem}_test.dart"
    score = 200 if candidate.name == expected_name else 0
    if source_package is None:
        return score
    try:
        source_under_lib = source.relative_to(source_package[0] / "lib")
        candidate_under_package = candidate.relative_to(source_package[0])
    except ValueError:
        return score
    mirrored = source_under_lib.with_name(expected_name)
    if candidate_under_package in {Path("test") / mirrored, Path("integration_test") / mirrored}:
        score += 400
    elif candidate.name == expected_name and candidate_under_package.parts[0] in {"test", "integration_test"}:
        score += 100
    return score


def _validated_test_path(root: Path, path: str) -> str | None:
    candidate = _repo_file(root, path)
    if candidate is None:
        return None
    relative = candidate.relative_to(root)
    if not _is_test_path(relative) or is_generated_dart_path(relative):
        return None
    return relative.as_posix()


def _repo_file(root: Path, path: str) -> Path | None:
    lexical_root = Path(os.path.abspath(root))
    candidate = Path(path)
    lexical_candidate = Path(os.path.abspath(candidate if candidate.is_absolute() else lexical_root / candidate))
    try:
        relative = lexical_candidate.relative_to(lexical_root)
        if not relative.parts:
            return None
        current = lexical_root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                return None
        resolved_root = lexical_root.resolve(strict=True)
        resolved = lexical_candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except OSError, ValueError:
        return None
    return resolved if resolved.is_file() else None


def _is_test_path(path: Path) -> bool:
    return path.name.endswith("_test.dart") and bool({"test", "integration_test"} & set(path.parts))


def _read_bounded_text(path: Path, max_bytes: int) -> str | None:
    try:
        if path.stat().st_size > max_bytes:
            return None
        return path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None


def _is_identifier(value: str) -> bool:
    return (value[0].isalpha() or value[0] in "_$") and all(
        character.isalnum() or character in "_$" for character in value
    )


def _deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and monotonic() >= deadline
