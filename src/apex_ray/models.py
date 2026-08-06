import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

_HTTP_FIELD_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


class ApexModel(BaseModel):
    model_config = ConfigDict(use_enum_values=True, populate_by_name=True)


class StrictApexModel(ApexModel):
    model_config = ConfigDict(use_enum_values=True, extra="forbid", populate_by_name=True)


class TargetMode(StrEnum):
    BASE = "base"
    STAGED = "staged"
    WORKTREE = "worktree"
    PATCH = "patch"


class FileStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"


class FileKind(StrEnum):
    SOURCE = "source"
    TEST = "test"
    CONFIG = "config"
    MIGRATION = "migration"
    SCHEMA = "schema"
    DEPENDENCY = "dependency"
    LOCKFILE = "lockfile"
    DOCS = "docs"
    GENERATED = "generated"
    VENDORED = "vendored"
    UNKNOWN = "unknown"


class DiffLineKind(StrEnum):
    CONTEXT = "context"
    ADD = "add"
    DELETE = "delete"


class RiskSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LLMProviderName(StrEnum):
    FAKE = "fake"
    CODEX_CLI = "codex_cli"
    CLAUDE_CODE_CLI = "claude_code_cli"
    OPENAI_API = "openai_api"
    ANTHROPIC_API = "anthropic_api"
    DEEPSEEK_API = "deepseek_api"
    QWEN_API = "qwen_api"
    KIMI_API = "kimi_api"
    ZAI_API = "zai_api"
    OPENAI_COMPATIBLE = "openai_compatible"


class LLMAPIProtocol(StrEnum):
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    OPENAI_CHAT = "openai_chat"


class LLMStructuredOutput(StrEnum):
    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"
    PROMPT_ONLY = "prompt_only"


