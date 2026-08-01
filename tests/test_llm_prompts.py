from pathlib import Path

import pytest

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
    FileKind,
    FileStatus,
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
    assert "UNRELATED_DIFF_SENTINEL" not in prompt
    assert "UNRELATED_PACK_SENTINEL" not in prompt
    assert "UNRELATED_DIFF_SENTINEL_031" not in prompt
    assert "UNRELATED_PACK_SENTINEL_031" not in prompt
    assert '"applied": true' in prompt
    assert '"char_truncated_sections": [' in prompt
    assert '"current_context_packs"' in prompt
    assert '"truncated": true' in prompt
    assert '"unselected_unanchored_diff_files": 0' in prompt
    assert '"unselected_unanchored_context_packs": 32' in prompt
    assert '"excluded_ineligible_fallback_diff_files": 32' in prompt
    assert '"selected_fallback_diff_files": []' in prompt
    assert '"selected_fallback_context_pack_ids": []' in prompt


def test_resolution_prompt_treats_code_paths_as_literals_and_omits_ineligible_fallback(
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
    assert "UNRELATED_GLOBAL_WARNING_SENTINEL" in prompt
    assert '"global_diff_warnings_count": 1' in prompt
    assert '"omitted_global_diff_warnings": 0' in prompt
    assert '"selected_fallback_diff_files": []' in prompt
    assert '"selected_fallback_context_pack_ids": []' in prompt
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


def test_resolution_prompt_includes_bounded_fallback_for_new_fix_surface(
    tmp_path: Path,
) -> None:
    previous_pack = ContextPack(
        id="src/registry.ts#register:1",
        file="src/registry.ts",
    )
    finding = Finding(
        title="Provider is not registered",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/registry.ts",
        failure_mode="The provider cannot be selected.",
        evidence="The old registry does not expose the provider.",
        suggested_fix="Register the provider through a new adapter.",
        suggested_test="Select the provider through configuration.",
        context_pack_id=previous_pack.id,
    )
    new_fix_file = ChangedFile(
        old_path=None,
        new_path="src/adapters/new-provider.ts",
        additions=1,
        hunks=[
            ChangedHunk(
                old_start=0,
                old_lines=0,
                new_start=1,
                new_lines=1,
                lines=[
                    DiffLine(
                        kind=DiffLineKind.ADD,
                        content="NEW_FIX_SURFACE_DIFF_SENTINEL",
                        new_line=1,
                    )
                ],
            )
        ],
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, files=[new_fix_file]),
        context_packs=[
            ContextPack(
                id="src/adapters/new-provider.ts#register:1",
                file="src/adapters/new-provider.ts",
                diff_snippet=["NEW_FIX_SURFACE_PACK_SENTINEL"],
            )
        ],
    )

    prompt = build_resolution_prompt(finding, previous_pack, report)

    assert "NEW_FIX_SURFACE_DIFF_SENTINEL" in prompt
    assert "NEW_FIX_SURFACE_PACK_SENTINEL" in prompt
    assert '"selected_fallback_diff_files": [' in prompt
    assert '"selected_fallback_context_pack_ids": [' in prompt


