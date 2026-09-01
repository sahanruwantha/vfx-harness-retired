"""Pure nested-state mutations owned by transactional work-unit replanning."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from vfx_harness.domain import unit_attempts, unit_completion_receipts
from vfx_harness.domain.unit_outcomes import HypothesisFalsification


def require_unconsumed_falsification(
    value: Mapping[str, Any],
    falsification_id: str,
    expected_payload: Mapping[str, Any] | None,
) -> None:
    """Bind a replan to one exact, still-unconsumed state-native finding."""

    if not isinstance(expected_payload, Mapping):
        raise ValueError("replan falsification requires its exact state-read payload")
    expected = dict(expected_payload)
    finding_contract = HypothesisFalsification.parse(
        expected,
        "expected replan falsification",
    )
    if finding_contract.record_id != falsification_id:
        raise ValueError("replan falsification id does not match its expected payload")
    matching_findings = [
        row
        for row in value.get("falsifications", [])
        if isinstance(row, dict)
        and row.get("record_id") == falsification_id
        and row == expected
    ]
    finding = matching_findings[0] if len(matching_findings) == 1 else None
    matching_slots = [
        slot
        for slot in value["units"].values()
        if isinstance(slot, dict) and slot.get("falsification") == finding
    ]
    consumed = any(
        row.get("falsification_id") == falsification_id
        for row in value.get("replans", [])
        if isinstance(row, dict)
    )
    if finding is None or len(matching_slots) != 1 or consumed:
        raise ValueError(
            "replan falsification must be one unconsumed record bound identically "
            "to exactly one durable unit slot"
        )
    HypothesisFalsification.parse(finding)


def revoke_active_authority(
    slots: Mapping[str, dict[str, Any]],
    *,
    plan_changed: bool,
    evidence: list[str],
    at: str,
) -> None:
    """Revoke every live attempt and every receipt made stale by a plan change."""

    for unit_id, slot in slots.items():
        reason = f"transactional replan replaced authority for {unit_id}"
        prior_status = str(slot.get("status") or "")
        unit_attempts.revoke_active_attempt(
            slot,
            reason=reason,
            evidence=list(evidence),
            at=at,
            next_status="retryable",
        )
        if plan_changed:
            unit_completion_receipts.archive_completion_receipt(
                slot,
                disposition="revoked",
                reason=reason,
                evidence=list(evidence),
                at=at,
            )
            # A passed lifecycle bit is not acceptance authority by itself.  Its
            # completion receipt binds the old plan and selection generation, so a
            # plan-identity change that revokes that receipt must also reopen the
            # unit.  Keeping ``passed`` here allowed a digest-identical producer to
            # satisfy a successor after its only acceptance receipt was archived.
            # The checkpoint remains available as a reviewed warm-start input; it no
            # longer grants dependency or publication authority.
            if prior_status == "passed":
                slot.setdefault("history", []).append(
                    {
                        "at": at,
                        "from": "passed",
                        "to": "retryable",
                        "reason": reason,
                        "metadata": {"evidence": list(evidence)},
                    }
                )
                slot["status"] = "retryable"
                slot["updated"] = at


def retire_slot(
    slot: Mapping[str, Any],
    unit_id: str,
    *,
    new_plan_hash: str,
    evidence: list[str],
    at: str,
) -> dict[str, Any]:
    """Copy one current slot into strict superseded audit state."""

    prior = dict(slot)
    unit_completion_receipts.archive_completion_receipt(
        prior,
        disposition="superseded",
        reason=f"transactional replan retired authority for {unit_id}",
        evidence=list(evidence),
        at=at,
    )
    prior.update(
        {
            "id": unit_id,
            "status": "superseded",
            "superseded_at": at,
            "superseded_by_plan": new_plan_hash,
        }
    )
    return prior
