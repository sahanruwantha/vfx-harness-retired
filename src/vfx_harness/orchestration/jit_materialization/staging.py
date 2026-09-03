"""Pinned materialization of one globally deferred layer."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from vfx_harness.domain.atomicity import atomicity_gaps
from vfx_harness.domain.construction import CONSTRUCTION_ROUTE_RULE
from vfx_harness.domain.construction_routes import construction_route_gaps
from vfx_harness.domain.dressing import SAME_LAYER_DRESS_RULE, same_layer_dress_gaps
from vfx_harness.domain.image_debts import IMAGE_PROPERTY_VOCABULARY_RULE, image_property_vocabulary_gaps
from vfx_harness.domain.json_pointer import set_at as set_pointer
from vfx_harness.domain.json_pointer import split as split_pointer
from vfx_harness.domain.refobs import UNREGISTERED_WITNESS_RULE
from vfx_harness.domain.work_units import (
    CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
    DEFERRED_CONTRACT_CONTEXT_RULE,
    PROJECTED_ORIGIN_REPAIR_RULE,
    WorkUnit,
    allowed_unit_provides,
    bound_claim_contract_ids,
    deferred_claim_binding_gaps,
    point_projection_interface_gaps,
    validate_unit_script_path,
)
from vfx_harness.domain.work_units.graph import MUTATION_CLAIM_COVERAGE_RULE, uncovered_mutation_roles
from vfx_harness.domain.work_units.subject_framing import (
    SUBJECT_FRAMING_COVERAGE_RULE,
    uncovered_subject_framing_frames,
)
from vfx_harness.evidence.checks import METRICS
from vfx_harness.evidence.scene_checks import (
    PROJECTED_ORIGIN_KINDS,
    validate_row,
    validate_row_set,
)
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.jit_materialization.schema import (
    MATERIALIZATION_FIELDS,
    MATERIALIZATION_SCHEMA,
    MaterializedLayer,
    UnstagedMaterializationUnit,
    _document,
    _mutate_materialization_candidate,
    _rows,
    materialization_base_selection,
)
from vfx_harness.orchestration.jit_materialization.validate import validate_materialization
from vfx_harness.orchestration.refobs import missing_witness_ids


def inspect_materialization(
    global_root: str | Path,
    materialization_path: str | Path,
    *,
    expected_bundle_hash: str,
    base_layers_path: str | Path | None = None,
    base_scene_checks_path: str | Path | None = None,
    resolutions_path: str | Path | None = None,
    base_requirements_path: str | Path | None = None,
) -> tuple[list[str], MaterializedLayer | None]:
    """Return every collectable finding without requiring the caller to catch ValueError."""
    try:
        return [], validate_materialization(
            global_root,
            materialization_path,
            expected_bundle_hash=expected_bundle_hash,
            base_layers_path=base_layers_path,
            base_scene_checks_path=base_scene_checks_path,
            resolutions_path=resolutions_path,
            base_requirements_path=base_requirements_path,
        )
    except ValueError as exc:
        return [line for line in str(exc).split("\n") if line], None


def seed_materialization_candidate(
    global_root: str | Path,
    materialization_path: str | Path,
    *,
    layer_id: str,
    bundle_hash: str,
    base_selection: AuthoritySelectionToken,
) -> Path:
    """Create the deterministic wrapper for an incrementally staged layer design.

    The model owns unit decomposition and evidence, but not schema wrappers, bundle
    identity, global structural fields, or output paths. Seeding those facts lets the
    materializer publish one bounded unit at a time instead of generating one monolithic
    first-write document before the harness can observe any progress.
    """
    root = Path(global_root)
    rows = _rows(_document(root / "layers.json"), "layers", "layers.json")
    source = next((row for row in rows if str(row.get("id")) == str(layer_id)), None)
    if source is None or source.get("execution") != "jit_deferred":
        raise ValueError(f"layer {layer_id!r} is not selected jit_deferred authority")
    layer = dict(source)
    layer.pop("jit", None)
    layer["execution"] = "ready"
    layer["stages"] = []
    payload = {
        "schema": MATERIALIZATION_SCHEMA,
        "bundle_hash": bundle_hash,
        "base_selection": base_selection.to_dict(),
        "layer": layer,
        "scene_contracts": [],
        "image_contracts": [],
        "requirement_bindings": [],
        "acceptance": [],
    }
    target = Path(materialization_path)
    atomic_write(target, json.dumps(payload, indent=1) + "\n")
    return target


def _validate_local_staged_units(
    payload: dict[str, Any],
    *,
    allowed_provides: frozenset[str] | None = None,
    shot_folder: str | Path | None = None,
) -> None:
    """Enforce unit-local publication predicates on an in-memory candidate."""

    if payload.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError("candidate has unsupported materialization schema")
    if set(payload) != MATERIALIZATION_FIELDS:
        raise ValueError("candidate fields do not match the v3 materialization schema")
    materialization_base_selection(payload)
    stages = _rows(payload.get("layer") or {}, "stages", "candidate.layer")
    contracts = _rows(payload, "scene_contracts", "candidate")
    bindings = _rows(payload, "requirement_bindings", "candidate")
    parsed_units = [WorkUnit.parse(row, f"staged unit[{index}]") for index, row in enumerate(stages)]

    deferred_gaps = deferred_claim_binding_gaps(parsed_units, contracts)
    if deferred_gaps:
        detail = "; ".join(
            f"unit {gap.unit_id} claim {gap.claim_id} binds {gap.contract_id} "
            f"(owner_layer={gap.owner_layer}, activates_at={gap.activates_at})"
            for gap in deferred_gaps
        )
        raise ValueError(
            "deferred contract claim binding refused before candidate write: "
            + detail
            + ". "
            + DEFERRED_CONTRACT_CONTEXT_RULE
        )
    if allowed_provides is not None:
        for unit in parsed_units:
            disallowed = sorted(set(unit.provides) - allowed_provides)
            if disallowed:
                raise ValueError(
                    "global capability boundary refused before candidate write: "
                    f"unit {unit.id} declares {disallowed}; allowed here: "
                    f"{sorted(allowed_provides)}. " + CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE
                )
    layer_id = str((payload.get("layer") or {}).get("id") or "")
    for unit_index, unit in enumerate(parsed_units):
        validate_unit_script_path(layer_id, unit, f"staged unit[{unit_index}]")
        uncovered = uncovered_mutation_roles(unit)
        if uncovered:
            judged = sorted(
                {role for claim in unit.evaluation.claims if claim.required for role in claim.subject_roles}
            )
            raise ValueError(
                "required-claim coverage refused before candidate write: "
                f"unit {unit.id} mutates {list(uncovered)} without a required claim; "
                f"required claim subject_roles on this unit: {judged}. "
                + MUTATION_CLAIM_COVERAGE_RULE
            )
    image_property_gaps = image_property_vocabulary_gaps(parsed_units, METRICS)
    if image_property_gaps:
        detail = "; ".join(
            f"unit {gap.unit_id} claim {gap.claim_id} property {gap.property!r} for {list(gap.contract_ids)}"
            for gap in image_property_gaps
        )
        raise ValueError(
            "image property vocabulary refused before candidate write: "
            + detail
            + ". "
            + IMAGE_PROPERTY_VOCABULARY_RULE
        )
    unit_ids = [unit.id for unit in parsed_units]
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("staged unit ids must be unique before candidate write")
    contract_ids = [str(row.get("id") or "") for row in contracts]
    if any(not identifier for identifier in contract_ids) or len(contract_ids) != len(set(contract_ids)):
        raise ValueError("staged scene contract ids must be non-empty and unique before candidate write")
    for row in contracts:
        if error := validate_row(row):
            raise ValueError(f"scene contract {row.get('id', '<missing>')}: {error}")
    if contradictions := validate_row_set(contracts):
        # Run 20260903T031317Z (layer-1 rematerialization take 4) staged a derivative floor
        # and a cap that no curve satisfies in one call and learned it at finalize; the
        # cross-row rules read only the candidate's own contracts, so they run here too.
        raise ValueError(
            "cross-row contradiction refused before candidate write: " + "; ".join(contradictions)
        )
    requirement_ids = [str(row.get("requirement_id") or "") for row in bindings]
    if any(not identifier for identifier in requirement_ids) or len(requirement_ids) != len(set(requirement_ids)):
        raise ValueError("staged requirement ids must be non-empty and unique before candidate write")
    units_by_id = {item.id: item for item in parsed_units}
    contracts_by_id = {str(row.get("id")): row for row in contracts}
    for staged_unit in parsed_units:
        for claim in staged_unit.evaluation.claims:
            if not claim.required:
                continue
            owner = units_by_id.get(claim.repair_owner)
            for binding in claim.evidence:
                row = contracts_by_id.get(binding.id)
                if (
                    binding.kind == "scene_contract"
                    and row is not None
                    and str(row.get("kind") or "") in PROJECTED_ORIGIN_KINDS
                    and owner is not None
                    and "camera" not in owner.provides
                ):
                    raise ValueError(
                        "point-projection ownership refused before candidate write: "
                        f"unit {staged_unit.id} claim {claim.id} names repair_owner "
                        f"{owner.id}, which does not provide camera. " + PROJECTED_ORIGIN_REPAIR_RULE
                    )
    interface_gaps = point_projection_interface_gaps(parsed_units, contracts)
    if interface_gaps:
        gap = interface_gaps[0]
        if gap.reason == "owner_mutation":
            detail = f"camera owner mutates observed selector {gap.selector!r}"
        else:
            detail = (
                f"selector {gap.selector!r} is produced by {list(gap.producer_ids)} "
                "without a compatible consumed interface"
            )
        raise ValueError(
            "point-projection interface refused before candidate write: "
            f"unit {gap.unit_id} contract {gap.contract_id}: {detail}. " + PROJECTED_ORIGIN_REPAIR_RULE
        )

    dress_gaps = same_layer_dress_gaps(parsed_units)
    if dress_gaps:
        gap = dress_gaps[0]
        raise ValueError(
            "same-layer dressing refused before candidate write: "
            f"unit {gap.unit_id} dresses {list(gap.selectors)} mutated on this "
            f"layer by {list(gap.producer_ids)}. " + SAME_LAYER_DRESS_RULE
        )
    gaps = atomicity_gaps(
        parsed_units,
        contracts,
        layer_id=layer_id,
        raw_stages=stages,
    )
    if gaps:
        detail = "; ".join(f"unit {gap.unit_id} {gap.code}: {gap.detail}" for gap in gaps)
        raise ValueError("unit atomicity refused before candidate write: " + detail)
    route_gaps = construction_route_gaps(parsed_units, contracts)
    if route_gaps:
        gap = route_gaps[0]
        raise ValueError(
            "construction route refused before candidate write: "
            f"unit {gap.unit_id} {gap.code}: {gap.detail}. " + CONSTRUCTION_ROUTE_RULE
        )
    for unit in parsed_units:
        if unit.construction.route != "generate":
            continue
        if shot_folder is None:
            raise ValueError(
                f"generate unit {unit.id} names witnesses "
                f"{list(unit.construction.witnesses)} without a refobs registry. " + UNREGISTERED_WITNESS_RULE
            )
        missing = missing_witness_ids(shot_folder, unit.construction.witnesses)
        if missing:
            raise ValueError(
                f"generate unit {unit.id} names unregistered witnesses {list(missing)}. " + UNREGISTERED_WITNESS_RULE
            )
    layer_row = payload.get("layer") or {}
    if any("camera" in unit.provides for unit in parsed_units) and "projected_composition" in (
        layer_row.get("evidence_domains") or []
    ):
        judges = [
            int(row["frame"])
            for row in layer_row.get("judge") or []
            if isinstance(row, dict) and isinstance(row.get("frame"), int)
        ]
        stage_rows = {str(row.get("id")): row for row in stages if isinstance(row, dict) and row.get("id")}
        uncovered = uncovered_subject_framing_frames(layer_id, judges, stage_rows, contracts)
        if uncovered:
            # Layer-1 rematerializations 20260903T023810Z-8509f9 and 20260903T035326Z-290f3c
            # both learned this at the terminal gate after staging the camera unit.
            raise ValueError(
                "subject-framing coverage refused before candidate write: judge frame(s) "
                f"{list(uncovered)} have no bbox_* row of a rendered subject bound on this "
                "camera layer. Stage the camera unit with those rows in scene_contracts and "
                "their ids in composition_context (use the deferred subject-composition "
                "activation card compiled in your kickoff for activates_at). "
                + SUBJECT_FRAMING_COVERAGE_RULE
            )


def _stage_materialization_payload(
    payload: dict[str, Any],
    *,
    unit: dict[str, Any],
    scene_contracts: list[dict[str, Any]],
    requirement_bindings: list[dict[str, Any]],
    layer_updates: dict[str, Any] | None,
    allowed_provides: frozenset[str] | None,
    shot_folder: str | Path | None,
) -> None:
    """Apply one stage operation and validate it before the transaction writes."""

    if payload.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError("candidate has unsupported materialization schema")
    if not isinstance(unit, dict):
        raise ValueError("unit must be an object")
    parsed = WorkUnit.parse(unit, "staged unit")
    contracts = list(scene_contracts)
    bindings = list(requirement_bindings)
    if any(not isinstance(row, dict) for row in contracts):
        raise ValueError("scene_contracts must contain objects")
    if any(not isinstance(row, dict) for row in bindings):
        raise ValueError("requirement_bindings must contain objects")
    for row in contracts:
        if error := validate_row(row):
            raise ValueError(f"scene contract {row.get('id', '<missing>')}: {error}")
    stages = _rows(payload.get("layer") or {}, "stages", "candidate.layer")
    existing_units = {str(row.get("id")) for row in stages}
    if parsed.id in existing_units:
        raise ValueError(f"unit {parsed.id!r} is already staged")
    existing_contracts = {str(row.get("id")) for row in _rows(payload, "scene_contracts", "candidate")}
    incoming_contracts = [str(row.get("id") or "") for row in contracts]
    if any(not item for item in incoming_contracts):
        raise ValueError("every staged scene contract needs an id")
    duplicate_contracts = sorted(
        existing_contracts.intersection(incoming_contracts)
        | {item for item in incoming_contracts if incoming_contracts.count(item) > 1}
    )
    if duplicate_contracts:
        raise ValueError("staged scene contract ids are not unique: " + ", ".join(duplicate_contracts))
    existing_requirements = {
        str(row.get("requirement_id")) for row in _rows(payload, "requirement_bindings", "candidate")
    }
    incoming_requirements = [str(row.get("requirement_id") or "") for row in bindings]
    if any(not item for item in incoming_requirements):
        raise ValueError("every staged requirement binding needs requirement_id")
    duplicate_requirements = sorted(
        existing_requirements.intersection(incoming_requirements)
        | {item for item in incoming_requirements if incoming_requirements.count(item) > 1}
    )
    if duplicate_requirements:
        raise ValueError("staged requirement ids are not unique: " + ", ".join(duplicate_requirements))
    updates = layer_updates or {}
    unknown_updates = sorted(set(updates) - {"dressable"})
    if unknown_updates:
        raise ValueError("layer_updates may contain only dressable; got " + ", ".join(unknown_updates))
    if "dressable" in updates:
        dressable = updates["dressable"]
        if not isinstance(dressable, list) or any(
            not isinstance(value, str) or not value.strip() for value in dressable
        ):
            raise ValueError("layer_updates.dressable must be a list of non-empty strings")
        payload["layer"]["dressable"] = dressable
    stages.append(unit)
    payload["scene_contracts"].extend(contracts)
    payload["requirement_bindings"].extend(bindings)
    _validate_local_staged_units(payload, allowed_provides=allowed_provides, shot_folder=shot_folder)


def stage_materialization_unit(
    materialization_path: str | Path,
    *,
    unit: dict[str, Any],
    scene_contracts: list[dict[str, Any]],
    requirement_bindings: list[dict[str, Any]],
    layer_updates: dict[str, Any] | None = None,
    allowed_provides: frozenset[str] | None = None,
    expected_revision: str | None = None,
    shot_folder: str | Path | None = None,
    candidate_write_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> Path:
    """Append one bounded unit through the serialized candidate transaction."""
    path = Path(materialization_path)
    _mutate_materialization_candidate(
        path,
        lambda payload: _stage_materialization_payload(
            payload,
            unit=unit,
            scene_contracts=scene_contracts,
            requirement_bindings=requirement_bindings,
            layer_updates=layer_updates,
            allowed_provides=allowed_provides,
            shot_folder=shot_folder,
        ),
        expected_revision=expected_revision,
        candidate_write_guard=candidate_write_guard,
    )
    return path


def _unstage_materialization_payload(
    payload: dict[str, Any],
    *,
    unit_id: str,
    shot_folder: str | Path | None = None,
) -> UnstagedMaterializationUnit:
    """Remove one scratch unit and rows that no surviving unit can consume."""

    if payload.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError("candidate has unsupported materialization schema")
    stages = _rows(payload.get("layer") or {}, "stages", "candidate.layer")
    parsed = [WorkUnit.parse(row, f"staged unit[{index}]") for index, row in enumerate(stages)]
    target_index = next((index for index, unit in enumerate(parsed) if unit.id == unit_id), None)
    if target_index is None:
        available = ", ".join(unit.id for unit in parsed) or "(none)"
        raise ValueError(f"cannot unstage unknown unit {unit_id!r}; staged unit ids: {available}")
    dependants = sorted(
        unit.id
        for unit in parsed
        if unit.id != unit_id
        and (unit_id in unit.depends_on or any(consume.producer == unit_id for consume in unit.consumes))
    )
    if dependants:
        raise ValueError(
            f"cannot unstage unit {unit_id!r}; remaining unit(s) "
            + ", ".join(dependants)
            + " depend on or consume it. Unstage dependants first, or patch their exact "
            "depends_on/consumes fields before retrying."
        )

    target = parsed[target_index]
    surviving = tuple(unit for unit in parsed if unit.id != unit_id)
    surviving_contract_ids = {contract_id for unit in surviving for contract_id in bound_claim_contract_ids(unit)}
    removed_contract_ids = tuple(
        contract_id for contract_id in bound_claim_contract_ids(target) if contract_id not in surviving_contract_ids
    )
    removed_contract_set = set(removed_contract_ids)

    del stages[target_index]
    contracts = _rows(payload, "scene_contracts", "candidate")
    payload["scene_contracts"] = [row for row in contracts if str(row.get("id") or "") not in removed_contract_set]

    removed_requirement_ids: list[str] = []
    retained_bindings: list[dict[str, Any]] = []
    for row in _rows(payload, "requirement_bindings", "candidate"):
        contract_ids = [
            str(contract_id)
            for contract_id in row.get("contract_ids") or []
            if str(contract_id) not in removed_contract_set
        ]
        if row.get("contract_ids") is not None:
            if not contract_ids:
                removed_requirement_ids.append(str(row.get("requirement_id") or ""))
                continue
            row = {**row, "contract_ids": contract_ids}
        retained_bindings.append(row)
    payload["requirement_bindings"] = retained_bindings
    _validate_local_staged_units(payload, shot_folder=shot_folder)
    return UnstagedMaterializationUnit(
        unit_id=unit_id,
        removed_contract_ids=removed_contract_ids,
        removed_requirement_ids=tuple(removed_requirement_ids),
    )


def unstage_materialization_unit(
    materialization_path: str | Path,
    *,
    unit_id: str,
    expected_revision: str | None = None,
    shot_folder: str | Path | None = None,
    candidate_write_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> UnstagedMaterializationUnit:
    """Retire one unit from unpublished scratch through the candidate transaction."""
    token = str(unit_id).strip()
    if not token:
        raise ValueError("unit_id is required")
    result: UnstagedMaterializationUnit | None = None

    def mutate(payload: dict[str, Any]) -> None:
        nonlocal result
        result = _unstage_materialization_payload(payload, unit_id=token, shot_folder=shot_folder)

    _mutate_materialization_candidate(
        Path(materialization_path),
        mutate,
        expected_revision=expected_revision,
        candidate_write_guard=candidate_write_guard,
    )
    assert result is not None
    return result


def apply_materialization_patch(
    global_root: str | Path,
    materialization_path: str | Path,
    pointer: str,
    value: Any,
    *,
    expected_bundle_hash: str,
    base_layers_path: str | Path | None = None,
    base_scene_checks_path: str | Path | None = None,
    resolutions_path: str | Path | None = None,
    base_requirements_path: str | Path | None = None,
    expected_revision: str | None = None,
    shot_folder: str | Path | None = None,
    candidate_write_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> list[str]:
    """Set one JSON pointer on the candidate file and return remaining findings."""
    return apply_materialization_patches(
        global_root,
        materialization_path,
        ((pointer, value),),
        expected_bundle_hash=expected_bundle_hash,
        base_layers_path=base_layers_path,
        base_scene_checks_path=base_scene_checks_path,
        resolutions_path=resolutions_path,
        base_requirements_path=base_requirements_path,
        expected_revision=expected_revision,
        shot_folder=shot_folder,
        candidate_write_guard=candidate_write_guard,
    )


def apply_materialization_patches(
    global_root: str | Path,
    materialization_path: str | Path,
    patches: tuple[tuple[str, Any], ...] | list[tuple[str, Any]],
    *,
    expected_bundle_hash: str,
    base_layers_path: str | Path | None = None,
    base_scene_checks_path: str | Path | None = None,
    resolutions_path: str | Path | None = None,
    base_requirements_path: str | Path | None = None,
    expected_revision: str | None = None,
    shot_folder: str | Path | None = None,
    candidate_write_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> list[str]:
    """Atomically set several JSON pointers and validate the resulting candidate once.

    All pointer operations happen on an in-memory document before the first write. A bad
    pointer therefore leaves the candidate unchanged instead of publishing a partial
    repair. The single-patch API delegates here so both paths have identical semantics.
    """
    if not patches:
        raise ValueError("materialization patch transaction must contain at least one patch")
    path = Path(materialization_path)
    parsed_pointers: list[list[str]] = []
    for pointer, _value in patches:
        tokens = split_pointer(pointer)
        if tokens == ["layer"] or (tokens[:2] == ["layer", "stages"] and (len(tokens) <= 3 or tokens[2] == "-")):
            raise ValueError(
                "patch_materialization cannot add, replace, or reorder staged units; "
                "add each unit through stage_materialization_unit and patch only fields "
                "inside an existing staged unit"
            )
        parsed_pointers.append(tokens)

    findings: list[str] = []

    def mutate(payload: dict[str, Any]) -> None:
        nonlocal findings
        for (pointer, value), _tokens in zip(patches, parsed_pointers, strict=True):
            set_pointer(payload, pointer, value)
        if any(
            tokens[:2] == ["layer", "stages"]
            or tokens[:1] == ["scene_contracts"]
            or tokens[:1] == ["requirement_bindings"]
            for tokens in parsed_pointers
        ):
            layer_id = str((payload.get("layer") or {}).get("id") or "")
            global_layers = _rows(_document(Path(global_root) / "layers.json"), "layers", "layers.json")
            global_row = next(
                (row for row in global_layers if str(row.get("id") or "") == layer_id),
                None,
            )
            if global_row is None:
                raise ValueError(f"layer {layer_id!r} has no sparse global authority for candidate repair")
            _validate_local_staged_units(
                payload,
                allowed_provides=allowed_unit_provides(global_row),
                shot_folder=shot_folder,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".materialization.json",
            prefix=".candidate-validate-",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(json.dumps(payload, indent=1) + "\n")
            proposed_path = Path(handle.name)
        try:
            import vfx_harness.orchestration.jit_materialization as package  # noqa: PLC0415

            findings, _materialized = package.inspect_materialization(
                global_root,
                proposed_path,
                expected_bundle_hash=expected_bundle_hash,
                base_layers_path=base_layers_path,
                base_scene_checks_path=base_scene_checks_path,
                resolutions_path=resolutions_path,
                base_requirements_path=base_requirements_path,
            )
        finally:
            proposed_path.unlink(missing_ok=True)

    _mutate_materialization_candidate(
        path,
        mutate,
        expected_revision=expected_revision,
        candidate_write_guard=candidate_write_guard,
    )
    return findings
