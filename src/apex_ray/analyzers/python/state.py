import ast
from dataclasses import dataclass

from apex_ray.models import AnalyzerSymbol


@dataclass(frozen=True, slots=True)
class _PythonWorkspaceFile:
    path: str
    module_name: str
    source: str
    module: ast.Module
    symbols: list[AnalyzerSymbol]


@dataclass(frozen=True, slots=True)
class _PythonWorkspaceScan:
    files: dict[str, _PythonWorkspaceFile]
    warnings: list[str]
    partial: bool


@dataclass(frozen=True, slots=True)
class _PythonIndexedSymbol:
    path: str
    module_name: str
    identity: str
    symbol: AnalyzerSymbol


@dataclass(frozen=True, slots=True)
class _PythonImportBindings:
    direct_imports: dict[str, set[str]]
    module_imports: dict[str, str]


@dataclass(frozen=True, slots=True)
class _PythonCallContext:
    bindings: _PythonImportBindings
    instance_types: dict[str, str]


@dataclass(frozen=True, slots=True)
class _PythonCallSite:
    call: ast.Call
    context: _PythonCallContext


@dataclass(frozen=True, slots=True)
class _PythonAnnotationContext:
    bindings: _PythonImportBindings
    scope_prefixes: list[str]
