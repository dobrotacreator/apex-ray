import importlib
import importlib.metadata

import apex_ray


def test_version_falls_back_when_package_metadata_is_unavailable(monkeypatch) -> None:
    original_version = importlib.metadata.version

    def missing_version(package_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(importlib.metadata, "version", missing_version)
    reloaded = importlib.reload(apex_ray)

    assert reloaded.__version__ == "0+unknown"

    monkeypatch.setattr(importlib.metadata, "version", original_version)
    importlib.reload(apex_ray)
