import json

from pydantic import ValidationError

from apex_ray.llm.errors import LLMProviderError
from apex_ray.models import (
    Finding,
    FindingResolution,
    FindingResolutionResponse,
    FindingResponse,
    FindingVerification,
    VerificationBatchResponse,
    VerificationResponse,
)


def finding_response_schema() -> dict[str, object]:
    finding_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "file": {"type": "string"},
            "line": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "failure_mode": {"type": "string"},
            "evidence": {"type": "string"},
            "suggested_fix": {"type": "string"},
            "suggested_test": {"type": "string"},
            "context_pack_id": {"type": "string"},
        },
        "required": [
            "title",
            "severity",
            "confidence",
            "file",
            "line",
            "failure_mode",
            "evidence",
            "suggested_fix",
            "suggested_test",
            "context_pack_id",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "findings": {
                "type": "array",
                "items": finding_schema,
            }
        },
        "required": ["findings"],
    }


def verification_response_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "approved": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["approved", "confidence", "reason"],
    }


def verification_batch_response_schema() -> dict[str, object]:
    decision_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "finding_index": {"type": "integer", "minimum": 0},
            "approved": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["finding_index", "approved", "confidence", "reason"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decisions": {
                "type": "array",
                "items": decision_schema,
            }
        },
        "required": ["decisions"],
    }


def resolution_response_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["resolved", "still_present", "uncertain"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
            "evidence": {"type": "string"},
            "suggested_next_action": {"type": "string"},
        },
        "required": ["status", "confidence", "reason", "evidence", "suggested_next_action"],
    }


def parse_finding_response(text: str, context_pack_id: str) -> FindingResponse:
    raw = _load_json_response(text, "finding")

    try:
        response = FindingResponse.model_validate(raw)
    except ValidationError as exc:
        raise LLMProviderError(f"Invalid finding response: {exc}") from exc

    return FindingResponse(
        findings=[
            finding.model_copy(update={"context_pack_id": finding.context_pack_id or context_pack_id})
            for finding in response.findings
        ]
    )


def parse_verification_response(text: str, finding: Finding) -> FindingVerification:
    raw = _load_json_response(text, "verification")

    try:
        response = VerificationResponse.model_validate(raw)
    except ValidationError as exc:
        raise LLMProviderError(f"Invalid verification response: {exc}") from exc

    return FindingVerification(
        finding=finding,
        approved=response.approved,
        confidence=response.confidence,
        reason=response.reason,
    )


def parse_verification_batch_response(text: str, findings: list[Finding]) -> list[FindingVerification]:
    raw = _load_json_response(text, "verification")

    try:
        response = VerificationBatchResponse.model_validate(raw)
    except ValidationError as exc:
        raise LLMProviderError(f"Invalid verification response: {exc}") from exc

    decisions_by_index = {}
    for decision in response.decisions:
        if decision.finding_index >= len(findings):
            raise LLMProviderError(f"Verifier returned out-of-range finding_index: {decision.finding_index}")
        if decision.finding_index in decisions_by_index:
            raise LLMProviderError(f"Verifier returned duplicate finding_index: {decision.finding_index}")
        decisions_by_index[decision.finding_index] = decision

    expected_indexes = set(range(len(findings)))
    missing_indexes = expected_indexes - set(decisions_by_index)
    if missing_indexes:
        missing = ", ".join(str(index) for index in sorted(missing_indexes))
        raise LLMProviderError(f"Verifier omitted decisions for finding indexes: {missing}")

    return [
        FindingVerification(
            finding=finding,
            approved=decisions_by_index[index].approved,
            confidence=decisions_by_index[index].confidence,
            reason=decisions_by_index[index].reason,
        )
        for index, finding in enumerate(findings)
    ]


def parse_resolution_response(text: str, finding: Finding) -> FindingResolution:
    raw = _load_json_response(text, "resolution")

    try:
        response = FindingResolutionResponse.model_validate(raw)
    except ValidationError as exc:
        raise LLMProviderError(f"Invalid resolution response: {exc}") from exc

    return FindingResolution(
        finding=finding,
        status=response.status,
        confidence=response.confidence,
        reason=response.reason,
        evidence=response.evidence,
        suggested_next_action=response.suggested_next_action,
    )


def extract_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMProviderError("LLM response did not contain a JSON object.")
    return text[start : end + 1]


def _load_json_response(text: str, response_kind: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        extracted = extract_json_object(text)

    try:
        return json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise LLMProviderError(
            f"LLM {response_kind} response contained invalid JSON: "
            f"{exc.msg} at line {exc.lineno} column {exc.colno} (char {exc.pos})."
        ) from exc
