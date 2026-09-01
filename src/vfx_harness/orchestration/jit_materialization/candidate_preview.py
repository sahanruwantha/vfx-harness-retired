"""Projection of unpublished materialization state into an isolated gate view."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_head_records import canonical_view_hash
from vfx_harness.domain.authority_preview_records import (
    AUTHORITY_PREVIEW_REFERENCE_PATH,
    AuthorityPreviewReference,
)
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.jit_materialization.schema import (
    OVERLAY_ARTIFACTS,
    STATE_DIR,
)
from vfx_harness.orchestration.jit_materialization.transition import (
    PreparedMaterializationPublication,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_locator
from vfx_harness.orchestration.unit_state_lock import unit_state_path
from vfx_harness.orchestration.unit_state_serialization import (
    WorkUnitStateSerializationError,
    parse_work_unit_state_bytes,
    serialize_work_unit_state,
)

_IDENTIFIER_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")


def _reject_duplicate_json_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(
                f"candidate publication preview contains duplicate JSON key {key!r}"
            )
        value[key] = item
    return value


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(
        f"candidate publication preview contains non-finite number {value!r}"
    )


@dataclass(frozen=True, slots=True)
class _ValidatedAuthorityProjection:
    before_hashes: dict[str, str]
    after_hashes: dict[str, str]
    after_payloads: dict[str, bytes]
    after_states: dict[str, dict[str, Any]]
    effect_rows: tuple[Any, ...]


def _canonical_preview_root(view: Path) -> Path:
    """Return one absolute real directory without resolving through links."""

    root = Path(os.path.abspath(Path(view).expanduser()))
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"candidate preview root must not use symlink path components: {current}")
        if not current.is_dir():
            raise ValueError(f"candidate preview root and ancestors must be real directories: {current}")
    return root


def _contained_path(view: Path, path: Path, where: str) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(view)
    except ValueError as exc:
        raise ValueError(f"{where} escapes the candidate preview root") from exc
    if relative == Path("."):
        raise ValueError(f"{where} must not replace the candidate preview root")
    return candidate


def _layer_id(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value[0].isalnum()
        or any(character not in _IDENTIFIER_CHARACTERS for character in value)
    ):
        raise ValueError(f"{where} must be a canonical layer identifier")
    return value


def _state_target(view: Path, layer_id: str, where: str) -> Path:
    canonical_id = _layer_id(layer_id, where)
    target = _contained_path(
        view,
        unit_state_path(view, canonical_id),
        f"{where} state path",
    )
    expected_parent = view / "state" / "work-units"
    if target.parent != expected_parent:
        raise ValueError(f"{where} has a non-canonical work-unit state path")
    return target


def _canonical_overlay_payloads(
    payloads: Mapping[str, bytes],
) -> tuple[dict[str, bytes], dict[str, Any]]:
    if not isinstance(payloads, Mapping) or set(payloads) != set(OVERLAY_ARTIFACTS):
        raise ValueError("candidate publication preview must contain every overlay artifact")
    copied: dict[str, bytes] = {}
    documents: dict[str, Any] = {}
    for name in OVERLAY_ARTIFACTS:
        payload = payloads[name]
        if not isinstance(payload, bytes):
            raise ValueError(f"candidate publication preview member must be bytes: {name}")
        try:
            document = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_pairs,
                parse_constant=_reject_non_finite_json,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"candidate publication preview member is not UTF-8 JSON: {name}"
            ) from exc
        canonical = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if payload != canonical:
            raise ValueError(
                f"candidate publication preview member is not canonical: {name}"
            )
        copied[name] = payload
        documents[name] = document
    return copied, documents


def _verify_immutable_view(
    view: Path,
    root: Path,
    expected: Mapping[str, bytes],
) -> None:
    plan_bundle_integrity.require_real_directory(
        view,
        root,
        "candidate publication preview view",
    )
    try:
        children = tuple(root.iterdir())
    except OSError as exc:
        raise ValueError(f"candidate publication preview view is unreadable: {root}") from exc
    observed = {child.name for child in children}
    if observed != set(expected):
        raise ValueError(
            "candidate publication preview member set conflicts with its address; "
            f"missing={sorted(set(expected) - observed)}; "
            f"unexpected={sorted(observed - set(expected))}"
        )
    for child in children:
        if child.is_symlink() or not child.is_file():
            raise ValueError(f"candidate publication preview members must be real files: {child.name}")
        snapshot = plan_bundle_integrity.read_real_file_snapshot(
            root,
            child,
            f"candidate publication preview member {child.name}",
        )
        if snapshot.payload != expected[child.name]:
            raise ValueError(f"immutable candidate publication preview conflicts with staged bytes: {child.name}")


def _hash_mapping(value: object, where: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be a layer-to-digest mapping")
    hashes: dict[str, str] = {}
    for raw_layer_id, raw_digest in value.items():
        layer_id = _layer_id(raw_layer_id, f"{where} layer id")
        if not plan_bundle_integrity.is_digest(raw_digest):
            raise ValueError(f"{where}[{layer_id!r}] must be a lowercase SHA-256 digest")
        hashes[layer_id] = raw_digest
    return hashes


def _state_payload_mapping(
    value: object,
    where: str,
) -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be a layer-to-bytes mapping")
    payloads: dict[str, bytes] = {}
    states: dict[str, dict[str, Any]] = {}
    for raw_layer_id, raw_payload in value.items():
        layer_id = _layer_id(raw_layer_id, f"{where} layer id")
        if not isinstance(raw_payload, bytes):
            raise ValueError(f"{where}[{layer_id!r}] must be bytes")
        try:
            state = parse_work_unit_state_bytes(
                raw_payload,
                f"{where}[{layer_id!r}]",
            )
        except WorkUnitStateSerializationError as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(state, dict) or state.get("layer") != layer_id:
            raise ValueError(f"{where}[{layer_id!r}] must be an object for that exact layer")
        _projected_statuses(state)
        payloads[layer_id] = raw_payload
        states[layer_id] = state
    return payloads, states


def _rows_by_layer(rows: object, where: str) -> dict[str, Any]:
    if not isinstance(rows, tuple):
        raise ValueError(f"{where} must be an immutable tuple")
    by_layer: dict[str, Any] = {}
    for row in rows:
        layer_id = _layer_id(getattr(row, "layer_id", None), f"{where} layer id")
        if layer_id in by_layer:
            raise ValueError(f"{where} duplicates layer {layer_id!r}")
        by_layer[layer_id] = row
    return by_layer


def _capsule_layer_ids(publication: PreparedMaterializationPublication) -> set[str]:
    try:
        layers = publication.capsule_set.layers
    except AttributeError as exc:
        raise ValueError("candidate preview has no typed authority capsule set") from exc
    by_layer = _rows_by_layer(layers, "candidate preview capsule set")
    return set(by_layer)


def _validate_publication_projection(
    view: Path,
    publication: PreparedMaterializationPublication,
) -> _ValidatedAuthorityProjection:
    """Close every projected mutable byte on its prepared typed member."""

    capsule_layer_ids = _capsule_layer_ids(publication)
    before_hashes = _hash_mapping(
        publication.before_state_hashes,
        "candidate preview before-state hashes",
    )
    after_hashes = _hash_mapping(
        publication.after_state_hashes,
        "candidate preview after-state hashes",
    )
    after_payloads, after_states = _state_payload_mapping(
        publication.after_state_payloads,
        "candidate preview after-state payloads",
    )
    if set(after_payloads) != set(after_hashes):
        raise ValueError("candidate preview after-state payload and hash layer sets differ")
    for layer_id, payload in after_payloads.items():
        if hashlib.sha256(payload).hexdigest() != after_hashes[layer_id]:
            raise ValueError(f"candidate preview after-state hash does not match layer {layer_id!r}")
        _state_target(view, layer_id, "candidate preview after-state")

    prepared = publication.transition
    if prepared is None:
        if before_hashes != after_hashes:
            raise ValueError("no-op candidate preview must preserve exact before-state hashes")
        unknown = sorted(set(after_hashes) - capsule_layer_ids)
        if unknown:
            raise ValueError("no-op candidate preview state has no authority capsule: " + ", ".join(unknown))
        for layer_id in before_hashes:
            _state_target(view, layer_id, "candidate preview before-state")
        return _ValidatedAuthorityProjection(
            before_hashes,
            after_hashes,
            after_payloads,
            after_states,
            (),
        )

    try:
        intent = prepared.intent
        projection = prepared.effects_projection
        prepared_capsules = prepared.capsule_set
        prepared_payloads_raw = prepared.after_state_payloads
    except AttributeError as exc:
        raise ValueError("candidate preview transition is missing typed prepared authority") from exc
    if prepared_capsules != publication.capsule_set:
        raise ValueError("candidate preview publication and transition capsule sets differ")
    members = _rows_by_layer(
        intent.state_members,
        "candidate preview transition state members",
    )
    effect_rows = _rows_by_layer(
        projection.layers,
        "candidate preview transition effect projection",
    )
    proposal_effects = _rows_by_layer(
        intent.proposal.effects,
        "candidate preview transition proposal effects",
    )
    if set(members) != set(effect_rows) or set(members) != set(proposal_effects):
        raise ValueError("candidate preview transition member, projection, and effect layer sets differ")
    prepared_payloads, prepared_states = _state_payload_mapping(
        prepared_payloads_raw,
        "prepared transition after-state payloads",
    )
    if prepared_payloads != after_payloads:
        raise ValueError("candidate preview after-state payloads differ from the prepared transition")

    expected_before = {layer_id for layer_id, member in members.items() if member.before is not None}
    expected_after = {layer_id for layer_id, member in members.items() if member.after is not None}
    if set(before_hashes) != expected_before:
        raise ValueError("candidate preview before-state hash layers differ from transition members")
    if set(after_hashes) != expected_after or set(prepared_payloads) != expected_after:
        raise ValueError("candidate preview after-state layers differ from transition members")
    unknown_after = sorted(expected_after - capsule_layer_ids)
    if unknown_after:
        raise ValueError("candidate preview successor state has no authority capsule: " + ", ".join(unknown_after))

    for layer_id, member in members.items():
        target = _state_target(view, layer_id, "candidate preview transition member")
        locator = target.relative_to(view).as_posix()
        if member.live_locator != locator:
            raise ValueError(f"candidate preview transition member {layer_id!r} has a non-canonical live locator")
        row = effect_rows[layer_id]
        effect = proposal_effects[layer_id]
        if row.effect != effect or row.effect.layer_id != layer_id:
            raise ValueError(f"candidate preview transition effect disagrees for layer {layer_id!r}")
        if (row.before_state is None) != (member.before is None):
            raise ValueError(f"candidate preview transition before-state presence differs for layer {layer_id!r}")
        if member.before is not None:
            before_payload = serialize_work_unit_state(row.before_state)
            if (
                member.before.layer_id != layer_id
                or member.before.sha256 != before_hashes[layer_id]
                or hashlib.sha256(before_payload).hexdigest() != before_hashes[layer_id]
            ):
                raise ValueError(f"candidate preview transition before-state identity differs for layer {layer_id!r}")
        if (row.after_state is None) != (member.after is None):
            raise ValueError(f"candidate preview transition after-state presence differs for layer {layer_id!r}")
        if member.after is not None and (
            member.after.layer_id != layer_id
            or member.after.sha256 != after_hashes[layer_id]
            or prepared_states[layer_id] != row.after_state
        ):
            raise ValueError(f"candidate preview transition after-state identity differs for layer {layer_id!r}")
    return _ValidatedAuthorityProjection(
        before_hashes,
        after_hashes,
        after_payloads,
        after_states,
        tuple(effect_rows[layer_id] for layer_id in members),
    )


def _prepare_preview_state_directory(
    view: Path,
    before_hashes: Mapping[str, str],
) -> Path:
    """Require the isolated snapshot to be the transition's exact predecessor."""

    preview_dir = _contained_path(
        view,
        view / "state" / "work-units",
        "candidate preview work-unit state",
    )
    if preview_dir.is_symlink():
        raise ValueError("candidate preview work-unit state must be an isolated real directory")
    if preview_dir.exists():
        plan_bundle_integrity.require_real_directory(
            view,
            preview_dir,
            "candidate preview work-unit state",
        )
    else:
        if before_hashes:
            raise ValueError("candidate preview work-unit state omits prepared predecessor files")
        plan_bundle_integrity.ensure_real_directories(
            view,
            preview_dir,
            "candidate preview work-unit state",
        )

    expected = {
        _state_target(view, layer_id, "candidate preview predecessor").name: digest
        for layer_id, digest in before_hashes.items()
    }
    try:
        children = tuple(preview_dir.iterdir())
    except OSError as exc:
        raise ValueError(f"candidate preview work-unit state is unreadable: {preview_dir}") from exc
    observed = {child.name for child in children}
    if observed != set(expected):
        raise ValueError(
            "candidate preview work-unit state differs from the prepared predecessor; "
            f"missing={sorted(set(expected) - observed)}; "
            f"unexpected={sorted(observed - set(expected))}"
        )
    for child in children:
        if child.is_symlink() or not child.is_file():
            raise ValueError(f"candidate preview work-unit state must contain only real snapshot files: {child.name}")
        snapshot = plan_bundle_integrity.read_real_file_snapshot(
            preview_dir,
            child,
            f"candidate preview predecessor state {child.name}",
        )
        if snapshot.sha256 != expected[child.name]:
            raise ValueError(f"candidate preview work-unit predecessor hash changed: {child.name}")
    return preview_dir


