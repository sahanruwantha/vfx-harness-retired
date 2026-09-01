"""Source-closure verification for completion receipts preserved by HIR-0171.

Authority-state effects are deliberately pure: they decide which exact receipt may
cross an immediate-predecessor edge, but they cannot prove that the receipt's files
still exist unchanged.  This module performs that filesystem-owned half of the
decision.  It verifies only completed units the transition proposes to preserve;
invalidated historical work never blocks publication merely because its old source
bytes are unavailable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_capsules import AuthorityCapsuleSet
from vfx_harness.domain.authority_head_records import (
    AuthorityHeadRecordError,
    canonical_json_bytes,
    decode_canonical_json_object,
)
from vfx_harness.domain.authority_state_records import AuthorityStateLayerEffect
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import WorkUnit, canonical_unit_script_path
from vfx_harness.orchestration.unit_evaluation_receipts import (
    PreparedUnitEvaluationCompletion,
    UnitEvaluationConflict,
    prepare_unit_evaluation_completion,
    require_prepared_unit_evaluation_completion,
)
from vfx_harness.orchestration.unit_state_identity import unit_digest


class PreservedUnitCompletionSourceConflict(ValueError):
    """A proposed preserved completion no longer has its exact causal sources."""


@dataclass(frozen=True, slots=True)
class PreparedPreservedUnitCompletionSources:
    """Pinned causal inputs for one preserved immutable completion receipt."""

    layer_id: str
    unit_id: str
    unit_generation_digest: str
    receipt: UnitCompletionReceipt
    unit: WorkUnit
    checkpoint_bytes: bytes
    evaluation: PreparedUnitEvaluationCompletion

    @property
    def completion_receipt_digest(self) -> str:
        return self.receipt.receipt_digest


def _checkpoint_bytes(checkpoint: Mapping[str, Any]) -> bytes:
    try:
        return canonical_json_bytes(dict(checkpoint))
    except AuthorityHeadRecordError as exc:
        raise PreservedUnitCompletionSourceConflict(
            f"preserved completion checkpoint is not canonical: {exc}"
        ) from exc


def _checkpoint_from_bytes(payload: bytes) -> dict[str, Any]:
    try:
        return decode_canonical_json_object(
            payload,
            "preserved completion checkpoint",
        )
    except AuthorityHeadRecordError as exc:
        raise PreservedUnitCompletionSourceConflict(str(exc)) from exc


def _require_receipt_state_closure(
    *,
    layer_id: str,
    unit: WorkUnit,
    receipt: UnitCompletionReceipt,
    checkpoint: Mapping[str, Any],
) -> None:
    if (
        receipt.claim.layer_id != layer_id
        or receipt.claim.unit_id != unit.id
        or receipt.claim.unit_digest != unit_digest(unit)
    ):
        raise PreservedUnitCompletionSourceConflict(
            f"preserved completion receipt for {layer_id}.{unit.id} belongs to "
            "another work-unit identity"
        )
    if receipt.script_path != canonical_unit_script_path(layer_id, unit.id):
        raise PreservedUnitCompletionSourceConflict(
            f"preserved completion receipt for {layer_id}.{unit.id} names a "
            "non-canonical replay script"
        )
    if canonical_digest(dict(checkpoint)) != receipt.checkpoint_digest:
        raise PreservedUnitCompletionSourceConflict(
            f"preserved completion checkpoint for {layer_id}.{unit.id} does not "
            "match its exact receipt"
        )
    if checkpoint.get("script_hash") != receipt.script_hash:
        raise PreservedUnitCompletionSourceConflict(
            f"preserved completion checkpoint for {layer_id}.{unit.id} changed its "
            "script identity"
        )


def _require_evaluation_closure(
    prepared: PreparedPreservedUnitCompletionSources,
) -> UnitCompletionReceipt:
    receipt = prepared.receipt
    checkpoint = _checkpoint_from_bytes(prepared.checkpoint_bytes)
    _require_receipt_state_closure(
        layer_id=prepared.layer_id,
        unit=prepared.unit,
        receipt=receipt,
        checkpoint=checkpoint,
    )
    stored = prepared.evaluation.stored
    if (
        stored.locator != receipt.evaluation_receipt_locator
        or stored.sha256 != receipt.evaluation_receipt_sha256
        or stored.receipt.receipt_digest != receipt.evaluation_receipt_digest
    ):
        raise PreservedUnitCompletionSourceConflict(
            f"preserved completion evaluator receipt for "
            f"{prepared.layer_id}.{prepared.unit_id} changed identity"
        )
    if prepared.evaluation.script_sha256 != receipt.script_hash:
        raise PreservedUnitCompletionSourceConflict(
            f"preserved completion canonical replay script for "
            f"{prepared.layer_id}.{prepared.unit_id} changed identity"
        )
    if stored.receipt.passed_evidence != receipt.passed_evidence:
        raise PreservedUnitCompletionSourceConflict(
            f"preserved completion evaluator evidence for "
            f"{prepared.layer_id}.{prepared.unit_id} disagrees with its receipt"
        )
    try:
        require_prepared_unit_evaluation_completion(
            prepared.evaluation,
            prepared.unit,
            checkpoint,
            layer_id=prepared.layer_id,
            claim=receipt.claim,
        )
    except UnitEvaluationConflict as exc:
        raise PreservedUnitCompletionSourceConflict(
            f"preserved completion source closure for "
            f"{prepared.layer_id}.{prepared.unit_id} is invalid: {exc}"
        ) from exc
    return receipt


def prepare_preserved_unit_completion_sources(
    folder: str | Path,
    *,
    layer_id: str,
    unit: WorkUnit,
    unit_generation_digest: str,
    receipt: UnitCompletionReceipt,
    checkpoint: Mapping[str, Any],
) -> PreparedPreservedUnitCompletionSources:
    """Read and pin one preserved receipt's complete executable source closure."""

    if not isinstance(unit, WorkUnit):
        raise PreservedUnitCompletionSourceConflict(
            "preserved completion source verification requires a typed work unit"
        )
    layer_id = str(layer_id)
    if unit.id != receipt.claim.unit_id:
        raise PreservedUnitCompletionSourceConflict(
            "preserved completion source verification received mismatched unit inputs"
        )
    unit_generation_digest = require_digest(
        unit_generation_digest,
        "preserved completion unit generation digest",
    )
    try:
        receipt = UnitCompletionReceipt.parse(
            receipt.as_dict(),
            f"preserved completion receipt {layer_id}.{unit.id}",
        )
    except ValueError as exc:
        raise PreservedUnitCompletionSourceConflict(str(exc)) from exc
    checkpoint_payload = _checkpoint_bytes(checkpoint)
    checkpoint_value = _checkpoint_from_bytes(checkpoint_payload)
    _require_receipt_state_closure(
        layer_id=layer_id,
        unit=unit,
        receipt=receipt,
        checkpoint=checkpoint_value,
    )
    try:
        evaluation = prepare_unit_evaluation_completion(
            folder,
            layer_id,
            unit,
            receipt.claim,
        )
    except UnitEvaluationConflict as exc:
        raise PreservedUnitCompletionSourceConflict(
            f"preserved completion source closure for {layer_id}.{unit.id} is "
            f"invalid: {exc}"
        ) from exc
    prepared = PreparedPreservedUnitCompletionSources(
        layer_id=layer_id,
        unit_id=unit.id,
        unit_generation_digest=unit_generation_digest,
        receipt=receipt,
        unit=unit,
        checkpoint_bytes=checkpoint_payload,
        evaluation=evaluation,
    )
    _require_evaluation_closure(prepared)
    return prepared


