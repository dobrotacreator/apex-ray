from typing import Final

DART_GENERATED_SUFFIXES: Final[tuple[str, ...]] = (
    ".chopper.dart",
    ".config.dart",
    ".freezed.dart",
    ".g.dart",
    ".gr.dart",
    ".mocks.dart",
)

DART_DIRECTIVE_SOURCE_CHAR_LIMIT: Final = 262_144
DART_DIRECTIVE_LIMIT: Final = 256
DART_DIRECTIVE_CHAR_LIMIT: Final = 8_192
DART_RELATED_TEST_FILE_LIMIT: Final = 10_000
DART_RELATED_TEST_FILE_SIZE_LIMIT: Final = 512_000
DART_PLATFORM_CHANNEL_FILE_LIMIT: Final = 5_000
DART_PLATFORM_CHANNEL_FILE_SIZE_LIMIT: Final = 512_000
DART_PLATFORM_CHANNEL_ENDPOINT_LIMIT: Final = 1_000
DART_METADATA_LIMIT: Final = 64

DART_SCAN_IGNORED_DIRS: Final[frozenset[str]] = frozenset(
    {
        ".dart_tool",
        ".fvm",
        ".git",
        ".hg",
        ".idea",
        ".pub-cache",
        ".svn",
        ".vscode",
        "build",
        "coverage",
        "node_modules",
    }
)

PLATFORM_CHANNEL_LANGUAGES: Final[dict[str, str]] = {
    ".dart": "dart",
    ".java": "java",
    ".kt": "kotlin",
    ".m": "objective-c",
    ".mm": "objective-c++",
    ".swift": "swift",
}
