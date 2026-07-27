from pathlib import Path

from apex_ray.llm.prompts import RESOLUTION_PROMPT_MAX_CHARS, build_resolution_prompt
from apex_ray.models import (
    ChangedFile,
    ChangedHunk,
    CodeSnippet,
    ContextPack,
    DiffLine,
    DiffLineKind,
    DiffStats,
    DiffSummary,
    Finding,
    FindingConfidence,
    FindingSeverity,
    ProjectProfile,
    ReviewConfig,
    RuleMatch,
    RuleMode,
    TargetMode,
)
from apex_ray.report import build_report


def test_resolution_prompt_is_bounded_and_keeps_only_relevant_delta_evidence(
    tmp_path: Path,
) -> None:
    previous_pack = ContextPack(
        id="src/relevant.ts#target:1",
        file="src/relevant.ts",
        changed_lines=[(10, 12)],
        diff_snippet=["PREVIOUS_PACK_EVIDENCE_SENTINEL"],
    )
    finding = Finding(
        title="Relevant carried finding",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/relevant.ts",
        line=11,
        failure_mode="PREVIOUS_FINDING_EVIDENCE_SENTINEL",
        evidence="The old target implementation violates the invariant.",
        suggested_fix="Repair the target implementation.",
        suggested_test="Add a target regression test.",
        context_pack_id=previous_pack.id,
    )
    oversized_relevant_tail = "r" * 600_000
    relevant_diff = ChangedFile(
        old_path="src/relevant.ts",
        new_path="src/renamed-relevant.ts",
        hunks=[
            ChangedHunk(
                old_start=10,
                old_lines=1,
                new_start=10,
                new_lines=1,
                lines=[
                    DiffLine(
                        kind=DiffLineKind.ADD,
                        content=f"CURRENT_DIFF_EVIDENCE_SENTINEL_{oversized_relevant_tail}",
                        new_line=11,
                    )
                ],
            )
        ],
    )
    relevant_pack = ContextPack(
        id="src/renamed-relevant.ts#target:2",
        file="src/renamed-relevant.ts",
        changed_lines=[(10, 12)],
        diff_snippet=[f"CURRENT_PACK_EVIDENCE_SENTINEL_{oversized_relevant_tail}"],
        changed_snippets=[
            CodeSnippet(
                file="src/renamed-relevant.ts",
                start_line=10,
                end_line=12,
                code="CURRENT_CHANGED_SNIPPET_SENTINEL",
            )
        ],
        warnings=["CURRENT_PACK_TAIL_EVIDENCE_SENTINEL"],
    )
    unrelated_blob = "x" * 30_000
    unrelated_files = [
        ChangedFile(
            old_path=f"src/unrelated-{index:03d}.ts",
            new_path=f"src/unrelated-{index:03d}.ts",
            hunks=[
                ChangedHunk(
                    old_start=1,
                    old_lines=1,
                    new_start=1,
                    new_lines=1,
                    lines=[
                        DiffLine(
                            kind=DiffLineKind.ADD,
                            content=f"UNRELATED_DIFF_SENTINEL_{index:03d}_{unrelated_blob}",
                            new_line=1,
                        )
                    ],
                )
            ],
        )
        for index in range(32)
    ]
    unrelated_packs = [
        ContextPack(
            id=f"src/unrelated-{index:03d}.ts#unrelated:1",
            file=f"src/unrelated-{index:03d}.ts",
            changed_lines=[(1, 1)],
            diff_snippet=[f"UNRELATED_PACK_SENTINEL_{index:03d}_{unrelated_blob}"],
        )
        for index in range(32)
    ]
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            files=[relevant_diff, *unrelated_files],
        ),
        context_packs=[relevant_pack, *unrelated_packs],
    )

    prompt = build_resolution_prompt(finding, previous_pack, report)

    assert RESOLUTION_PROMPT_MAX_CHARS < 500_000
    assert len(prompt) <= RESOLUTION_PROMPT_MAX_CHARS
    assert prompt == build_resolution_prompt(finding, previous_pack, report)
    assert "PREVIOUS_FINDING_EVIDENCE_SENTINEL" in prompt
    assert "PREVIOUS_PACK_EVIDENCE_SENTINEL" in prompt
    assert "CURRENT_DIFF_EVIDENCE_SENTINEL" in prompt
    assert "CURRENT_PACK_EVIDENCE_SENTINEL" in prompt
    assert "CURRENT_CHANGED_SNIPPET_SENTINEL" in prompt
    assert "CURRENT_PACK_TAIL_EVIDENCE_SENTINEL" in prompt
    assert "UNRELATED_DIFF_SENTINEL" not in prompt
    assert "UNRELATED_PACK_SENTINEL" not in prompt
    assert '"applied": true' in prompt
    assert '"char_truncated_sections": [' in prompt
    assert '"truncated": true' in prompt
    assert '"excluded_irrelevant_diff_files": 32' in prompt
    assert '"excluded_irrelevant_context_packs": 32' in prompt