def require_preserved_unit_completion_sources(
    prepared: PreparedPreservedUnitCompletionSources,
) -> UnitCompletionReceipt:
    """Recheck every pinned source identity before the transition becomes pending."""

    if not isinstance(prepared, PreparedPreservedUnitCompletionSources):
        raise PreservedUnitCompletionSourceConflict(
            "preserved completion source guard requires typed prepared sources"
        )
    require_digest(
        prepared.unit_generation_digest,
        "preserved completion unit generation digest",
    )
    return _require_evaluation_closure(prepared)


def prepare_transition_preserved_unit_sources(
    folder: str | Path,
    *,
    capsules: AuthorityCapsuleSet,
    states: Mapping[str, Mapping[str, Any]],
    effects: Iterable[AuthorityStateLayerEffect],
) -> tuple[PreparedPreservedUnitCompletionSources, ...]:
    """Verify exactly the completed receipts named by transition preservation effects."""

    prepared: list[PreparedPreservedUnitCompletionSources] = []
    seen: set[tuple[str, str]] = set()
    for effect in effects:
        if not isinstance(effect, AuthorityStateLayerEffect):
            raise PreservedUnitCompletionSourceConflict(
                "transition source verification requires typed layer effects"
            )
        state = states.get(effect.layer_id)
        for binding in effect.preserved_units:
            receipt_digest = binding.completion_receipt_digest
            if receipt_digest is None:
                continue
            identity = (effect.layer_id, binding.unit_id)
            if identity in seen:
                raise PreservedUnitCompletionSourceConflict(
                    f"transition preserves completion {effect.layer_id}."
                    f"{binding.unit_id} more than once"
                )
            seen.add(identity)
            if not isinstance(state, Mapping):
                raise PreservedUnitCompletionSourceConflict(
                    f"transition preserves completion {effect.layer_id}."
                    f"{binding.unit_id} without successor state"
                )
            try:
                slot = state["units"][binding.unit_id]
                receipt = UnitCompletionReceipt.parse(
                    slot["completion_receipt"],
                    (
                        f"transition preserved completion {effect.layer_id}."
                        f"{binding.unit_id}"
                    ),
                )
                checkpoint = slot["checkpoint"]
                capsule = capsules.unit(effect.layer_id, binding.unit_id)
                unit = WorkUnit.parse(
                    capsule.projection["work_unit"]["row"],
                    (
                        f"transition preserved unit {effect.layer_id}."
                        f"{binding.unit_id}"
                    ),
                )
            except (KeyError, StopIteration, TypeError, ValueError) as exc:
                raise PreservedUnitCompletionSourceConflict(str(exc)) from exc
            if not isinstance(checkpoint, Mapping):
                raise PreservedUnitCompletionSourceConflict(
                    f"transition preserved completion {effect.layer_id}."
                    f"{binding.unit_id} has no checkpoint"
                )
            if slot.get("status") != "passed":
                raise PreservedUnitCompletionSourceConflict(
                    f"transition preserved completion {effect.layer_id}."
                    f"{binding.unit_id} is not passed"
                )
            if receipt.receipt_digest != receipt_digest:
                raise PreservedUnitCompletionSourceConflict(
                    f"transition preserved completion {effect.layer_id}."
                    f"{binding.unit_id} does not match its effect"
                )
            if capsule.capsule_digest != binding.unit_generation_digest:
                raise PreservedUnitCompletionSourceConflict(
                    f"transition preserved completion {effect.layer_id}."
                    f"{binding.unit_id} changed unit generation"
                )
            prepared.append(
                prepare_preserved_unit_completion_sources(
                    folder,
                    layer_id=effect.layer_id,
                    unit=unit,
                    unit_generation_digest=binding.unit_generation_digest,
                    receipt=receipt,
                    checkpoint=checkpoint,
                )
            )
    return tuple(sorted(prepared, key=lambda row: (row.layer_id, row.unit_id)))


