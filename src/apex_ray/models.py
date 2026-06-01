from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApexModel(BaseModel):
    model_config = ConfigDict(use_enum_values=True)


class StrictApexModel(ApexModel):
    model_config = ConfigDict(use_enum_values=True, extra="forbid")


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
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LLMProviderName(StrEnum):
    FAKE = "fake"
    CODEX_CLI = "codex_cli"
    CLAUDE_CODE_CLI = "claude_code_cli"


class LLMCoverageMode(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    EXHAUSTIVE = "exhaustive"


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


class LLMProfile(StrictApexModel):
    provider: LLMProviderName | None = None
    model: str | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)
    codex_path: str | None = None
    claude_path: str | None = None


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
    timeout_seconds: int = Field(default=300, gt=0)
    jobs: int = Field(default=1, ge=1)
    max_packs: int = Field(default=64, gt=0)
    coverage_mode: LLMCoverageMode = LLMCoverageMode.BALANCED
    max_deep_packs: int | None = Field(default=48, gt=0)
    max_input_tokens: int | None = Field(default=300_000, gt=0)
    min_source_line_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    min_high_risk_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    review_depth: Literal["deep", "shallow"] = "deep"
    codex_path: str = "codex"
    claude_path: str = "claude"
    verify: bool = True
    cache_enabled: bool = True
    cache_dir: str | None = None
    refresh_cache: bool = False
    profiles: dict[str, LLMProfile] = Field(default_factory=dict)
    routing: LLMRoutingConfig = Field(default_factory=LLMRoutingConfig)


class AnalyzerConfig(StrictApexModel):
    index_cache_enabled: bool = True
    index_cache_dir: str | None = None
    refresh_index_cache: bool = False
    timeout_seconds: int = Field(default=120, gt=0)
    changed_file_shard_size: int = Field(default=40, gt=0)
    adaptive_sharding: bool = True
    large_change_file_threshold: int = Field(default=20, gt=0)
    large_change_shard_size: int = Field(default=8, gt=0)
    script_path: str | None = None


class TelemetryConfig(StrictApexModel):
    enabled: bool = False
    path: str = ".apex-ray/telemetry/review-runs.jsonl"


class PrePushGateConfig(StrictApexModel):
    enabled: bool = True
    min_finding_severity: FindingSeverity | None = FindingSeverity.HIGH
    require_verified_findings: bool = True
    fail_on_quality_gate: bool = True
    fail_on_partial_severity: Literal["none", "minor", "major", "critical"] | None = "critical"
    max_stdout_findings: int = Field(default=10, ge=0)
    stdout_format: Literal["agent", "compact"] = "agent"
    auto_followup_p0: bool = True


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
    analyzer: AnalyzerConfig = Field(default_factory=AnalyzerConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
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


class ReportSummary(ApexModel):
    files_by_kind: dict[str, int] = Field(default_factory=dict)
    files_by_language: dict[str, int] = Field(default_factory=dict)
    risk_by_severity: dict[str, int] = Field(default_factory=dict)
    total_risk_signals: int = 0


class LLMRouteSummary(ApexModel):
    kind: str
    provider: str
    model: str | None = None
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
    approved: bool
    confidence: FindingConfidence
    reason: str


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
    profile: str | None = None
    route_reason: str | None = None
    prompt_version: str | None = None
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
    summary: ReportSummary
    llm_selection: LLMContextSelection | None = None
    llm_coverage: LLMCoverageSummary = Field(default_factory=LLMCoverageSummary)
    memory_summary: MemorySummary = Field(default_factory=MemorySummary)
    rules: list[str] = Field(default_factory=list)
    analyzer_results: list[AnalyzerResult] = Field(default_factory=list)
    context_packs: list[ContextPack] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    verifications: list[FindingVerification] = Field(default_factory=list)
    llm_runs: list[LLMRun] = Field(default_factory=list)
    generated_at: datetime
    version: str
