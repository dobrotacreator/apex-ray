from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

from apex_ray.discovery import LANGUAGE_EXTENSIONS
from apex_ray.models import (
    ChangedFile,
    DiffLineKind,
    DiffSummary,
    FileKind,
    RiskSeverity,
    RiskSignal,
)

LOCKFILE_NAMES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "poetry.lock",
    "pdm.lock",
    "pipfile.lock",
    "cargo.lock",
    "go.sum",
    "uv.lock",
}

DEPENDENCY_CONFIG_NAMES = {
    "package.json",
    "pyproject.toml",
    "cargo.toml",
    "go.mod",
    "requirements.txt",
    "requirements-dev.txt",
    "pipfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
}

CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}
DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
SCHEMA_NAMES = {"schema.sql", "schema.prisma", "openapi.yaml", "openapi.yml", "swagger.yaml", "swagger.yml"}

RISK_KEYWORDS: dict[str, tuple[RiskSeverity, tuple[str, ...], str]] = {
    "auth": (
        RiskSeverity.HIGH,
        ("auth", "permission", "authorize", "authorization", "role", "session", "token", "jwt", "oauth"),
        "Authentication or authorization-related code changed.",
    ),
    "validation": (
        RiskSeverity.MEDIUM,
        ("validate", "validation", "sanitize", "schema", "required", "zod", "joi", "pydantic"),
        "Validation-related code changed.",
    ),
    "persistence": (
        RiskSeverity.MEDIUM,
        (
            "select ",
            "insert ",
            "update ",
            "delete ",
            "transaction",
            "rollback",
            "commit",
            "repository",
            "orm",
            "database",
            "db.",
        ),
        "Persistence or transaction-related code changed.",
    ),
    "public_api": (
        RiskSeverity.MEDIUM,
        ("route", "router", "controller", "handler", "endpoint", "request", "response", "openapi", "graphql"),
        "Public API or boundary code changed.",
    ),
    "external_io": (
        RiskSeverity.MEDIUM,
        ("fetch(", "requests.", "httpx.", "axios.", "urllib", "subprocess", "read_text", "write_text", "open("),
        "External I/O, filesystem, network, or process boundary changed.",
    ),
    "shell": (
        RiskSeverity.HIGH,
        ("subprocess", "exec(", "spawn(", "system(", "shell=true"),
        "Shell or subprocess execution changed.",
    ),
    "path": (
        RiskSeverity.MEDIUM,
        ("path", "filename", "filepath", "dirname", "resolve(", "joinpath", "path.join"),
        "Path handling changed.",
    ),
    "serialization": (
        RiskSeverity.MEDIUM,
        ("json.parse", "json.loads", "yaml.", "pickle", "deserialize", "serialize"),
        "Serialization or deserialization changed.",
    ),
    "cache": (
        RiskSeverity.LOW,
        ("cache", "redis", "memo", "ttl"),
        "Cache-related code changed.",
    ),
    "concurrency": (
        RiskSeverity.MEDIUM,
        ("async ", "await ", "lock", "mutex", "thread", "queue", "worker", "retry"),
        "Concurrency, queue, retry, or worker code changed.",
    ),
}


def classify_diff(diff: DiffSummary, ignore_patterns: list[str]) -> DiffSummary:
    test_changed = False
    for file in diff.files:
        classify_file(file, ignore_patterns)
        if file.file_kind == FileKind.TEST and not file.is_ignored:
            test_changed = True

    for file in diff.files:
        if file.is_ignored:
            continue
        add_risk_signals(file)
        if file.file_kind == FileKind.SOURCE and not test_changed:
            file.risk_signals.append(
                RiskSignal(
                    kind="test_gap",
                    severity=RiskSeverity.LOW,
                    reason="Source changed without test files in the same diff.",
                    file=file.path,
                )
            )

    diff.stats.ignored_files = sum(1 for file in diff.files if file.is_ignored)
    return diff


