"""Apex Ray local code review engine."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("apex-ray")
except PackageNotFoundError:
    __version__ = "0+unknown"
