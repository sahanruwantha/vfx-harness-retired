"""Test-only adapters that exercise the public claimed work-unit lifecycle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vfx_harness.domain.unit_attempts import UnitAttemptClaim
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import WorkUnit, canonical_unit_script_path
from vfx_harness.orchestration import (
    unit_evaluation_receipts,
    unit_state,
    unit_state_claims,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.plan_bundle_integrity import read_real_file_snapshot
from vfx_harness.orchestration.unit_evaluation_receipts import ExecutedReplayInput

ABSENT_SELECTION_TOKEN = AuthoritySelectionToken(0, None, 0, None)


def executed_replay_input(
    folder: str | Path,
    script_path: str,
    *,
    source_path: str | Path | None = None,
) -> ExecutedReplayInput:
    """Capture exact fixture bytes as though the canonical evaluator executed them."""

    root = Path(folder).expanduser().absolute()
    source = root / script_path if source_path is None else Path(source_path)
    snapshot = read_real_file_snapshot(root, source, "fixture replay input")
    return ExecutedReplayInput(
        script_path=script_path,
        script_sha256=snapshot.sha256,
        source_binding=snapshot.binding,
    )


def synthetic_completion_receipt(
    layer_id: str,
    unit_id: str,
    passed_evidence: set[tuple[str, str]],
) -> UnitCompletionReceipt:
    """Make a strict typed receipt for isolated resolution-ledger tests."""

    claim = UnitAttemptClaim.mint(
        attempt_revision=1,
        run_id="fixture-resolution",
        layer_id=str(layer_id),
        unit_id=unit_id,
        unit_digest="a" * 64,
        plan_hash="b" * 64,
        selection_token=ABSENT_SELECTION_TOKEN.to_dict(),
        phase="building",
        at="2026-01-01T00:00:00+00:00",
    )
    return UnitCompletionReceipt.mint(
        claim=claim,
        checkpoint={"fixture": "resolution"},
        script_path=canonical_unit_script_path(str(layer_id), unit_id),
        script_hash="c" * 64,
        evaluation_receipt_locator=(
            f"runs/{claim.run_id}/checkpoints/unit-evaluations/{claim.claim_id}.json"
        ),
        evaluation_receipt_sha256="d" * 64,
        evaluation_receipt_digest="e" * 64,
        passed_evidence=passed_evidence,
        completed_at="2026-01-01T00:00:01+00:00",
    )


def completion_evidence(layer_id: str, unit: WorkUnit) -> set[tuple[str, str]]:
    """Exact evidence keys a completed fixture unit is allowed to receipt."""

    rows = {
        (binding.kind, binding.id)
        for claim in unit.evaluation.claims
        if claim.required
        for binding in claim.evidence
    }
    rows.add(("replay", f"{layer_id}.{unit.id}"))
    return rows


def claim_for_build(
    folder: str | Path,
    layer_id: str,
    units: tuple[WorkUnit, ...],
    unit_id: str,
    *,
    plan_hash: str,
    eligible_passed: set[str] | None = None,
    selection_token: AuthoritySelectionToken = ABSENT_SELECTION_TOKEN,
) -> UnitAttemptClaim:
    planning = unit_state_claims.claim_ready_unit_for_planning(
        folder,
        layer_id,
        unit_id,
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=eligible_passed,
        run_id=f"fixture-{layer_id}-{unit_id}",
        selection_token=selection_token,
        reason="fixture dependency closure proved ready",
    )
    return unit_state_claims.claim_ready_unit_for_build(
        folder,
        layer_id,
        unit_id,
        units,
        planning,
        expected_plan_hash=plan_hash,
        eligible_passed=eligible_passed,
        run_id=planning.run_id,
        selection_token=selection_token,
        reason="fixture plan passed its gate",
    )


def freeze_unit(
    folder: str | Path,
    layer_id: str,
    unit: WorkUnit,
    attempt: UnitAttemptClaim,
    *,
    active_contract_ids=(),
    candidate_hash: str = "missing",
    settings_hash: str = "fixture-settings",
    script_hash: str | None = None,
    input_hash: str = "fixture-input",
    layer_active_vis_ids=(),
    selection_token: AuthoritySelectionToken = ABSENT_SELECTION_TOKEN,
) -> dict:
    script = Path(folder) / canonical_unit_script_path(str(layer_id), unit.id)
    script.parent.mkdir(parents=True, exist_ok=True)
    if not script.is_file():
        script.write_text(
            f"# deterministic fixture unit {layer_id}.{unit.id}\npass\n",
            encoding="utf-8",
        )
    observed_script_hash = hashlib.sha256(script.read_bytes()).hexdigest()
    if script_hash is not None and script_hash != observed_script_hash:
        raise AssertionError(
            "fixture script_hash must identify the canonical unit script bytes"
        )
    return unit_state.freeze_checkpoint(
        folder,
        layer_id,
        unit,
        active_contract_ids=active_contract_ids,
        candidate_hash=candidate_hash,
        settings_hash=settings_hash,
        script_hash=observed_script_hash,
        input_hash=input_hash,
        layer_active_vis_ids=layer_active_vis_ids,
        attempt=attempt,
        selection_token=selection_token,
    )


def publish_passed_evaluation(
    folder: str | Path,
    layer_id: str,
    unit: WorkUnit,
    attempt: UnitAttemptClaim,
    *,
    candidate_path: str | None = None,
    milestone_id: str | None = None,
):
    """Publish independent evaluator rows for a fixture's exact claimed script."""

    evidence = [
        {
            "id": identifier,
            "source": "interface_contract" if kind == "scene_contract" else kind,
            "pass": True,
            "authoritative": True,
        }
        for kind, identifier in sorted(completion_evidence(layer_id, unit))
        if kind != "replay"
    ]
    verdicts = [
        (
            (point.frame, point.ref),
            {
                "pass": True,
                "mean": 5.0,
                "issues": [],
                "evidence": evidence,
                "evidence_kind": "executable_only",
                "decided_by": "unit_executable_evidence",
                "judge_conflict": False,
                "contract_gap": False,
            },
        )
        for point in unit.evaluation.judges
    ]
    script_path = canonical_unit_script_path(str(layer_id), unit.id)
    canonical_rounds = [
        {
            "kind": "canonical",
            "render": "",
            "pass": True,
            "evidence": evidence,
            "decided_by": "unit_executable_evidence",
            "judge_conflict": False,
            "contract_gap": False,
        }
        for _point in unit.evaluation.judges
    ]
    slot = {
        "run_id": attempt.run_id,
        "attempt": attempt.attempt_revision,
        "status": "passed",
        "script": script_path,
        "unit_hash": attempt.unit_digest,
        "artifact_unit_hash": attempt.unit_digest,
        "best": {"render": candidate_path},
        "rounds": canonical_rounds,
    }
    root = Path(folder)
    ledger_path = root / "shot.json"
    ledger = (
        json.loads(ledger_path.read_text(encoding="utf-8"))
        if ledger_path.is_file()
        else {"shot": root.name, "milestones": {}}
    )
    milestones = ledger.setdefault("milestones", {})
    if not isinstance(milestones, dict):
        raise AssertionError("fixture shot.json milestones must be an object")
    durable_milestone_id = milestone_id or str(layer_id)
    milestones[durable_milestone_id] = slot
    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    return unit_evaluation_receipts.publish_unit_evaluation_receipt(
        folder,
        layer_id,
        unit,
        attempt,
        result="passed",
        canonical_verdicts=verdicts,
        ledger_slot=slot,
        replay_inputs=(executed_replay_input(folder, script_path),),
        candidate_path=candidate_path,
    )


