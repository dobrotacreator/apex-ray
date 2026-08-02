from pathlib import PurePath, PurePosixPath

from .constants import DART_GENERATED_SUFFIXES

_DART_GENERATED_DIRECTORY_NAMES = frozenset({"generated", "__generated__", "build", "dist"})


def is_generated_dart_path(path: str | PurePath) -> bool:
    """Return whether *path* uses a known generated suffix or directory.

    The policy mirrors project classification for generated directories while
    keeping suffix matching conservative. A broad match such as ``*.gen.dart``
    would hide handwritten files in existing projects.
    """

    normalized = str(path).replace("\\", "/").casefold()
    directory_parts = PurePosixPath(normalized).parts[:-1]
    return normalized.endswith(DART_GENERATED_SUFFIXES) or any(
        part in _DART_GENERATED_DIRECTORY_NAMES for part in directory_parts
    )