def test_resolution_prompt_reserves_total_caps_for_bounded_fallback(
    tmp_path: Path,
) -> None:
    anchored_paths = [f"src/anchored-{index:02d}.ts" for index in range(20)]
    fallback_paths = [f"src/fallback-{index:02d}.ts" for index in range(6)]
    previous_pack = ContextPack(
        id="src/main.ts#target:1",
        file="src/main.ts",
        related_tests=anchored_paths,
    )
    finding = Finding(
        title="Fix may span a new file",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/main.ts",
        failure_mode="The old implementation violates the invariant.",
        evidence="The target lacks the required boundary.",
        suggested_fix="Restore the boundary, including through a new adapter if needed.",
        suggested_test="Cover the repaired boundary.",
        context_pack_id=previous_pack.id,
    )
    anchored_files = [
        ChangedFile(
            old_path=path,
            new_path=path,
            hunks=[
                ChangedHunk(
                    old_start=1,
                    old_lines=1,
                    new_start=1,
                    new_lines=1,
                    lines=[
                        DiffLine(
                            kind=DiffLineKind.ADD,
                            content=f"ANCHORED_DIFF_SENTINEL_{index:02d}",
                            new_line=1,
                        )
                    ],
                )
            ],
        )
        for index, path in enumerate(anchored_paths)
    ]
    fallback_files = [
        ChangedFile(
            old_path=None,
            new_path=path,
            hunks=[
                ChangedHunk(
                    old_start=0,
                    old_lines=0,
                    new_start=1,
                    new_lines=1,
                    lines=[
                        DiffLine(
                            kind=DiffLineKind.ADD,
                            content=f"FALLBACK_DIFF_SENTINEL_{index:02d}",
                            new_line=1,
                        )
                    ],
                )
            ],
        )
        for index, path in enumerate(fallback_paths)
    ]
    anchored_packs = [
        ContextPack(
            id=f"{path}#target:{index + 2}",
            file=path,
            diff_snippet=[f"ANCHORED_PACK_SENTINEL_{index:02d}"],
        )
        for index, path in enumerate(anchored_paths)
    ]
    fallback_packs = [
        ContextPack(
            id=f"{path}#adapter:{index + 1}",
            file=path,
            diff_snippet=[f"FALLBACK_PACK_SENTINEL_{index:02d}"],
        )
        for index, path in enumerate(fallback_paths)
    ]

    def render(
        files: list[ChangedFile],
        packs: list[ContextPack],
    ) -> str:
        report = build_report(
            ProjectProfile(root=str(tmp_path), is_git_repo=True),
            ReviewConfig(),
            DiffSummary(target_mode=TargetMode.PATCH, files=files),
            context_packs=packs,
        )
        return build_resolution_prompt(finding, previous_pack, report)

    prompt = render(
        [*anchored_files, *fallback_files],
        [*anchored_packs, *fallback_packs],
    )

    assert prompt == render(
        [*reversed(fallback_files), *reversed(anchored_files)],
        [*reversed(fallback_packs), *reversed(anchored_packs)],
    )
    assert "ANCHORED_DIFF_SENTINEL_15" in prompt
    assert "ANCHORED_DIFF_SENTINEL_16" not in prompt
    assert "FALLBACK_DIFF_SENTINEL_03" in prompt
    assert "FALLBACK_DIFF_SENTINEL_04" not in prompt
    assert "ANCHORED_PACK_SENTINEL_05" in prompt
    assert "ANCHORED_PACK_SENTINEL_06" not in prompt
    assert "FALLBACK_PACK_SENTINEL_01" in prompt
    assert "FALLBACK_PACK_SENTINEL_02" not in prompt
    assert '"omitted_relevant_diff_files": 4' in prompt
    assert '"omitted_relevant_context_packs": 14' in prompt
    assert '"unselected_unanchored_diff_files": 2' in prompt
    assert '"unselected_unanchored_context_packs": 4' in prompt


