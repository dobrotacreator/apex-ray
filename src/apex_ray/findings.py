import hashlib
import json
import re
from typing import Any

from apex_ray.models import ContextPack, Finding

_TOKEN_RE = re.compile(r"\s+")


def finding_fingerprint(finding: Finding) -> str:
    payload = "|".join(
        [
            _normalize_path(finding.file),
            str(finding.line or ""),
            _compact_text(finding.title).lower(),
            _compact_text(finding.failure_mode).lower()[:500],
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"apex-{digest}"


def context_pack_fingerprint(pack: ContextPack | None) -> str:
    if pack is None:
        return ""
    return payload_fingerprint(pack.model_dump(mode="json"))


def payload_fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").removeprefix("./")


def _compact_text(value: str) -> str:
    return _TOKEN_RE.sub(" ", value.strip())
