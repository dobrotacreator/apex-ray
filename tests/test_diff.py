from pathlib import Path

from apex_ray.diff import parse_unified_diff
from apex_ray.models import DiffLineKind, FileStatus, TargetMode


def test_parse_unified_diff_add_modify_and_rename() -> None:
    text = Path("tests/fixtures/sample.diff").read_text(encoding="utf-8")

    summary = parse_unified_diff(text, target_mode=TargetMode.PATCH)

    assert summary.stats.files_changed == 3
    assert summary.stats.additions == 7
    assert summary.stats.deletions == 3

    first = summary.files[0]
    assert first.path == "src/auth.py"
    assert first.status == FileStatus.MODIFIED
    assert first.hunks[0].new_start == 1

    second = summary.files[1]
    assert second.path == "docs/usage.md"
    assert second.status == FileStatus.ADDED

    third = summary.files[2]
    assert third.old_path == "src/old_name.py"
    assert third.new_path == "src/new_name.py"
    assert third.status == FileStatus.RENAMED


def test_parse_empty_diff() -> None:
    summary = parse_unified_diff("", target_mode=TargetMode.PATCH)

    assert summary.files == []
    assert summary.stats.files_changed == 0
    assert summary.warnings == []


def test_parse_blank_hunk_line_as_context() -> None:
    summary = parse_unified_diff(
        """diff --git a/src/example.ts b/src/example.ts
index 1111111..2222222 100644
--- a/src/example.ts
+++ b/src/example.ts
@@ -1,4 +1,4 @@
 import { value } from './value.js';

 export const result =
-  value;
+  value + 1;
""",
        target_mode=TargetMode.PATCH,
    )

    hunk = summary.files[0].hunks[0]
    assert summary.warnings == []
    assert hunk.lines[1].kind == DiffLineKind.CONTEXT
    assert hunk.lines[1].content == ""
    assert hunk.lines[1].old_line == 2
    assert hunk.lines[1].new_line == 2


def test_parse_hunk_lines_that_look_like_diff_markers() -> None:
    summary = parse_unified_diff(
        """diff --git a/tests/example.py b/tests/example.py
index 1111111..2222222 100644
--- a/tests/example.py
+++ b/tests/example.py
@@ -1,4 +1,4 @@
 text = '''
---- old marker
++++ new marker
 '''
""",
        target_mode=TargetMode.PATCH,
    )

    hunk = summary.files[0].hunks[0]
    assert summary.warnings == []
    assert hunk.lines[1].kind == DiffLineKind.DELETE
    assert hunk.lines[1].content == "--- old marker"
    assert hunk.lines[2].kind == DiffLineKind.ADD
    assert hunk.lines[2].content == "+++ new marker"
