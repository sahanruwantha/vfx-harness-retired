"""Durable current-generation lifecycle for qualitative judgment debt (HIR-0163)."""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentDebtState,
    activate_judgment_debt,
    resolve_judgment_debt,
    validate_judgment_debt_replay_prefix,
)
from vfx_harness.domain.plan_records import load_judgment_debt_catalog
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.observability.run_artifacts import shot_state_dir
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.plan_authority import selected_artifact_path
from vfx_harness.orchestration.unit_state import load as load_unit_state
from vfx_harness.orchestration.unit_state import unit_digest, validate_current

EVENT_SCHEMA = "vfx-harness.judgment-debt-event/v1"
EVENTS = "judgment-debts.jsonl"
REPLAY_PREFIX_RECEIPT_SCHEMA = "vfx-harness.judgment-debt-replay-prefix/v1"


def _require_sha256(value: str, where: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{where} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ReplayPrefixUnitReceipt:
    """One checkpoint-concordant unit actually replayed in canonical order."""

    layer_id: str
    unit_id: str
    unit_digest: str
    checkpoint_unit_digest: str
    script_path: str
    script_sha256: str
    checkpoint_script_sha256: str

    def __post_init__(self) -> None:
        for value, where in (
            (self.layer_id, "ReplayPrefixUnitReceipt.layer_id"),
            (self.unit_id, "ReplayPrefixUnitReceipt.unit_id"),
            (self.script_path, "ReplayPrefixUnitReceipt.script_path"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where} must be a non-empty string")
        for value, where in (
            (self.unit_digest, "ReplayPrefixUnitReceipt.unit_digest"),
            (self.checkpoint_unit_digest, "ReplayPrefixUnitReceipt.checkpoint_unit_digest"),
            (self.script_sha256, "ReplayPrefixUnitReceipt.script_sha256"),
            (self.checkpoint_script_sha256, "ReplayPrefixUnitReceipt.checkpoint_script_sha256"),
        ):
            _require_sha256(value, where)
        if self.checkpoint_unit_digest != self.unit_digest:
            raise ValueError("replay receipt checkpoint unit digest does not match selected unit")
        if self.checkpoint_script_sha256 != self.script_sha256:
            raise ValueError("replay receipt checkpoint script digest does not match replayed artifact")

    @property
    def identity(self) -> str:
        return f"{self.layer_id}:{self.unit_id}"

    def as_dict(self) -> dict[str, str]:
        return {
            "layer_id": self.layer_id,
            "unit_id": self.unit_id,
            "unit_digest": self.unit_digest,
            "checkpoint_unit_digest": self.checkpoint_unit_digest,
            "script_path": self.script_path,
            "script_sha256": self.script_sha256,
            "checkpoint_script_sha256": self.checkpoint_script_sha256,
        }


@dataclass(frozen=True, slots=True)
class ReplayPrefixLayerReceipt:
    """One selected layer script and its exact replayed unit receipts."""

    layer_id: str
    script_path: str
    script_sha256: str
    units: tuple[ReplayPrefixUnitReceipt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.layer_id, str) or not self.layer_id.strip():
            raise ValueError("ReplayPrefixLayerReceipt.layer_id must be a non-empty string")
        if not isinstance(self.script_path, str) or not self.script_path.strip():
            raise ValueError("ReplayPrefixLayerReceipt.script_path must be a non-empty string")
        _require_sha256(self.script_sha256, "ReplayPrefixLayerReceipt.script_sha256")
        if not self.units:
            raise ValueError("ReplayPrefixLayerReceipt.units must be non-empty")
        if any(unit.layer_id != self.layer_id for unit in self.units):
            raise ValueError("replay receipt unit layer ids must match their layer receipt")
        unit_ids = [unit.unit_id for unit in self.units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("replay receipt layer contains duplicate unit ids")

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "script_path": self.script_path,
            "script_sha256": self.script_sha256,
            "units": [unit.as_dict() for unit in self.units],
        }


@dataclass(frozen=True, slots=True)
class ReplayPrefixReceipt:
    """Canonical receipt for the exact empty-scene prefix used by a debt payment."""

    layers: tuple[ReplayPrefixLayerReceipt, ...]

    def __post_init__(self) -> None:
        if not self.layers:
            raise ValueError("ReplayPrefixReceipt.layers must be non-empty")
        layer_ids = [layer.layer_id for layer in self.layers]
        if len(layer_ids) != len(set(layer_ids)):
            raise ValueError("replay receipt contains duplicate layer ids")

    @property
    def unit_digests(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (unit.identity, unit.unit_digest)
            for layer in self.layers
            for unit in layer.units
        )

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            {"schema": REPLAY_PREFIX_RECEIPT_SCHEMA, "layers": [layer.as_dict() for layer in self.layers]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": REPLAY_PREFIX_RECEIPT_SCHEMA,
            "layers": [layer.as_dict() for layer in self.layers],
            "replay_prefix_digest": self.digest,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def replay_prefix_receipt(
    shot_folder: str | Path,
    *,
    replayed_layer_scripts: Sequence[str | Path],
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> ReplayPrefixReceipt:
    """Issue a canonical receipt for layer scripts replayed from the empty scene.

    The builder calls this only from the replay-ready callback, after every named prior
    and the current composed layer artifact executed successfully.  Selected unit shape,
    durable pass state, checkpoint identity, and current unit-script bytes must all still
    agree before the receipt can activate qualitative judgment debt.
    """
    if isinstance(replayed_layer_scripts, (str, bytes)) or not isinstance(
        replayed_layer_scripts, Sequence
    ):
        raise ValueError("replayed_layer_scripts must be a sequence of layer script paths")
    shot = Path(shot_folder).resolve()
    layers_path = (
        selected_artifact_path(shot, "layers.json")
        if selected_authority is None
        else (
            shot / "layers.json"
            if selected_authority.plan is None
            else selected_authority.artifact_paths["layers.json"]
        )
    )
    layers = load_layers_from_path(layers_path)
    by_script = {Path(layer.script).as_posix(): layer for layer in layers.values()}
    replayed: list[ReplayPrefixLayerReceipt] = []
    seen_layers: set[str] = set()
    for raw_path in replayed_layer_scripts:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            try:
                relative = candidate.resolve().relative_to(shot).as_posix()
            except ValueError as exc:
                raise ValueError(
                    f"replayed layer script {candidate} escapes shot root {shot}"
                ) from exc
        else:
            relative = candidate.as_posix()
        layer = by_script.get(relative)
        if layer is None:
            raise ValueError(
                f"replayed layer script {relative!r} is not selected layer authority"
            )
        if layer.id in seen_layers:
            raise ValueError(f"replay prefix repeats selected layer {layer.id}")
        seen_layers.add(layer.id)
        layer_script = shot / relative
        if not layer_script.is_file():
            raise ValueError(
                f"replay prefix names missing selected layer artifact {relative}"
            )
        layer_script_hash = _sha256(layer_script)
        state = load_unit_state(shot, str(layer.id))
        validate_current(state, str(layer.id), layer.stages)
        if not state:
            raise ValueError(
                f"replayed layer {layer.id} has no durable work-unit state"
            )
        units: list[ReplayPrefixUnitReceipt] = []
        for unit in layer.stages:
            row = (state.get("units") or {}).get(unit.id) or {}
            identity = f"{layer.id}:{unit.id}"
            expected_unit_digest = unit_digest(unit)
            if row.get("status") != "passed" or row.get("unit_hash") != expected_unit_digest:
                raise ValueError(
                    f"replayed payer unit {identity} is not an exact current passed unit"
                )
            checkpoint = row.get("checkpoint")
            if not isinstance(checkpoint, dict):
                raise ValueError(
                    f"replayed payer unit {identity} has no accepted checkpoint"
                )
            if checkpoint.get("unit_hash") != expected_unit_digest:
                raise ValueError(
                    f"replayed payer unit {identity} checkpoint names a stale unit digest"
                )
            if len(unit.mutates.script_spans) != 1:
                raise ValueError(
                    f"replayed payer unit {identity} must own exactly one replay artifact"
                )
            artifact = shot / unit.mutates.script_spans[0]
            if not artifact.is_file():
                raise ValueError(
                    f"replayed payer unit {identity} artifact is missing: "
                    f"{unit.mutates.script_spans[0]}"
                )
            script_hash = _sha256(artifact)
            if checkpoint.get("script_hash") != script_hash:
                raise ValueError(
                    f"replayed payer unit {identity} artifact changed after its checkpoint"
                )
            units.append(
                ReplayPrefixUnitReceipt(
                    layer_id=str(layer.id),
                    unit_id=str(unit.id),
                    unit_digest=expected_unit_digest,
                    checkpoint_unit_digest=str(checkpoint["unit_hash"]),
                    script_path=unit.mutates.script_spans[0],
                    script_sha256=script_hash,
                    checkpoint_script_sha256=str(checkpoint["script_hash"]),
                )
            )
        replayed.append(
            ReplayPrefixLayerReceipt(
                layer_id=str(layer.id),
                script_path=relative,
                script_sha256=layer_script_hash,
                units=tuple(units),
            )
        )
    return ReplayPrefixReceipt(tuple(replayed))


def replay_prefix_unit_digests(
    shot_folder: str | Path,
    *,
    replayed_layer_scripts: Sequence[str | Path],
) -> tuple[tuple[str, str], ...]:
    """Return the legacy exact payer pairs from the richer replay-prefix receipt."""
    return tuple(sorted(replay_prefix_receipt(
        shot_folder,
        replayed_layer_scripts=replayed_layer_scripts,
    ).unit_digests))


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
    return _states_for_authority(
        shot_folder,
        bundle_digest=bundle_digest,
        definitions=definitions,
        activations=activations,
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


def _states_for_authority(
    shot_folder: str | Path,
    *,
    bundle_digest: str,
    definitions: tuple[JudgmentDebtDefinition, ...],
    activations: tuple[JudgmentDebtActivation, ...],
) -> tuple[
    tuple[JudgmentDebtDefinition, JudgmentDebtActivation | None, JudgmentDebtState],
    ...,
]:
    """Replay state against one atomically resolved authority snapshot."""
    activation_by_definition = {activation.definition_digest: activation for activation in activations}
    by_digest = {definition.digest: definition for definition in definitions}
    states = {definition.digest: JudgmentDebtState.pending(definition) for definition in definitions}
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
        # A payer rematerialization creates a new activation/payment generation.  Events
        # for the superseded exact payer remain immutable audit history, but cannot
        # settle or poison the newly selected generation.
        if state.activation_digest is not None and (activation is None or state.activation_digest != activation.digest):
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
            if prior.status != "due" or state.activation_digest != prior.activation_digest:
                raise ValueError(f"state/{EVENTS}:{index} skips or rewrites a judgment-debt transition")
        else:
            raise ValueError(f"state/{EVENTS}:{index} may append only due or terminal states")
        states[definition.digest] = state
    return tuple(
        (
            definition,
            activation_by_definition.get(definition.digest),
            states[definition.digest],
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
) -> None:
    path = shot_state_dir(shot_folder) / EVENTS
    row = {
        "schema": EVENT_SCHEMA,
        "bundle_digest": bundle_digest,
        "debt_id": definition.debt_id,
        "definition_digest": definition.digest,
        "predecessor_state_digest": predecessor.digest,
        "state": state.as_dict(),
    }
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    atomic_write(path, existing + json.dumps(row, sort_keys=True) + "\n")


def mark_judgment_debt_due(
    shot_folder: str | Path,
    definition_digest: str,
    *,
    layer_id: str,
    replayed_unit_digests: Sequence[tuple[str, str]],
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> JudgmentDebtState:
    selected = selected_authority or _resolve_current_authority_snapshot(shot_folder)
    bundle_digest, definitions, activations = _current_authority(
        shot_folder,
        selected,
    )
    event_path = shot_state_dir(shot_folder) / EVENTS
    with _event_lock(event_path):
        rows = _states_for_authority(
            shot_folder,
            bundle_digest=bundle_digest,
            definitions=definitions,
            activations=activations,
        )
        try:
            definition, activation, state = next(row for row in rows if row[0].digest == definition_digest)
        except StopIteration as exc:
            raise ValueError(f"unknown current judgment debt definition {definition_digest}") from exc
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
        validate_judgment_debt_replay_prefix(
            activation,
            replayed_unit_digests,
        )
        if state.status == "due":
            return state
        if state.status != "pending_not_due":
            raise ValueError(f"judgment debt {definition.debt_id} cannot become due from {state.status}")
        due = activate_judgment_debt(
            definition,
            state,
            activation=activation,
            layer_id=str(layer_id),
            replayed_unit_digests=replayed_unit_digests,
        )
        _append_state(
            shot_folder,
            bundle_digest=bundle_digest,
            definition=definition,
            predecessor=state,
            state=due,
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
        rows = _states_for_authority(
            shot_folder,
            bundle_digest=bundle_digest,
            definitions=definitions,
            activations=activations,
        )
        try:
            definition, _activation, state = next(row for row in rows if row[0].digest == definition_digest)
        except StopIteration as exc:
            raise ValueError(f"unknown current judgment debt definition {definition_digest}") from exc
        resolved = resolve_judgment_debt(
            definition,
            state,
            outcome=outcome,
            evidence_digest=evidence_digest,
        )
        _append_state(
            shot_folder,
            bundle_digest=bundle_digest,
            definition=definition,
            predecessor=state,
            state=resolved,
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
