"""Durable current-generation lifecycle for qualitative judgment debt (HIR-0163)."""

from __future__ import annotations

import fcntl
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.judgment_debt_activation import (
    activate_judgment_debt,
    resolve_judgment_debt,
)
from vfx_harness.domain.judgment_debt_models import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentDebtState,
)
from vfx_harness.domain.judgment_debt_payment_generations import (
    JudgmentDebtPaymentGeneration,
)
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixLayerReceipt as ReplayPrefixLayerReceipt,
)
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixReceipt,
    payment_generation_for_replay,
)
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixUnitReceipt as ReplayPrefixUnitReceipt,
)
from vfx_harness.domain.plan_records import load_judgment_debt_catalog
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.observability.run_artifacts import shot_state_dir
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.judgment_debt_payment_authority import (
    payment_generation_is_current,
)
from vfx_harness.orchestration.judgment_debt_replay_mint import (
    replay_prefix_receipt as replay_prefix_receipt,
)
from vfx_harness.orchestration.judgment_debt_replay_mint import (
    require_replay_inputs_unchanged as require_replay_inputs_unchanged,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.unit_state import unit_digest

EVENT_SCHEMA = "vfx-harness.judgment-debt-event/v2"
EVENTS = "judgment-debts.jsonl"


@contextmanager
def _event_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _current_authority(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
) -> tuple[
    str,
    tuple[JudgmentDebtDefinition, ...],
    tuple[JudgmentDebtActivation, ...],
]:
    selected_bundle = selected_authority.assertion.bundle
    if (
        selected_authority.assertion.selection != "selected"
        or selected_authority.plan is None
        or selected_bundle is None
        or selected_authority.assertion.effective_view is None
        or selected_authority.plan.bundle.content_hash != selected_bundle.digest
    ):
        raise ValueError("judgment debt state requires selected plan authority")
    try:
        requirements_path = selected_authority.artifact_paths["requirements.json"]
        layers_path = selected_authority.artifact_paths["layers.json"]
    except KeyError as exc:
        raise ValueError("selected judgment debt authority omits required artifacts") from exc
    definitions, activations = load_judgment_debt_catalog(
        requirements_path,
        selected_bundle_digest=selected_bundle.digest,
    )
    layers = load_layers_from_path(layers_path)
    selected_units = {
        f"{layer_id}:{unit.id}": unit_digest(unit)
        for layer_id, layer in layers.items()
        if layer.execution == "ready"
        for unit in layer.stages
    }
    for activation in activations:
        stale = [unit_id for unit_id, digest in activation.payer_unit_digests if selected_units.get(unit_id) != digest]
        if stale:
            raise ValueError(
                f"judgment debt activation {activation.digest} names stale or absent payer units: {', '.join(stale)}"
            )
    return selected_bundle.digest, definitions, activations


def _resolve_current_authority_snapshot(
    shot_folder: str | Path,
) -> ResolvedSelectedAuthority:
    """Resolve one fail-closed selected-authority snapshot for a state operation."""

    try:
        return resolve_selected_authority(shot_folder)
    except SelectedAuthorityResolutionError as exc:
        raise ValueError(str(exc)) from exc


def _event_rows(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.is_file():
        return ()
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"state/{EVENTS}:{line_no} is invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"state/{EVENTS}:{line_no} must contain an object")
        expected = {
            "schema",
            "bundle_digest",
            "debt_id",
            "definition_digest",
            "predecessor_state_digest",
            "state",
            "payment_generation",
            "replay_prefix",
        }
        if set(row) != expected or row.get("schema") != EVENT_SCHEMA:
            raise ValueError(f"state/{EVENTS}:{line_no} has unsupported judgment-debt event shape")
        rows.append(row)
    return tuple(rows)


def current_judgment_debt_states(
    shot_folder: str | Path,
) -> tuple[
    tuple[JudgmentDebtDefinition, JudgmentDebtActivation | None, JudgmentDebtState],
    ...,
]:
    """Replay append-only events for exactly the selected definition generations."""
    selected = _resolve_current_authority_snapshot(shot_folder)
    return current_judgment_debt_states_for_authority(
        shot_folder,
        selected,
    )


def current_judgment_debt_states_for_authority(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
) -> tuple[
    tuple[JudgmentDebtDefinition, JudgmentDebtActivation | None, JudgmentDebtState],
    ...,
]:
    """Replay debt state against one caller-resolved plan/JIT snapshot."""

    bundle_digest, definitions, activations = _current_authority(
        shot_folder,
        selected_authority,
    )
    return tuple(
        (row.definition, row.activation, row.state)
        for row in _generation_states_for_authority(
            shot_folder,
            selected_authority=selected_authority,
            bundle_digest=bundle_digest,
            definitions=definitions,
            activations=activations,
        )
    )


def current_judgment_debt_state_digest(shot_folder: str | Path) -> str:
    """Digest the exact selected debt definitions, activations, and lifecycle states."""

    selected = _resolve_current_authority_snapshot(shot_folder)
    return current_judgment_debt_state_digest_for_authority(
        shot_folder,
        selected,
    )


def current_judgment_debt_state_digest_for_authority(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
) -> str:
    """Digest lifecycle state using the caller's exact selected snapshot."""

    rows = [
        {
            "debt_id": definition.debt_id,
            "definition_digest": definition.digest,
            "activation_digest": activation.digest if activation is not None else None,
            "state_digest": state.digest,
            "status": state.status,
        }
        for definition, activation, state in current_judgment_debt_states_for_authority(
            shot_folder,
            selected_authority,
        )
    ]
    payload = {
        "schema": "vfx-harness.current-judgment-debt-state/v1",
        "debts": sorted(rows, key=lambda row: (row["debt_id"], row["definition_digest"])),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class _CurrentDebtGeneration:
    definition: JudgmentDebtDefinition
    activation: JudgmentDebtActivation | None
    state: JudgmentDebtState
    payment_generation: JudgmentDebtPaymentGeneration | None
    replay_prefix: ReplayPrefixReceipt | None


def _generation_states_for_authority(
    shot_folder: str | Path,
    *,
    selected_authority: ResolvedSelectedAuthority,
    bundle_digest: str,
    definitions: tuple[JudgmentDebtDefinition, ...],
    activations: tuple[JudgmentDebtActivation, ...],
) -> tuple[
    _CurrentDebtGeneration,
    ...,
]:
    """Replay state against one atomically resolved authority snapshot."""
    activation_by_definition = {activation.definition_digest: activation for activation in activations}
    by_digest = {definition.digest: definition for definition in definitions}
    states = {definition.digest: JudgmentDebtState.pending(definition) for definition in definitions}
    generations: dict[str, JudgmentDebtPaymentGeneration | None] = dict.fromkeys(
        by_digest
    )
    replay_prefixes: dict[str, ReplayPrefixReceipt | None] = dict.fromkeys(by_digest)
    generation_currency: dict[str, bool] = {}
    for index, row in enumerate(
        _event_rows(shot_state_dir(shot_folder) / EVENTS),
        1,
    ):
        if row.get("bundle_digest") != bundle_digest:
            continue
        definition = by_digest.get(str(row.get("definition_digest") or ""))
        if definition is None:
            continue
        if row.get("debt_id") != definition.debt_id:
            raise ValueError(f"state/{EVENTS}:{index} debt_id does not match its current definition")
        state = JudgmentDebtState.from_dict(row.get("state"), f"state/{EVENTS}:{index}.state")
        if state.definition_digest != definition.digest:
            raise ValueError(f"state/{EVENTS}:{index} state is pinned to another definition")
        activation = activation_by_definition.get(definition.digest)
        generation = JudgmentDebtPaymentGeneration.from_dict(
            row.get("payment_generation"),
            f"state/{EVENTS}:{index}.payment_generation",
        )
        replay_prefix = ReplayPrefixReceipt.from_dict(
            row.get("replay_prefix"),
            f"state/{EVENTS}:{index}.replay_prefix",
        )
        if (
            state.payment_generation_digest != generation.digest
            or generation.replay_prefix_digest != replay_prefix.digest
        ):
            raise ValueError(
                f"state/{EVENTS}:{index} state does not bind its exact payment generation"
            )
        # A payer rematerialization creates a new activation/payment generation.  Events
        # for the superseded exact payer remain immutable audit history, but cannot
        # settle or poison the newly selected generation.
        if state.activation_digest is not None and (activation is None or state.activation_digest != activation.digest):
            continue
        assert activation is not None
        generation.assert_matches(definition, activation)
        is_current = generation_currency.get(generation.digest)
        if is_current is None:
            is_current = payment_generation_is_current(
                shot_folder,
                selected_authority,
                definition,
                activation,
                generation,
                replay_prefix,
            )
            generation_currency[generation.digest] = is_current
        if not is_current:
            continue
        prior = states[definition.digest]
        if row.get("predecessor_state_digest") != prior.digest:
            raise ValueError(f"state/{EVENTS}:{index} predecessor_state_digest does not name the exact current state")
        if state.status == "due":
            if prior.status != "pending_not_due" or activation is None:
                raise ValueError(f"state/{EVENTS}:{index} has an illegal judgment-debt due transition")
            if state.activation_digest != activation.digest:
                raise ValueError(f"state/{EVENTS}:{index} due state names a stale activation")
        elif state.status in {"satisfied", "falsified"}:
            if (
                prior.status != "due"
                or state.activation_digest != prior.activation_digest
                or state.payment_generation_digest
                != prior.payment_generation_digest
            ):
                raise ValueError(f"state/{EVENTS}:{index} skips or rewrites a judgment-debt transition")
        else:
            raise ValueError(f"state/{EVENTS}:{index} may append only due or terminal states")
        states[definition.digest] = state
        generations[definition.digest] = generation
        replay_prefixes[definition.digest] = replay_prefix
    return tuple(
        _CurrentDebtGeneration(
            definition=definition,
            activation=activation_by_definition.get(definition.digest),
            state=states[definition.digest],
            payment_generation=generations[definition.digest],
            replay_prefix=replay_prefixes[definition.digest],
        )
        for definition in definitions
    )


def _append_state(
    shot_folder: str | Path,
    *,
    bundle_digest: str,
    definition: JudgmentDebtDefinition,
    predecessor: JudgmentDebtState,
    state: JudgmentDebtState,
    payment_generation: JudgmentDebtPaymentGeneration,
    replay_prefix: ReplayPrefixReceipt,
) -> None:
    path = shot_state_dir(shot_folder) / EVENTS
    row = {
        "schema": EVENT_SCHEMA,
        "bundle_digest": bundle_digest,
        "debt_id": definition.debt_id,
        "definition_digest": definition.digest,
        "predecessor_state_digest": predecessor.digest,
        "state": state.as_dict(),
        "payment_generation": payment_generation.as_dict(),
        "replay_prefix": replay_prefix.as_dict(),
    }
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    atomic_write(path, existing + json.dumps(row, sort_keys=True) + "\n")


def mark_judgment_debt_due(
    shot_folder: str | Path,
    definition_digest: str,
    *,
    layer_id: str,
    replay_receipt: ReplayPrefixReceipt,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> JudgmentDebtState:
    selected = selected_authority or _resolve_current_authority_snapshot(shot_folder)
    bundle_digest, definitions, activations = _current_authority(
        shot_folder,
        selected,
    )
    event_path = shot_state_dir(shot_folder) / EVENTS
    with _event_lock(event_path):
        rows = _generation_states_for_authority(
            shot_folder,
            selected_authority=selected,
            bundle_digest=bundle_digest,
            definitions=definitions,
            activations=activations,
        )
        try:
            current = next(
                row for row in rows if row.definition.digest == definition_digest
            )
        except StopIteration as exc:
            raise ValueError(f"unknown current judgment debt definition {definition_digest}") from exc
        definition = current.definition
        activation = current.activation
        state = current.state
        if activation is None:
            raise ValueError(
                f"judgment debt {definition.debt_id} has no selected payer activation"
            )
        activation.assert_matches(definition)
        if str(layer_id) != activation.payer_layer:
            raise ValueError(
                f"judgment debt {definition.debt_id} payer_layer="
                f"{activation.payer_layer!r}, not layer {str(layer_id)!r}"
            )
        # A direct build restart must prove the current replay prefix again even when a
        # prior invocation already persisted `due`. Selected authority alone is not
        # evidence that those exact artifacts executed in this canonical attempt.
        payment_generation = payment_generation_for_replay(
            definition,
            activation,
            replay_receipt,
        )
        if not payment_generation_is_current(
            shot_folder,
            selected,
            definition,
            activation,
            payment_generation,
            replay_receipt,
        ):
            raise ValueError(
                f"judgment debt {definition.debt_id} replay prefix is not current "
                "coordinator-authorized completion authority"
            )
        if state.status in {"due", "satisfied", "falsified"}:
            # Reconciliation always reasserts activation before its exact terminal
            # projection. A matching terminal state proves that transition already
            # occurred; return it idempotently after validating this replay prefix.
            if state.payment_generation_digest != payment_generation.digest:
                raise ValueError(
                    f"judgment debt {definition.debt_id} is already bound to another "
                    "payment generation"
                )
            return state
        if state.status != "pending_not_due":
            raise ValueError(
                f"judgment debt {definition.debt_id} cannot become due from "
                f"{state.status}"
            )
        due = activate_judgment_debt(
            definition,
            state,
            activation=activation,
            payment_generation_digest=payment_generation.digest,
            layer_id=str(layer_id),
            replayed_unit_digests=replay_receipt.unit_digests,
        )
        _append_state(
            shot_folder,
            bundle_digest=bundle_digest,
            definition=definition,
            predecessor=state,
            state=due,
            payment_generation=payment_generation,
            replay_prefix=replay_receipt,
        )
        return due


def resolve_current_judgment_debt(
    shot_folder: str | Path,
    definition_digest: str,
    *,
    outcome: str,
    evidence_digest: str,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> JudgmentDebtState:
    selected = selected_authority or _resolve_current_authority_snapshot(shot_folder)
    bundle_digest, definitions, activations = _current_authority(
        shot_folder,
        selected,
    )
    event_path = shot_state_dir(shot_folder) / EVENTS
    with _event_lock(event_path):
        rows = _generation_states_for_authority(
            shot_folder,
            selected_authority=selected,
            bundle_digest=bundle_digest,
            definitions=definitions,
            activations=activations,
        )
        try:
            current = next(
                row for row in rows if row.definition.digest == definition_digest
            )
        except StopIteration as exc:
            raise ValueError(f"unknown current judgment debt definition {definition_digest}") from exc
        definition = current.definition
        state = current.state
        if state.status in {"satisfied", "falsified"}:
            if state.status == outcome and state.evidence_digest == evidence_digest:
                return state
            raise ValueError(
                f"judgment debt {definition.debt_id} already resolved as "
                f"{state.status} with different terminal evidence"
            )
        resolved = resolve_judgment_debt(
            definition,
            state,
            outcome=outcome,
            evidence_digest=evidence_digest,
        )
        if current.payment_generation is None or current.replay_prefix is None:
            raise ValueError(
                f"judgment debt {definition.debt_id} has no current paid replay generation"
            )
        _append_state(
            shot_folder,
            bundle_digest=bundle_digest,
            definition=definition,
            predecessor=state,
            state=resolved,
            payment_generation=current.payment_generation,
            replay_prefix=current.replay_prefix,
        )
        return resolved


def require_judgment_debts_satisfied(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> None:
    rows = (
        current_judgment_debt_states(shot_folder)
        if selected_authority is None
        else current_judgment_debt_states_for_authority(
            shot_folder,
            selected_authority,
        )
    )
    unresolved = [
        (definition.debt_id, state.status)
        for definition, _activation, state in rows
        if state.status != "satisfied"
    ]
    if unresolved:
        detail = ", ".join(f"{debt_id}={status}" for debt_id, status in unresolved)
        raise ValueError("shot acceptance is blocked by unresolved current judgment debt: " + detail)
