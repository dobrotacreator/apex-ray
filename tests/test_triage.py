import json
from datetime import UTC, datetime, timedelta

from apex_ray.models import ContextPack, Finding, FindingConfidence, FindingSeverity, TriageConfig
from apex_ray.triage import (
    TriageState,
    add_or_replace_suppression,
    apply_suppressions,
    create_suppression,
    finding_candidate,
    finding_snapshot,
    load_triage_state,
    prune_triage_state,
)


def _finding() -> Finding:
    return Finding(
        title="Missing tenant predicate",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        file="src/orders.ts",
        line=84,
        failure_mode="The changed query can return another tenant's order.",
        evidence="The diff removes tenantId from the lookup predicate.",
        suggested_fix="Restore the tenantId predicate.",
        suggested_test="Add a cross-tenant lookup regression test.",
        context_pack_id="src/orders.ts#getOrder:1",
    )


def test_triage_suppression_matches_only_same_context_pack() -> None:
    finding = _finding()
    original_pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
        diff_snippet=["@@ -1 +1 @@", "-old", "+new"],
    )
    changed_pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
        diff_snippet=["@@ -1 +1 @@", "-old", "+different"],
    )
    now = datetime(2026, 6, 1, tzinfo=UTC)
    suppression, _ = create_suppression(
        snapshot=finding_snapshot(finding, original_pack),
        reason="Known false positive.",
        config=TriageConfig(default_expiry_days=14),
        now=now,
    )
    state = add_or_replace_suppression(TriageState(), suppression)

    matched = apply_suppressions([finding_candidate(finding, original_pack)], state, now=now)
    stale = apply_suppressions([finding_candidate(finding, changed_pack)], state, now=now)

    assert matched.remaining_findings == []
    assert len(matched.suppressed_findings) == 1
    assert stale.remaining_findings == [finding]
    assert stale.stale_count == 1
    assert stale.state.suppressions == []


def test_triage_prunes_expired_suppressions() -> None:
    finding = _finding()
    now = datetime(2026, 6, 1, tzinfo=UTC)
    suppression, _ = create_suppression(
        snapshot=finding_snapshot(finding),
        reason="Known false positive.",
        config=TriageConfig(default_expiry_days=1),
        now=now - timedelta(days=2),
    )
    state = add_or_replace_suppression(TriageState(), suppression)

    result = prune_triage_state(state, TriageConfig(default_expiry_days=1), now=now)

    assert result.state.suppressions == []
    assert result.expired_count == 1
    assert result.events[0].event == "suppression_expired"


def test_triage_loads_legacy_scope_base_ref(tmp_path) -> None:
    finding = _finding()
    pack = ContextPack(
        id=finding.context_pack_id,
        file=finding.file,
        diff_snippet=["@@ -1 +1 @@", "-old", "+new"],
    )
    now = datetime(2026, 6, 1, tzinfo=UTC)
    suppression, _ = create_suppression(
        snapshot=finding_snapshot(finding, pack),
        reason="Known false positive.",
        config=TriageConfig(default_expiry_days=14),
        now=now,
        target_base_ref="main",
    )
    payload = TriageState(suppressions=[suppression]).model_dump(mode="json")
    payload["suppressions"][0]["scope_base_ref"] = payload["suppressions"][0].pop("target_base_ref")
    state_path = tmp_path / "triage-state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    state = load_triage_state(state_path)

    assert state.suppressions[0].target_base_ref == "main"
    wrong_base = apply_suppressions([finding_candidate(finding, pack)], state, target_base_ref="release", now=now)
    matching_base = apply_suppressions([finding_candidate(finding, pack)], state, target_base_ref="main", now=now)
    assert wrong_base.remaining_findings == [finding]
    assert matching_base.remaining_findings == []
