"""Pure preservation and invalidation projection for HIR-0171 transitions.

The compiler consumes already-verified capsule sets and parsed durable unit-state
documents.  It performs no filesystem I/O.  Receipt source bytes are verified by the
transaction boundary before this projection may be committed.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain import unit_attempts, unit_completion_receipts
from vfx_harness.domain.authority_capsules import (
    AuthorityCapsuleSet,
    LayerAuthorityCapsule,
    UnitAuthorityCapsule,
)
from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.authority_state_records import (
    AuthorityStateLayerEffect,
    AuthorityStateTransitionProposal,
    AuthorityUnitBinding,
    LayerAuthorityBinding,
    PredecessorLayerBinding,
)
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationClaim,
    LayerFinalizationReceipt,
)
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration import layer_finalization_lifecycle
from vfx_harness.orchestration.unit_state_identity import (
    DIGEST_SCHEMA,
    downstream,
    unit_digest,
)


class AuthorityStateEffectsError(ValueError):
    """Current state cannot be moved mechanically to the proposed capsules."""


@dataclass(frozen=True, slots=True)
class ProjectedLayerAuthorityState:
    """One layer's exact before/after mutable bytes plus semantic effect."""

    layer_id: str
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    before_binding: LayerAuthorityBinding | None
    after_unit_bindings: tuple[AuthorityUnitBinding, ...]
    after_predecessor_ids: tuple[str, ...]
    after_layer_generation_digest: str | None
    after_finalization_receipt_digest: str | None
    effect: AuthorityStateLayerEffect


@dataclass(frozen=True, slots=True)
class AuthorityStateEffectsProjection:
    """Complete deterministic state projection for one selection transition."""

    layers: tuple[ProjectedLayerAuthorityState, ...]

    @property
    def effects(self) -> tuple[AuthorityStateLayerEffect, ...]:
        return tuple(row.effect for row in self.layers)

    def bind_after_authority(
        self,
        proposal: AuthorityStateTransitionProposal,
    ) -> tuple[tuple[ProjectedLayerAuthorityState, LayerAuthorityBinding | None], ...]:
        """Mint successor bindings only after the acyclic proposal digest exists."""

        if proposal.effects != self.effects:
            raise AuthorityStateEffectsError(
                "authority-state proposal effects do not match the projected state"
            )
        bindings: dict[str, LayerAuthorityBinding] = {}
        rows: list[tuple[ProjectedLayerAuthorityState, LayerAuthorityBinding | None]] = []
        for row in self.layers:
            if row.after_state is None:
                rows.append((row, None))
                continue
            if row.after_layer_generation_digest is None:
                raise AuthorityStateEffectsError(
                    f"layer {row.layer_id!r} has successor state without a generation"
                )
            predecessors: list[PredecessorLayerBinding] = []
            for predecessor_id in row.after_predecessor_ids:
                predecessor = bindings.get(predecessor_id)
                if predecessor is None:
                    raise AuthorityStateEffectsError(
                        f"layer {row.layer_id!r} predecessor {predecessor_id!r} has no "
                        "successor state binding"
                    )
                predecessors.append(
                    PredecessorLayerBinding.mint(
                        layer_id=predecessor.layer_id,
                        layer_generation_digest=predecessor.layer_generation_digest,
                        finalization_receipt_digest=(
                            predecessor.finalization_receipt_digest
                        ),
                    )
                )
            binding = LayerAuthorityBinding.mint(
                transition_revision=proposal.transition_revision,
                transition_proposal_digest=proposal.digest,
                selection_token=proposal.after_selection_token,
                layer_id=row.layer_id,
                layer_generation_digest=row.after_layer_generation_digest,
                units=row.after_unit_bindings,
                predecessors=predecessors,
                finalization_receipt_digest=(
                    row.after_finalization_receipt_digest
                ),
            )
            bindings[row.layer_id] = binding
            rows.append((row, binding))
        return tuple(rows)


