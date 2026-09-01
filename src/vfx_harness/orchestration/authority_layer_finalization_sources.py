"""Source closure for terminal layer receipts preserved by HIR-0171.

An unchanged layer capsule is necessary but not sufficient to preserve a terminal
receipt.  The receipt is consumable only while its terminal state, cumulative replay
receipt, composed script and replay inputs, sealed v3 outcome, and ledger projection
all close on the same exact receipt.  This module captures that complete closure
without taking selection or unit-state locks so transition callers can invoke it
inside their existing canonical lock order.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_capsules import (
    AuthorityCapsuleSet,
    LayerAuthorityCapsule,
)
from vfx_harness.domain.authority_state_records import AuthorityStateLayerEffect
from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.infrastructure.trusted_files import TrustedFileError
from vfx_harness.orchestration.layer_finalization_state import (
    CapturedTerminalLayerSources,
    LayerFinalizationConflict,
    capture_terminal_layer_sources,
    require_terminal_layer_sources_unchanged,
)
from vfx_harness.orchestration.layer_publication import (
    CapturedLayerPublicationProjections,
    LayerPublicationConflict,
    capture_layer_publication_projections,
    require_layer_publication_projections_unchanged,
)
from vfx_harness.orchestration.unit_state_identity import unit_digest
from vfx_harness.orchestration.unit_state_serialization import (
    parse_work_unit_state_bytes,
    serialize_work_unit_state,
)


class PreservedLayerFinalizationSourceConflict(ValueError):
    """A proposed preserved terminal receipt lacks its exact public sources."""


@dataclass(frozen=True, slots=True)
class PreparedPreservedLayerFinalizationSources:
    """Pinned complete publication closure for one preserved terminal receipt."""

    layer_id: str
    layer_generation_digest: str
    finalization_receipt_digest: str
    effect: AuthorityStateLayerEffect
    capsule: LayerAuthorityCapsule
    terminal_state_bytes: bytes
    receipt: LayerFinalizationReceipt
    terminal_sources: CapturedTerminalLayerSources
    projections: CapturedLayerPublicationProjections

    @property
    def terminal_state_sha256(self) -> str:
        return hashlib.sha256(self.terminal_state_bytes).hexdigest()


def _work_units(
    capsules: AuthorityCapsuleSet,
    layer: LayerAuthorityCapsule,
) -> tuple[WorkUnit, ...]:
    units: list[WorkUnit] = []
    for unit_id, _capsule_digest in layer.unit_capsule_digests:
        try:
            row = capsules.unit(layer.layer_id, unit_id).projection["work_unit"]["row"]
            units.append(
                WorkUnit.parse(
                    row,
                    f"preserved terminal layer {layer.layer_id!r} unit {unit_id!r}",
                )
            )
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise PreservedLayerFinalizationSourceConflict(str(exc)) from exc
    return tuple(units)


def _terminal_receipt(
    state: Mapping[str, Any],
    *,
    layer_id: str,
) -> LayerFinalizationReceipt:
    slot = state.get("layer_finalization")
    raw = slot.get("terminal_receipt") if isinstance(slot, Mapping) else None
    try:
        return LayerFinalizationReceipt.parse(
            raw,
            f"preserved terminal receipt for layer {layer_id}",
        )
    except ValueError as exc:
        raise PreservedLayerFinalizationSourceConflict(str(exc)) from exc


def _completion_receipts(
    state: Mapping[str, Any],
    *,
    layer_id: str,
    units: tuple[WorkUnit, ...],
) -> dict[str, UnitCompletionReceipt]:
    slots = state.get("units")
    if not isinstance(slots, Mapping) or set(slots) != {unit.id for unit in units}:
        raise PreservedLayerFinalizationSourceConflict(
            f"preserved terminal state for layer {layer_id} does not contain its "
            "exact unit set"
        )
    receipts: dict[str, UnitCompletionReceipt] = {}
    for unit in units:
        raw = slots[unit.id]
        if not isinstance(raw, Mapping) or raw.get("status") != "passed":
            raise PreservedLayerFinalizationSourceConflict(
                f"preserved terminal state for {layer_id}.{unit.id} is not passed"
            )
        try:
            receipt = UnitCompletionReceipt.parse(
                raw.get("completion_receipt"),
                f"preserved terminal completion {layer_id}.{unit.id}",
            )
        except ValueError as exc:
            raise PreservedLayerFinalizationSourceConflict(str(exc)) from exc
        if (
            receipt.claim.layer_id != layer_id
            or receipt.claim.unit_id != unit.id
            or receipt.claim.unit_digest != unit_digest(unit)
        ):
            raise PreservedLayerFinalizationSourceConflict(
                f"preserved terminal completion {layer_id}.{unit.id} belongs to "
                "another unit identity"
            )
        receipts[unit.id] = receipt
    return receipts


def _require_unit_input_closure(
    *,
    layer_id: str,
    layer: LayerAuthorityCapsule,
    effect: AuthorityStateLayerEffect,
    receipt: LayerFinalizationReceipt,
    units: tuple[WorkUnit, ...],
    completions: Mapping[str, UnitCompletionReceipt],
) -> None:
    capsule_digests = dict(layer.unit_capsule_digests)
    preserved = {row.unit_id: row for row in effect.preserved_units}
    if set(preserved) != set(capsule_digests) or any(
        row.unit_generation_digest != capsule_digests[row.unit_id]
        or row.completion_receipt_digest is None
        for row in preserved.values()
    ):
        raise PreservedLayerFinalizationSourceConflict(
            f"preserved terminal receipt for layer {layer_id} does not preserve "
            "every exact unit generation and completion"
        )
    inputs = {row.unit_id: row for row in receipt.claim.unit_inputs}
    if len(inputs) != len(receipt.claim.unit_inputs) or set(inputs) != set(completions):
        raise PreservedLayerFinalizationSourceConflict(
            f"preserved terminal receipt for layer {layer_id} has a stale unit-input set"
        )
    units_by_id = {unit.id: unit for unit in units}
    for unit_id, completion in completions.items():
        row = inputs[unit_id]
        expected = (
            unit_digest(units_by_id[unit_id]),
            completion.receipt_digest,
            completion.script_path,
            completion.script_hash,
        )
        observed = (
            row.unit_digest,
            row.completion_receipt_digest,
            row.script_path,
            row.script_sha256,
        )
        if observed != expected:
            raise PreservedLayerFinalizationSourceConflict(
                f"preserved terminal receipt for {layer_id}.{unit_id} has a stale "
                "completion input"
            )
        if preserved[unit_id].completion_receipt_digest != completion.receipt_digest:
            raise PreservedLayerFinalizationSourceConflict(
                f"preserved terminal effect for {layer_id}.{unit_id} names another "
                "completion receipt"
            )


def _require_predecessor_closure(
    *,
    layer_id: str,
    capsules: AuthorityCapsuleSet,
    receipt: LayerFinalizationReceipt,
    states: Mapping[str, Mapping[str, Any]],
) -> None:
    ordered_ids = tuple(row.layer_id for row in capsules.layers)
    matches = tuple(index for index, current in enumerate(ordered_ids) if current == layer_id)
    if len(matches) != 1:
        raise PreservedLayerFinalizationSourceConflict(
            f"preserved terminal layer {layer_id} is ambiguous in the successor "
            "capsule order"
        )
    expected_ids = ordered_ids[: matches[0]]
    inputs = receipt.claim.predecessor_inputs
    if tuple(row.layer_id for row in inputs) != expected_ids:
        raise PreservedLayerFinalizationSourceConflict(
            f"preserved terminal receipt for layer {layer_id} has a stale predecessor "
            "input order or set"
        )
    for row in inputs:
        predecessor_state = states.get(row.layer_id)
        if not isinstance(predecessor_state, Mapping):
            raise PreservedLayerFinalizationSourceConflict(
                f"preserved terminal receipt for layer {layer_id} has no terminal "
                f"predecessor state for {row.layer_id}"
            )
        predecessor = _terminal_receipt(
            predecessor_state,
            layer_id=row.layer_id,
        )
        observed = (
            predecessor.receipt_digest,
            predecessor.layer_script_path,
            predecessor.layer_script_sha256,
        )
        expected = (
            row.finalization_receipt_digest,
            row.script_path,
            row.script_sha256,
        )
        if predecessor.final_status != "passed" or observed != expected:
            raise PreservedLayerFinalizationSourceConflict(
                f"preserved terminal receipt for layer {layer_id} has a stale "
                f"predecessor input for {row.layer_id}"
            )


def _require_state_closure(
    state: Mapping[str, Any],
    *,
    capsules: AuthorityCapsuleSet,
    layer: LayerAuthorityCapsule,
    effect: AuthorityStateLayerEffect,
    states: Mapping[str, Mapping[str, Any]],
) -> LayerFinalizationReceipt:
    layer_id = layer.layer_id
    if (
        state.get("layer") != layer_id
        or state.get("plan_hash") != layer.capsule_digest
    ):
        raise PreservedLayerFinalizationSourceConflict(
            f"preserved terminal state for layer {layer_id} is not bound to its "
            "exact successor capsule"
        )
    receipt = _terminal_receipt(state, layer_id=layer_id)
    if (
        effect.layer_id != layer_id
        or effect.effect_kind != "unchanged"
        or effect.preserved_finalization_receipt_digest != receipt.receipt_digest
        or receipt.final_status != "passed"
        or receipt.claim.layer_id != layer_id
        or receipt.claim.plan_hash != layer.capsule_digest
    ):
        raise PreservedLayerFinalizationSourceConflict(
            f"preserved terminal state for layer {layer_id} does not close on the "
            "exact passing preservation effect"
        )
    effective_layer = layer.projection.get("effective_layer")
    if (
        not isinstance(effective_layer, Mapping)
        or effective_layer.get("script") != receipt.layer_script_path
    ):
        raise PreservedLayerFinalizationSourceConflict(
            f"preserved terminal receipt for layer {layer_id} names a non-authoritative "
            "composed script"
        )
    units = _work_units(capsules, layer)
    completions = _completion_receipts(
        state,
        layer_id=layer_id,
        units=units,
    )
    _require_unit_input_closure(
        layer_id=layer_id,
        layer=layer,
        effect=effect,
        receipt=receipt,
        units=units,
        completions=completions,
    )
    _require_predecessor_closure(
        layer_id=layer_id,
        capsules=capsules,
        receipt=receipt,
        states=states,
    )
    return receipt


def prepare_transition_preserved_finalization_sources(
    folder: str | Path,
    *,
    capsules: AuthorityCapsuleSet,
    states: Mapping[str, Mapping[str, Any]],
    effects: Iterable[AuthorityStateLayerEffect],
) -> tuple[PreparedPreservedLayerFinalizationSources, ...]:
    """Capture exactly every complete terminal publication a transition preserves."""

    root = Path(folder).expanduser().absolute()
    prepared: list[PreparedPreservedLayerFinalizationSources] = []
    for effect in effects:
        if not isinstance(effect, AuthorityStateLayerEffect):
            raise PreservedLayerFinalizationSourceConflict(
                "transition terminal source verification requires typed layer effects"
            )
        digest = effect.preserved_finalization_receipt_digest
        if digest is None:
            continue
        state = states.get(effect.layer_id)
        if not isinstance(state, Mapping):
            raise PreservedLayerFinalizationSourceConflict(
                f"transition preserves terminal receipt for layer {effect.layer_id} "
                "without successor terminal state"
            )
        try:
            layer = capsules.layer(effect.layer_id)
        except StopIteration as exc:
            raise PreservedLayerFinalizationSourceConflict(
                f"transition preserves terminal receipt for unknown layer {effect.layer_id}"
            ) from exc
        receipt = _require_state_closure(
            state,
            capsules=capsules,
            layer=layer,
            effect=effect,
            states=states,
        )
        try:
            terminal_sources = capture_terminal_layer_sources(root, receipt)
            projections = capture_layer_publication_projections(
                root,
                layer_id=effect.layer_id,
                receipt=receipt,
            )
            require_terminal_layer_sources_unchanged(terminal_sources)
            require_layer_publication_projections_unchanged(projections)
        except (
            LayerFinalizationConflict,
            LayerPublicationConflict,
            TrustedFileError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise PreservedLayerFinalizationSourceConflict(str(exc)) from exc
        prepared.append(
            PreparedPreservedLayerFinalizationSources(
                layer_id=effect.layer_id,
                layer_generation_digest=layer.capsule_digest,
                finalization_receipt_digest=digest,
                effect=effect,
                capsule=layer,
                terminal_state_bytes=serialize_work_unit_state(state),
                receipt=receipt,
                terminal_sources=terminal_sources,
                projections=projections,
            )
        )
    rows = tuple(sorted(prepared, key=lambda row: row.layer_id))
    require_transition_preserved_finalization_sources(
        rows,
        effects=effects,
        successor_state_sha256={
            layer_id: hashlib.sha256(serialize_work_unit_state(state)).hexdigest()
            for layer_id, state in states.items()
        },
    )
    return rows


def require_preserved_layer_finalization_sources(
    prepared: PreparedPreservedLayerFinalizationSources,
) -> LayerFinalizationReceipt:
    """Recheck one pinned terminal publication closure without acquiring locks."""

    if not isinstance(prepared, PreparedPreservedLayerFinalizationSources):
        raise PreservedLayerFinalizationSourceConflict(
            "preserved terminal source guard requires typed prepared sources"
        )
    try:
        state = parse_work_unit_state_bytes(
            prepared.terminal_state_bytes,
            f"prepared preserved terminal state {prepared.layer_id}",
        )
        receipt = _terminal_receipt(state, layer_id=prepared.layer_id)
        if (
            receipt != prepared.receipt
            or prepared.layer_generation_digest != prepared.capsule.capsule_digest
            or prepared.effect.layer_id != prepared.layer_id
            or prepared.effect.preserved_finalization_receipt_digest
            != prepared.finalization_receipt_digest
            or receipt.receipt_digest != prepared.finalization_receipt_digest
            or prepared.projections.layer_id != prepared.layer_id
            or prepared.projections.receipt != receipt
            or prepared.terminal_sources.stored_evaluation.receipt.claim
            != receipt.claim
        ):
            raise PreservedLayerFinalizationSourceConflict(
                f"prepared terminal source closure for layer {prepared.layer_id} "
                "changed typed identity"
            )
        receipt.assert_matches_evaluation(
            prepared.terminal_sources.stored_evaluation.receipt
        )
        require_terminal_layer_sources_unchanged(prepared.terminal_sources)
        require_layer_publication_projections_unchanged(prepared.projections)
    except PreservedLayerFinalizationSourceConflict:
        raise
    except (
        LayerFinalizationConflict,
        LayerPublicationConflict,
        TrustedFileError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise PreservedLayerFinalizationSourceConflict(str(exc)) from exc
    return receipt


def require_transition_preserved_finalization_sources(
    prepared: Iterable[PreparedPreservedLayerFinalizationSources],
    *,
    effects: Iterable[AuthorityStateLayerEffect],
    successor_state_sha256: Mapping[str, str],
) -> None:
    """Require exact source coverage for every preserved finalization digest."""

    rows = tuple(prepared)
    observed = {
        (row.layer_id, row.finalization_receipt_digest)
        for row in rows
    }
    if len(observed) != len(rows):
        raise PreservedLayerFinalizationSourceConflict(
            "prepared transition terminal source closures contain duplicates"
        )
    expected = {
        (effect.layer_id, effect.preserved_finalization_receipt_digest)
        for effect in effects
        if effect.preserved_finalization_receipt_digest is not None
    }
    if observed != expected:
        raise PreservedLayerFinalizationSourceConflict(
            "prepared transition terminal source closures do not exactly cover "
            f"preserved finalization receipts; expected={sorted(expected)}; "
            f"observed={sorted(observed)}"
        )
    effects_by_layer = {effect.layer_id: effect for effect in effects}
    for row in rows:
        if row.effect != effects_by_layer.get(row.layer_id):
            raise PreservedLayerFinalizationSourceConflict(
                f"prepared terminal source closure for layer {row.layer_id} binds "
                "another transition effect"
            )
        if successor_state_sha256.get(row.layer_id) != row.terminal_state_sha256:
            raise PreservedLayerFinalizationSourceConflict(
                f"prepared terminal source closure for layer {row.layer_id} does "
                "not bind the exact successor terminal state bytes"
            )
        require_preserved_layer_finalization_sources(row)


__all__ = [
    "PreparedPreservedLayerFinalizationSources",
    "PreservedLayerFinalizationSourceConflict",
    "prepare_transition_preserved_finalization_sources",
    "require_preserved_layer_finalization_sources",
    "require_transition_preserved_finalization_sources",
]
