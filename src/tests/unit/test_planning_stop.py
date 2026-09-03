"""Typed stop authority at the rejected global plan-gate boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from vfx_harness.agents.planner.planning_stop import (
    publish_global_plan_gate_stop,
    publish_layer_plan_gate_stop,
)
from vfx_harness.agents.planner.types import PlanGateFailure, PlanLoopResult
from vfx_harness.domain.stop_amendment_transactions import amendment_after_source
from vfx_harness.domain.stop_envelopes import StopEnvelope
from vfx_harness.domain.stop_transactions import (
    EngineeringRouteCommitted,
    PublishValidatedAmendmentTarget,
    RouteEngineeringTarget,
    SelectedAuthorityAmendmentCommitted,
    action_idempotency_key,
)
from vfx_harness.evaluation.plan_gate import Finding, GateResult
from vfx_harness.observability import run_artifacts


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rejected_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
    where: str = "requirements.json#/requirements/0",
    generated_at: str | None = None,
) -> tuple[run_artifacts.RunLayout, PlanLoopResult]:
    shot = tmp_path / run_id
    shot.mkdir()
    candidate = shot / "runs" / run_id / "scratch" / "plan-workspace" / "plans" / "global.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("# rejected candidate\n", encoding="utf-8")
    monkeypatch.setenv(run_artifacts.ENV, str(shot / "runs" / run_id))
    layout = run_artifacts.create(shot, run_id, shot_id="fixture")
    result = GateResult(
        "fixture",
        findings=[
            Finding(
                check="requirement-closure",
                blocking=True,
                where=where,
                what="a normative requirement has no structural owner",
                fix="bind the requirement to one declared owner",
            ),
            Finding(
                check="citation-strength",
                blocking=False,
                where="plans/global.md",
                what="a supporting citation is weaker than claimed",
            ),
        ],
    )
    report = result.to_dict(outcome="budget")
    if generated_at is not None:
        report["generated_at"] = generated_at
    layout.write_report("plan_gate", report)
    return layout, PlanLoopResult(candidate, "budget", 1)


def test_structural_gate_blockers_publish_one_authority_action_and_exact_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, result = _rejected_candidate(tmp_path, monkeypatch, run_id="plan-stop-1")

    envelope = publish_global_plan_gate_stop(layout, result)

    assert envelope.stage == "plan_gate"
    assert envelope.stop_class == "authority_defect"
    assert envelope.retryable is False
    assert envelope.identity.bundle_digest is None
    assert envelope.identity.view_digest is None
    assert envelope.identity.candidate_digest == _sha(result.path)
    assert [action.transaction_id for action in envelope.actions] == ["publish_validated_amendment"]
    action = envelope.actions[0]
    assert isinstance(action.target, PublishValidatedAmendmentTarget)
    assert action.target.scope == "global_plan"
    assert action.target.base_authority.selection == "absent"
    assert action.target.base_authority.bundle is None
    assert action.target.base_authority.effective_view is None
    assert action.target.base_authority.digest == envelope.authoritative_before_digest
    assert action.target.layer_id is None
    assert action.target.findings
    assert isinstance(action.postcondition, SelectedAuthorityAmendmentCommitted)
    assert action.postcondition.gate_policy_id == ("structural-authority/runtime-falsification-v1")
    assert action.postcondition.base_authority_digest == envelope.authoritative_before_digest
    assert action.postcondition.required_after_source == "bundle"
    assert StopEnvelope.from_dict(envelope.as_dict(), "envelope") == envelope
    assert "apply-replan" in envelope.next_action
    evidence = json.loads((layout.reports / "plan-stop-evidence.json").read_text(encoding="utf-8"))
    audit = json.loads((layout.reports / "plan-stop-audit.json").read_text(encoding="utf-8"))
    assert evidence["candidate"]["sha256"] == _sha(result.path)
    assert evidence["gate_report"]["policy"] == "structural-authority/runtime-falsification-v1"
    assert "generated_at" not in evidence["gate_report"]
    assert evidence["blocking_findings"][0]["finding_id"] in envelope.cause.finding_ids
    assert evidence["blocking_findings"][0]["causal_fact"]["what"] == (
        "a normative requirement has no structural owner"
    )
    assert evidence["authoritative_before"]["selection"] == "absent"
    assert evidence["authoritative_before_digest"] == envelope.authoritative_before_digest
    assert "run_id" not in evidence
    assert "locator" not in json.dumps(evidence, sort_keys=True)
    assert "action_digest" not in evidence
    assert "envelope_digest" not in evidence
    causal_ref = action.target.findings[0].evidence
    assert envelope.evidence_refs == (causal_ref,)
    assert causal_ref.locator == f"runs/{layout.run_id}/reports/plan-stop-evidence.json"
    assert causal_ref.sha256 == _sha(layout.reports / "plan-stop-evidence.json")
    assert causal_ref.record_digest == evidence["record_digest"]
    assert audit["run_id"] == layout.run_id
    assert audit["audit"]["gate_report_record"]["sha256"] == _sha(layout.reports / "plan_gate.json")
    assert audit["audit"]["gate_report_record"]["locator"] == (f"runs/{layout.run_id}/reports/plan_gate.json")


def test_action_identity_excludes_run_timestamp_and_file_locators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_layout, first_result = _rejected_candidate(
        tmp_path,
        monkeypatch,
        run_id="plan-stop-a",
        where="requirements.json#/requirements/0",
        generated_at="2026-01-01T00:00:00+00:00",
    )
    first = publish_global_plan_gate_stop(first_layout, first_result)
    second_layout, second_result = _rejected_candidate(
        tmp_path,
        monkeypatch,
        run_id="plan-stop-b",
        where="requirements.json#/requirements/0",
        generated_at="2099-12-31T23:59:59+00:00",
    )
    second = publish_global_plan_gate_stop(second_layout, second_result)

    assert first.cause_fingerprint == second.cause_fingerprint
    assert first.identity.run_id != second.identity.run_id
    assert first.authoritative_before_digest == second.authoritative_before_digest
    assert first.attempt_evidence_digest == second.attempt_evidence_digest
    assert first.actions[0].precondition_digest == second.actions[0].precondition_digest
    assert first.actions[0].digest == second.actions[0].digest
    assert first.evidence_refs[0].digest == second.evidence_refs[0].digest
    assert first.evidence_refs[0].sha256 == second.evidence_refs[0].sha256
    assert first.evidence_refs[0].locator != second.evidence_refs[0].locator
    assert action_idempotency_key(
        first.actions[0],
        authoritative_before_digest=first.authoritative_before_digest,
        attempt_evidence_digest=first.attempt_evidence_digest,
    ) == action_idempotency_key(
        second.actions[0],
        authoritative_before_digest=second.authoritative_before_digest,
        attempt_evidence_digest=second.attempt_evidence_digest,
    )
    first_packet = (first_layout.reports / "plan-stop-evidence.json").read_bytes()
    second_packet = (second_layout.reports / "plan-stop-evidence.json").read_bytes()
    assert first_packet == second_packet
    assert (first_layout.reports / "plan-stop-audit.json").read_bytes() != (
        second_layout.reports / "plan-stop-audit.json"
    ).read_bytes()


def test_duplicate_finding_text_preserves_each_exact_semantic_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, result = _rejected_candidate(
        tmp_path,
        monkeypatch,
        run_id="plan-stop-duplicate-scope",
    )
    report_path = layout.reports / "plan_gate.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    duplicate = dict(report["findings"][0])
    duplicate["where"] = "requirements.json#/requirements/1"
    report["findings"].insert(1, duplicate)
    report["blocking_count"] = 2
    report["signature"] = "|".join(
        sorted(f"{row['check']}:{row['where']}:{row['what'][:60]}" for row in report["findings"])
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")
    result = PlanLoopResult(result.path, result.outcome, 2)

    envelope = publish_global_plan_gate_stop(layout, result)
    packet = json.loads((layout.reports / "plan-stop-evidence.json").read_text(encoding="utf-8"))
    action = envelope.actions[0]
    assert isinstance(action.target, PublishValidatedAmendmentTarget)
    assert len(action.target.findings) == 2
    assert len({finding.record_id for finding in action.target.findings}) == 2
    assert {row["causal_fact"]["where"] for row in packet["blocking_findings"]} == {
        "requirements.json#/requirements/0",
        "requirements.json#/requirements/1",
    }


def test_cause_changes_when_the_violated_structural_fact_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_layout, first_result = _rejected_candidate(
        tmp_path,
        monkeypatch,
        run_id="plan-stop-fact-a",
    )
    first = publish_global_plan_gate_stop(first_layout, first_result)
    second_layout, second_result = _rejected_candidate(
        tmp_path,
        monkeypatch,
        run_id="plan-stop-fact-b",
    )
    report_path = second_layout.reports / "plan_gate.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["findings"][0]["what"] = "a declared owner points to no current layer"
    report["signature"] = "|".join(
        sorted(f"{row['check']}:{row['where']}:{row['what'][:60]}" for row in report["findings"])
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")
    second = publish_global_plan_gate_stop(second_layout, second_result)

    assert first.cause_fingerprint != second.cause_fingerprint
    assert first.cause.finding_ids != second.cause.finding_ids
    assert first.attempt_evidence_digest != second.attempt_evidence_digest
    assert first.actions[0].precondition_digest != second.actions[0].precondition_digest
    assert first.actions[0].digest != second.actions[0].digest
    assert first.evidence_refs[0].digest != second.evidence_refs[0].digest


@pytest.mark.parametrize("failure", ["missing", "wrong_schema", "count_mismatch"])
def test_missing_or_inconsistent_gate_evidence_routes_to_engineering_without_repair_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    layout, result = _rejected_candidate(
        tmp_path,
        monkeypatch,
        run_id=f"plan-stop-{failure}",
    )
    report = layout.reports / "plan_gate.json"
    if failure == "missing":
        report.unlink()
    else:
        value = json.loads(report.read_text(encoding="utf-8"))
        if failure == "wrong_schema":
            value["schema"] = "vfx-harness.plan-gate/v0"
        else:
            value["blocking_count"] = 7
        report.write_text(json.dumps(value), encoding="utf-8")

    envelope = publish_global_plan_gate_stop(layout, result)

    assert envelope.stop_class == "harness_defect"
    assert envelope.stage == "planning"
    assert [action.transaction_id for action in envelope.actions] == ["route_engineering"]
    action = envelope.actions[0]
    assert isinstance(action.target, RouteEngineeringTarget)
    assert isinstance(action.postcondition, EngineeringRouteCommitted)
    causal_ref = action.target.defect_record.evidence
    assert envelope.evidence_refs == (causal_ref,)
    assert causal_ref.locator == f"runs/{layout.run_id}/reports/plan-stop-evidence.json"
    assert (layout.reports / "plan-stop-audit.json").is_file()
    causal_packet = json.loads((layout.reports / "plan-stop-evidence.json").read_text(encoding="utf-8"))
    assert causal_packet["evidence_kind"] == "harness_defect"
    assert "run_id" not in causal_packet
    assert "locator" not in json.dumps(causal_packet, sort_keys=True)
    assert all(
        action.transaction_id
        not in {
            "publish_validated_amendment",
            "apply_revision_checked_replan",
        }
        for action in envelope.actions
    )
    assert envelope.cause.invariant_id == "planning_gate_requires_typed_evidence"


@pytest.mark.parametrize("pointer_state", ["malformed", "invalid", "unreadable"])
def test_corrupt_selected_authority_routes_to_engineering_without_amendment_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pointer_state: str,
) -> None:
    layout, result = _rejected_candidate(
        tmp_path,
        monkeypatch,
        run_id=f"plan-stop-pointer-{pointer_state}",
    )
    pointer = layout.shot / "plans" / "current.json"
    pointer.parent.mkdir(parents=True)
    if pointer_state == "malformed":
        pointer.write_text("{", encoding="utf-8")
    elif pointer_state == "invalid":
        pointer.write_text("{}\n", encoding="utf-8")
    else:
        pointer.mkdir()

    envelope = publish_global_plan_gate_stop(layout, result)

    assert envelope.stop_class == "harness_defect"
    assert [action.transaction_id for action in envelope.actions] == ["route_engineering"]
    assert isinstance(envelope.actions[0].target, RouteEngineeringTarget)
    assert all(
        action.transaction_id != "publish_validated_amendment"
        for action in envelope.actions
    )
    packet = json.loads(
        (layout.reports / "plan-stop-evidence.json").read_text(encoding="utf-8")
    )
    assert packet["authoritative_before"]["selection"] == pointer_state
    assert f"selected_authority_{pointer_state}" in packet["issues"]


def test_harness_defect_action_identity_is_stable_across_run_audit_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_layout, first_result = _rejected_candidate(
        tmp_path,
        monkeypatch,
        run_id="plan-stop-harness-a",
        where="requirements.json#/requirements/0",
        generated_at="2026-01-01T00:00:00+00:00",
    )
    second_layout, second_result = _rejected_candidate(
        tmp_path,
        monkeypatch,
        run_id="plan-stop-harness-b",
        where="requirements.json#/requirements/0",
        generated_at="2099-12-31T23:59:59+00:00",
    )
    for layout in (first_layout, second_layout):
        path = layout.reports / "plan_gate.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        report["schema"] = "vfx-harness.plan-gate/v0"
        path.write_text(json.dumps(report), encoding="utf-8")

    first = publish_global_plan_gate_stop(first_layout, first_result)
    second = publish_global_plan_gate_stop(second_layout, second_result)

    assert first.stop_class == second.stop_class == "harness_defect"
    assert first.attempt_evidence_digest == second.attempt_evidence_digest
    assert first.actions[0].precondition_digest == second.actions[0].precondition_digest
    assert first.actions[0].digest == second.actions[0].digest
    assert first.evidence_refs[0].digest == second.evidence_refs[0].digest
    assert (first_layout.reports / "plan-stop-evidence.json").read_bytes() == (
        second_layout.reports / "plan-stop-evidence.json"
    ).read_bytes()


def test_plan_gate_failure_carries_the_compiled_envelope_to_run_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, result = _rejected_candidate(tmp_path, monkeypatch, run_id="plan-stop-exit")
    envelope = publish_global_plan_gate_stop(layout, result)

    with pytest.raises(PlanGateFailure) as raised:
        raise PlanGateFailure(result, stop_envelope=envelope)

    assert raised.value.code == 3
    assert raised.value.stop_envelope is envelope
    assert raised.value.terminal_cause == "authority_defect"
    assert "1 blocking structural finding" in str(raised.value)


def test_a_layer_amendment_lands_in_the_jit_head_and_a_global_one_in_a_bundle() -> None:
    """Run 20260903T180933Z-ea3e7a: the builder restated this pairing and got it wrong.

    The scope was parameterized but the postcondition still declared the global
    ``required_after_source``, so the domain refused the envelope at construction and
    the driver died with an untyped traceback.  Producers now ask the domain, so a stop
    cannot be built that its own validator rejects.
    """
    assert amendment_after_source("global_plan") == "bundle"
    assert amendment_after_source("layer_view") == "jit"


def test_a_layer_view_rejection_without_selected_authority_stays_global(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no layer view to amend until authority is selected."""
    layout, result = _rejected_candidate(tmp_path, monkeypatch, run_id="layer-plan-stop-1")

    with pytest.raises(ValueError, match="layer-view amendment requires selected"):
        publish_layer_plan_gate_stop(layout, result, layer_id="2")


def test_the_global_scope_keeps_its_own_amendment_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared builder must not leak the layer contract back into global stops."""
    layout, result = _rejected_candidate(tmp_path, monkeypatch, run_id="global-plan-stop-1")

    envelope = publish_global_plan_gate_stop(layout, result)

    action = envelope.actions[0]
    assert action.target.scope == "global_plan"
    assert action.target.layer_id is None
    assert action.postcondition.required_after_source == "bundle"
    assert envelope.identity.layer_id is None
