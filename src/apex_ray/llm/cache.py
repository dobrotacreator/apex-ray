import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from apex_ray.memory import pack_prompt_payload
from apex_ray.models import (
    ContextPack,
    Finding,
    FindingResponse,
    FindingVerification,
    LLMConfig,
    LLMProviderName,
    VerificationResponse,
)

LLM_CACHE_VERSION = "apex-ray-llm-cache-v1"
REVIEW_PROMPT_VERSION = "review-v8"
REVIEW_SHALLOW_PROMPT_VERSION = "review-shallow-v1"
VERIFIER_PROMPT_VERSION = "verify-v8"
DEFAULT_CACHE_DIR = ".apex-ray/cache/llm"


class LLMCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def read_review(self, key: str) -> list[Finding] | None:
        raw = self._read_entry(key, "review")
        if raw is None:
            return None
        try:
            return FindingResponse.model_validate(raw["response"]).findings
        except KeyError, TypeError, ValidationError:
            return None

    def write_review(
        self,
        key: str,
        config: LLMConfig,
        findings: list[Finding],
    ) -> None:
        self._write_entry(
            key,
            {
                "version": LLM_CACHE_VERSION,
                "kind": "review",
                "key": key,
                "provider": config.provider,
                "model": config.model,
                "prompt_version": review_prompt_version(config),
                "created_at": _now_iso(),
                "response": FindingResponse(findings=findings).model_dump(mode="json"),
            },
        )

    def read_verification(self, key: str, finding: Finding) -> FindingVerification | None:
        raw = self._read_entry(key, "verify")
        if raw is None:
            return None
        try:
            response = VerificationResponse.model_validate(raw["response"])
        except KeyError, TypeError, ValidationError:
            return None
        return FindingVerification(
            finding=finding,
            approved=response.approved,
            confidence=response.confidence,
            reason=response.reason,
        )

    def write_verification(
        self,
        key: str,
        config: LLMConfig,
        verification: FindingVerification,
    ) -> None:
        self._write_entry(
            key,
            {
                "version": LLM_CACHE_VERSION,
                "kind": "verify",
                "key": key,
                "provider": config.provider,
                "model": config.model,
                "prompt_version": VERIFIER_PROMPT_VERSION,
                "created_at": _now_iso(),
                "response": {
                    "approved": verification.approved,
                    "confidence": verification.confidence,
                    "reason": verification.reason,
                },
            },
        )

    def _read_entry(self, key: str, kind: Literal["review", "verify"]) -> dict[str, Any] | None:
        path = self._path_for_key(key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("version") != LLM_CACHE_VERSION or raw.get("kind") != kind or raw.get("key") != key:
            return None
        return raw

    def _write_entry(self, key: str, raw: dict[str, Any]) -> None:
        path = self._path_for_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    def _path_for_key(self, key: str) -> Path:
        return self.root / f"{key}.json"


def cache_for_config(repo_root: Path, config: LLMConfig) -> LLMCache | None:
    if not config.cache_enabled:
        return None
    if config.provider == LLMProviderName.FAKE and config.cache_dir is None:
        return None
    return LLMCache(resolve_cache_dir(repo_root, config))


def resolve_cache_dir(repo_root: Path, config: LLMConfig) -> Path:
    configured = Path(config.cache_dir) if config.cache_dir else Path(DEFAULT_CACHE_DIR)
    if configured.is_absolute():
        return configured
    return repo_root / configured


def review_cache_key(pack: ContextPack, config: LLMConfig) -> str:
    depth = config.review_depth
    return _cache_key(
        {
            "kind": "review",
            "cache_version": LLM_CACHE_VERSION,
            "prompt_version": review_prompt_version(config),
            "review_depth": depth,
            "provider": config.provider,
            "model": config.model,
            "pack": pack_prompt_payload(pack, "review", depth=depth),
        }
    )


def verification_cache_key(finding: Finding, pack: ContextPack, config: LLMConfig) -> str:
    return _cache_key(
        {
            "kind": "verify",
            "cache_version": LLM_CACHE_VERSION,
            "prompt_version": VERIFIER_PROMPT_VERSION,
            "provider": config.provider,
            "model": config.model,
            "finding": finding.model_dump(mode="json"),
            "pack": pack_prompt_payload(pack, "verify"),
        }
    )


def _cache_key(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_prompt_version(config: LLMConfig) -> str:
    if config.review_depth == "shallow":
        return REVIEW_SHALLOW_PROMPT_VERSION
    return REVIEW_PROMPT_VERSION


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
