"""Validation for durable layer-finalization authority and archive history."""

from __future__ import annotations

from collections.abc import Mapping
from itertools import pairwise
from typing import Any

from vfx_harness.domain.layer_finalization_claims import (
    LayerFinalizationClaim,
    _text,
)
from vfx_harness.domain.layer_finalization_receipts import LayerFinalizationReceipt
from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt

LAYER_FINALIZATION_CLAIM_ARCHIVE_SCHEMA = "vfx-harness.layer-finalization-claim-archive/v1"
LAYER_FINALIZATION_RECEIPT_ARCHIVE_SCHEMA = "vfx-harness.layer-finalization-receipt-archive/v1"

_STATE_SLOT_FIELDS = frozenset(
    {
        "attempt_revision",
        "active_claim",
        "terminal_receipt",
        "claim_history",
        "receipt_history",
    }
)
_CLAIM_ARCHIVE_FIELDS = frozenset({"schema", "claim", "disposition", "reason", "evidence", "at"})
_RECEIPT_ARCHIVE_FIELDS = frozenset({"schema", "receipt", "disposition", "reason", "evidence", "at"})
_CLAIM_ARCHIVE_DISPOSITIONS = frozenset({"completed", "released", "revoked"})
_RECEIPT_ARCHIVE_DISPOSITIONS = frozenset({"superseded", "revoked"})


