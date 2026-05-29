import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from apex_ray.llm_cli import (
    build_claude_command,
    build_codex_command,
    claude_result_text,
    resolve_claude_path,
    resolve_codex_path,
)
from apex_ray.llm_errors import LLMProviderError
from apex_ray.llm_prompts import build_review_prompt, build_shallow_review_prompt, build_verifier_batch_prompt
from apex_ray.llm_responses import (
    finding_response_schema,
    parse_finding_response,
    parse_verification_batch_response,
    verification_batch_response_schema,
)
from apex_ray.models import (
    ContextPack,
    Finding,
    FindingConfidence,
    FindingVerification,
    LLMConfig,
    LLMProviderName,
)


class LLMProvider(Protocol):
    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]: ...

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification: ...


class FakeLLMProvider:
    def __init__(
        self,
        findings: list[Finding] | None = None,
        verification_approvals: list[bool] | None = None,
    ) -> None:
        self.findings = findings or []
        self.verification_approvals = verification_approvals or []
        self.reviewed_pack_ids: list[str] = []
        self.verified_batch_pack_ids: list[str] = []
        self.verified_batches: list[list[str]] = []
        self.verified_finding_titles: list[str] = []

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


class CodexCLIProvider:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
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

            return parse_finding_response(output_path.read_text(encoding="utf-8"), pack.id).findings

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
        return self.verify_findings([finding], pack, repo_root)[0]

    def verify_findings(self, findings: list[Finding], pack: ContextPack, repo_root: Path) -> list[FindingVerification]:
        if not findings:
            return []

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

            return parse_verification_batch_response(output_path.read_text(encoding="utf-8"), findings)


class ClaudeCodeCLIProvider:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def review_context_pack(self, pack: ContextPack, repo_root: Path) -> list[Finding]:
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
            return parse_finding_response(response_text, pack.id).findings

    def verify_finding(self, finding: Finding, pack: ContextPack, repo_root: Path) -> FindingVerification:
        return self.verify_findings([finding], pack, repo_root)[0]

    def verify_findings(self, findings: list[Finding], pack: ContextPack, repo_root: Path) -> list[FindingVerification]:
        if not findings:
            return []

        claude_path = resolve_claude_path(self.config.claude_path, repo_root)
        prompt = build_verifier_batch_prompt(findings, pack)

        with tempfile.TemporaryDirectory(prefix="apex-ray-claude-verify-") as tmp:
            tmp_path = Path(tmp)
            command = build_claude_command(
                claude_path=claude_path,
                schema=verification_batch_response_schema(),
                model=self.config.model,
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
            return parse_verification_batch_response(response_text, findings)


def provider_from_config(config: LLMConfig) -> LLMProvider:
    if config.provider == LLMProviderName.FAKE:
        return FakeLLMProvider()
    if config.provider == LLMProviderName.CODEX_CLI:
        return CodexCLIProvider(config)
    if config.provider == LLMProviderName.CLAUDE_CODE_CLI:
        return ClaudeCodeCLIProvider(config)
    raise LLMProviderError(f"Unsupported LLM provider: {config.provider}")


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
