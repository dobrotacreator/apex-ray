from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TS_ANALYZER = ROOT / "analyzers" / "typescript"


@pytest.fixture(scope="session")
def built_ts_analyzer() -> None:
    if shutil.which("npm") is None:
        pytest.skip("npm is required for the TypeScript analyzer tests")
    subprocess.run(["npm", "install"], cwd=TS_ANALYZER, check=True, capture_output=True, text=True)
    subprocess.run(["npm", "run", "build"], cwd=TS_ANALYZER, check=True, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def isolated_apex_ray_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_RAY_CACHE_HOME", str(tmp_path / "apex-ray-cache"))
