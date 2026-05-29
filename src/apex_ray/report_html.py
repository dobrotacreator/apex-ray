from html import escape

from apex_ray.models import Finding, ReviewReport
from apex_ray.report_coverage import _unreviewed_pack_reason
from apex_ray.report_formatting import summarize_notes


def render_html(report: ReviewReport) -> str:
    warnings = [*report.diff.warnings]
    for analyzer_result in report.analyzer_results:
        warnings.extend(analyzer_result.warnings)
    coverage = report.llm_coverage
    memory = report.memory_summary
    findings_html = "\n".join(_finding_html(finding) for finding in report.findings) or "<p>No findings.</p>"
    memory_html = _html_list(
        [
            f"<code>{escape(card_id)}</code> - {escape(reason)}"
            for card_id, reason in list(memory.omitted_card_reasons.items())[:50]
        ],
        empty="No omitted memory cards.",
    )
    unreviewed_html = _html_list(
        [
            f"<code>{escape(pack_id)}</code> - {escape(_unreviewed_pack_reason(pack_id, coverage))}"
            for pack_id in coverage.unreviewed_context_pack_ids[:50]
        ],
        empty="No unreviewed context packs.",
    )
    routes_html = _html_list(
        [
            (
                f"{escape(route.kind)}/{escape(route.provider)} "
                f"<code>{escape(route.profile or route.model or 'default')}</code> "
                f"({escape(route.status)}): {route.runs} runs, "
                f"{route.findings_count} findings, ~{route.estimated_input_tokens} input tokens, "
                f"{route.cache_hits} cache hits / {route.cache_misses} misses"
            )
            for route in coverage.routes
        ],
        empty="No model routes recorded.",
    )
    slices_html = _html_list(
        [
            (
                f"<code>{escape(item.slice)}</code>: "
                f"{item.reviewed_context_packs}/{item.total_context_packs} reviewed, "
                f"{item.deep_reviewed_context_packs}/{item.shallow_reviewed_context_packs} deep/shallow, "
                f"{item.reviewed_high_risk_context_packs}/{item.high_risk_context_packs} high-risk, "
                f"residual {escape(item.residual_priority or 'none')}"
            )
            for item in coverage.slice_coverage
        ],
        empty="No slice coverage recorded.",
    )
    warnings_html = _html_list([escape(warning) for warning in warnings], empty="No warnings.")
    context_rows = "\n".join(
        "<tr>"
        f"<td><code>{escape(pack.id)}</code></td>"
        f"<td>{escape(pack.file)}</td>"
        f"<td>{pack.stats.estimated_chars}</td>"
        f"<td>{pack.stats.memory_cards}</td>"
        f"<td>{'yes' if pack.stats.truncated else 'no'}</td>"
        f"<td>{escape('; '.join(summarize_notes(pack.stats.truncation_notes)))}</td>"
        "</tr>"
        for pack in sorted(report.context_packs, key=lambda item: item.stats.estimated_chars, reverse=True)[:50]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Apex Ray Review</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2937; }}
    main {{ max-width: 1180px; margin: 0 auto; }}
    h1, h2 {{ color: #111827; }}
    code {{ background: #f3f4f6; padding: 1px 4px; border-radius: 4px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 16px 0; }}
    .metric {{ border: 1px solid #d1d5db; border-radius: 8px; padding: 12px; background: #fff; }}
    .metric strong {{ display: block; font-size: 24px; color: #111827; }}
    .finding {{ border-left: 4px solid #b91c1c; padding: 10px 12px; background: #fff7ed; margin: 12px 0; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f9fafb; }}
    details {{ border: 1px solid #d1d5db; border-radius: 8px; padding: 10px 12px; margin: 12px 0; }}
    summary {{ cursor: pointer; font-weight: 600; }}
  </style>
</head>
<body>
<main>
  <h1>Apex Ray Review</h1>
  <div class="grid">
    <div class="metric"><span>Files changed</span><strong>{report.diff.stats.files_changed}</strong></div>
    <div class="metric"><span>Findings</span><strong>{len(report.findings)}</strong></div>
    <div class="metric"><span>Context packs</span><strong>{len(report.context_packs)}</strong></div>
    <div class="metric"><span>LLM reviewed packs</span><strong>{coverage.reviewed_context_packs}/{coverage.total_context_packs}</strong></div>
    <div class="metric"><span>Deep / shallow packs</span><strong>{coverage.deep_reviewed_context_packs}/{coverage.shallow_reviewed_context_packs}</strong></div>
    <div class="metric"><span>Memory cards</span><strong>{memory.applied_cards}/{memory.loaded_cards}</strong></div>
    <div class="metric"><span>Coverage gate</span><strong>{escape(coverage.quality_gate_status)}</strong></div>
    <div class="metric"><span>Estimated input tokens</span><strong>~{coverage.estimated_input_tokens}</strong></div>
    <div class="metric"><span>LLM duration</span><strong>{coverage.total_duration_ms}ms</strong></div>
  </div>

  <h2>Findings</h2>
  {findings_html}

  <h2>LLM Coverage</h2>
  <ul>
    <li>Enabled: <code>{str(coverage.enabled).lower()}</code></li>
    <li>Coverage mode: <code>{escape(str(coverage.coverage_mode))}</code></li>
    <li>Review runs: <code>{coverage.review_runs}</code>; verifier runs: <code>{coverage.verify_runs}</code></li>
    <li>Source changed-line coverage: <code>{coverage.source_changed_line_coverage_ratio:.1%}</code></li>
    <li>High-risk coverage: <code>{coverage.high_risk_coverage_ratio:.1%}</code></li>
    <li>Cache: <code>{coverage.cache_hits}</code> hits / <code>{coverage.cache_misses}</code> misses</li>
    <li>Over-budget packs: <code>{len(coverage.over_budget_context_pack_ids)}</code></li>
    <li>Over-token-budget packs: <code>{len(coverage.over_token_budget_context_pack_ids)}</code></li>
    <li>Truncated packs: <code>{len(coverage.truncated_context_pack_ids)}</code></li>
    <li>Residual P0/P1 packs: <code>{len(coverage.residual_risk_p0_context_pack_ids)}</code> / <code>{len(coverage.residual_risk_p1_context_pack_ids)}</code></li>
    <li>Cluster/file/symbol packs: <code>{coverage.cluster_context_packs}</code> / <code>{coverage.file_context_packs}</code> / <code>{coverage.symbol_context_packs}</code></li>
  </ul>
  <details open><summary>Model Routes</summary>{routes_html}</details>
  <details><summary>Slice Coverage</summary>{slices_html}</details>
  <details><summary>Unreviewed Context Packs</summary>{unreviewed_html}</details>

  <h2>Memory</h2>
  <ul>
    <li>Enabled: <code>{str(memory.enabled).lower()}</code></li>
    <li>Loaded cards: <code>{memory.loaded_cards}</code></li>
    <li>Matched/applied/omitted: <code>{memory.matched_cards}</code> / <code>{memory.applied_cards}</code> / <code>{memory.omitted_cards}</code></li>
    <li>Prompt chars: <code>{memory.total_prompt_chars}</code></li>
  </ul>
  <details><summary>Omitted Memory Cards</summary>{memory_html}</details>

  <h2>Context Budget</h2>
  <table>
    <thead><tr><th>Pack</th><th>File</th><th>Chars</th><th>Memory</th><th>Truncated</th><th>Notes</th></tr></thead>
    <tbody>{context_rows or '<tr><td colspan="6">No context packs.</td></tr>'}</tbody>
  </table>

  <h2>Warnings</h2>
  {warnings_html}
</main>
</body>
</html>
"""


def _finding_html(finding: Finding) -> str:
    location = f"{finding.file}:{finding.line}" if finding.line else finding.file
    return (
        '<article class="finding">'
        f"<h3>{escape(finding.title)}</h3>"
        f"<p><strong>{escape(str(finding.severity)).title()}</strong> "
        f"at <code>{escape(location)}</code>, confidence <code>{escape(str(finding.confidence))}</code></p>"
        f"<p><strong>Failure mode:</strong> {escape(finding.failure_mode)}</p>"
        f"<p><strong>Evidence:</strong> {escape(finding.evidence)}</p>"
        f"<p><strong>Suggested fix:</strong> {escape(finding.suggested_fix)}</p>"
        f"<p><strong>Suggested test:</strong> {escape(finding.suggested_test)}</p>"
        f"<p><strong>Context pack:</strong> <code>{escape(finding.context_pack_id or 'n/a')}</code></p>"
        "</article>"
    )


def _html_list(items: list[str], *, empty: str) -> str:
    if not items:
        return f"<p>{escape(empty)}</p>"
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"
