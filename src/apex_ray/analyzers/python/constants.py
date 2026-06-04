import re

PYTHON_LANGUAGES = {"python"}
PYTHON_READ_ERRORS = (OSError, UnicodeDecodeError, SyntaxError)
PYTHON_RELATED_TEST_LIMIT = 10
PYTHON_REFERENCE_LIMIT = 24
PYTHON_CALLEE_LIMIT = 16
PYTHON_WORKSPACE_FILE_LIMIT = 4000
PYTHON_WORKSPACE_FILE_SIZE_LIMIT = 1_000_000
PYTHON_LITERAL_ANNOTATION_HEADS = {"Literal", "typing.Literal", "typing_extensions.Literal"}
PYTHON_ANNOTATED_ANNOTATION_HEADS = {"Annotated", "typing.Annotated", "typing_extensions.Annotated"}
PYTHON_TYPE_CHECKING_IDENTITIES = {"TYPE_CHECKING", "typing.TYPE_CHECKING"}
PYTHON_SCAN_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "site-packages",
}
PYTHON_DELETED_SYMBOL_RE = re.compile(
    r"^(?P<indent>\s*)(?P<definition_kind>async\s+def|def|class)\s+(?P<name>[A-Za-z_]\w*)"
)