def test_resolution_prompt_treats_code_paths_as_literals_and_omits_global_diff_data(
    tmp_path: Path,
) -> None:
    previous_pack = ContextPack(
        id="src/pages/[id].ts#render:1",
        file="src/pages/[id].ts",
        diff_snippet=["PREVIOUS_LITERAL_PATH_SENTINEL"],
    )
    finding = Finding(
        title="Literal path finding",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/pages/[id].ts",
        failure_mode="The dynamic route can return the wrong record.",
        evidence="The route ignores the requested id.",
        suggested_fix="Use the requested id.",
        suggested_test="Cover two route ids.",
        context_pack_id=previous_pack.id,
    )
    relevant_file = ChangedFile(
        old_path="src/pages/[id].ts",
        new_path="src/pages/[id].ts",
        additions=1,
        hunks=[
            ChangedHunk(
                old_start=1,
                old_lines=1,
                new_start=1,
                new_lines=1,
                lines=[
                    DiffLine(
                        kind=DiffLineKind.ADD,
                        content="RELEVANT_LITERAL_PATH_DIFF_SENTINEL",
                        new_line=1,
                    )
                ],
            )
        ],
    )
    unrelated_file = ChangedFile(
        old_path="src/pages/i.ts",
        new_path="src/pages/i.ts",
        additions=100,
        hunks=[
            ChangedHunk(
                old_start=1,
                old_lines=1,
                new_start=1,
                new_lines=1,
                lines=[
                    DiffLine(
                        kind=DiffLineKind.ADD,
                        content="UNRELATED_BRACKET_GLOB_DIFF_SENTINEL",
                        new_line=1,
                    )
                ],
            )
        ],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            files=[relevant_file, unrelated_file],
            stats=DiffStats(files_changed=2, additions=101),
            warnings=["UNRELATED_GLOBAL_WARNING_SENTINEL"],
        ),
        context_packs=[
            ContextPack(
                id="src/pages/[id].ts#render:2",
                file="src/pages/[id].ts",
                diff_snippet=["RELEVANT_LITERAL_PATH_PACK_SENTINEL"],
            ),
            ContextPack(
                id="src/pages/i.ts#render:1",
                file="src/pages/i.ts",
                diff_snippet=["UNRELATED_BRACKET_GLOB_PACK_SENTINEL"],
            ),
        ],
    )

    prompt = build_resolution_prompt(finding, previous_pack, report)

    assert "RELEVANT_LITERAL_PATH_DIFF_SENTINEL" in prompt
    assert "RELEVANT_LITERAL_PATH_PACK_SENTINEL" in prompt
    assert "UNRELATED_BRACKET_GLOB_DIFF_SENTINEL" not in prompt
    assert "UNRELATED_BRACKET_GLOB_PACK_SENTINEL" not in prompt
    assert "UNRELATED_GLOBAL_WARNING_SENTINEL" not in prompt
    assert '"global_diff_warnings_excluded": 1' in prompt
    assert '"files_changed": 1' in prompt
    assert '"additions": 1' in prompt
    assert '"additions": 101' not in prompt


