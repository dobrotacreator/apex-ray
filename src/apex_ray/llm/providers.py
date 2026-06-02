import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from apex_ray.llm.cli import (
    build_claude_command,
    build_codex_command,
    claude_result_text,
    resolve_claude_path,
    resolve_codex_path,
)
from apex_ray.llm.errors import LLMProviderError
from apex_ray.llm.prompts import (
    build_resolution_prompt,
    build_review_prompt,
    build_shallow_review_prompt,
    build_verifier_batch_prompt,
)
from apex_ray.llm.responses import (
    finding_response_schema,
    parse_finding_response,
    parse_resolution_response,
    parse_verification_batch_response,
    resolution_response_schema,
    verification_batch_response_schema,
)
from apex_ray.llm.usage import parse_claude_usage_from_json, parse_codex_usage_from_jsonl
from apex_ray.models import (
    ContextPack,
    Finding,
    FindingConfidence,
    FindingResolution,
    FindingResolutionStatus,
    FindingVerification,
    LLMConfig,
    LLMProviderName,
    LLMReviewResult,
    LLMVerificationResult,
    ReviewReport,
)


class LLMProvider(Protocol):
    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]: ...

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification: ...

    def resolve_finding(
        self,
        finding: Finding,
        previous_pack: ContextPack | None,
        delta_report: ReviewReport,
        repo_root: Path,
    ) -> FindingResolution: ...


class FakeLLMProvider:
    def __init__(
        self,
        findings: list[Finding] | None = None,
        verification_approvals: list[bool] | None = None,
        resolution_statuses: list[FindingResolutionStatus | str] | None = None,
    ) -> None:
        self.findings = findings or []
        self.verification_approvals = verification_approvals or []
        self.resolution_statuses = resolution_statuses or []
        self.reviewed_pack_ids: list[str] = []
        self.verified_batch_pack_ids: list[str] = []
        self.verified_batches: list[list[str]] = []
        self.verified_finding_titles: list[str] = []
        self.resolved_finding_titles: list[str] = []

    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
        self.reviewed_pack_ids.append(pack.id)
        return [finding.model_copy(update={"context_pack_id": pack.id}) for finding in self.findings]

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
        self.verified_finding_titles.append(finding.title)
        approval_index = len(self.verified_finding_titles) - 1
        approved = (
            self.verification_approvals[approval_index] if approval_index < len(self.verification_approvals) else True
        )
        return FindingVerification(
            finding=finding,
            approved=approved,
            confidence=FindingConfidence.HIGH,
            reason="Fake verifier approved the finding." if approved else "Fake verifier rejected the finding.",
        )

    def verify_findings(self, findings: list[Finding], pack: ContextPack, repo_root: Path) -> list[FindingVerification]:
        self.verified_batch_pack_ids.append(pack.id)
        self.verified_batches.append([finding.title for finding in findings])
        return [self.verify_finding(finding, pack, repo_root) for finding in findings]

    def resolve_finding(
        self,
        finding: Finding,
        previous_pack: ContextPack | None,
        delta_report: ReviewReport,
        repo_root: Path,
    ) -> FindingResolution:
        self.resolved_finding_titles.append(finding.title)
        resolution_index = len(self.resolved_finding_titles) - 1
        status = (
            self.resolution_statuses[resolution_index]
            if resolution_index < len(self.resolution_statuses)
            else FindingResolutionStatus.UNCERTAIN
        )
        return FindingResolution(
            finding=finding,
            status=FindingResolutionStatus(status),
            confidence=FindingConfidence.HIGH,
            reason=f"Fake resolver returned {status}.",
        )