def _verify_projected_state_directory(
    view: Path,
    expected_payloads: Mapping[str, bytes],
) -> None:
    preview_dir = view / "state" / "work-units"
    plan_bundle_integrity.require_real_directory(
        view,
        preview_dir,
        "candidate preview projected work-unit state",
    )
    expected = {
        _state_target(view, layer_id, "candidate preview successor").name: payload
        for layer_id, payload in expected_payloads.items()
    }
    children = tuple(preview_dir.iterdir())
    observed = {child.name for child in children}
    if observed != set(expected):
        raise ValueError(
            "candidate preview projected state member set differs from the successor; "
            f"missing={sorted(set(expected) - observed)}; "
            f"unexpected={sorted(observed - set(expected))}"
        )
    for child in children:
        if child.is_symlink() or not child.is_file():
            raise ValueError(f"candidate preview projected state must contain only real files: {child.name}")
        snapshot = plan_bundle_integrity.read_real_file_snapshot(
            preview_dir,
            child,
            f"candidate preview successor state {child.name}",
        )
        if snapshot.payload != expected[child.name]:
            raise ValueError(f"candidate preview successor state bytes differ: {child.name}")


def stage_candidate_publication_view(
    view: Path,
    view_hash: str,
    payloads: Mapping[str, bytes],
) -> Path:
    """Install or verify the future pointer's immutable content-addressed view."""

    view = _canonical_preview_root(view)
    if not plan_bundle_integrity.is_digest(view_hash):
        raise ValueError("candidate publication preview view_hash must be a lowercase SHA-256 digest")
    copied, documents = _canonical_overlay_payloads(payloads)
    if canonical_view_hash(documents) != view_hash:
        raise ValueError("candidate publication preview view_hash does not address its exact documents")
    parent = _contained_path(
        view,
        view / STATE_DIR / "views",
        "candidate publication preview views directory",
    )
    plan_bundle_integrity.ensure_real_directories(
        view,
        parent,
        "candidate publication preview views directory",
    )
    root = _contained_path(
        view,
        parent / view_hash,
        "candidate publication preview target",
    )
    if not root.exists() and not root.is_symlink():
        try:
            plan_bundle_integrity.durably_install_bundle_directory(
                view,
                root,
                copied,
            )
        except plan_bundle_integrity.PlanPublicationError:
            # A concurrent install is legal only when its exact immutable bytes won.
            if not root.exists() and not root.is_symlink():
                raise
    _verify_immutable_view(view, root, copied)
    return root