def test_resolution_prompt_matches_declared_resolution_surface_globs(
    tmp_path: Path,
) -> None:
    previous_pack = ContextPack(
        id="src/schema.ts#schema:1",
        file="src/schema.ts",
        rule_matches=[
            RuleMatch(
                id="schema-migration",
                title="Keep schema and migrations aligned",
                severity=FindingSeverity.HIGH,
                mode=RuleMode.STRICT,
                resolution_surfaces=["db/migrations/**"],
            )
        ],
    )
    finding = Finding(
        title="Schema lacks a migration",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/schema.ts",
        failure_mode="Deployments can use an incompatible schema.",
        evidence="The schema changed without a migration.",
        suggested_fix="Add a migration.",
        suggested_test="Run migration validation.",
        context_pack_id=previous_pack.id,
    )
    migration_file = ChangedFile(
        old_path=None,
        new_path="db/migrations/001_add_field.sql",
        hunks=[
            ChangedHunk(
                old_start=0,
                old_lines=0,
                new_start=1,
                new_lines=1,
                lines=[
                    DiffLine(
                        kind=DiffLineKind.ADD,
                        content="RELEVANT_RESOLUTION_GLOB_DIFF_SENTINEL",
                        new_line=1,
                    )
                ],
            )
        ],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, files=[migration_file]),
        context_packs=[
            ContextPack(
                id="db/migrations/001_add_field.sql#migration:1",
                file="db/migrations/001_add_field.sql",
                diff_snippet=["RELEVANT_RESOLUTION_GLOB_PACK_SENTINEL"],
            )
        ],
    )

    prompt = build_resolution_prompt(finding, previous_pack, report)

    assert "RELEVANT_RESOLUTION_GLOB_DIFF_SENTINEL" in prompt
    assert "RELEVANT_RESOLUTION_GLOB_PACK_SENTINEL" in prompt
    assert '"db/migrations/**"' in prompt


def test_resolution_prompt_prioritizes_primary_pack_over_cross_file_line_matches(
    tmp_path: Path,
) -> None:
    related_test_paths = [f"tests/related-{index}.test.ts" for index in range(8)]
    previous_pack = ContextPack(
        id="src/main.ts#target:1",
        file="src/main.ts",
        changed_lines=[(10, 10)],
        related_tests=related_test_paths,
    )
    finding = Finding(
        title="Primary implementation finding",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/main.ts",
        line=10,
        failure_mode="The primary implementation violates the invariant.",
        evidence="The old target has the invalid behavior.",
        suggested_fix="Repair the primary target.",
        suggested_test="Cover the primary target.",
        context_pack_id=previous_pack.id,
    )
    secondary_packs = [
        ContextPack(
            id=f"{path}#related:1",
            file=path,
            changed_lines=[(10, 10)],
            diff_snippet=[f"SECONDARY_LINE_MATCH_SENTINEL_{index}"],
        )
        for index, path in enumerate(related_test_paths)
    ]
    primary_pack = ContextPack(
        id="src/main.ts#target:50",
        file="src/main.ts",
        changed_lines=[(50, 50)],
        diff_snippet=["PRIMARY_CURRENT_PACK_SENTINEL"],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            files=[ChangedFile(old_path="src/main.ts", new_path="src/main.ts")],
        ),
        context_packs=[*secondary_packs, primary_pack],
    )

    prompt = build_resolution_prompt(finding, previous_pack, report)

    assert "PRIMARY_CURRENT_PACK_SENTINEL" in prompt
    assert '"omitted_relevant_context_packs": 1' in prompt