def require_transition_preserved_unit_sources(
    prepared: Iterable[PreparedPreservedUnitCompletionSources],
    *,
    effects: Iterable[AuthorityStateLayerEffect],
) -> None:
    """Recheck a prepared set and require exact coverage of completed preservation."""

    rows = tuple(prepared)
    observed = {
        (
            row.layer_id,
            row.unit_id,
            row.unit_generation_digest,
            row.completion_receipt_digest,
        )
        for row in rows
    }
    if len(observed) != len(rows):
        raise PreservedUnitCompletionSourceConflict(
            "prepared transition completion source closures contain duplicates"
        )
    expected = {
        (
            effect.layer_id,
            binding.unit_id,
            binding.unit_generation_digest,
            binding.completion_receipt_digest,
        )
        for effect in effects
        for binding in effect.preserved_units
        if binding.completion_receipt_digest is not None
    }
    if observed != expected:
        raise PreservedUnitCompletionSourceConflict(
            "prepared transition completion source closures do not exactly cover "
            f"preserved receipts; expected={sorted(expected)}; "
            f"observed={sorted(observed)}"
        )
    for row in rows:
        require_preserved_unit_completion_sources(row)


__all__ = [
    "PreparedPreservedUnitCompletionSources",
    "PreservedUnitCompletionSourceConflict",
    "prepare_preserved_unit_completion_sources",
    "prepare_transition_preserved_unit_sources",
    "require_preserved_unit_completion_sources",
    "require_transition_preserved_unit_sources",
]
