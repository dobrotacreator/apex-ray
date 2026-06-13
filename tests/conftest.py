import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TS_ANALYZER = ROOT / "analyzer-runtimes" / "typescript"


@pytest.fixture(scope="session")
def built_ts_analyzer() -> None:
    if shutil.which("npm") is None:
        pytest.skip("npm is required for the TypeScript analyzer tests")
    with _ts_analyzer_build_lock():
        if _needs_ts_analyzer_install():
            subprocess.run(["npm", "ci"], cwd=TS_ANALYZER, check=True, capture_output=True, text=True)
        if _needs_ts_analyzer_build():
            subprocess.run(["npm", "run", "build"], cwd=TS_ANALYZER, check=True, capture_output=True, text=True)


@contextmanager
def _ts_analyzer_build_lock() -> Iterator[None]:
    lock_name = sha256(str(TS_ANALYZER.resolve()).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"apex-ray-ts-analyzer-{lock_name}.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            import fcntl
        except ImportError:
            yield
            return

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _needs_ts_analyzer_install() -> bool:
    required_files = [
        TS_ANALYZER / "node_modules" / "typescript" / "lib" / "typescript.js",
        TS_ANALYZER / "node_modules" / "@types" / "node" / "package.json",
    ]
    return any(not path.exists() for path in required_files)


def _needs_ts_analyzer_build() -> bool:
    dist = TS_ANALYZER / "dist" / "analyze.js"
    if not dist.exists():
        return True
    dist_mtime = dist.stat().st_mtime
    source_paths = [TS_ANALYZER / "tsconfig.json", TS_ANALYZER / "package.json", TS_ANALYZER / "package-lock.json"]
    source_paths.extend((TS_ANALYZER / "src").rglob("*.ts"))
    return any(path.exists() and path.stat().st_mtime > dist_mtime for path in source_paths)


@pytest.fixture(autouse=True)
def isolated_apex_ray_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_RAY_CACHE_HOME", str(tmp_path / "apex-ray-cache"))
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
        monkeypatch.delenv(name, raising=False)