def _projected_statuses(state: dict[str, Any]) -> dict[str, str]:
    units = state.get("units")
    if not isinstance(units, dict):
        raise ValueError("candidate preview unit state must contain a units object")
    statuses: dict[str, str] = {}
    for unit_id, slot in units.items():
        if not isinstance(slot, dict) or not isinstance(slot.get("status"), str):
            raise ValueError(f"candidate preview unit state has no typed status for {unit_id}")
        statuses[str(unit_id)] = str(slot["status"])
    return statuses


def _record_ids(record: dict[str, Any], field: str) -> frozenset[str]:
    value = record.get(field, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"candidate preview replan {field} must be a string list")
    return frozenset(value)


def _project_candidate_ledger(
    view: Path,
    layer_id: str,
    record: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    """Reopen only ledger slots whose authority the preview replan replaced.

    Returns whether the projected layer is no longer fully passed.  The aggregate
    layer slot is a scheduling projection, while ``layer@unit`` slots mirror the
    exact surviving unit status (or ``superseded`` for retired identities).
    """

    statuses = _projected_statuses(state)
    reopened = not statuses or any(status != "passed" for status in statuses.values())
    if not reopened:
        return False

    path = view / "shot.json"
    if not path.exists() and not path.is_symlink():
        return True
    snapshot = plan_bundle_integrity.read_real_file_snapshot(
        view,
        path,
        "candidate preview ledger",
    )
    try:
        ledger = json.loads(snapshot.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate preview shot.json is not readable JSON") from exc
    if not isinstance(ledger, dict) or not isinstance(ledger.get("milestones"), dict):
        raise ValueError("candidate preview shot.json must contain a milestones object")

    added = _record_ids(record, "added")
    removed = _record_ids(record, "removed")
    changed = _record_ids(record, "changed")
    invalidated = _record_ids(record, "invalidated")
    preserved = _record_ids(record, "preserved")
    orphaned = _record_ids(record, "orphaned") if "orphaned" in record else frozenset()
    affected = added | removed | changed | invalidated | preserved | orphaned
    milestones = ledger["milestones"]

    aggregate = milestones.get(layer_id)
    if aggregate is not None:
        if not isinstance(aggregate, dict):
            raise ValueError(f"candidate preview ledger milestone {layer_id} must be an object")
        aggregate["status"] = "pending"

    unit_prefix = f"{layer_id}@"
    for milestone_id, slot in milestones.items():
        if not isinstance(milestone_id, str) or not milestone_id.startswith(unit_prefix):
            continue
        unit_id = milestone_id[len(unit_prefix) :]
        if unit_id not in affected:
            continue
        if not isinstance(slot, dict):
            raise ValueError(f"candidate preview ledger milestone {milestone_id} must be an object")
        slot["status"] = statuses.get(unit_id, "superseded")

    atomic_write(path, json.dumps(ledger, indent=2) + "\n")
    return True


def _remove_reopened_outcome(view: Path, layer_id: str) -> None:
    """Remove one affected sealed outcome from scratch, never from live authority."""

    outcomes = view / "plans" / "outcomes"
    plan_bundle_integrity.require_real_directory(
        view,
        outcomes,
        "candidate preview outcomes",
    )
    path = _contained_path(
        view,
        view / layer_outcome_locator(layer_id),
        "candidate preview outcome",
    )
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"candidate preview outcome must be absent or a real regular file: {path}")
    path.unlink(missing_ok=True)


def project_candidate_authority_state(
    view: Path,
    publication: PreparedMaterializationPublication,
) -> None:
    """Install the gate-attested all-layer transition into isolated scratch."""

    view = _canonical_preview_root(view)
    validated = _validate_publication_projection(view, publication)
    reference_path = _contained_path(
        view,
        view / AUTHORITY_PREVIEW_REFERENCE_PATH,
        "candidate authority preview reference",
    )
    if reference_path.is_symlink() or (reference_path.exists() and not reference_path.is_file()):
        raise ValueError("candidate authority preview reference must be absent or a real file")
    _prepare_preview_state_directory(view, validated.before_hashes)
    for layer_id, payload in validated.after_payloads.items():
        target = _state_target(view, layer_id, "candidate preview successor")
        atomic_write(target, payload.decode("utf-8"))
    prepared = publication.transition
    if prepared is None:
        _verify_projected_state_directory(view, validated.after_payloads)
        reference_path.unlink(missing_ok=True)
        return
    for row in validated.effect_rows:
        target = _state_target(
            view,
            row.layer_id,
            "candidate preview transition effect",
        )
        if row.after_state is None:
            target.unlink(missing_ok=True)
            projected = {"units": {}}
        else:
            projected = validated.after_states[row.layer_id]
        effect = row.effect
        record = {
            "added": list(effect.invalidation_seed_unit_ids) if effect.effect_kind == "added" else [],
            "removed": list(effect.invalidation_seed_unit_ids) if effect.effect_kind == "removed" else [],
            "changed": list(effect.invalidation_seed_unit_ids)
            if effect.effect_kind in {"changed", "incomparable"}
            else [],
            "invalidated": list(effect.invalidated_unit_ids),
            "preserved": [unit.unit_id for unit in effect.preserved_units],
        }
        if _project_candidate_ledger(view, row.layer_id, record, projected):
            _remove_reopened_outcome(view, row.layer_id)

    _verify_projected_state_directory(view, validated.after_payloads)
    reference = AuthorityPreviewReference.mint(
        transition_intent_ref=prepared.intent_ref,
        predecessor_head_ref=publication.authority_state_head_ref,
        before_selection_token=prepared.intent.proposal.before_selection_token,
        after_selection_token=prepared.intent.proposal.after_selection_token,
        capsule_set_digest=publication.capsule_set.capsule_set_digest,
        effects_digest=prepared.intent.proposal.effects_digest,
        before_state_hashes=publication.before_state_hashes,
        after_state_hashes=publication.after_state_hashes,
    )
    atomic_write(reference_path, reference.to_bytes().decode("utf-8"))


__all__ = [
    "project_candidate_authority_state",
    "stage_candidate_publication_view",
]