def _archive_evidence(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list")
    rows = tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(rows) != len(set(rows)):
        raise ValueError(f"{where} contains duplicates")
    return rows


def _parse_claim_archive(
    value: object,
    where: str,
) -> LayerFinalizationClaim:
    if not isinstance(value, Mapping) or set(value) != _CLAIM_ARCHIVE_FIELDS:
        raise ValueError(f"{where} must contain exactly {sorted(_CLAIM_ARCHIVE_FIELDS)}")
    if value.get("schema") != LAYER_FINALIZATION_CLAIM_ARCHIVE_SCHEMA:
        raise ValueError(f"{where}.schema must be {LAYER_FINALIZATION_CLAIM_ARCHIVE_SCHEMA!r}")
    disposition = value.get("disposition")
    if disposition not in _CLAIM_ARCHIVE_DISPOSITIONS:
        raise ValueError(f"{where}.disposition must be one of {sorted(_CLAIM_ARCHIVE_DISPOSITIONS)}")
    _text(value.get("reason"), f"{where}.reason")
    _archive_evidence(value.get("evidence"), f"{where}.evidence")
    _text(value.get("at"), f"{where}.at")
    return LayerFinalizationClaim.parse(value.get("claim"), f"{where}.claim")


def _parse_receipt_archive(
    value: object,
    where: str,
) -> LayerFinalizationReceipt:
    if not isinstance(value, Mapping) or set(value) != _RECEIPT_ARCHIVE_FIELDS:
        raise ValueError(f"{where} must contain exactly {sorted(_RECEIPT_ARCHIVE_FIELDS)}")
    if value.get("schema") != LAYER_FINALIZATION_RECEIPT_ARCHIVE_SCHEMA:
        raise ValueError(f"{where}.schema must be {LAYER_FINALIZATION_RECEIPT_ARCHIVE_SCHEMA!r}")
    disposition = value.get("disposition")
    if disposition not in _RECEIPT_ARCHIVE_DISPOSITIONS:
        raise ValueError(f"{where}.disposition must be one of {sorted(_RECEIPT_ARCHIVE_DISPOSITIONS)}")
    _text(value.get("reason"), f"{where}.reason")
    _archive_evidence(value.get("evidence"), f"{where}.evidence")
    _text(value.get("at"), f"{where}.at")
    return LayerFinalizationReceipt.parse(value.get("receipt"), f"{where}.receipt")


def _strictly_increasing(revisions: list[int], where: str) -> None:
    if any(right <= left for left, right in pairwise(revisions)):
        raise ValueError(f"{where} attempt revisions must be strictly increasing")


def _validate_current_claim_against_state(
    state: Mapping[str, Any],
    claim: LayerFinalizationClaim,
    where: str,
) -> None:
    layer_id = _text(state.get("layer"), "work-unit state.layer")
    plan_hash = require_digest(state.get("plan_hash"), "work-unit state.plan_hash")
    if claim.layer_id != layer_id:
        raise ValueError(f"{where}.layer_id does not match work-unit state.layer")
    if claim.plan_hash != plan_hash:
        raise ValueError(f"{where}.plan_hash does not match work-unit state.plan_hash")
    units = state.get("units")
    if not isinstance(units, Mapping):
        raise ValueError("work-unit state.units must be an object")
    claimed = {row.unit_id: row for row in claim.unit_inputs}
    if set(claimed) != set(units):
        raise ValueError(f"{where}.unit_inputs must exactly cover the current work-unit state")
    for unit_id, unit_input in claimed.items():
        unit_where = f"work-unit state.units[{unit_id!r}]"
        slot = units.get(unit_id)
        if not isinstance(slot, Mapping):
            raise ValueError(f"{unit_where} must be an object")
        if slot.get("status") != "passed":
            raise ValueError(f"{where}.unit_inputs requires {unit_id!r} to be passed")
        if unit_input.unit_digest != require_digest(
            slot.get("unit_hash"),
            f"{unit_where}.unit_hash",
        ):
            raise ValueError(f"{where}.unit_inputs[{unit_id!r}] has a stale unit digest")
        completion = UnitCompletionReceipt.parse(
            slot.get("completion_receipt"),
            f"{unit_where}.completion_receipt",
        )
        expected = (
            completion.receipt_digest,
            completion.script_path,
            completion.script_hash,
        )
        observed = (
            unit_input.completion_receipt_digest,
            unit_input.script_path,
            unit_input.script_sha256,
        )
        if observed != expected:
            raise ValueError(f"{where}.unit_inputs[{unit_id!r}] does not match its exact completion receipt")


def validate_state_layer_finalization_contracts(value: Mapping[str, Any]) -> None:
    """Validate the optional nested finalization state of one work-unit document."""

    if "layer_finalization" not in value:
        return
    slot = value.get("layer_finalization")
    if not isinstance(slot, Mapping):
        raise ValueError("work-unit state.layer_finalization must be an object")
    found = set(slot)
    if found != _STATE_SLOT_FIELDS:
        raise ValueError(
            "work-unit state.layer_finalization fields mismatch; "
            f"missing={sorted(_STATE_SLOT_FIELDS - found)}; "
            f"unexpected={sorted(found - _STATE_SLOT_FIELDS)}"
        )
    revision = slot.get("attempt_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("work-unit state.layer_finalization.attempt_revision must be a non-negative integer")
    active_raw = slot.get("active_claim")
    terminal_raw = slot.get("terminal_receipt")
    if active_raw is not None and terminal_raw is not None:
        raise ValueError("work-unit state.layer_finalization active_claim and terminal_receipt are mutually exclusive")
    active = (
        None
        if active_raw is None
        else LayerFinalizationClaim.parse(
            active_raw,
            "work-unit state.layer_finalization.active_claim",
        )
    )
    terminal = (
        None
        if terminal_raw is None
        else LayerFinalizationReceipt.parse(
            terminal_raw,
            "work-unit state.layer_finalization.terminal_receipt",
        )
    )
    claim_history_raw = slot.get("claim_history")
    if not isinstance(claim_history_raw, list):
        raise ValueError("work-unit state.layer_finalization.claim_history must be a list")
    receipt_history_raw = slot.get("receipt_history")
    if not isinstance(receipt_history_raw, list):
        raise ValueError("work-unit state.layer_finalization.receipt_history must be a list")
    archived_claims = [
        _parse_claim_archive(
            row,
            f"work-unit state.layer_finalization.claim_history[{index}]",
        )
        for index, row in enumerate(claim_history_raw)
    ]
    archived_receipts = [
        _parse_receipt_archive(
            row,
            f"work-unit state.layer_finalization.receipt_history[{index}]",
        )
        for index, row in enumerate(receipt_history_raw)
    ]
    claim_revisions = [row.attempt_revision for row in archived_claims]
    receipt_revisions = [row.claim.attempt_revision for row in archived_receipts]
    _strictly_increasing(
        claim_revisions,
        "work-unit state.layer_finalization.claim_history",
    )
    _strictly_increasing(
        receipt_revisions,
        "work-unit state.layer_finalization.receipt_history",
    )
    current_claim = active if active is not None else terminal.claim if terminal else None
    if current_claim is not None:
        if current_claim.attempt_revision != revision:
            raise ValueError(
                "work-unit state.layer_finalization current authority revision does not match attempt_revision"
            )
        _validate_current_claim_against_state(
            value,
            current_claim,
            "work-unit state.layer_finalization current claim",
        )
    known_revisions = [*claim_revisions, *receipt_revisions]
    if current_claim is not None:
        known_revisions.append(current_claim.attempt_revision)
    if revision == 0:
        if known_revisions:
            raise ValueError("work-unit state.layer_finalization revision 0 forbids authority history")
    elif not known_revisions or max(known_revisions) != revision:
        raise ValueError(
            "work-unit state.layer_finalization attempt_revision does not match its current or archived authority"
        )
    if any(item > revision for item in known_revisions):
        raise ValueError("work-unit state.layer_finalization history exceeds attempt_revision")
    if active is not None and any(item >= revision for item in claim_revisions):
        raise ValueError("work-unit state.layer_finalization active claim must be newer than archived claims")
    if terminal is not None and any(item >= revision for item in receipt_revisions):
        raise ValueError("work-unit state.layer_finalization terminal receipt must be newer than archived receipts")


__all__ = [
    "LAYER_FINALIZATION_CLAIM_ARCHIVE_SCHEMA",
    "LAYER_FINALIZATION_RECEIPT_ARCHIVE_SCHEMA",
    "validate_state_layer_finalization_contracts",
]
