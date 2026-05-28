import re

from apex_ray.models import (
    ChangedFile,
    ChangedHunk,
    DiffLine,
    DiffLineKind,
    DiffStats,
    DiffSummary,
    FileStatus,
    TargetMode,
)

DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*?) b/(.*)$")
HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_lines>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))? @@ ?(?P<header>.*)$"
)


def parse_unified_diff(text: str, target_mode: TargetMode, base: str | None = None) -> DiffSummary:
    files: list[ChangedFile] = []
    current_file: ChangedFile | None = None
    current_hunk: ChangedHunk | None = None
    old_line = 0
    new_line = 0
    warnings: list[str] = []

    for raw_line in text.splitlines():
        header = DIFF_HEADER_RE.match(raw_line)
        if header:
            if current_file is not None:
                files.append(current_file)
            current_file = ChangedFile(
                old_path=header.group(1),
                new_path=header.group(2),
                status=FileStatus.MODIFIED,
            )
            current_hunk = None
            continue

        if current_file is None:
            if raw_line.strip():
                warnings.append(f"Ignored diff line before file header: {raw_line[:80]}")
            continue

        if raw_line.startswith("new file mode"):
            current_file.status = FileStatus.ADDED
            continue
        if raw_line.startswith("deleted file mode"):
            current_file.status = FileStatus.DELETED
            continue
        if raw_line.startswith("rename from "):
            current_file.status = FileStatus.RENAMED
            current_file.old_path = raw_line.removeprefix("rename from ").strip()
            continue
        if raw_line.startswith("rename to "):
            current_file.status = FileStatus.RENAMED
            current_file.new_path = raw_line.removeprefix("rename to ").strip()
            continue
        if raw_line.startswith("copy from "):
            current_file.status = FileStatus.COPIED
            current_file.old_path = raw_line.removeprefix("copy from ").strip()
            continue
        if raw_line.startswith("copy to "):
            current_file.status = FileStatus.COPIED
            current_file.new_path = raw_line.removeprefix("copy to ").strip()
            continue
        if current_hunk is None and raw_line.startswith("--- "):
            path = _parse_marker_path(raw_line[4:])
            if path is None:
                current_file.old_path = None
            elif current_file.status not in {FileStatus.RENAMED, FileStatus.COPIED}:
                current_file.old_path = path
            continue
        if current_hunk is None and raw_line.startswith("+++ "):
            path = _parse_marker_path(raw_line[4:])
            if path is None:
                current_file.new_path = None
            elif current_file.status not in {FileStatus.RENAMED, FileStatus.COPIED}:
                current_file.new_path = path
            continue

        hunk_match = HUNK_RE.match(raw_line)
        if hunk_match:
            old_line = int(hunk_match.group("old_start"))
            new_line = int(hunk_match.group("new_start"))
            current_hunk = ChangedHunk(
                old_start=old_line,
                old_lines=int(hunk_match.group("old_lines") or "1"),
                new_start=new_line,
                new_lines=int(hunk_match.group("new_lines") or "1"),
                section_header=hunk_match.group("header") or "",
            )
            current_file.hunks.append(current_hunk)
            continue

        if current_hunk is None:
            continue

        if raw_line.startswith("+"):
            current_hunk.lines.append(
                DiffLine(kind=DiffLineKind.ADD, content=raw_line[1:], old_line=None, new_line=new_line)
            )
            current_file.additions += 1
            new_line += 1
        elif raw_line.startswith("-"):
            current_hunk.lines.append(
                DiffLine(kind=DiffLineKind.DELETE, content=raw_line[1:], old_line=old_line, new_line=None)
            )
            current_file.deletions += 1
            old_line += 1
        elif raw_line.startswith(" ") or raw_line == "":
            current_hunk.lines.append(
                DiffLine(
                    kind=DiffLineKind.CONTEXT,
                    content=raw_line[1:] if raw_line else "",
                    old_line=old_line,
                    new_line=new_line,
                )
            )
            old_line += 1
            new_line += 1
        elif raw_line.startswith("\\"):
            continue
        else:
            warnings.append(f"Unrecognized hunk line in {current_file.path}: {raw_line[:80]}")

    if current_file is not None:
        files.append(current_file)

    return DiffSummary(
        base=base,
        target_mode=target_mode,
        files=files,
        stats=DiffStats(
            files_changed=len(files),
            additions=sum(file.additions for file in files),
            deletions=sum(file.deletions for file in files),
        ),
        warnings=warnings,
    )


def _parse_marker_path(value: str) -> str | None:
    value = value.strip()
    if value == "/dev/null":
        return None
    if value.startswith("a/") or value.startswith("b/"):
        return value[2:]
    return value
