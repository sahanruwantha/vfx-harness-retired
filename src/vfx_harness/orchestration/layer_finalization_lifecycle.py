"""Pure mutations for the nested layer-finalization lifecycle slot."""

from __future__ import annotations

from vfx_harness.domain.layer_finalizations import (
    LAYER_FINALIZATION_CLAIM_ARCHIVE_SCHEMA,
    LAYER_FINALIZATION_RECEIPT_ARCHIVE_SCHEMA,
    LayerFinalizationClaim,
    LayerFinalizationReceipt,
)


def ensure_finalization_slot(value: dict) -> dict:
    """Return the closed nested slot, creating only the unfinalized state."""

    raw = value.get("layer_finalization")
    if raw is None:
        raw = {
            "attempt_revision": 0,
            "active_claim": None,
            "terminal_receipt": None,
            "claim_history": [],
            "receipt_history": [],
        }
        value["layer_finalization"] = raw
    if not isinstance(raw, dict):
        raise ValueError("work-unit state.layer_finalization must be an object")
    return raw


def archive_layer_finalization(
    value: dict,
    *,
    disposition: str,
    reason: str,
    evidence: list[str],
    at: str,
) -> None:
    """Revoke current layer authority inside an owning unit-state transaction."""

    if disposition not in {"superseded", "revoked"}:
        raise ValueError("layer finalization receipt disposition must be superseded or revoked")
    if not isinstance(reason, str) or not reason.strip() or not evidence:
        raise ValueError("layer finalization retirement requires a reason and non-empty evidence")
    slot = value.get("layer_finalization")
    if slot is None:
        return
    if not isinstance(slot, dict):
        raise ValueError("work-unit state.layer_finalization must be an object")
    active = slot.get("active_claim")
    terminal = slot.get("terminal_receipt")
    if active is not None:
        claim = LayerFinalizationClaim.parse(active)
        slot.setdefault("claim_history", []).append(
            {
                "schema": LAYER_FINALIZATION_CLAIM_ARCHIVE_SCHEMA,
                "claim": claim.as_dict(),
                "disposition": "revoked",
                "reason": reason,
                "evidence": list(evidence),
                "at": at,
            }
        )
        slot["active_claim"] = None
    if terminal is not None:
        receipt = LayerFinalizationReceipt.parse(terminal)
        slot.setdefault("receipt_history", []).append(
            {
                "schema": LAYER_FINALIZATION_RECEIPT_ARCHIVE_SCHEMA,
                "receipt": receipt.as_dict(),
                "disposition": disposition,
                "reason": reason,
                "evidence": list(evidence),
                "at": at,
            }
        )
        slot["terminal_receipt"] = None


__all__ = ["archive_layer_finalization", "ensure_finalization_slot"]
