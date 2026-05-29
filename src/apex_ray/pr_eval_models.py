from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from apex_ray.models import Finding

DEFAULT_FIRST_PASS_WINDOW_MINUTES = 15


class StrictPrEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GreptileFinding(StrictPrEvalModel):
    id: str
    source: Literal["review_comment", "summary_issue"]
    title: str
    body: str
    severity: str | None = None
    file: str | None = None
    line: int | None = None
    original_line: int | None = None
    url: str | None = None
    commit_id: str | None = None
    original_commit_id: str | None = None
    created_at: str
    updated_at: str | None = None
    first_pass: bool = True


class GreptileComment(StrictPrEvalModel):
    id: str
    source: Literal["issue_comment", "review_comment", "review"]
    author: str
    body: str
    file: str | None = None
    line: int | None = None
    original_line: int | None = None
    url: str | None = None
    commit_id: str | None = None
    original_commit_id: str | None = None
    created_at: str
    updated_at: str | None = None
    includes_created_edit: bool = False


class PullRequestEvalCase(StrictPrEvalModel):
    number: int
    title: str
    url: str
    base_ref_name: str
    head_ref_name: str
    base_sha: str
    head_sha: str
    replay_base_sha: str | None = None
    replay_head_sha: str | None = None
    merge_commit_sha: str | None = None
    created_at: str
    merged_at: str | None = None
    first_greptile_at: str | None = None
    first_pass_window_minutes: int = DEFAULT_FIRST_PASS_WINDOW_MINUTES
    diff_path: str = "pr.diff"
    greptile_comments_path: str = "greptile-comments.json"
    greptile_findings: list[GreptileFinding] = Field(default_factory=list)


class PullRequestEvalCaptureResult(StrictPrEvalModel):
    output_dir: str
    cases: list[PullRequestEvalCase] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PrEvalFindingMatch(StrictPrEvalModel):
    greptile_finding: GreptileFinding
    matched: bool
    matched_apex_title: str | None = None
    matched_apex_file: str | None = None
    matched_apex_line: int | None = None
    score: float = 0.0


class PrEvalGreptileFindingLabel(StrictPrEvalModel):
    verdict: Literal["valid", "not_issue", "out_of_scope", "unknown"] = "valid"
    title: str = ""
    file: str | None = None
    line: int | None = None
    notes: str = ""


class PrEvalApexFindingLabel(StrictPrEvalModel):
    verdict: Literal["unknown", "true_positive", "false_positive", "duplicate", "not_actionable"] = "unknown"
    title: str = ""
    file: str | None = None
    line: int | None = None
    notes: str = ""


class PrEvalLabels(StrictPrEvalModel):
    pr: int
    updated_at: str = ""
    case_status: Literal["active", "quarantined"] = "active"
    case_status_reason: str = ""
    greptile_findings: dict[str, PrEvalGreptileFindingLabel] = Field(default_factory=dict)
    apex_findings: dict[str, PrEvalApexFindingLabel] = Field(default_factory=dict)


class PrEvalCaseStatus(StrictPrEvalModel):
    number: int
    title: str = ""
    status: Literal["pending", "running", "succeeded", "partial", "failed", "timed_out", "quarantined", "skipped"]
    phase: str = ""
    started_at: str = ""
    updated_at: str = ""
    ended_at: str | None = None
    elapsed_ms: int = 0
    error: str | None = None
    eval_result_path: str | None = None
    report_path: str | None = None
    run_fingerprint: str | None = None


