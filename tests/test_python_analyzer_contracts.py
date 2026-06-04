from pathlib import Path

import pytest

from apex_ray.analyzers import run_python_analyzer
from apex_ray.models import AnalyzerFile, AnalyzerSymbol, ChangedFile, FileKind


def _changed_python_file(path: str, *, new_start: int) -> ChangedFile:
    return ChangedFile(
        old_path=path,
        new_path=path,
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": new_start,
                "old_lines": 1,
                "new_start": new_start,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return before"},
                    {"kind": "add", "content": "    return after"},
                ],
            }
        ],
    )


def _changed_symbol(analyzed_file: AnalyzerFile, name: str) -> AnalyzerSymbol:
    return next(symbol for symbol in analyzed_file.changed_symbols if symbol.name == name)


def _contracts_for_symbol(result_file_symbol: AnalyzerSymbol) -> list[tuple[str, str, str]]:
    return [(contract.file, contract.kind, contract.text) for contract in result_file_symbol.contracts]


@pytest.mark.parametrize(
    "signature",
    [
        "def create_resource(payload: ResourceCreate) -> ResourceRead:",
        "def create_resource(payload: 'ResourceCreate') -> 'ResourceRead':",
        "def create_resource(payload: list['ResourceCreate']) -> list['ResourceRead']:",
    ],
)
def test_python_analyzer_resolves_function_annotation_contracts_to_imported_models(
    tmp_path: Path,
    signature: str,
) -> None:
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "schemas.py").write_text(
        "from pydantic import BaseModel\n\n"
        "class ResourceCreate(BaseModel):\n"
        "    name: str\n\n"
        "class ResourceRead(BaseModel):\n"
        "    id: str\n"
        "    name: str\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "api" / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "from .schemas import ResourceCreate, ResourceRead\n\n"
        "router = APIRouter()\n\n"
        "@router.post('/resources', response_model=ResourceRead)\n"
        f"{signature}\n"
        "    return ResourceRead(id='resource-1', name=payload.name.strip())\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/api/routes.py",
        new_path="src/api/routes.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 8,
                "old_lines": 1,
                "new_start": 8,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return ResourceRead(id='resource-1', name=payload.name)"},
                    {
                        "kind": "add",
                        "content": "    return ResourceRead(id='resource-1', name=payload.name.strip())",
                    },
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    route_symbol = result.files[0].changed_symbols[0]
    assert [(contract.file, contract.kind, contract.text) for contract in route_symbol.contracts] == [
        ("src/api/schemas.py", "contract", "class ResourceCreate(BaseModel)"),
        ("src/api/schemas.py", "contract", "class ResourceRead(BaseModel)"),
    ]


def test_python_analyzer_ignores_literal_and_annotated_metadata_strings(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text(
        "from typing import Annotated, Literal\n\n"
        "class ResourceCreate:\n"
        "    pass\n\n"
        "def parse_status(kind: Literal['ResourceCreate']) -> Annotated[int, 'ResourceCreate']:\n"
        "    return 1\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/service.py",
        new_path="src/service.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 7,
                "old_lines": 1,
                "new_start": 7,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return 0"},
                    {"kind": "add", "content": "    return 1"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    parse_symbol = result.files[0].changed_symbols[0]
    assert parse_symbol.contracts == []


@pytest.mark.parametrize(
    ("imports", "signature"),
    [
        (
            "from typing import Annotated as Ann, Literal as Lit",
            "def create_resource(payload: Ann['ResourceCreate', 'ResourceRead'], kind: Lit['ResourceRead']) -> None:",
        ),
        (
            "import typing as t",
            "def create_resource("
            "payload: t.Annotated['ResourceCreate', 'ResourceRead'], kind: t.Literal['ResourceRead']"
            ") -> None:",
        ),
    ],
)
def test_python_analyzer_resolves_special_annotation_aliases(
    tmp_path: Path,
    imports: str,
    signature: str,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "routes.py").write_text(
        f"{imports}\n\n"
        "class ResourceCreate:\n"
        "    pass\n\n"
        "class ResourceRead:\n"
        "    pass\n\n"
        f"{signature}\n"
        "    return None\n",
        encoding="utf-8",
    )

    result = run_python_analyzer(tmp_path, [_changed_python_file("src/routes.py", new_start=10)])

    assert result is not None
    route_symbol = result.files[0].changed_symbols[0]
    assert _contracts_for_symbol(route_symbol) == [
        ("src/routes.py", "contract", "class ResourceCreate"),
    ]


def test_python_analyzer_resolves_type_checking_annotation_imports(
    tmp_path: Path,
) -> None:
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "schemas.py").write_text(
        "class ResourceCreate:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "api" / "routes.py").write_text(
        "from typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n"
        "    from .schemas import ResourceCreate\n\n"
        "def create_resource(payload: 'ResourceCreate') -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )
    changed = ChangedFile(
        old_path="src/api/routes.py",
        new_path="src/api/routes.py",
        language="python",
        file_kind=FileKind.SOURCE,
        hunks=[
            {
                "old_start": 7,
                "old_lines": 1,
                "new_start": 7,
                "new_lines": 1,
                "lines": [
                    {"kind": "delete", "content": "    return payload"},
                    {"kind": "add", "content": "    return None"},
                ],
            }
        ],
    )

    result = run_python_analyzer(tmp_path, [changed])

    assert result is not None
    route_symbol = result.files[0].changed_symbols[0]
    assert [(contract.file, contract.kind, contract.text) for contract in route_symbol.contracts] == [
        ("src/api/schemas.py", "contract", "class ResourceCreate")
    ]


@pytest.mark.parametrize(
    "guard",
    [
        "import typing as t\n\nif t.TYPE_CHECKING:",
        "from typing import TYPE_CHECKING as TC\n\nif TC:",
    ],
)
def test_python_analyzer_resolves_aliased_type_checking_annotation_imports(
    tmp_path: Path,
    guard: str,
) -> None:
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "schemas.py").write_text(
        "class ResourceCreate:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "api" / "routes.py").write_text(
        f"{guard}\n"
        "    from .schemas import ResourceCreate\n\n"
        "def create_resource(payload: 'ResourceCreate') -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )

    result = run_python_analyzer(tmp_path, [_changed_python_file("src/api/routes.py", new_start=7)])

    assert result is not None
    route_symbol = result.files[0].changed_symbols[0]
    assert _contracts_for_symbol(route_symbol) == [
        ("src/api/schemas.py", "contract", "class ResourceCreate"),
    ]


def test_python_analyzer_resolves_annotations_through_dotted_module_imports(tmp_path: Path) -> None:
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "schemas.py").write_text(
        "class ResourceCreate:\n    pass\n\nclass ResourceRead:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "api" / "routes.py").write_text(
        "import api.schemas\n\n"
        "def create_resource(payload: api.schemas.ResourceCreate) -> api.schemas.ResourceRead:\n"
        "    return api.schemas.ResourceRead()\n",
        encoding="utf-8",
    )

    result = run_python_analyzer(tmp_path, [_changed_python_file("src/api/routes.py", new_start=4)])

    assert result is not None
    route_symbol = result.files[0].changed_symbols[0]
    assert _contracts_for_symbol(route_symbol) == [
        ("src/api/schemas.py", "contract", "class ResourceCreate"),
        ("src/api/schemas.py", "contract", "class ResourceRead"),
    ]


def test_python_analyzer_does_not_treat_literal_strings_as_forward_annotation_contracts(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "routes.py").write_text(
        "from typing import Literal\n\n"
        "class ResourceCreate:\n"
        "    pass\n\n"
        "def create_resource(kind: Literal['ResourceCreate']) -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )

    result = run_python_analyzer(tmp_path, [_changed_python_file("src/routes.py", new_start=7)])

    assert result is not None
    route_symbol = result.files[0].changed_symbols[0]
    assert _contracts_for_symbol(route_symbol) == []


def test_python_analyzer_annotation_contracts_follow_top_level_shadowing(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "schemas.py").write_text(
        "class ResourceCreate:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "routes.py").write_text(
        "from schemas import ResourceCreate\n\n"
        "class ResourceCreate:\n"
        "    pass\n\n"
        "def create_resource(payload: ResourceCreate) -> ResourceCreate:\n"
        "    return payload\n",
        encoding="utf-8",
    )

    result = run_python_analyzer(tmp_path, [_changed_python_file("src/routes.py", new_start=7)])

    assert result is not None
    route_symbol = result.files[0].changed_symbols[0]
    assert _contracts_for_symbol(route_symbol) == [
        ("src/routes.py", "contract", "class ResourceCreate"),
    ]


def test_python_analyzer_resolves_unqualified_nested_class_annotation_contracts(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "routes.py").write_text(
        "class ResourceController:\n"
        "    class Payload:\n"
        "        pass\n\n"
        "    def create_resource(self, payload: Payload) -> Payload:\n"
        "        return payload\n",
        encoding="utf-8",
    )

    result = run_python_analyzer(tmp_path, [_changed_python_file("src/routes.py", new_start=6)])

    assert result is not None
    route_symbol = _changed_symbol(result.files[0], "ResourceController.create_resource")
    assert _contracts_for_symbol(route_symbol) == [
        ("src/routes.py", "contract", "class Payload"),
    ]