def pass_unit(
    folder: str | Path,
    layer_id: str,
    unit: WorkUnit,
    units: tuple[WorkUnit, ...],
    *,
    plan_hash: str,
    eligible_passed: set[str] | None = None,
    active_contract_ids=(),
    selection_token: AuthoritySelectionToken = ABSENT_SELECTION_TOKEN,
) -> UnitAttemptClaim:
    attempt = claim_for_build(
        folder,
        layer_id,
        units,
        unit.id,
        plan_hash=plan_hash,
        eligible_passed=eligible_passed,
        selection_token=selection_token,
    )
    freeze_unit(
        folder,
        layer_id,
        unit,
        attempt,
        active_contract_ids=active_contract_ids,
        selection_token=selection_token,
    )
    unit_state.transition(
        folder,
        layer_id,
        unit.id,
        "evaluating",
        reason="fixture executable evidence ready",
        attempt=attempt,
        selection_token=selection_token,
    )
    publish_passed_evaluation(
        folder,
        layer_id,
        unit,
        attempt,
        milestone_id=(str(layer_id) if len(units) == 1 else f"{layer_id}@{unit.id}"),
    )
    unit_state_claims.complete_unit_attempt(
        folder,
        layer_id,
        unit.id,
        units,
        attempt,
        expected_plan_hash=plan_hash,
        selection_token=selection_token,
        reason="fixture evidence accepted",
        evidence=["fixture:canonical-pass", "fixture:completion-debt-clear"],
    )
    return attempt