def classify_file(file: ChangedFile, ignore_patterns: list[str]) -> ChangedFile:
    path = file.path
    file.language = detect_language(path)
    file.file_kind = detect_file_kind(path)

    ignore_match = match_ignore(path, ignore_patterns)
    if ignore_match:
        file.is_ignored = True
        file.ignore_reason = f"Matched ignore pattern: {ignore_match}"
    return file


def detect_language(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return LANGUAGE_EXTENSIONS.get(suffix, _config_language(path))


def detect_file_kind(path: str) -> FileKind:
    normalized = path.replace("\\", "/").lower()
    pure = PurePosixPath(normalized)
    name = pure.name
    parts = set(pure.parts)
    suffix = pure.suffix

    if name in LOCKFILE_NAMES or name.endswith(".lock"):
        return FileKind.LOCKFILE
    if "vendor" in parts or "vendors" in parts:
        return FileKind.VENDORED
    if _contains_part(normalized, ("generated", "__generated__", "dist", "build")) or name.endswith(".generated.ts"):
        return FileKind.GENERATED
    if name in DEPENDENCY_CONFIG_NAMES:
        return FileKind.DEPENDENCY
    if _contains_part(normalized, ("docs", "doc")) or suffix in DOC_EXTENSIONS:
        return FileKind.DOCS
    if "migration" in normalized or "migrations" in parts:
        return FileKind.MIGRATION
    if (
        name in SCHEMA_NAMES
        or suffix in {".graphql", ".gql"}
        or "schema" in name
        or "openapi" in name
        or "swagger" in name
    ):
        return FileKind.SCHEMA
    if _looks_like_test(normalized):
        return FileKind.TEST
    if suffix in CONFIG_EXTENSIONS:
        return FileKind.CONFIG
    if detect_language(path) != "unknown":
        return FileKind.SOURCE
    return FileKind.UNKNOWN


def match_ignore(path: str, patterns: list[str]) -> str | None:
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(f"/{normalized}", pattern):
            return pattern
        if pattern.startswith("**/") and fnmatch.fnmatch(normalized, pattern[3:]):
            return pattern
    return None


def add_risk_signals(file: ChangedFile) -> None:
    seen: set[tuple[str, int | None]] = set()

    if file.file_kind == FileKind.MIGRATION:
        _append_signal(file, seen, "migration", RiskSeverity.HIGH, "Migration file changed.", None)
    elif file.file_kind == FileKind.SCHEMA:
        _append_signal(file, seen, "public_api", RiskSeverity.MEDIUM, "Schema or API contract file changed.", None)
    elif file.file_kind == FileKind.DEPENDENCY:
        _append_signal(file, seen, "dependency", RiskSeverity.LOW, "Dependency manifest changed.", None)

    for hunk in file.hunks:
        for line in hunk.lines:
            if line.kind == DiffLineKind.CONTEXT:
                continue
            content = line.content.lower()
            target_line = line.new_line or line.old_line
            for kind, (severity, keywords, reason) in RISK_KEYWORDS.items():
                if any(keyword in content for keyword in keywords):
                    signal = _append_signal(file, seen, kind, severity, reason, target_line)
                    if signal:
                        hunk.risk_signals.append(signal)


def _append_signal(
    file: ChangedFile,
    seen: set[tuple[str, int | None]],
    kind: str,
    severity: RiskSeverity,
    reason: str,
    line: int | None,
) -> RiskSignal | None:
    key = (kind, line)
    if key in seen:
        return None
    seen.add(key)
    signal = RiskSignal(kind=kind, severity=severity, reason=reason, file=file.path, line=line)
    file.risk_signals.append(signal)
    return signal


def _config_language(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in CONFIG_EXTENSIONS:
        return suffix.removeprefix(".")
    return "unknown"


def _contains_part(path: str, names: tuple[str, ...]) -> bool:
    parts = set(PurePosixPath(path).parts)
    return any(name in parts for name in names)


def _looks_like_test(path: str) -> bool:
    return bool(
        re.search(r"(^|/)(tests?|__tests__|spec)(/|$)", path) or re.search(r"(\.|_)(test|spec)\.[a-z0-9]+$", path)
    )