class CodexCLIProvider:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
        return self.review_context_pack_with_usage(pack, repo_root).findings

    def review_context_pack_with_usage(self, pack: ContextPack, repo_root: Path) -> LLMReviewResult:
        codex_path = resolve_codex_path(self.config.codex_path, repo_root)
        prompt = (
            build_shallow_review_prompt(pack) if self.config.review_depth == "shallow" else build_review_prompt(pack)
        )

        with tempfile.TemporaryDirectory(prefix="apex-ray-codex-") as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "finding_schema.json"
            output_path = tmp_path / "findings.json"
            schema_path.write_text(
                json.dumps(finding_response_schema(), indent=2),
                encoding="utf-8",
            )

            command = build_codex_command(
                codex_path=codex_path,
                schema_path=schema_path,
                output_path=output_path,
                model=self.config.model,
                effort=self.config.effort,
            )
            proc = subprocess.run(
                command,
                cwd=tmp_path,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )

            if proc.returncode != 0:
                details = proc.stderr.strip() or proc.stdout.strip()
                raise LLMProviderError(f"Codex CLI failed with exit {proc.returncode}: {details}")
            if not output_path.exists():
                raise LLMProviderError("Codex CLI did not write an output message.")

            response = parse_finding_response(output_path.read_text(encoding="utf-8"), pack.id)
            return LLMReviewResult(findings=response.findings, usage=parse_codex_usage_from_jsonl(proc.stdout))

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
        return self.verify_findings([finding], pack, repo_root)[0]

    def verify_findings(self, findings: list[Finding], pack: ContextPack, repo_root: Path) -> list[FindingVerification]:
        return self.verify_findings_with_usage(findings, pack, repo_root).verifications

    def verify_findings_with_usage(
        self, findings: list[Finding], pack: ContextPack, repo_root: Path
    ) -> LLMVerificationResult:
        if not findings:
            return LLMVerificationResult()

        codex_path = resolve_codex_path(self.config.codex_path, repo_root)
        prompt = build_verifier_batch_prompt(findings, pack)

        with tempfile.TemporaryDirectory(prefix="apex-ray-codex-verify-") as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "verification_schema.json"
            output_path = tmp_path / "verification.json"
            schema_path.write_text(
                json.dumps(verification_batch_response_schema(), indent=2),
                encoding="utf-8",
            )

            command = build_codex_command(
                codex_path=codex_path,
                schema_path=schema_path,
                output_path=output_path,
                model=self.config.model,
                effort=self.config.effort,
            )
            proc = subprocess.run(
                command,
                cwd=tmp_path,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )

            if proc.returncode != 0:
                details = proc.stderr.strip() or proc.stdout.strip()
                raise LLMProviderError(f"Codex CLI verifier failed with exit {proc.returncode}: {details}")
            if not output_path.exists():
                raise LLMProviderError("Codex CLI verifier did not write an output message.")

            return LLMVerificationResult(
                verifications=parse_verification_batch_response(output_path.read_text(encoding="utf-8"), findings),
                usage=parse_codex_usage_from_jsonl(proc.stdout),
            )

    def resolve_finding(
        self,
        finding: Finding,
        previous_pack: ContextPack | None,
        delta_report: ReviewReport,
        repo_root: Path,
    ) -> FindingResolution:
        codex_path = resolve_codex_path(self.config.codex_path, repo_root)
        prompt = build_resolution_prompt(finding, previous_pack, delta_report)

        with tempfile.TemporaryDirectory(prefix="apex-ray-codex-resolve-") as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "resolution_schema.json"
            output_path = tmp_path / "resolution.json"
            schema_path.write_text(
                json.dumps(resolution_response_schema(), indent=2),
                encoding="utf-8",
            )

            command = build_codex_command(
                codex_path=codex_path,
                schema_path=schema_path,
                output_path=output_path,
                model=self.config.model,
                effort=self.config.effort,
            )
            proc = subprocess.run(
                command,
                cwd=tmp_path,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )

            if proc.returncode != 0:
                details = proc.stderr.strip() or proc.stdout.strip()
                raise LLMProviderError(f"Codex CLI resolver failed with exit {proc.returncode}: {details}")
            if not output_path.exists():
                raise LLMProviderError("Codex CLI resolver did not write an output message.")

            return parse_resolution_response(output_path.read_text(encoding="utf-8"), finding)