class PullRequestEvalRunResult(StrictPrEvalModel):
    number: int
    title: str
    url: str
    passed: bool
    status: Literal["succeeded", "partial", "failed", "timed_out", "quarantined", "skipped"] = "succeeded"
    scored: bool = True
    analysis_partial: bool = False
    coverage_partial_severity: str = "none"
    coverage_quality_gate_status: str = "disabled"
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    error_message: str | None = None
    status_path: str | None = None
    run_fingerprint: str | None = None
    greptile_findings_count: int
    ignored_greptile_findings: int = 0
    apex_findings_count: int
    matched_greptile_findings: int
    missed_greptile_findings: int
    extra_apex_findings: int
    triaged_extra_true_positives: int = 0
    triaged_extra_false_positives: int = 0
    triaged_extra_duplicates: int = 0
    triaged_extra_not_actionable: int = 0
    triaged_extra_unknown: int = 0
    context_packs_count: int
    reviewed_context_packs_count: int = 0
    unreviewed_context_packs_count: int = 0
    residual_p0_context_packs_count: int = 0
    residual_p1_context_packs_count: int = 0
    failed_llm_review_runs_count: int = 0
    failed_llm_verify_runs_count: int = 0
    llm_coverage_ratio: float = 0.0
    source_changed_line_coverage_ratio: float = 0.0
    high_risk_coverage_ratio: float = 0.0
    llm_runs_count: int
    llm_duration_ms: int = 0
    llm_input_chars: int = 0
    llm_estimated_input_tokens: int = 0
    llm_cache_hits: int = 0
    llm_cache_misses: int = 0
    report_path: str
    markdown_path: str
    labels_path: str | None = None
    matches: list[PrEvalFindingMatch] = Field(default_factory=list)
    extra_findings: list[Finding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PullRequestEvalRunReport(StrictPrEvalModel):
    model_config = ConfigDict(extra="ignore")

    cases: list[PullRequestEvalRunResult]
    total: int
    passed: int
    failed: int
    partial: int = 0
    timed_out: int = 0
    quarantined: int = 0
    skipped: int = 0

    @computed_field
    @property
    def greptile_findings_total(self) -> int:
        return sum(case.greptile_findings_count for case in self.cases if case.scored)

    @computed_field
    @property
    def matched_greptile_findings_total(self) -> int:
        return sum(case.matched_greptile_findings for case in self.cases if case.scored)

    @computed_field
    @property
    def missed_greptile_findings_total(self) -> int:
        return sum(case.missed_greptile_findings for case in self.cases if case.scored)

    @computed_field
    @property
    def extra_apex_findings_total(self) -> int:
        return sum(case.extra_apex_findings for case in self.cases if case.scored)

    @computed_field
    @property
    def triaged_extra_true_positives_total(self) -> int:
        return sum(case.triaged_extra_true_positives for case in self.cases)

    @computed_field
    @property
    def triaged_extra_false_positives_total(self) -> int:
        return sum(case.triaged_extra_false_positives for case in self.cases)

    @computed_field
    @property
    def triaged_extra_duplicates_total(self) -> int:
        return sum(case.triaged_extra_duplicates for case in self.cases)

    @computed_field
    @property
    def triaged_extra_not_actionable_total(self) -> int:
        return sum(case.triaged_extra_not_actionable for case in self.cases)

    @computed_field
    @property
    def triaged_extra_unknown_total(self) -> int:
        return sum(case.triaged_extra_unknown for case in self.cases)

    @computed_field
    @property
    def estimated_input_tokens_total(self) -> int:
        return sum(case.llm_estimated_input_tokens for case in self.cases)


class PrEvalTelemetryCase(StrictPrEvalModel):
    number: int
    passed: bool
    status: str = "succeeded"
    scored: bool = True
    duration_ms: int = 0
    coverage_partial_severity: str = "none"
    coverage_quality_gate_status: str = "disabled"
    greptile_findings_count: int
    ignored_greptile_findings: int = 0
    matched_greptile_findings: int
    missed_greptile_findings: int
    extra_apex_findings: int
    triaged_extra_true_positives: int = 0
    triaged_extra_false_positives: int = 0
    triaged_extra_duplicates: int = 0
    triaged_extra_not_actionable: int = 0
    triaged_extra_unknown: int = 0
    context_packs_count: int
    reviewed_context_packs_count: int = 0
    unreviewed_context_packs_count: int = 0
    residual_p0_context_packs_count: int = 0
    residual_p1_context_packs_count: int = 0
    failed_llm_review_runs_count: int = 0
    failed_llm_verify_runs_count: int = 0
    llm_coverage_ratio: float = 0.0
    source_changed_line_coverage_ratio: float = 0.0
    high_risk_coverage_ratio: float = 0.0
    llm_runs_count: int
    llm_duration_ms: int = 0
    llm_estimated_input_tokens: int = 0
    llm_cache_hits: int = 0
    llm_cache_misses: int = 0


class PrEvalTelemetryEntry(StrictPrEvalModel):
    run_id: str
    created_at: str
    source_repo: str
    output_dir: str
    total: int
    passed: int
    failed: int
    partial: int = 0
    timed_out: int = 0
    quarantined: int = 0
    greptile_findings_total: int
    matched_greptile_findings_total: int
    missed_greptile_findings_total: int
    extra_apex_findings_total: int
    triaged_extra_true_positives_total: int = 0
    triaged_extra_false_positives_total: int = 0
    triaged_extra_duplicates_total: int = 0
    triaged_extra_not_actionable_total: int = 0
    triaged_extra_unknown_total: int = 0
    estimated_input_tokens_total: int = 0
    cases: list[PrEvalTelemetryCase] = Field(default_factory=list)
