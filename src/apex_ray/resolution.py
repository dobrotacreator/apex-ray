from apex_ray.models import ChangedFile, ContextPack

_NON_REVIEWABLE_RESOLUTION_FILE_KINDS = frozenset(
    {
        "docs",
        "generated",
        "lockfile",
        "vendored",
    }
)


def resolution_file_is_reviewable(changed_file: ChangedFile) -> bool:
    return not changed_file.is_ignored and str(changed_file.file_kind) not in _NON_REVIEWABLE_RESOLUTION_FILE_KINDS


def novel_resolution_file_is_reviewable(changed_file: ChangedFile) -> bool:
    status = str(changed_file.status)
    if changed_file.old_path is None and changed_file.new_path is not None:
        status = "added"
    return resolution_file_is_reviewable(changed_file) and status in {
        "added",
        "copied",
        "renamed",
    }


def context_pack_resolution_paths(pack: ContextPack) -> set[str]:
    return {
        *pack.related_tests,
        *(
            reference.file
            for reference in [
                *pack.references,
                *pack.callees,
                *pack.contracts,
                *pack.metadata,
            ]
            if reference.file
        ),
        *(
            snippet.file
            for snippet in [
                *pack.reference_snippets,
                *pack.callee_snippets,
                *pack.contract_snippets,
                *pack.metadata_snippets,
                *pack.related_test_snippets,
            ]
            if snippet.file
        ),
    }