class ClaudeCodeCLIProvider:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
        return self.review_context_pack_with_usage(pack, repo_root).findings

    def review_context_pack_with_usage(self, pack: ContextPack, repo_root: Path) -> LLMReviewResult:
        claude_path = resolve_claude_path(self.config.claude_path, repo_root)
        prompt = (
            build_shallow_review_prompt(pack) if self.config.review_depth == "shallow" else build_review_prompt(pack)
        )

        with tempfile.TemporaryDirectory(prefix="apex-ray-claude-") as tmp:
            tmp_path = Path(tmp)
            command = build_claude_command(
                claude_path=claude_path,
                schema=finding_response_schema(),
                model=self.config.model,
                effort=self.config.effort,
            )
            proc = subprocess.run(
                command,
                cwd=tmp_path,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )

            if proc.returncode != 0:
                details = proc.stderr.strip() or proc.stdout.strip()
                raise LLMProviderError(f"Claude Code CLI failed with exit {proc.returncode}: {details}")
            response_text = claude_result_text(proc.stdout)
            response = parse_finding_response(response_text, pack.id)
            return LLMReviewResult(findings=response.findings, usage=parse_claude_usage_from_json(proc.stdout))

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
        return self.verify_findings([finding], pack, repo_root)[0]

    def verify_findings(self, findings: list[Finding], pack: ContextPack, repo_root: Path) -> list[FindingVerification]:
        return self.verify_findings_with_usage(findings, pack, repo_root).verifications

    def verify_findings_with_usage(
        self, findings: list[Finding], pack: ContextPack, repo_root: Path
    ) -> LLMVerificationResult:
        if not findings:
            return LLMVerificationResult()

        claude_path = resolve_claude_path(self.config.claude_path, repo_root)
        prompt = build_verifier_batch_prompt(findings, pack)

        with tempfile.TemporaryDirectory(prefix="apex-ray-claude-verify-") as tmp:
            tmp_path = Path(tmp)
            command = build_claude_command(
                claude_path=claude_path,
                schema=verification_batch_response_schema(),
                model=self.config.model,
                effort=self.config.effort,
            )
            proc = subprocess.run(
                command,
                cwd=tmp_path,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )

            if proc.returncode != 0:
                details = proc.stderr.strip() or proc.stdout.strip()
                raise LLMProviderError(f"Claude Code CLI verifier failed with exit {proc.returncode}: {details}")
            response_text = claude_result_text(proc.stdout)
            return LLMVerificationResult(
                verifications=parse_verification_batch_response(response_text, findings),
                usage=parse_claude_usage_from_json(proc.stdout),
            )

    def resolve_finding(
        self,
        finding: Finding,
        previous_pack: ContextPack | None,
        delta_report: ReviewReport,
        repo_root: Path,
    ) -> FindingResolution:
        claude_path = resolve_claude_path(self.config.claude_path, repo_root)
        prompt = build_resolution_prompt(finding, previous_pack, delta_report)

        with tempfile.TemporaryDirectory(prefix="apex-ray-claude-resolve-") as tmp:
            tmp_path = Path(tmp)
            command = build_claude_command(
                claude_path=claude_path,
                schema=resolution_response_schema(),
                model=self.config.model,
                effort=self.config.effort,
            )
            proc = subprocess.run(
                command,
                cwd=tmp_path,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )

            if proc.returncode != 0:
                details = proc.stderr.strip() or proc.stdout.strip()
                raise LLMProviderError(f"Claude Code CLI resolver failed with exit {proc.returncode}: {details}")
            response_text = claude_result_text(proc.stdout)
            return parse_resolution_response(response_text, finding)


def provider_from_config(config: LLMConfig) -> LLMProvider:
    if config.provider == LLMProviderName.FAKE:
        return FakeLLMProvider()
    if config.provider == LLMProviderName.CODEX_CLI:
        return CodexCLIProvider(config)
    if config.provider == LLMProviderName.CLAUDE_CODE_CLI:
        return ClaudeCodeCLIProvider(config)
    raise LLMProviderError(f"Unsupported LLM provider: {config.provider}")


def review_context_pack_with_provider(
    provider: LLMProvider,
    pack: ContextPack,
    repo_root: Path,
) -> LLMReviewResult:
    batch_reviewer = getattr(provider, "review_context_pack_with_usage", None)
    if callable(batch_reviewer):
        reviewer = cast(Callable[[ContextPack, Path], LLMReviewResult], batch_reviewer)
        return reviewer(pack, repo_root)
    return LLMReviewResult(findings=provider.review_context_pack(pack, repo_root))


def verify_findings_with_provider_result(
    provider: LLMProvider,
    findings: list[Finding],
    pack: ContextPack,
    repo_root: Path,
) -> LLMVerificationResult:
    usage_verifier = getattr(provider, "verify_findings_with_usage", None)
    if callable(usage_verifier):
        verifier = cast(
            Callable[[list[Finding], ContextPack, Path], LLMVerificationResult],
            usage_verifier,
        )
        return verifier(findings, pack, repo_root)
    return LLMVerificationResult(verifications=verify_findings_with_provider(provider, findings, pack, repo_root))


def verify_findings_with_provider(
    provider: LLMProvider,
    findings: list[Finding],
    pack: ContextPack,
    repo_root: Path,
) -> list[FindingVerification]:
    batch_verifier = getattr(provider, "verify_findings", None)
    if callable(batch_verifier):
        verifier = cast(
            Callable[[list[Finding], ContextPack, Path], list[FindingVerification]],
            batch_verifier,
        )
        return verifier(findings, pack, repo_root)
    return [provider.verify_finding(finding, pack, repo_root) for finding in findings]
