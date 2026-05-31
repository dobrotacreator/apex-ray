from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from apex_ray.models import (
    Finding,
    FindingConfidence,
    FindingSeverity,
    LLMProfile,
    LLMProviderName,
    LLMRoutingConfig,
)


class StrictBenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedFinding(StrictBenchmarkModel):
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    line_min: int | None = Field(default=None, ge=1)
    line_max: int | None = Field(default=None, ge=1)
    title_contains: str | None = None
    severity: FindingSeverity | None = None
    confidence: FindingConfidence | None = None
    failure_mode_contains: str | None = None
    evidence_contains: str | None = None
    suggested_fix_contains: str | None = None
    suggested_test_contains: str | None = None

    @model_validator(mode="after")
    def validate_line_range(self) -> ExpectedFinding:
        if self.line_min is not None and self.line_max is not None and self.line_min > self.line_max:
            raise ValueError("line_min must be less than or equal to line_max")
        return self


class ExpectedContext(StrictBenchmarkModel):
    pack_file: str | None = None
    pack_id_contains: str | None = None
    related_test: str | None = None
    related_test_index: int | None = Field(default=None, ge=0)
    related_test_snippet_contains: str | None = None
    related_test_snippet_start_min: int | None = Field(default=None, ge=1)
    reference_file: str | None = None
    reference_kind: str | None = None
    reference_text_contains: str | None = None
    reference_snippet_contains: str | None = None
    callee_file: str | None = None
    callee_kind: str | None = None
    callee_text_contains: str | None = None
    callee_snippet_contains: str | None = None
    contract_file: str | None = None
    contract_kind: str | None = None
    contract_text_contains: str | None = None
    contract_snippet_contains: str | None = None
    metadata_file: str | None = None
    metadata_kind: str | None = None
    metadata_text_contains: str | None = None
    metadata_snippet_contains: str | None = None


class BenchmarkCase(StrictBenchmarkModel):
    name: str
    repo: str
    diff: str
    rules: list[str] = Field(default_factory=list)
    llm: bool | None = None
    provider: LLMProviderName | None = None
    model: str | None = None
    profiles: dict[str, LLMProfile] = Field(default_factory=dict)
    routing: LLMRoutingConfig | None = None
    verify: bool | None = None
    fake_findings: list[Finding] = Field(default_factory=list)
    expected: list[ExpectedFinding] = Field(default_factory=list)
    expected_context: list[ExpectedContext] = Field(default_factory=list)


class ExpectedFindingResult(BaseModel):
    expected: ExpectedFinding
    matched: bool
    matched_title: str | None = None


class ExpectedContextResult(BaseModel):
    expected: ExpectedContext
    matched: bool
    matched_pack_id: str | None = None


class BenchmarkCaseResult(BaseModel):
    name: str
    passed: bool
    repo: str
    diff: str
    findings_count: int
    context_packs_count: int
    llm_runs_count: int
    llm_cache_hits: int = 0
    llm_cache_misses: int = 0
    llm_duration_ms: int
    llm_input_chars: int = 0
    llm_estimated_input_tokens: int = 0
    llm_prompt_versions: list[str] = Field(default_factory=list)
    llm_models: list[str] = Field(default_factory=list)
    llm_profiles: list[str] = Field(default_factory=list)
    llm_routes: list[str] = Field(default_factory=list)
    verifications_count: int = 0
    verifier_approved_count: int = 0
    verifier_rejected_count: int = 0
    expected_results: list[ExpectedFindingResult]
    expected_context_results: list[ExpectedContextResult] = Field(default_factory=list)
    extra_findings: list[Finding]
    warnings: list[str] = Field(default_factory=list)


class BenchmarkReport(BaseModel):
    cases: list[BenchmarkCaseResult]
    total: int
    passed: int
    failed: int

    @computed_field
    @property
    def expected_findings_total(self) -> int:
        return sum(len(case.expected_results) for case in self.cases)

    @computed_field
    @property
    def missed_findings_total(self) -> int:
        return sum(1 for case in self.cases for result in case.expected_results if not result.matched)

    @computed_field
    @property
    def expected_context_total(self) -> int:
        return sum(len(case.expected_context_results) for case in self.cases)

    @computed_field
    @property
    def missed_context_total(self) -> int:
        return sum(1 for case in self.cases for result in case.expected_context_results if not result.matched)

    @computed_field
    @property
    def extra_findings_total(self) -> int:
        return sum(len(case.extra_findings) for case in self.cases)


class BenchmarkCaseComparison(BaseModel):
    name: str
    status: str
    old_passed: bool | None = None
    new_passed: bool | None = None
    old_findings_count: int | None = None
    new_findings_count: int | None = None
    old_missed_expected_count: int | None = None
    new_missed_expected_count: int | None = None
    old_extra_findings_count: int | None = None
    new_extra_findings_count: int | None = None
    old_llm_duration_ms: int | None = None
    new_llm_duration_ms: int | None = None
    llm_duration_delta_ms: int | None = None
    old_llm_cache_hits: int | None = None
    new_llm_cache_hits: int | None = None
    llm_cache_hit_delta: int | None = None
    old_llm_cache_misses: int | None = None
    new_llm_cache_misses: int | None = None
    llm_cache_miss_delta: int | None = None
    old_llm_prompt_versions: list[str] = Field(default_factory=list)
    new_llm_prompt_versions: list[str] = Field(default_factory=list)
    old_verifications_count: int | None = None
    new_verifications_count: int | None = None
    old_verifier_approved_count: int | None = None
    new_verifier_approved_count: int | None = None
    old_verifier_rejected_count: int | None = None
    new_verifier_rejected_count: int | None = None
    messages: list[str] = Field(default_factory=list)


class BenchmarkComparisonSummary(BaseModel):
    old_total: int
    new_total: int
    old_passed: int
    new_passed: int
    old_failed: int
    new_failed: int
    regressions: int
    improvements: int
    added: int
    removed: int
    unchanged: int
    llm_duration_delta_ms: int
    llm_cache_hit_delta: int
    llm_cache_miss_delta: int
    old_context_misses: int = 0
    new_context_misses: int = 0
    context_miss_delta: int = 0


class BenchmarkComparisonReport(BaseModel):
    summary: BenchmarkComparisonSummary
    cases: list[BenchmarkCaseComparison]


class CaptureResult(BaseModel):
    output_dir: str
    case_path: str
    diff_path: str
    repo_dir: str
    copied_files: list[str]
    warnings: list[str] = Field(default_factory=list)