def _unit_capsules(
    capsule_set: AuthorityCapsuleSet | None,
) -> dict[str, dict[str, UnitAuthorityCapsule]]:
    out: dict[str, dict[str, UnitAuthorityCapsule]] = {}
    if capsule_set is None:
        return out
    for capsule in capsule_set.units:
        out.setdefault(capsule.layer_id, {})[capsule.unit_id] = capsule
    return out


def _layer_capsules(
    capsule_set: AuthorityCapsuleSet | None,
) -> dict[str, LayerAuthorityCapsule]:
    if capsule_set is None:
        return {}
    return {capsule.layer_id: capsule for capsule in capsule_set.layers}


def _work_units(
    layer_id: str,
    capsules: Mapping[str, UnitAuthorityCapsule],
    ordered_ids: tuple[str, ...],
) -> tuple[WorkUnit, ...]:
    units: list[WorkUnit] = []
    for unit_id in ordered_ids:
        capsule = capsules.get(unit_id)
        if capsule is None:
            raise AuthorityStateEffectsError(
                f"layer {layer_id!r} omits unit capsule {unit_id!r}"
            )
        try:
            row = capsule.projection["work_unit"]["row"]
            units.append(
                WorkUnit.parse(
                    row,
                    f"authority capsule layer {layer_id!r} unit {unit_id!r}",
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthorityStateEffectsError(str(exc)) from exc
    return tuple(units)


def _unit_order(layer: LayerAuthorityCapsule | None) -> tuple[str, ...]:
    return () if layer is None else tuple(key for key, _digest in layer.unit_capsule_digests)


def _predecessor_ids(layer: LayerAuthorityCapsule | None) -> tuple[str, ...]:
    return () if layer is None else tuple(key for key, _digest in layer.predecessor_layer_digests)


def _state_revision(value: Mapping[str, Any], layer_id: str) -> int:
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        raise AuthorityStateEffectsError(
            f"work-unit state for layer {layer_id!r} has no positive revision"
        )
    return revision


def _require_state_shape(
    layer_id: str,
    state: Mapping[str, Any],
    layer: LayerAuthorityCapsule,
    capsules: Mapping[str, UnitAuthorityCapsule],
) -> tuple[WorkUnit, ...]:
    if state.get("schema") != 1:
        raise AuthorityStateEffectsError(
            f"work-unit state for layer {layer_id!r} has unsupported schema "
            f"{state.get('schema')!r}"
        )
    if str(state.get("layer")) != layer_id:
        raise AuthorityStateEffectsError(
            f"work-unit state for layer {layer_id!r} belongs to {state.get('layer')!r}"
        )
    if int(state.get("digest_schema", 0)) != DIGEST_SCHEMA:
        raise AuthorityStateEffectsError(
            f"work-unit state for layer {layer_id!r} has incomparable digest schema"
        )
    if state.get("plan_hash") != layer.capsule_digest:
        raise AuthorityStateEffectsError(
            f"work-unit state for layer {layer_id!r} is not bound to its semantic "
            "layer capsule"
        )
    units = _work_units(layer_id, capsules, _unit_order(layer))
    slots = state.get("units")
    if not isinstance(slots, Mapping) or set(slots) != {unit.id for unit in units}:
        raise AuthorityStateEffectsError(
            f"work-unit state IDs for layer {layer_id!r} do not match selected capsules"
        )
    for unit in units:
        slot = slots[unit.id]
        if not isinstance(slot, Mapping) or slot.get("unit_hash") != unit_digest(unit):
            raise AuthorityStateEffectsError(
                f"work-unit state identity changed for {layer_id}.{unit.id}"
            )
    _state_revision(state, layer_id)
    return units


def _completion_digest(
    *,
    layer_id: str,
    unit: WorkUnit,
    slot: Mapping[str, Any],
    layer_digest: str,
    selection_token: AuthoritySelectionTokenProjection,
    prior: AuthorityUnitBinding,
) -> str | None:
    raw = slot.get("completion_receipt")
    if raw is None:
        return None
    try:
        receipt = unit_completion_receipts.UnitCompletionReceipt.parse(
            raw,
            f"work-unit state {layer_id}.{unit.id}.completion_receipt",
        )
    except ValueError as exc:
        raise AuthorityStateEffectsError(str(exc)) from exc
    if (
        slot.get("status") != "passed"
        or receipt.claim.layer_id != layer_id
        or receipt.claim.unit_id != unit.id
        or receipt.claim.unit_digest != unit_digest(unit)
    ):
        raise AuthorityStateEffectsError(
            f"completion receipt for {layer_id}.{unit.id} does not close on current state"
        )
    directly_current = receipt.claim.selection_token == selection_token
    adopted_current = prior.completion_receipt_digest == receipt.receipt_digest
    if directly_current and receipt.claim.plan_hash != layer_digest:
        raise AuthorityStateEffectsError(
            f"completion receipt for {layer_id}.{unit.id} has a stale execution capsule"
        )
    if not directly_current and not adopted_current:
        raise AuthorityStateEffectsError(
            f"completion receipt for {layer_id}.{unit.id} has neither current execution "
            "identity nor immediate-predecessor authorization"
        )
    return receipt.receipt_digest


def _terminal_receipt(
    layer_id: str,
    state: Mapping[str, Any],
) -> LayerFinalizationReceipt | None:
    slot = state.get("layer_finalization")
    raw = slot.get("terminal_receipt") if isinstance(slot, Mapping) else None
    if raw is None:
        return None
    try:
        return LayerFinalizationReceipt.parse(
            raw,
            f"layer {layer_id} terminal finalization receipt",
        )
    except ValueError as exc:
        raise AuthorityStateEffectsError(str(exc)) from exc


def _effective_before_bindings(
    *,
    capsules: AuthorityCapsuleSet,
    states: Mapping[str, Mapping[str, Any]],
    prior_bindings: Mapping[str, LayerAuthorityBinding],
    head_revision: int,
    selection_token: AuthoritySelectionTokenProjection,
) -> dict[str, LayerAuthorityBinding]:
    units = _unit_capsules(capsules)
    effective: dict[str, LayerAuthorityBinding] = {}
    replay_prefix: dict[str, str | None] = {}
    for layer in capsules.layers:
        layer_id = layer.layer_id
        state = states.get(layer_id)
        if state is None:
            replay_prefix[layer_id] = None
            continue
        parsed_units = _require_state_shape(layer_id, state, layer, units.get(layer_id, {}))
        prior = prior_bindings.get(layer_id)
        if prior is None:
            raise AuthorityStateEffectsError(
                f"work-unit state for layer {layer_id!r} has no immediate-predecessor "
                "authority binding"
            )
        expected_unit_digests = dict(layer.unit_capsule_digests)
        if (
            prior.transition_revision != head_revision
            or prior.selection_token != selection_token
            or prior.layer_id != layer_id
            or prior.layer_generation_digest != layer.capsule_digest
            or {row.unit_id: row.unit_generation_digest for row in prior.units}
            != expected_unit_digests
        ):
            raise AuthorityStateEffectsError(
                f"immediate-predecessor binding for layer {layer_id!r} is stale"
            )
        prior_units = {row.unit_id: row for row in prior.units}
        current_units = tuple(
            AuthorityUnitBinding.mint(
                unit_id=unit.id,
                unit_generation_digest=expected_unit_digests[unit.id],
                completion_receipt_digest=_completion_digest(
                    layer_id=layer_id,
                    unit=unit,
                    slot=state["units"][unit.id],
                    layer_digest=layer.capsule_digest,
                    selection_token=selection_token,
                    prior=prior_units[unit.id],
                ),
            )
            for unit in parsed_units
        )
        predecessor_rows: list[PredecessorLayerBinding] = []
        for predecessor_id in _predecessor_ids(layer):
            predecessor = effective.get(predecessor_id)
            if predecessor is None:
                raise AuthorityStateEffectsError(
                    f"layer {layer_id!r} has no bound predecessor {predecessor_id!r}"
                )
            predecessor_rows.append(
                PredecessorLayerBinding.mint(
                    layer_id=predecessor_id,
                    layer_generation_digest=predecessor.layer_generation_digest,
                    finalization_receipt_digest=(
                        predecessor.finalization_receipt_digest
                    ),
                )
            )
        finalization = _terminal_receipt(layer_id, state)
        finalization_digest: str | None = None
        if finalization is not None:
            if (
                finalization.claim.layer_id != layer_id
                or finalization.claim.plan_hash != layer.capsule_digest
            ):
                raise AuthorityStateEffectsError(
                    f"terminal receipt for layer {layer_id!r} does not close on its capsule"
                )
            directly_current = finalization.claim.selection_token == selection_token
            adopted_current = (
                prior.finalization_receipt_digest == finalization.receipt_digest
            )
            if not directly_current and not adopted_current:
                raise AuthorityStateEffectsError(
                    f"terminal receipt for layer {layer_id!r} has no current authorization"
                )
            # Cumulative replay is the complete stable selected-DAG prefix, not
            # merely this layer's direct dependency edges.  ``effective`` is built
            # in capsule order and therefore names every earlier layer exactly once.
            expected_predecessors = tuple(replay_prefix.items())
            observed_predecessors = tuple(
                (row.layer_id, row.finalization_receipt_digest)
                for row in finalization.claim.predecessor_inputs
            )
            if observed_predecessors != expected_predecessors:
                raise AuthorityStateEffectsError(
                    f"terminal receipt for layer {layer_id!r} has stale predecessor inputs"
                )
            if any(row.completion_receipt_digest is None for row in current_units):
                raise AuthorityStateEffectsError(
                    f"terminal receipt for layer {layer_id!r} lacks completed constituents"
                )
            finalization_digest = finalization.receipt_digest
        effective[layer_id] = LayerAuthorityBinding.mint(
            transition_revision=head_revision,
            transition_proposal_digest=prior.transition_proposal_digest,
            selection_token=selection_token,
            layer_id=layer_id,
            layer_generation_digest=layer.capsule_digest,
            units=current_units,
            predecessors=predecessor_rows,
            finalization_receipt_digest=finalization_digest,
        )
        replay_prefix[layer_id] = finalization_digest
    unknown_states = sorted(set(states) - set(effective))
    if unknown_states:
        raise AuthorityStateEffectsError(
            "work-unit state has no selected comparable capsule/binding for layer(s): "
            + ", ".join(unknown_states)
        )
    return effective


def _descendants(layers: tuple[LayerAuthorityCapsule, ...]) -> dict[str, tuple[str, ...]]:
    reverse: dict[str, set[str]] = {row.layer_id: set() for row in layers}
    for row in layers:
        for predecessor_id in _predecessor_ids(row):
            reverse.setdefault(predecessor_id, set()).add(row.layer_id)
    result: dict[str, tuple[str, ...]] = {}
    for layer_id in reverse:
        found: set[str] = set()
        frontier = list(reverse[layer_id])
        while frontier:
            current = frontier.pop()
            if current in found:
                continue
            found.add(current)
            frontier.extend(reverse.get(current, ()))
        result[layer_id] = tuple(sorted(found))
    return result


def _active_claim_ids(state: Mapping[str, Any]) -> tuple[str, ...]:
    claims: list[str] = []
    for slot in state.get("units", {}).values():
        if not isinstance(slot, Mapping) or slot.get("active_attempt") is None:
            continue
        try:
            claims.append(
                unit_attempts.UnitAttemptClaim.parse(slot["active_attempt"]).claim_id
            )
        except ValueError as exc:
            raise AuthorityStateEffectsError(str(exc)) from exc
    return tuple(sorted(claims))


def _active_finalization_claim_id(state: Mapping[str, Any]) -> str | None:
    slot = state.get("layer_finalization")
    raw = slot.get("active_claim") if isinstance(slot, Mapping) else None
    if raw is None:
        return None
    try:
        return LayerFinalizationClaim.parse(raw).claim_id
    except ValueError as exc:
        raise AuthorityStateEffectsError(str(exc)) from exc


def _new_state(
    layer_id: str,
    layer_digest: str,
    units: tuple[WorkUnit, ...],
    at: str,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "digest_schema": DIGEST_SCHEMA,
        "layer": layer_id,
        "plan_hash": layer_digest,
        "revision": 1,
        "units": {
            unit.id: {
                "status": "pending",
                "unit_hash": unit_digest(unit),
                "updated": at,
                "history": [],
            }
            for unit in units
        },
        "superseded": [],
        "replans": [],
        "attempt_lineage": {},
        "updated": at,
    }


def _retire_slot(
    slot: Mapping[str, Any],
    unit_id: str,
    *,
    layer_digest: str,
    at: str,
) -> dict[str, Any]:
    retired = copy.deepcopy(dict(slot))
    reason = f"authority-state transition invalidated {unit_id}"
    unit_attempts.archive_active_attempt(
        retired,
        disposition="revoked",
        reason=reason,
        evidence=["authority-state-transition"],
        at=at,
    )
    unit_completion_receipts.archive_completion_receipt(
        retired,
        disposition="superseded",
        reason=reason,
        evidence=["authority-state-transition"],
        at=at,
    )
    retired.update(
        {
            "id": unit_id,
            "status": "superseded",
            "superseded_at": at,
            "superseded_by_plan": layer_digest,
        }
    )
    return retired


def _after_state(
    *,
    layer_id: str,
    state: Mapping[str, Any] | None,
    after_layer: LayerAuthorityCapsule,
    after_units: tuple[WorkUnit, ...],
    preserved_ids: set[str],
    preserve_finalization: bool,
    at: str,
) -> dict[str, Any]:
    if state is None:
        return _new_state(layer_id, after_layer.capsule_digest, after_units, at)
    value = copy.deepcopy(dict(state))
    before_serialized = copy.deepcopy(value)
    reason = "authority selection changed during atomic authority-state transition"
    for slot in value["units"].values():
        unit_attempts.revoke_active_attempt(
            slot,
            reason=reason,
            evidence=["authority-state-transition"],
            at=at,
            next_status="retryable",
        )
    finalization_slot = value.get("layer_finalization")
    if (
        isinstance(finalization_slot, Mapping)
        and finalization_slot.get("active_claim") is not None
    ) or not preserve_finalization:
        layer_finalization_lifecycle.archive_layer_finalization(
            value,
            disposition="superseded",
            reason=reason,
            evidence=["authority-state-transition"],
            at=at,
        )
    old_slots = value["units"]
    retiring = set(old_slots) - preserved_ids
    superseded = list(value.get("superseded") or [])
    superseded.extend(
        _retire_slot(
            old_slots[unit_id],
            unit_id,
            layer_digest=after_layer.capsule_digest,
            at=at,
        )
        for unit_id in sorted(retiring)
    )
    next_slots: dict[str, dict[str, Any]] = {}
    for unit in after_units:
        if unit.id in preserved_ids:
            slot = copy.deepcopy(old_slots[unit.id])
            slot["unit_hash"] = unit_digest(unit)
        else:
            slot = {
                "status": "pending",
                "unit_hash": unit_digest(unit),
                "updated": at,
                "history": [
                    {
                        "at": at,
                        "from": "superseded" if unit.id in old_slots else None,
                        "to": "pending",
                        "reason": "atomic authority-state transition",
                    }
                ],
            }
        next_slots[unit.id] = slot
    value["digest_schema"] = DIGEST_SCHEMA
    value["plan_hash"] = after_layer.capsule_digest
    value["units"] = next_slots
    value["superseded"] = superseded
    if value != before_serialized:
        value["revision"] = _state_revision(state, layer_id) + 1
        value["updated"] = at
    return value


def compile_authority_state_effects(
    *,
    before_capsules: AuthorityCapsuleSet | None,
    after_capsules: AuthorityCapsuleSet,
    states: Mapping[str, Mapping[str, Any]],
    prior_bindings: Mapping[str, LayerAuthorityBinding],
    predecessor_head_revision: int,
    before_selection_token: AuthoritySelectionTokenProjection,
    at: str,
) -> AuthorityStateEffectsProjection:
    """Derive the complete immediate-predecessor state movement."""

    if predecessor_head_revision <= 0:
        if states or prior_bindings:
            raise AuthorityStateEffectsError(
                "genesis authority-state transition cannot adopt existing work-unit state"
            )
        effective_before: dict[str, LayerAuthorityBinding] = {}
    else:
        effective_before = _effective_before_bindings(
            capsules=before_capsules,
            states=states,
            prior_bindings=prior_bindings,
            head_revision=predecessor_head_revision,
            selection_token=before_selection_token,
        )
    before_layers = _layer_capsules(before_capsules)
    after_layers = _layer_capsules(after_capsules)
    before_units = _unit_capsules(before_capsules)
    after_units = _unit_capsules(after_capsules)
    descendants = _descendants(after_capsules.layers)
    ordered_ids = [row.layer_id for row in after_capsules.layers]
    ordered_ids.extend(sorted(set(states) - set(ordered_ids)))
    projected: list[ProjectedLayerAuthorityState] = []
    projected_finalizations: dict[str, str | None] = {}
    for layer_id in ordered_ids:
        state = states.get(layer_id)
        before_layer = before_layers.get(layer_id)
        after_layer = after_layers.get(layer_id)
        prior = effective_before.get(layer_id)
        after_order = _unit_order(after_layer)
        parsed_after_units = _work_units(
            layer_id,
            after_units.get(layer_id, {}),
            after_order,
        )
        active_claims = () if state is None else _active_claim_ids(state)
        active_finalization = (
            None if state is None else _active_finalization_claim_id(state)
        )
        if not after_order:
            if state is None:
                projected_finalizations[layer_id] = None
                continue
            effect = AuthorityStateLayerEffect.mint(
                layer_id=layer_id,
                effect_kind="removed",
                invalidation_seed_unit_ids=tuple(state["units"]),
                invalidated_unit_ids=tuple(state["units"]),
                invalidated_downstream_layer_ids=descendants.get(layer_id, ()),
                revoked_unit_attempt_claim_ids=active_claims,
                revoked_layer_finalization_claim_id=active_finalization,
            )
            projected.append(
                ProjectedLayerAuthorityState(
                    layer_id,
                    copy.deepcopy(dict(state)),
                    None,
                    prior,
                    (),
                    (),
                    None,
                    None,
                    effect,
                )
            )
            projected_finalizations[layer_id] = None
            continue
        if state is None:
            invalidated = tuple(sorted(after_order))
            effect = AuthorityStateLayerEffect.mint(
                layer_id=layer_id,
                effect_kind="added",
                invalidation_seed_unit_ids=invalidated,
                invalidated_unit_ids=invalidated,
            )
            bindings = tuple(
                AuthorityUnitBinding.mint(
                    unit_id=unit_id,
                    unit_generation_digest=after_units[layer_id][unit_id].capsule_digest,
                )
                for unit_id in after_order
            )
            projected.append(
                ProjectedLayerAuthorityState(
                    layer_id,
                    None,
                    _new_state(
                        layer_id,
                        after_layer.capsule_digest,
                        parsed_after_units,
                        at,
                    ),
                    None,
                    bindings,
                    _predecessor_ids(after_layer),
                    after_layer.capsule_digest,
                    None,
                    effect,
                )
            )
            projected_finalizations[layer_id] = None
            continue
        if before_layer is None or prior is None:
            raise AuthorityStateEffectsError(
                f"existing state for layer {layer_id!r} has no comparable predecessor"
            )
        before_ids = set(_unit_order(before_layer))
        after_ids = set(after_order)
        changed_ids = {
            unit_id
            for unit_id in before_ids & after_ids
            if before_units[layer_id][unit_id].capsule_digest
            != after_units[layer_id][unit_id].capsule_digest
        }
        seeds = (before_ids - after_ids) | (after_ids - before_ids) | changed_ids
        predecessor_changed = (
            before_layer.predecessor_layer_digests
            != after_layer.predecessor_layer_digests
        )
        if predecessor_changed:
            seeds |= before_ids | after_ids
        invalidated = downstream(set(seeds) & after_ids, parsed_after_units) | (
            before_ids - after_ids
        )
        preserved_ids = (before_ids & after_ids) - invalidated
        prior_units = {row.unit_id: row for row in prior.units}
        preserved_bindings = tuple(
            AuthorityUnitBinding.mint(
                unit_id=unit_id,
                unit_generation_digest=after_units[layer_id][unit_id].capsule_digest,
                completion_receipt_digest=prior_units[unit_id].completion_receipt_digest,
            )
            for unit_id in sorted(preserved_ids)
        )
        unchanged = before_layer.capsule_digest == after_layer.capsule_digest
        finalization = _terminal_receipt(layer_id, state)
        expected_replay_prefix = tuple(projected_finalizations.items())
        observed_replay_prefix = (
            ()
            if finalization is None
            else tuple(
                (row.layer_id, row.finalization_receipt_digest)
                for row in finalization.claim.predecessor_inputs
            )
        )
        preserve_finalization = (
            unchanged
            and prior.finalization_receipt_digest is not None
            and finalization is not None
            and observed_replay_prefix == expected_replay_prefix
        )
        effect = AuthorityStateLayerEffect.mint(
            layer_id=layer_id,
            effect_kind="unchanged" if unchanged else "changed",
            preserved_units=preserved_bindings,
            preserved_finalization_receipt_digest=(
                prior.finalization_receipt_digest if preserve_finalization else None
            ),
            invalidation_seed_unit_ids=seeds,
            invalidated_unit_ids=invalidated,
            invalidated_downstream_layer_ids=(
                () if unchanged else descendants.get(layer_id, ())
            ),
            revoked_unit_attempt_claim_ids=active_claims,
            revoked_layer_finalization_claim_id=active_finalization,
        )
        after_bindings = tuple(
            AuthorityUnitBinding.mint(
                unit_id=unit_id,
                unit_generation_digest=after_units[layer_id][unit_id].capsule_digest,
                completion_receipt_digest=(
                    prior_units[unit_id].completion_receipt_digest
                    if unit_id in preserved_ids
                    else None
                ),
            )
            for unit_id in after_order
        )
        projected.append(
            ProjectedLayerAuthorityState(
                layer_id,
                copy.deepcopy(dict(state)),
                _after_state(
                    layer_id=layer_id,
                    state=state,
                    after_layer=after_layer,
                    after_units=parsed_after_units,
                    preserved_ids=preserved_ids,
                    preserve_finalization=preserve_finalization,
                    at=at,
                ),
                prior,
                after_bindings,
                _predecessor_ids(after_layer),
                after_layer.capsule_digest,
                prior.finalization_receipt_digest if preserve_finalization else None,
                effect,
            )
        )
        projected_finalizations[layer_id] = (
            prior.finalization_receipt_digest if preserve_finalization else None
        )
    return AuthorityStateEffectsProjection(tuple(projected))


__all__ = [
    "AuthorityStateEffectsError",
    "AuthorityStateEffectsProjection",
    "ProjectedLayerAuthorityState",
    "compile_authority_state_effects",
]