def test_resolution_prompt_prioritizes_reviewable_fallback_over_ignored_docs(
    tmp_path: Path,
) -> None:
    previous_pack = ContextPack(id="src/registry.ts#register:1", file="src/registry.ts")
    finding = Finding(
        title="Provider is not registered",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=previous_pack.file,
        failure_mode="The provider cannot be selected.",
        evidence="The old registry does not expose the provider.",
        suggested_fix="Register the provider through a new adapter.",
        suggested_test="Select the provider through configuration.",
        context_pack_id=previous_pack.id,
    )
    ignored_docs = [
        ChangedFile(
            old_path=None,
            new_path=f"a-docs/generated-{index}.md",
            status=FileStatus.ADDED,
            file_kind=FileKind.DOCS,
            is_ignored=True,
            hunks=[
                ChangedHunk(
                    old_start=0,
                    old_lines=0,
                    new_start=1,
                    new_lines=1,
                    lines=[
                        DiffLine(
                            kind=DiffLineKind.ADD,
                            content=f"IGNORED_DOC_SENTINEL_{index}",
                            new_line=1,
                        )
                    ],
                )
            ],
        )
        for index in range(4)
    ]
    provider_path = "z-src/new-provider.ts"
    provider_file = ChangedFile(
        old_path=None,
        new_path=provider_path,
        status=FileStatus.ADDED,
        file_kind=FileKind.SOURCE,
        hunks=[
            ChangedHunk(
                old_start=0,
                old_lines=0,
                new_start=1,
                new_lines=1,
                lines=[
                    DiffLine(
                        kind=DiffLineKind.ADD,
                        content="REVIEWABLE_PROVIDER_DIFF_SENTINEL",
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
            files=[*ignored_docs, provider_file],
        ),
        context_packs=[
            *[
                ContextPack(
                    id=f"{changed_file.path}#docs:1",
                    file=changed_file.path,
                    diff_snippet=[f"IGNORED_DOC_PACK_SENTINEL_{index}"],
                )
                for index, changed_file in enumerate(ignored_docs)
            ],
            ContextPack(
                id=f"{provider_path}#register:1",
                file=provider_path,
                diff_snippet=["REVIEWABLE_PROVIDER_PACK_SENTINEL"],
            ),
        ],
    )

    prompt = build_resolution_prompt(finding, previous_pack, report)

    assert "REVIEWABLE_PROVIDER_DIFF_SENTINEL" in prompt
    assert "REVIEWABLE_PROVIDER_PACK_SENTINEL" in prompt
    assert "IGNORED_DOC_SENTINEL" not in prompt
    assert "IGNORED_DOC_PACK_SENTINEL" not in prompt


def test_resolution_prompt_sanitizes_warnings_and_fails_closed_when_incomplete(
    tmp_path: Path,
) -> None:
    previous_pack = ContextPack(id="src/main.ts#target:1", file="src/main.ts")
    finding = Finding(
        title="Warning-sensitive resolution",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/main.ts",
        failure_mode="Analyzer failures could hide the old issue.",
        evidence="The prior review reported an analyzer warning.",
        suggested_fix="Repair the issue and restore complete analysis.",
        suggested_test="Run the analyzer successfully.",
        context_pack_id=previous_pack.id,
    )
    bearer_secret = "bearer-secret-value"
    api_secret = "sk-proj-1234567890abcdef"
    json_secret = "TOPSECRET123"
    env_secret = "TOPSECRET456"
    github_secret = "ghp_1234567890abcdefghijklmnop"
    slack_secret = "xox" + "b-1234567890-abcdefghijklmnop"
    jwt_secret = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123"
    warnings = [
        (
            f"Analyzer log {tmp_path}/private/trace.log and /builds/company/private-repo/job.log; "
            f"Authorization: Bearer {bearer_secret}; api_key={api_secret}; "
            f'{{"api_key":"{json_secret}"}}; OPENAI_API_KEY={env_secret}; '
            f"github={github_secret}; slack={slack_secret}; jwt={jwt_secret}"
        ),
        "OVERSIZED_WARNING_SENTINEL_" + ("w" * 2_000),
        *[f"warning-{index}" for index in range(8)],
    ]
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(target_mode=TargetMode.PATCH, warnings=warnings),
    )

    prompt = build_resolution_prompt(finding, previous_pack, report)

    assert str(tmp_path) not in prompt
    assert bearer_secret not in prompt
    assert api_secret not in prompt
    assert json_secret not in prompt
    assert env_secret not in prompt
    assert github_secret not in prompt
    assert slack_secret not in prompt
    assert jwt_secret not in prompt
    assert "/builds/company/private-repo/job.log" not in prompt
    assert "<repo>" in prompt
    assert "<absolute-path>" in prompt
    assert "[REDACTED]" in prompt
    assert "OVERSIZED_WARNING_SENTINEL" in prompt
    assert '"global_diff_warnings_count": 10' in prompt
    assert '"omitted_global_diff_warnings": 2' in prompt
    assert '"truncated_global_diff_warnings": 1' in prompt
    assert '"diff_warning_evidence_incomplete": true' in prompt
    assert "When diff_warning_evidence_incomplete is true, return `uncertain` unconditionally." in prompt


def test_resolution_prompt_bounds_oversized_warning_before_sanitization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_pack = ContextPack(id="src/main.ts#target:1", file="src/main.ts")
    finding = Finding(
        title="Oversized warning",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/main.ts",
        failure_mode="Incomplete analyzer output could hide the old issue.",
        evidence="The prior review reported an analyzer warning.",
        suggested_fix="Restore complete analyzer output.",
        suggested_test="Run the analyzer successfully.",
        context_pack_id=previous_pack.id,
    )
    sanitizer_input_lengths: list[int] = []

    def recording_sanitizer(value: str, root: str) -> str:
        sanitizer_input_lengths.append(len(value))
        return value

    monkeypatch.setattr(
        "apex_ray.report.sarif.sanitize_external_text",
        recording_sanitizer,
    )
    report = build_report(
        ProjectProfile(root=str(tmp_path), is_git_repo=True),
        ReviewConfig(),
        DiffSummary(
            target_mode=TargetMode.PATCH,
            warnings=["OVERSIZED_WARNING_SENTINEL_" + ("w" * 5_000_000)],
        ),
    )

    prompt = build_resolution_prompt(finding, previous_pack, report)

    assert sanitizer_input_lengths == [1_024]
    assert "OVERSIZED_WARNING_SENTINEL" in prompt
    assert '"truncated_global_diff_warnings": 1' in prompt
    assert '"diff_warning_evidence_incomplete": true' in prompt


def test_resolution_prompt_prioritizes_primary_pack_over_cross_file_line_matches(
    tmp_path: Path,
) -> None:
    related_test_paths = [f"a/related-{index}.test.ts" for index in range(8)]
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
            id=f"{path}#target:{index + 2}",
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


def test_resolution_prompt_propagates_identity_across_rename_but_not_copy(
    tmp_path: Path,
) -> None:
    related_paths = [f"a/related-{index}.ts" for index in range(8)]
    previous_pack = ContextPack(
        id="src/old.ts#cluster:validate+save:1",
        file="src/old.ts",
        related_tests=related_paths,
    )
    finding = Finding(
        title="Renamed implementation finding",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file=previous_pack.file,
        failure_mode="The old implementation violates the invariant.",
        evidence="The validate/save cluster has unsafe behavior.",
        suggested_fix="Repair or move the cluster safely.",
        suggested_test="Cover the cluster after the move.",
        context_pack_id=previous_pack.id,
    )
    colliding_packs = [
        ContextPack(
            id=f"{path}#validate:{index + 2}",
            file=path,
            diff_snippet=[f"COLLIDING_PACK_SENTINEL_{index}"],
        )
        for index, path in enumerate(related_paths)
    ]

    def render(status: FileStatus, target_path: str, sentinel: str) -> str:
        report = build_report(
            ProjectProfile(root=str(tmp_path), is_git_repo=True),
            ReviewConfig(),
            DiffSummary(
                target_mode=TargetMode.PATCH,
                files=[
                    ChangedFile(
                        old_path=previous_pack.file,
                        new_path=target_path,
                        status=status,
                    )
                ],
            ),
            context_packs=[
                *colliding_packs,
                ContextPack(
                    id=f"{target_path}#cluster:validate+save:50",
                    file=target_path,
                    diff_snippet=[sentinel],
                ),
            ],
        )
        return build_resolution_prompt(finding, previous_pack, report)

    renamed_prompt = render(
        FileStatus.RENAMED,
        "z/renamed.ts",
        "RENAMED_LINEAGE_PACK_SENTINEL",
    )
    copied_prompt = render(
        FileStatus.COPIED,
        "z/copied.ts",
        "COPIED_LINEAGE_PACK_SENTINEL",
    )

    assert "RENAMED_LINEAGE_PACK_SENTINEL" in renamed_prompt
    assert "COPIED_LINEAGE_PACK_SENTINEL" not in copied_prompt
