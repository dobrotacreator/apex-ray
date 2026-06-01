import os
import sys
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol, TextIO


class ProgressSink(Protocol):
    def event(self, message: str, *, key: str | None = None, force: bool = False) -> None: ...


@dataclass
class NoopProgress:
    def event(self, message: str, *, key: str | None = None, force: bool = False) -> None:
        return None


@dataclass
class StreamProgress:
    stream: TextIO = field(default_factory=lambda: sys.stderr)
    prefix: str = "apex-ray"
    interval_seconds: float = 5.0
    _last_by_key: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def event(self, message: str, *, key: str | None = None, force: bool = False) -> None:
        with self._lock:
            if not force and key is not None and self.interval_seconds > 0:
                now = time.monotonic()
                last = self._last_by_key.get(key)
                if last is not None and now - last < self.interval_seconds:
                    return
                self._last_by_key[key] = now
            print(f"{self.prefix}: {message}", file=self.stream, flush=True)


def progress_enabled(mode: str, *, env: dict[str, str] | None = None) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    effective_env = os.environ if env is None else env
    return not _env_truthy(effective_env.get("CI"))


def _env_truthy(value: str | None) -> bool:
    return value is not None and value.lower() not in {"", "0", "false", "no", "off"}