class LLMCoverageMode(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    EXHAUSTIVE = "exhaustive"


class LLMReasoningEffort(StrEnum):
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ProgressMode(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RuleMode(StrEnum):
    ADVISORY = "advisory"
    STRICT = "strict"


class MemoryKind(StrEnum):
    INVARIANT = "invariant"
    BUG_PATTERN = "bug_pattern"
    FALSE_POSITIVE = "false_positive"
    SEVERITY_CALIBRATION = "severity_calibration"
    GLOSSARY = "glossary"


class RuleTriggers(StrictApexModel):
    imports: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    risk: list[str] = Field(default_factory=list)
    text: list[str] = Field(default_factory=list)


class ReviewRule(StrictApexModel):
    id: str
    title: str = ""
    severity: FindingSeverity = FindingSeverity.MEDIUM
    mode: RuleMode = RuleMode.ADVISORY
    paths: list[str] = Field(default_factory=list)
    context_paths: list[str] = Field(default_factory=list)
    resolution_surfaces: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    triggers: RuleTriggers = Field(default_factory=RuleTriggers)
    model: str | None = None
    verify: str | None = None
    body: str = ""
    source_path: str | None = None


class RuleMatch(ApexModel):
    id: str
    title: str
    severity: FindingSeverity
    mode: RuleMode
    resolution_surfaces: list[str] = Field(default_factory=list)
    model: str | None = None
    verify: str | None = None
    source_path: str | None = None


class MemoryCard(StrictApexModel):
    id: str
    title: str = ""
    kind: MemoryKind = MemoryKind.BUG_PATTERN
    severity: FindingSeverity = FindingSeverity.MEDIUM
    paths: list[str] = Field(default_factory=list)
    context_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    triggers: RuleTriggers = Field(default_factory=RuleTriggers)
    tags: list[str] = Field(default_factory=list)
    applies_to: Literal["review", "verify", "both"] | None = None
    max_prompt_chars: int | None = Field(default=None, gt=0)
    body: str = ""
    source_path: str | None = None


class MemoryMatch(ApexModel):
    id: str
    title: str
    kind: MemoryKind
    severity: FindingSeverity
    applies_to: Literal["review", "verify", "both"]
    source_path: str | None = None
    score: int = 0
    reason: str = ""
    rendered: str = ""
    prompt_chars: int = 0


class MemoryOmission(ApexModel):
    id: str
    title: str = ""
    kind: MemoryKind = MemoryKind.BUG_PATTERN
    reason: str
    score: int = 0
    source_path: str | None = None


class ContextConfig(StrictApexModel):
    max_changed_snippets: int = Field(default=6, gt=0)
    max_changed_snippet_lines: int = Field(default=180, gt=0)
    max_hunk_snippets: int = Field(default=4, gt=0)
    hunk_context_lines: int = Field(default=8, gt=0)
    max_reference_snippets: int = Field(default=8, gt=0)
    reference_snippet_context_lines: int = Field(default=4, gt=0)
    max_related_test_snippets: int = Field(default=4, gt=0)
    max_related_test_snippet_lines: int = Field(default=24, gt=0)
    max_pack_chars: int = Field(default=40000, gt=0)


class MemoryConfig(StrictApexModel):
    enabled: bool = True
    paths: list[str] = Field(default_factory=lambda: [".apex-ray/memory"])
    max_cards_per_pack: int = Field(default=4, ge=0)
    max_chars_per_pack: int = Field(default=2400, ge=0)
    max_chars_per_card: int = Field(default=700, gt=0)
    max_context_ratio: float = Field(default=0.10, ge=0.0, le=1.0)


class RiskRule(StrictApexModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    title: str = ""
    severity: RiskSeverity = RiskSeverity.MEDIUM
    score: int | None = Field(default=None, ge=0, le=100)
    paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    file_kinds: list[FileKind] = Field(default_factory=list)
    statuses: list[FileStatus] = Field(default_factory=list)
    text: list[str] = Field(default_factory=list)
    risk: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    reviewer_tags: list[str] = Field(default_factory=list)
    guidance: str = ""


class RiskConfig(StrictApexModel):
    built_in_enabled: bool = True
    rules: list[RiskRule] = Field(default_factory=list)


class LLMAPIConfig(StrictApexModel):
    protocol: LLMAPIProtocol | None = None
    structured_output: LLMStructuredOutput | None = None
    base_url: str | None = None
    base_url_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    allowed_hosts_env: str = Field(
        default="APEX_RAY_API_ALLOWED_HOSTS",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    headers_from_env: dict[str, str] = Field(default_factory=dict)
    api_version: str | None = None
    max_output_tokens: int = Field(default=4096, gt=0)
    max_retries: int = Field(default=2, ge=0, le=8)
    retry_backoff_seconds: float = Field(default=0.5, gt=0.0, le=60.0)
    retry_max_seconds: float = Field(default=8.0, gt=0.0, le=300.0)
    use_system_proxy: bool = True
    allow_insecure_loopback_http: bool = False

    @model_validator(mode="after")
    def validate_endpoint_and_headers(self) -> Self:
        if self.base_url and self.base_url_env:
            raise ValueError("Use only one of base_url or base_url_env")
        if self.base_url:
            # Keep literal config validation on the same canonical URL path as
            # provider construction. The lazy import avoids the llm package's
            # public re-export cycle back through these foundational models.
            from apex_ray.llm.http import validate_api_endpoint_url

            try:
                validate_api_endpoint_url(
                    self.base_url,
                    allow_insecure_loopback_http=self.allow_insecure_loopback_http,
                )
            except ValueError as exc:
                detail = str(exc)
                if detail in {
                    "API endpoint must use HTTPS.",
                    "Loopback HTTP requires explicit opt-in.",
                }:
                    detail = "API base_url must use HTTPS; loopback HTTP requires allow_insecure_loopback_http=true"
                else:
                    detail = detail.replace("API endpoint", "API base_url")
                raise ValueError(detail) from exc
        for header, env_name in self.headers_from_env.items():
            if _HTTP_FIELD_NAME.fullmatch(header) is None:
                raise ValueError(f"Invalid API header name: {header!r}")
            if not env_name or not env_name.replace("_", "a").isalnum() or env_name[0].isdigit():
                raise ValueError(f"Invalid environment variable name for API header {header!r}")
        return self


class LLMProfile(StrictApexModel):
    provider: LLMProviderName | None = None
    model: str | None = None
    effort: LLMReasoningEffort | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)
    codex_path: str | None = None
    claude_path: str | None = None
    api: LLMAPIConfig | None = None


class LLMRoutingCondition(StrictApexModel):
    risk: list[str] = Field(default_factory=list)
    rule_severity: list[FindingSeverity] = Field(default_factory=list)
    finding_severity: list[FindingSeverity] = Field(default_factory=list)
    finding_confidence: list[FindingConfidence] = Field(default_factory=list)
    file_kind: list[FileKind] = Field(default_factory=list)
    exclude_file_kind: list[FileKind] = Field(default_factory=list)
    strict_rule: bool = False
    pack_truncated: bool = False
    min_pack_chars: int | None = Field(default=None, gt=0)


class LLMRoutingConfig(StrictApexModel):
    review_profile: str | None = None
    verify_profile: str | None = None
    escalated_review_profile: str | None = None
    escalated_verify_profile: str | None = None
    escalate_review_when: LLMRoutingCondition = Field(default_factory=LLMRoutingCondition)
    escalate_verify_when: LLMRoutingCondition = Field(default_factory=LLMRoutingCondition)


class LLMConfig(StrictApexModel):
    enabled: bool = False
    provider: LLMProviderName = LLMProviderName.CODEX_CLI
    model: str | None = None
    effort: LLMReasoningEffort | None = None
    timeout_seconds: int = Field(default=300, gt=0)
    jobs: int = Field(default=1, ge=1)
    max_consecutive_provider_failures: int = Field(default=3, ge=1, le=100)
    max_packs: int = Field(default=64, gt=0)
    coverage_mode: LLMCoverageMode = LLMCoverageMode.BALANCED
    max_deep_packs: int | None = Field(default=48, gt=0)
    max_input_tokens: int | None = Field(default=300_000, gt=0)
    min_source_line_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    min_high_risk_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    review_depth: Literal["deep", "shallow"] = "deep"
    codex_path: str = "codex"
    claude_path: str = "claude"
    api: LLMAPIConfig = Field(default_factory=LLMAPIConfig)
    verify: bool = True
    cache_enabled: bool = True
    cache_dir: str | None = None
    refresh_cache: bool = False
    profiles: dict[str, LLMProfile] = Field(default_factory=dict)
    routing: LLMRoutingConfig = Field(default_factory=LLMRoutingConfig)


class ReviewerConfig(StrictApexModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = ""
    enabled: bool = True
    focus: str = ""
    instructions: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    file_kinds: list[FileKind] = Field(default_factory=list)
    risk: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    profile: str | None = None
    verify_profile: str | None = None
    coverage_mode: LLMCoverageMode | None = None
    review_depth: Literal["balanced", "deep", "shallow"] = "balanced"
    max_packs: int | None = Field(default=None, gt=0)
    max_deep_packs: int | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    verify: bool | None = None
    required: bool = False


class DartAnalyzerConfig(StrictApexModel):
    enabled: bool = True
    command: list[str] = Field(default_factory=list)
    flutter: Literal["auto", "enabled", "disabled"] = "auto"
    plugins: bool = True
    max_changed_symbols: int = Field(default=80, gt=0)
    max_references_per_symbol: int = Field(default=24, gt=0)
    max_callees_per_symbol: int = Field(default=16, gt=0)
    max_related_tests_per_file: int = Field(default=12, gt=0)
    max_dependency_package_anchors: int = Field(default=16, gt=0)

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if any(not argument.strip() or "\x00" in argument for argument in self.command):
            raise ValueError("review.analyzer.dart.command arguments must be non-empty and contain no NUL bytes")
        return self


class AnalyzerConfig(StrictApexModel):
    index_cache_enabled: bool = True
    index_cache_dir: str | None = None
    refresh_index_cache: bool = False
    timeout_seconds: int = Field(default=120, gt=0)
    changed_file_shard_size: int = Field(default=40, gt=0)
    adaptive_sharding: bool = True
    large_change_file_threshold: int = Field(default=20, gt=0)
    large_change_shard_size: int = Field(default=4, gt=0)
    script_path: str | None = None
    dart: DartAnalyzerConfig = Field(default_factory=DartAnalyzerConfig)


class LocalDataConfig(StrictApexModel):
    root: str = ".apex-ray"


class TelemetryConfig(StrictApexModel):
    enabled: bool = False
    path: str = ".apex-ray/telemetry/review-runs.jsonl"
    path_mode: Literal["anonymized", "full"] = "full"


class ReportsConfig(StrictApexModel):
    archive: bool = False
    archive_dir: str = ".apex-ray/reports/runs"
    retention: int | None = Field(default=20, ge=1)
    compression: Literal["none", "gzip", "auto"] = "none"
    compression_min_bytes: int = Field(default=65_536, ge=0)


class TriageConfig(StrictApexModel):
    enabled: bool = True
    state_path: str = ".apex-ray/triage/suppressions.json"
    events_path: str = ".apex-ray/triage/events.jsonl"
    default_expiry_days: int = Field(default=14, ge=1)
    max_active_suppressions: int = Field(default=200, ge=1)
    events_retention_days: int | None = Field(default=90, ge=1)


class IncrementalPrePushRetryConfig(StrictApexModel):
    enabled: bool = False
    state_path: str = ".apex-ray/reports/pre-push-state.json"
    fallback_on_uncertain_resolution: Literal["block"] = "block"
    max_resolution_calls_per_retry: int = Field(default=8, ge=1, le=64)


DEFAULT_AUTO_FOLLOWUP_P0_MAX_PACK_REVIEWS = 16


class PrePushGateConfig(StrictApexModel):
    enabled: bool = True
    min_finding_severity: FindingSeverity | None = FindingSeverity.HIGH
    require_verified_findings: bool = True
    fail_on_quality_gate: bool = True
    fail_on_partial_severity: Literal["none", "minor", "major", "critical"] | None = "critical"
    max_stdout_findings: int = Field(default=10, ge=0)
    stdout_format: Literal["agent", "compact"] = "agent"
    auto_followup: bool | None = None
    auto_followup_max_pack_reviews: int | None = Field(default=None, gt=0)
    auto_followup_p0: bool = True
    auto_followup_p0_max_pack_reviews: int = Field(
        default=DEFAULT_AUTO_FOLLOWUP_P0_MAX_PACK_REVIEWS,
        gt=0,
    )
    progress: ProgressMode = ProgressMode.AUTO
    progress_interval_seconds: float = Field(default=5.0, ge=0.0)
    incremental_retry: IncrementalPrePushRetryConfig = Field(default_factory=IncrementalPrePushRetryConfig)


class GatesConfig(StrictApexModel):
    pre_push: PrePushGateConfig = Field(default_factory=PrePushGateConfig)


class ReviewConfig(StrictApexModel):
    base: str = "main"
    ignore: list[str] = Field(default_factory=lambda: ["**/*.lock", "**/generated/**"])
    languages: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    rule_paths: list[str] = Field(default_factory=lambda: [".apex-ray/rules"])
    rule_definitions: list[ReviewRule] = Field(default_factory=list)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    memory_definitions: list[MemoryCard] = Field(default_factory=list)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    analyzer: AnalyzerConfig = Field(default_factory=AnalyzerConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    local_data: LocalDataConfig = Field(default_factory=LocalDataConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    reviewers: list[ReviewerConfig] = Field(default_factory=list)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    triage: TriageConfig = Field(default_factory=TriageConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)


class ProjectProfile(ApexModel):
    root: str
    is_git_repo: bool
    config_path: str | None = None
    detected_languages: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    framework_hints: list[str] = Field(default_factory=list)
    ignored_patterns: list[str] = Field(default_factory=list)


class DiffLine(ApexModel):
    kind: DiffLineKind
    content: str
    old_line: int | None = None
    new_line: int | None = None


class RiskSignal(ApexModel):
    kind: str
    severity: RiskSeverity
    reason: str
    file: str
    line: int | None = None
    score: int = Field(default=0, ge=0, le=100)
    source: str = "built_in"
    rule_id: str | None = None
    categories: list[str] = Field(default_factory=list)
    reviewer_tags: list[str] = Field(default_factory=list)
    guidance: str = ""


class ChangedHunk(ApexModel):
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    section_header: str = ""
    lines: list[DiffLine] = Field(default_factory=list)
    risk_signals: list[RiskSignal] = Field(default_factory=list)


class ChangedFile(ApexModel):
    old_path: str | None
    new_path: str | None
    status: FileStatus = FileStatus.MODIFIED
    language: str = "unknown"
    file_kind: FileKind = FileKind.UNKNOWN
    additions: int = 0
    deletions: int = 0
    hunks: list[ChangedHunk] = Field(default_factory=list)
    risk_signals: list[RiskSignal] = Field(default_factory=list)
    is_ignored: bool = False
    ignore_reason: str | None = None

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or "<unknown>"


class DiffStats(ApexModel):
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    ignored_files: int = 0


class DiffSummary(ApexModel):
    base: str | None = None
    target_mode: TargetMode
    files: list[ChangedFile] = Field(default_factory=list)
    stats: DiffStats = Field(default_factory=DiffStats)
    warnings: list[str] = Field(default_factory=list)


class ReviewInputSnapshot(ApexModel):
    schema_version: Literal["review-input-snapshot/v1"] = "review-input-snapshot/v1"
    target_mode: TargetMode
    base_ref: str | None = None
    head_sha: str | None = None
    merge_base_sha: str | None = None
    range_start_sha: str | None = None
    diff_sha256: str

    @model_validator(mode="after")
    def validate_git_identity(self) -> Self:
        object_id_fields = ("head_sha", "merge_base_sha", "range_start_sha")
        for field_name in object_id_fields:
            value = getattr(self, field_name)
            if value is not None and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
                raise ValueError(f"{field_name} must be a full lowercase Git object id")
        if re.fullmatch(r"[0-9a-f]{64}", self.diff_sha256) is None:
            raise ValueError("diff_sha256 must be a lowercase SHA-256 digest")
        if self.base_ref is not None and (
            not self.base_ref or self.base_ref.startswith("-") or any(ord(char) < 32 for char in self.base_ref)
        ):
            raise ValueError("base_ref must be a non-option Git revision without control characters")

        target_mode = TargetMode(self.target_mode)
        if target_mode == TargetMode.BASE:
            if self.base_ref is None or self.head_sha is None or self.merge_base_sha is None:
                raise ValueError("base snapshots require base_ref, head_sha, and merge_base_sha")
            if self.range_start_sha is not None:
                raise ValueError("base snapshots cannot contain range_start_sha")
        elif target_mode in {TargetMode.STAGED, TargetMode.WORKTREE}:
            if self.head_sha is None:
                raise ValueError(f"{target_mode} snapshots require head_sha")
            if self.base_ref is not None or self.merge_base_sha is not None or self.range_start_sha is not None:
                raise ValueError(f"{target_mode} snapshots cannot contain base or range identity")
        elif self.range_start_sha is not None:
            if self.head_sha is None or self.base_ref is None:
                raise ValueError("live range snapshots require base_ref, head_sha, and range_start_sha")
            if self.merge_base_sha is not None:
                raise ValueError("live range snapshots cannot contain merge_base_sha")
        elif self.base_ref is not None or self.head_sha is not None or self.merge_base_sha is not None:
            raise ValueError("detached patch snapshots cannot contain live Git identity")
        return self


class ReportSummary(ApexModel):
    files_by_kind: dict[str, int] = Field(default_factory=dict)
    files_by_language: dict[str, int] = Field(default_factory=dict)
    risk_by_severity: dict[str, int] = Field(default_factory=dict)
    total_risk_signals: int = 0


class LLMRouteSummary(ApexModel):
    kind: str
    reviewer_id: str = "general"
    provider: str
    model: str | None = None
    effort: str | None = None
    profile: str | None = None
    route_reason: str | None = None
    status: str
    runs: int = 0
    findings_count: int = 0
    duration_ms: int = 0
    input_chars: int = 0
    estimated_input_tokens: int = 0
    actual_input_tokens: int = 0
    actual_cached_input_tokens: int = 0
    actual_output_tokens: int = 0
    actual_reasoning_output_tokens: int = 0
    actual_total_tokens: int = 0
    actual_cache_read_input_tokens: int = 0
    actual_cache_creation_input_tokens: int = 0
    estimated_saved_input_tokens: int = 0
    estimated_cost_usd: float | None = None
    usage_sources: list[str] = Field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0
    errors: int = 0


class LLMSelectionStageSummary(ApexModel):
    stage: str
    budget_packs: int | None = None
    budget_tokens: int | None = None
    selected_estimated_tokens: int = 0
    selected_context_pack_ids: list[str] = Field(default_factory=list)
    unselected_context_pack_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class LLMContextSelection(ApexModel):
    total_context_pack_ids: list[str] = Field(default_factory=list)
    selected_context_pack_ids: list[str] = Field(default_factory=list)
    deep_selected_context_pack_ids: list[str] = Field(default_factory=list)
    shallow_selected_context_pack_ids: list[str] = Field(default_factory=list)
    unselected_context_pack_ids: list[str] = Field(default_factory=list)
    over_budget_context_pack_ids: list[str] = Field(default_factory=list)
    over_token_budget_context_pack_ids: list[str] = Field(default_factory=list)
    skipped_context_pack_reasons: dict[str, str] = Field(default_factory=dict)
    stages: list[LLMSelectionStageSummary] = Field(default_factory=list)


class LLMResidualRiskSummary(ApexModel):
    context_pack_id: str
    file: str
    file_kind: FileKind = FileKind.UNKNOWN
    priority: str
    reason: str
    risk_by_severity: dict[str, int] = Field(default_factory=dict)
    rule_modes: dict[str, int] = Field(default_factory=dict)
    rule_severities: dict[str, int] = Field(default_factory=dict)
    estimated_chars: int = 0
    truncated: bool = False


class LLMPackReviewStatus(ApexModel):
    context_pack_id: str
    file: str
    file_kind: FileKind = FileKind.UNKNOWN
    status: str
    priority: str | None = None
    slice: str = "other"
    reason: str = ""
    review_depth: Literal["deep", "shallow"] | None = None
    estimated_chars: int = 0
    changed_lines: list[tuple[int, int]] = Field(default_factory=list)
    changed_symbols: list[str] = Field(default_factory=list)
    error: str | None = None


class LLMCoverageTodo(ApexModel):
    context_pack_id: str
    file: str
    reviewer_id: str | None = None
    file_kind: FileKind = FileKind.UNKNOWN
    priority: str
    slice: str = "other"
    reason: str = ""
    suggested_command: str = ""
    estimated_chars: int = 0
    changed_lines: list[tuple[int, int]] = Field(default_factory=list)
    changed_symbols: list[str] = Field(default_factory=list)


class LLMFileCoverageSummary(ApexModel):
    file: str
    file_kind: FileKind = FileKind.UNKNOWN
    total_context_packs: int = 0
    reviewed_context_packs: int = 0
    unreviewed_context_packs: int = 0
    cluster_context_packs: int = 0
    file_context_packs: int = 0
    symbol_context_packs: int = 0
    over_budget_context_packs: int = 0
    truncated_context_packs: int = 0
    risk_by_severity: dict[str, int] = Field(default_factory=dict)
    residual_priority: str | None = None
    reviewed_changed_lines: list[tuple[int, int]] = Field(default_factory=list)
    unreviewed_changed_lines: list[tuple[int, int]] = Field(default_factory=list)
    reviewed_changed_symbols: list[str] = Field(default_factory=list)
    unreviewed_changed_symbols: list[str] = Field(default_factory=list)
    reviewed_context_pack_ids: list[str] = Field(default_factory=list)
    unreviewed_context_pack_ids: list[str] = Field(default_factory=list)


class LLMSliceCoverageSummary(ApexModel):
    slice: str
    total_context_packs: int = 0
    reviewed_context_packs: int = 0
    unreviewed_context_packs: int = 0
    deep_reviewed_context_packs: int = 0
    shallow_reviewed_context_packs: int = 0
    high_risk_context_packs: int = 0
    reviewed_high_risk_context_packs: int = 0
    residual_priority: str | None = None
    reviewed_context_pack_ids: list[str] = Field(default_factory=list)
    unreviewed_context_pack_ids: list[str] = Field(default_factory=list)


class LLMReviewerCoverageSummary(ApexModel):
    reviewer_id: str
    required: bool = False
    verify_enabled: bool = False
    coverage_mode: LLMCoverageMode | None = None
    review_depth: Literal["balanced", "deep", "shallow"] | None = None
    max_packs: int | None = None
    max_deep_packs: int | None = None
    max_input_tokens: int | None = None
    status: Literal["not_applicable", "pass", "warn", "fail"] = "not_applicable"
    reasons: list[str] = Field(default_factory=list)
    matching_context_packs: int = 0
    selected_context_packs: int = 0
    reviewed_context_packs: int = 0
    failed_review_runs: int = 0
    failed_verify_runs: int = 0
    matching_context_pack_ids: list[str] = Field(default_factory=list)
    selected_context_pack_ids: list[str] = Field(default_factory=list)
    reviewed_context_pack_ids: list[str] = Field(default_factory=list)
    estimated_input_tokens: int = 0
    actual_total_tokens: int = 0
    estimated_cost_usd: float | None = None


class LLMCoverageSummary(ApexModel):
    enabled: bool = False
    verify_enabled: bool = False
    max_packs: int = 0
    coverage_mode: LLMCoverageMode = LLMCoverageMode.BALANCED
    max_deep_packs: int | None = None
    max_input_tokens: int | None = None
    total_context_packs: int = 0
    reviewed_context_packs: int = 0
    unreviewed_context_packs: int = 0
    coverage_ratio: float = 0.0
    source_changed_line_coverage_ratio: float = 0.0
    high_risk_coverage_ratio: float = 0.0
    high_risk_context_packs: int = 0
    reviewed_high_risk_context_packs: int = 0
    shallow_only_high_risk_context_pack_ids: list[str] = Field(default_factory=list)
    quality_gate_status: str = "disabled"
    quality_gate_reasons: list[str] = Field(default_factory=list)
    partial_severity: Literal["none", "minor", "major", "critical"] = "none"
    partial_reasons: list[str] = Field(default_factory=list)
    reviewed_context_pack_ids: list[str] = Field(default_factory=list)
    unreviewed_context_pack_ids: list[str] = Field(default_factory=list)
    unreviewed_context_pack_reasons: dict[str, str] = Field(default_factory=dict)
    pack_statuses: list[LLMPackReviewStatus] = Field(default_factory=list)
    coverage_todos: list[LLMCoverageTodo] = Field(default_factory=list)
    over_budget_context_pack_ids: list[str] = Field(default_factory=list)
    over_token_budget_context_pack_ids: list[str] = Field(default_factory=list)
    truncated_context_pack_ids: list[str] = Field(default_factory=list)
    deep_selected_context_pack_ids: list[str] = Field(default_factory=list)
    shallow_selected_context_pack_ids: list[str] = Field(default_factory=list)
    deep_reviewed_context_pack_ids: list[str] = Field(default_factory=list)
    shallow_reviewed_context_pack_ids: list[str] = Field(default_factory=list)
    deep_reviewed_context_packs: int = 0
    shallow_reviewed_context_packs: int = 0
    residual_risk_p0_context_pack_ids: list[str] = Field(default_factory=list)
    residual_risk_p1_context_pack_ids: list[str] = Field(default_factory=list)
    residual_risk_context_packs: list[LLMResidualRiskSummary] = Field(default_factory=list)
    file_coverage: list[LLMFileCoverageSummary] = Field(default_factory=list)
    slice_coverage: list[LLMSliceCoverageSummary] = Field(default_factory=list)
    reviewers: list[LLMReviewerCoverageSummary] = Field(default_factory=list)
    cluster_context_packs: int = 0
    file_context_packs: int = 0
    symbol_context_packs: int = 0
    reviewed_files: list[str] = Field(default_factory=list)
    unreviewed_files: list[str] = Field(default_factory=list)
    review_runs: int = 0
    verify_runs: int = 0
    failed_review_runs: int = 0
    failed_verify_runs: int = 0
    run_status_counts: dict[str, int] = Field(default_factory=dict)
    total_duration_ms: int = 0
    input_chars: int = 0
    estimated_input_tokens: int = 0
    actual_input_tokens: int = 0
    actual_cached_input_tokens: int = 0
    actual_output_tokens: int = 0
    actual_reasoning_output_tokens: int = 0
    actual_total_tokens: int = 0
    actual_cache_read_input_tokens: int = 0
    actual_cache_creation_input_tokens: int = 0
    estimated_saved_input_tokens: int = 0
    estimated_cost_usd: float | None = None
    usage_sources: list[str] = Field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0
    routes: list[LLMRouteSummary] = Field(default_factory=list)

    @computed_field(return_type=Literal["disabled", "complete", "partial", "incomplete"])
    @property
    def completion_status(self) -> Literal["disabled", "complete", "partial", "incomplete"]:
        if not self.enabled:
            return "disabled"
        reviewer_assignment_debt = any(
            reviewer.reviewed_context_packs < reviewer.matching_context_packs for reviewer in self.reviewers
        )
        reviewer_status_debt = any(reviewer.status in {"warn", "fail"} for reviewer in self.reviewers)
        required_reviewer_failed = any(reviewer.required and reviewer.status == "fail" for reviewer in self.reviewers)
        active_reviewer_run_failed = any(
            reviewer.failed_review_runs or reviewer.failed_verify_runs for reviewer in self.reviewers
        )
        failed_pack = any(status.status.startswith("failed_") for status in self.pack_statuses)
        if failed_pack or active_reviewer_run_failed or self.over_budget_context_pack_ids or required_reviewer_failed:
            return "incomplete"
        if (
            self.unreviewed_context_packs == 0
            and self.partial_severity == "none"
            and not reviewer_assignment_debt
            and not reviewer_status_debt
        ):
            return "complete"
        return "partial"


CoverageStopReason = Literal["complete", "no_eligible_work", "no_progress", "max_batches"]


class ReviewCoverageCompletion(ApexModel):
    status: Literal["complete", "incomplete"]
    reviewer_ids: list[str] = Field(default_factory=list)
    batches: int = Field(default=0, ge=0)
    stop_reason: CoverageStopReason

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if len(self.reviewer_ids) != len(set(self.reviewer_ids)):
            raise ValueError("coverage completion reviewer_ids must be unique")
        if (self.status == "complete") != (self.stop_reason == "complete"):
            raise ValueError("coverage completion status and stop_reason are inconsistent")
        return self


class MemorySummary(ApexModel):
    enabled: bool = False
    loaded_cards: int = 0
    matched_cards: int = 0
    applied_cards: int = 0
    omitted_cards: int = 0
    applied_card_ids: list[str] = Field(default_factory=list)
    omitted_card_reasons: dict[str, str] = Field(default_factory=dict)
    total_prompt_chars: int = 0


class AnalyzerReference(ApexModel):
    file: str
    line: int
    end_line: int | None = Field(default=None, alias="endLine")
    text: str
    kind: str = "unknown"


class AnalyzerSymbol(ApexModel):
    name: str
    kind: str
    start_line: int = Field(alias="startLine")
    end_line: int = Field(alias="endLine")
    exported: bool = False
    signature: str = ""
    references: list[AnalyzerReference] = Field(default_factory=list)
    callees: list[AnalyzerReference] = Field(default_factory=list)
    contracts: list[AnalyzerReference] = Field(default_factory=list)
    metadata: list[AnalyzerReference] = Field(default_factory=list)


class AnalyzerFile(ApexModel):
    path: str
    tsconfig_path: str | None = Field(default=None, alias="tsconfigPath")
    symbols: list[AnalyzerSymbol] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    exports: list[str] = Field(default_factory=list)
    related_tests: list[str] = Field(default_factory=list, alias="relatedTests")
    changed_symbols: list[AnalyzerSymbol] = Field(default_factory=list, alias="changedSymbols")
    uncovered_changed_ranges: list[tuple[int, int]] = Field(
        default_factory=list,
        alias="uncoveredChangedRanges",
    )


class AnalyzerIndexCacheStats(ApexModel):
    path: str
    files: int = 0
    hits: int = 0
    misses: int = 0
    written: bool = False


class AnalyzerShardFailure(ApexModel):
    index: int
    total: int
    files: list[str] = Field(default_factory=list)
    reason: str
    status: Literal["failed", "timeout", "skipped"] = "failed"


class AnalyzerResult(ApexModel):
    language: str
    project_root: str = Field(alias="projectRoot")
    tsconfig_path: str | None = Field(default=None, alias="tsconfigPath")
    files: list[AnalyzerFile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    index_cache: AnalyzerIndexCacheStats | None = Field(default=None, alias="indexCache")
    partial: bool = False
    failed_files: list[str] = Field(default_factory=list, alias="failedFiles")
    shard_failures: list[AnalyzerShardFailure] = Field(default_factory=list, alias="shardFailures")


class CodeSnippet(ApexModel):
    file: str
    start_line: int
    end_line: int
    code: str


class ContextPackStats(ApexModel):
    diff_lines: int = 0
    changed_snippet_lines: int = 0
    reference_snippet_lines: int = 0
    callee_snippet_lines: int = 0
    contract_snippet_lines: int = 0
    metadata_snippet_lines: int = 0
    related_test_snippet_lines: int = 0
    memory_cards: int = 0
    memory_chars: int = 0
    estimated_chars: int = 0
    truncated: bool = False
    truncation_notes: list[str] = Field(default_factory=list)
    policy_key: str = ""


class ReviewerPromptContext(ApexModel):
    id: str
    name: str = ""
    focus: str = ""
    instructions: list[str] = Field(default_factory=list)


class ContextPack(ApexModel):
    id: str
    file: str
    file_kind: FileKind = FileKind.UNKNOWN
    changed_lines: list[tuple[int, int]] = Field(default_factory=list)
    impact_notes: list[str] = Field(default_factory=list)
    diff_snippet: list[str] = Field(default_factory=list)
    changed_snippets: list[CodeSnippet] = Field(default_factory=list)
    symbol: AnalyzerSymbol | None = None
    symbols: list[AnalyzerSymbol] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    exports: list[str] = Field(default_factory=list)
    related_tests: list[str] = Field(default_factory=list)
    references: list[AnalyzerReference] = Field(default_factory=list)
    callees: list[AnalyzerReference] = Field(default_factory=list)
    contracts: list[AnalyzerReference] = Field(default_factory=list)
    metadata: list[AnalyzerReference] = Field(default_factory=list)
    reference_snippets: list[CodeSnippet] = Field(default_factory=list)
    callee_snippets: list[CodeSnippet] = Field(default_factory=list)
    contract_snippets: list[CodeSnippet] = Field(default_factory=list)
    metadata_snippets: list[CodeSnippet] = Field(default_factory=list)
    related_test_snippets: list[CodeSnippet] = Field(default_factory=list)
    risk_signals: list[RiskSignal] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    rule_matches: list[RuleMatch] = Field(default_factory=list)
    memory_matches: list[MemoryMatch] = Field(default_factory=list)
    memory_omissions: list[MemoryOmission] = Field(default_factory=list)
    reviewer: ReviewerPromptContext | None = None
    warnings: list[str] = Field(default_factory=list)
    stats: ContextPackStats = Field(default_factory=ContextPackStats)


class Finding(ApexModel):
    title: str
    severity: FindingSeverity
    confidence: FindingConfidence
    file: str
    line: int | None = None
    failure_mode: str
    evidence: str
    suggested_fix: str
    suggested_test: str
    context_pack_id: str = ""
    reviewer_ids: list[str] = Field(default_factory=list)
    reviewer_context_pack_ids: dict[str, list[str]] = Field(default_factory=dict)


class FindingResponse(ApexModel):
    findings: list[Finding] = Field(default_factory=list)


class VerificationResponse(ApexModel):
    approved: bool
    confidence: FindingConfidence
    reason: str


class VerificationDecision(StrictApexModel):
    finding_index: int = Field(ge=0)
    approved: bool
    confidence: FindingConfidence
    reason: str


class VerificationBatchResponse(StrictApexModel):
    decisions: list[VerificationDecision] = Field(default_factory=list)


class FindingVerification(ApexModel):
    finding: Finding
    reviewer_id: str = "general"
    approved: bool
    confidence: FindingConfidence
    reason: str
    review_snapshot_id: str | None = None
    superseded: bool = False
    superseded_reason: str | None = None


class FindingResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    STILL_PRESENT = "still_present"
    UNCERTAIN = "uncertain"


class FindingResolutionResponse(StrictApexModel):
    status: FindingResolutionStatus
    confidence: FindingConfidence
    reason: str
    evidence: str = ""
    suggested_next_action: str = ""


class FindingResolution(ApexModel):
    finding: Finding
    status: FindingResolutionStatus
    confidence: FindingConfidence
    reason: str
    evidence: str = ""
    suggested_next_action: str = ""


class LLMUsage(ApexModel):
    source: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    estimated_cost_usd: float | None = None


class LLMReviewResult(ApexModel):
    findings: list[Finding] = Field(default_factory=list)
    usage: LLMUsage | None = None


class LLMVerificationResult(ApexModel):
    verifications: list[FindingVerification] = Field(default_factory=list)
    usage: LLMUsage | None = None


class LLMRun(ApexModel):
    kind: str = "review"
    provider: str
    model: str | None = None
    effort: str | None = None
    profile: str | None = None
    route_reason: str | None = None
    prompt_version: str | None = None
    reviewer_id: str = "general"
    context_pack_id: str
    status: str
    duration_ms: int
    input_chars: int = 0
    estimated_input_tokens: int = 0
    actual_input_tokens: int = 0
    actual_cached_input_tokens: int = 0
    actual_output_tokens: int = 0
    actual_reasoning_output_tokens: int = 0
    actual_total_tokens: int = 0
    actual_cache_read_input_tokens: int = 0
    actual_cache_creation_input_tokens: int = 0
    estimated_saved_input_tokens: int = 0
    estimated_cost_usd: float | None = None
    usage_source: str | None = None
    findings_count: int = 0
    cache_hit: bool = False
    cache_hits: int = 0
    cache_misses: int = 0
    cache_key: str | None = None
    error: str | None = None


class ReviewReport(ApexModel):
    schema_version: str = "review-report/v1"
    project: ProjectProfile
    config: ReviewConfig
    diff: DiffSummary
    input_snapshot: ReviewInputSnapshot | None = None
    summary: ReportSummary
    llm_selection: LLMContextSelection | None = None
    reviewer_selections: dict[str, LLMContextSelection] = Field(default_factory=dict)
    reviewer_scope_ids: list[str] | None = None
    stage_durations_ms: dict[str, int] = Field(default_factory=dict)
    llm_coverage: LLMCoverageSummary = Field(default_factory=LLMCoverageSummary)
    coverage_completion: ReviewCoverageCompletion | None = None
    memory_summary: MemorySummary = Field(default_factory=MemorySummary)
    rules: list[str] = Field(default_factory=list)
    analyzer_results: list[AnalyzerResult] = Field(default_factory=list)
    context_packs: list[ContextPack] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    verifications: list[FindingVerification] = Field(default_factory=list)
    llm_runs: list[LLMRun] = Field(default_factory=list)
    generated_at: datetime
    version: str
