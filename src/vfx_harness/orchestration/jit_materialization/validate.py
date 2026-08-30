"""Pinned materialization of one globally deferred layer."""

from __future__ import annotations

import fnmatch
import json
import tempfile
from pathlib import Path

from vfx_harness.domain.atomicity import ATOMICITY_RULE, atomicity_gaps
from vfx_harness.domain.dressing import DRESSING_CLOSURE_FIX, SAME_LAYER_DRESS_RULE, same_layer_dress_gaps
from vfx_harness.domain.image_debts import (
    IMAGE_PROPERTY_VOCABULARY_RULE,
    image_contract_debt_cards,
    image_property_vocabulary_gaps,
    payable_image_property_kinds,
)
from vfx_harness.domain.image_signal import (
    IMAGE_SIGNAL_DEPENDENCY_RULE,
    IMAGE_SUBJECT_DEPENDENCY_RULE,
    image_signal_dependency_gaps,
    image_signal_provider_ids,
    image_signal_witness_guidance,
    image_subject_dependency_gaps,
    image_subject_provider_ids,
)
from vfx_harness.domain.json_pointer import encode as json_ptr
from vfx_harness.domain.json_pointer import format_finding
from vfx_harness.domain.work_units import (
    CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
    DEFERRED_CONTRACT_CONTEXT_RULE,
    DEFERRED_SUBJECT_ACTIVATION_RULE,
    GEOMETRY_VIS_CYCLE_RULE,
    GEOMETRY_VIS_DEPENDENCY_RULE,
    LOOK_CAPABILITIES,
    LOOK_REQUIRES_IMAGE_DOMAIN_RULE,
    PROJECTED_ORIGIN_REPAIR_RULE,
    UNIT_JUDGE_CLAIM_COVERAGE_RULE,
    VIS_REPAIR_OWNER_RULE,
    allowed_unit_provides,
    compile_deferred_subject_activation,
    deferred_claim_binding_gaps,
    deferred_subject_activation_gaps,
    geometry_vis_dependency_cycles,
    geometry_vis_dependency_gaps,
    plan_selector_declared,
    point_projection_interface_gaps,
    uncovered_unit_judge_frames,
    unearned_look_judge_frames,
    vis_roles_unrepairable_by,
)
from vfx_harness.evidence.checks import METRICS
from vfx_harness.evidence.scene_checks import (
    PROJECTED_ORIGIN_KINDS,
    SURFACE_PROJECTED_KINDS,
    deferred_subject_composition_payment_gaps,
    validate_row,
    validate_row_set,
)
from vfx_harness.orchestration.jit_materialization.schema import (
    MATERIALIZATION_SCHEMA,
    ROLE_SELECTOR_CLOSURE_RULE,
    TWO_SIDED_MEASUREMENT_KINDS,
    MaterializedLayer,
    _document,
    _matches_reserved,
    _rows,
)
from vfx_harness.orchestration.jit_materialization.validate_requirements import (
    complete_validated_layer,
    note_required_claim_metric_domains,
)
from vfx_harness.orchestration.ledger import load_layers_from_path


def validate_materialization(
    global_root: str | Path,
    materialization_path: str | Path,
    *,
    expected_bundle_hash: str,
    base_layers_path: str | Path | None = None,
    base_scene_checks_path: str | Path | None = None,
    resolutions_path: str | Path | None = None,
    base_requirements_path: str | Path | None = None,
) -> MaterializedLayer:
    """Validate one overlay without publishing or creating durable unit state.

    Callers pass the shot's `state/plan-resolutions.jsonl` as `resolutions_path` and the
    selected-view register as `base_requirements_path`; bundles do not freeze either.
    """
    root = Path(global_root)
    source = Path(materialization_path)
    payload = _document(source)
    if payload.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError(f"{source} has unsupported JIT materialization schema")
    if payload.get("bundle_hash") != expected_bundle_hash:
        raise ValueError("JIT materialization is pinned to another global bundle")
    layer_row = payload.get("layer")
    if not isinstance(layer_row, dict):
        raise ValueError("JIT materialization.layer must be an object")
    layer_id = str(layer_row.get("id") or "")

    global_layers = _rows(_document(root / "layers.json"), "layers", "layers.json")
    by_id = {str(row.get("id")): row for row in global_layers}
    global_row = by_id.get(layer_id)
    if global_row is None or global_row.get("execution") != "jit_deferred":
        raise ValueError(f"layer {layer_id!r} is not selected jit_deferred authority")

    findings: list[str] = []

    def note(pointer: str, message: str) -> None:
        findings.append(format_finding(pointer, message))

    if layer_row.get("execution") not in {None, "ready"}:
        note(json_ptr("layer", "execution"), "materialized layer execution must be ready")
    layer_row = dict(layer_row)
    layer_row["execution"] = "ready"
    layer_row.pop("jit", None)
    structural = ("id", "script", "title", "primary_judge", "judge", "owns", "evidence_domains", "reads")
    changed = [key for key in structural if layer_row.get(key) != global_row.get(key)]
    if changed:
        note(
            json_ptr("layer"),
            "JIT materialization changes global structural authority: " + ", ".join(changed),
        )

    base_document = (
        _document(Path(base_layers_path)) if base_layers_path else _document(root / "layers.json")
    )
    base_layers = (
        _rows(base_document, "layers", "layers.json") if base_layers_path else global_layers
    )
    combined_layers = [layer_row if str(row.get("id")) == layer_id else row for row in base_layers]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".layers.json", prefix=".jit-validate-", dir=root, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        # Combined view keeps the base document schema; unit-first loader rules need schema 5.
        json.dump(
            {"schema": base_document.get("schema", 4), "layers": combined_layers}, handle
        )
    parsed = None
    layer = None
    try:
        parsed = load_layers_from_path(temp_path)
        layer = parsed[layer_id]
    except (ValueError, KeyError, TypeError) as exc:
        note(json_ptr("layer", "stages"), f"layer stages did not parse: {exc}")
    finally:
        temp_path.unlink(missing_ok=True)

    # Dressing closure (ADR-0007): a unit may declare appearance-assignment authority
    # only over selectors some OTHER layer explicitly marked dressable. Exact-string
    # match, not glob-vs-glob: the owner names the surface it exposes, the dresser
    # names the same surface. Same-layer mutation roles are never dressable here
    # (HIR-0161).
    if parsed is not None and layer is not None:

        declared_dressable = {
            selector
            for other in parsed.values()
            if str(other.id) != str(layer_id)
            for selector in other.dressable
        }
        same_layer = {
            gap.unit_id: set(gap.selectors) for gap in same_layer_dress_gaps(layer.stages)
        }
        unit_index_by_id = {unit.id: index for index, unit in enumerate(layer.stages)}
        for gap in same_layer_dress_gaps(layer.stages):
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "mutates",
                    "dresses",
                ),
                f"unit {gap.unit_id} dresses same-layer mutation roles "
                f"{list(gap.selectors)} produced by {list(gap.producer_ids)}. "
                + SAME_LAYER_DRESS_RULE,
            )
        for unit_index, unit in enumerate(layer.stages):
            undeclared_dresses = sorted(
                set(unit.mutates.dresses)
                - declared_dressable
                - same_layer.get(unit.id, set())
            )
            if undeclared_dresses:
                note(
                    json_ptr("layer", "stages", unit_index, "mutates", "dresses"),
                    f"unit {unit.id} dresses {', '.join(undeclared_dresses)} — no other "
                    "layer declares these selectors dressable. " + DRESSING_CLOSURE_FIX,
                )

    # Look scope is typed authority, and silence is not a declaration: a unit that omits
    # the key is indistinguishable from one that declares "no appearance", which is how
    # run 20260823T154920Z left an appearance-owning unit without image feedback. An
    # explicit empty list is the legal way to own no appearance.

    for stage_index, stage in enumerate(layer_row.get("stages") or []):
        if isinstance(stage, dict) and "look_capabilities" not in stage:
            note(
                json_ptr("layer", "stages", stage_index, "look_capabilities"),
                "materialized unit(s) must declare look_capabilities: "
                + str(stage.get("id") or "<unnamed>")
                + f" — a subset of {', '.join(sorted(LOOK_CAPABILITIES))}, or [] to own no "
                "appearance",
            )

    jit = global_row.get("jit") or {}
    raw_global_capabilities = jit.get("provides") or {}
    global_capabilities = (
        {
            str(capability): tuple(str(role) for role in roles)
            for capability, roles in raw_global_capabilities.items()
        }
        if isinstance(raw_global_capabilities, dict)
        else {}
    )
    reserved = tuple(map(str, jit.get("reserved_roles") or []))
    if layer is not None:

        allowed_capabilities = allowed_unit_provides(global_row)
        unit_capabilities = {
            capability for unit in layer.stages for capability in unit.provides
        }
        missing_capabilities = sorted(set(global_capabilities) - unit_capabilities)
        if missing_capabilities:
            note(
                json_ptr("layer", "stages"),
                "materialized layer does not fulfill globally declared scene "
                "capabilities: " + ", ".join(missing_capabilities),
            )
        for unit_index, unit in enumerate(layer.stages):
            disallowed = sorted(set(unit.provides) - allowed_capabilities)
            if disallowed:
                note(
                    json_ptr("layer", "stages", unit_index, "provides"),
                    f"unit {unit.id} declares capabilities outside sparse global "
                    f"authority: {', '.join(disallowed)}; allowed here: "
                    f"{', '.join(sorted(allowed_capabilities)) or '(none)'}. "
                    + CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
                )
            if "camera" in unit.provides:
                camera_roles = global_capabilities.get("camera", ())
                if not camera_roles:
                    note(
                        json_ptr("layer", "stages", unit_index, "provides"),
                        f"unit {unit.id} declares camera capability, but global layer "
                        f"{layer_id} did not reserve it in jit.provides; camera ownership "
                        "must be decided in the sparse global DAG",
                    )
                elif not any(
                    _matches_reserved(role, camera_roles) for role in unit.mutates.roles
                ):
                    note(
                        json_ptr("layer", "stages", unit_index, "provides"),
                        f"unit {unit.id} declares camera capability without mutating any "
                        "globally reserved camera interface role: "
                        + ", ".join(camera_roles),
                    )
            escaped = sorted(
                role for role in unit.mutates.roles if not _matches_reserved(role, reserved)
            )
            if escaped:
                note(
                    json_ptr("layer", "stages", unit_index, "mutates", "roles"),
                    "materialized roles escape global namespace reservations: "
                    + ", ".join(escaped),
                )

    try:
        scene_rows = _rows(payload, "scene_contracts", "materialization")
    except ValueError as exc:
        note(json_ptr("scene_contracts"), str(exc))
        scene_rows = []
    try:
        image_rows = _rows(payload, "image_contracts", "materialization")
    except ValueError as exc:
        note(json_ptr("image_contracts"), str(exc))
        image_rows = []
    if image_rows:
        note(
            json_ptr("image_contracts"),
            "materialization.image_contracts must be empty; candidate-sensitive image "
            "checks are proposed after the producing unit mutates the cumulative scene",
        )
    seen_ids: dict[str, int] = {}
    for index, row in enumerate(scene_rows):
        cid = str(row.get("id") or "")
        if not cid or cid in seen_ids:
            note(
                json_ptr("scene_contracts", index, "id"),
                "materialized contract ids must be present and unique",
            )
        else:
            seen_ids[cid] = index
    for index, row in enumerate(image_rows):
        cid = str(row.get("id") or "")
        if not cid or cid in seen_ids:
            note(
                json_ptr("image_contracts", index, "id"),
                "materialized contract ids must be present and unique",
            )
        else:
            seen_ids[cid] = index
    all_contracts = {
        str(row.get("id")): ("scene_contract", row) for row in scene_rows if row.get("id")
    }
    all_contracts.update({
        str(row.get("id")): ("image_contract", row) for row in image_rows if row.get("id")
    })
    if layer is not None:

        unit_index_by_id = {unit.id: index for index, unit in enumerate(layer.stages)}
        for gap in deferred_claim_binding_gaps(layer.stages, scene_rows):
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "evaluation",
                    "claims",
                ),
                f"unit {gap.unit_id} claim {gap.claim_id} directly binds deferred "
                f"scene contract {gap.contract_id} (owner_layer={gap.owner_layer}, "
                f"activates_at={gap.activates_at}). "
                + DEFERRED_CONTRACT_CONTEXT_RULE,
            )
    # HIR-0122: JIT materialization deliberately publishes no candidate-sensitive
    # image rows. Required image bindings on the typed work-unit claims are the
    # authoritative build-time debt catalog until propose_checks can measure a live
    # candidate. Requirement closure must therefore resolve those ids from the same
    # compiler used by builder payment rather than treating the intentionally-empty
    # image_contracts array as absence.
    image_debt_ids: set[str] = set()
    if layer is not None:

        image_debt_ids = {
            debt.id
            for unit in layer.stages
            for debt in image_contract_debt_cards(unit)
        }
    requirement_contract_ids = set(all_contracts) | image_debt_ids
    for index, row in enumerate(scene_rows):
        error = validate_row(row)
        if error:
            note(
                json_ptr("scene_contracts", index),
                f"scene contract {row.get('id', '<missing>')}: {error}",
            )
        if str(row.get("owner_layer") or row.get("activates_at") or "") != layer_id:
            # Run 20260825 (bb54f1): a correctly-designed cross-layer visibility row
            # died here as owner_layer=2 inside layer 1's materialization, and the bare
            # "must be owned" message cost the whole session. Name the legal form.
            note(
                json_ptr("scene_contracts", index, "owner_layer"),
                f"scene contract {row.get('id')} must be owned by layer {layer_id} "
                f"(it declares owner_layer={row.get('owner_layer')!r}). A contract this "
                "layer authors but a LATER layer evaluates keeps "
                f"owner_layer={layer_id!r} and sets activates_at to the later layer "
                "(fault_owner may still name this layer); only rows this layer owns "
                "may publish here",
            )

    activation_card = compile_deferred_subject_activation(global_layers, layer_id)
    for gap in deferred_subject_activation_gaps(activation_card, scene_rows):
        note(
            json_ptr("scene_contracts", gap.index, "activates_at"),
            f"scene contract {gap.contract_id} activates_at={gap.found!r}; compiled "
            f"earliest_geometry_layer is {gap.expected!r}. "
            + DEFERRED_SUBJECT_ACTIVATION_RULE,
        )
    for index, row in enumerate(image_rows):
        if str(row.get("owner_layer") or "") != layer_id:
            note(
                json_ptr("image_contracts", index, "owner_layer"),
                f"image contract {row.get('id')} must be owned by layer {layer_id}",
            )
    for cross_row_finding in validate_row_set(scene_rows):
        note(json_ptr("scene_contracts"), f"scene contract {cross_row_finding}")

    # HIR-0110: candidate-sensitive image debt may be due only after the cumulative
    # dependency closure has a typed way to affect optical signal.  Read earlier-layer
    # rows from the same selected consumer view as base layers; do not infer capability
    # from look labels, role names, or warm Blender state.
    base_scene_path = (
        Path(base_scene_checks_path)
        if base_scene_checks_path is not None
        else root / "scene_checks.json"
    )
    try:
        base_scene_rows = _rows(
            _document(base_scene_path), "contracts", "scene_checks.json"
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        note(
            json_ptr("scene_contracts"),
            f"cannot resolve earlier-layer optical-signal authority: {exc}",
        )
        base_scene_rows = []
    combined_scene_rows = [
        row
        for row in base_scene_rows
        if str(row.get("owner_layer") or "") != layer_id
    ] + list(scene_rows)

    # HIR-0132: a required visibility row activates at its repair-owner unit. Later
    # geometry must depend on and protect that owner; future surfaces are not due early.
    if layer is not None:
        unit_index_by_id = {unit.id: index for index, unit in enumerate(layer.stages)}
        for gap in deferred_subject_composition_payment_gaps(
            combined_scene_rows, layer.stages, layer_id
        ):
            note(
                json_ptr("layer", "stages"),
                f"deferred subject composition {gap.contract_id} selects roles "
                f"{list(gap.roles)} but no geometry unit has every overlapping producer "
                f"{list(gap.producer_ids)} in its dependency closure. Order the truthful "
                "write clusters so the first complete cumulative subject pays the "
                "camera-owned bbox; do not evaluate the row before its subject exists.",
            )
        vis_gaps = geometry_vis_dependency_gaps(layer.stages, scene_rows, layer_id)
        vis_cycles = geometry_vis_dependency_cycles(layer.stages, scene_rows, layer_id)
        cyclic_edges = {edge for cycle in vis_cycles for edge in cycle.edges}
        for cycle in vis_cycles:
            edge_text = ", ".join(f"{source}->{target}" for source, target in cycle.edges)
            note(
                json_ptr("layer", "stages"),
                f"mutual geometry visibility cycle among units {list(cycle.unit_ids)}; "
                f"protected contracts {list(cycle.contract_ids)} select roles "
                f"{list(cycle.roles)}; producer edges are [{edge_text}]. "
                + GEOMETRY_VIS_CYCLE_RULE,
            )
        for gap in vis_gaps:
            if any((gap.unit_id, producer) in cyclic_edges for producer in gap.producer_ids):
                continue
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "provides",
                ),
                f"geometry unit {gap.unit_id} protects visible_fraction "
                f"{gap.contract_id}, but its typed repair owner(s) / role producer(s) "
                f"{list(gap.producer_ids)} are outside the dependency closure for "
                f"role {gap.role!r}. "
                + GEOMETRY_VIS_DEPENDENCY_RULE,
            )
        for gap in point_projection_interface_gaps(layer.stages, scene_rows):
            if gap.reason == "owner_mutation":
                detail = (
                    f"camera owner also mutates observed selector {gap.selector!r}"
                )
            else:
                detail = (
                    f"selector {gap.selector!r} is produced by "
                    f"{list(gap.producer_ids)}, but the camera owner consumes no "
                    "compatible typed interface"
                )
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "consumes",
                ),
                f"point-projection contract {gap.contract_id}: {detail}. "
                + PROJECTED_ORIGIN_REPAIR_RULE,
            )


        raw_stages = tuple(
            row
            for row in (layer_row.get("stages") or [])
            if isinstance(row, dict)
        )
        for gap in atomicity_gaps(
            layer.stages, scene_rows, layer_id=layer_id, raw_stages=raw_stages
        ):
            pointer_field = {
                "padding": "id",
                "mixed_clusters": "mutates",
                "missing_interface": "id",
                "authored_exports": "publishes",
                "unknown_kind": "evaluation",
                "repair_owners": "evaluation",
                "consumed_mutation": "mutates",
                "invalid_coordination": "evaluation",
                "unresolved_family": "mutates",
                "missing_consumption": "consumes",
                "incompatible_interface": "consumes",
            }.get(gap.code, "mutates")
            extra = ""
            if gap.considered_exceptions:
                extra = (
                    " Exceptions considered and insufficient: "
                    + ", ".join(gap.considered_exceptions)
                    + "."
                )
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    pointer_field,
                ),
                f"unit {gap.unit_id}: {gap.detail}{extra} Legal next actions: "
                "split the unit, consume a typed assembly interface, bind dressing, "
                "or reassign evidence. " + ATOMICITY_RULE,
            )


        target_index = next(
            (
                index
                for index, row in enumerate(combined_layers)
                if str(row.get("id") or "") == layer_id
            ),
            0,
        )
        earlier_signal_available = any(
            str(row.get("execution") or "") == "ready"
            and str(row.get("id") or "") in parsed
            and image_signal_provider_ids(
                parsed[str(row.get("id"))].stages, combined_scene_rows
            )
            for row in combined_layers[:target_index]
        )
        earlier_subject_available = any(
            str(row.get("execution") or "") == "ready"
            and str(row.get("id") or "") in parsed
            and image_subject_provider_ids(
                parsed[str(row.get("id"))].stages, combined_scene_rows
            )
            for row in combined_layers[:target_index]
        )
        for gap in image_signal_dependency_gaps(
            layer.stages,
            combined_scene_rows,
            earlier_signal_available=earlier_signal_available,
        ):
            available = (
                " Same-layer signal provider(s) exist but are outside the dependency "
                f"closure: {list(gap.available_provider_ids)}."
                if gap.available_provider_ids
                else " No same-layer unit currently derives a signal family."
            )
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "depends_on",
                ),
                f"unit {gap.unit_id} owes image-contract debt "
                f"{list(gap.contract_ids)} before optical signal is available."
                + available
                + " Registered write-kind witnesses: "
                + image_signal_witness_guidance()
                + ". "
                + IMAGE_SIGNAL_DEPENDENCY_RULE,
            )
        for gap in image_subject_dependency_gaps(
            layer.stages,
            combined_scene_rows,
            earlier_subject_available=earlier_subject_available,
        ):
            available = (
                " Same-layer rendered-carrier unit(s) exist but are outside the "
                f"dependency closure: {list(gap.available_provider_ids)}."
                if gap.available_provider_ids
                else " No same-layer unit currently derives a mesh, volume, or compositor family."
            )
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "depends_on",
                ),
                f"unit {gap.unit_id} owes image-contract debt "
                f"{list(gap.contract_ids)} before a rendered carrier is available."
                + available
                + " "
                + IMAGE_SUBJECT_DEPENDENCY_RULE,
            )
    # A binding that declares its moments must include the bound contract's own frame:
    # declaring moments [150] for a frame-72 contract authors evidence that can never
    # be produced when it is due.
    frame_by_id = {
        str(row.get("id")): int(row["frame"])
        for row in scene_rows
        if isinstance(row, dict) and row.get("id") and row.get("frame") is not None
    }
    if layer is not None:

        for unit_index, unit in enumerate(layer.stages):
            missing_frames = uncovered_unit_judge_frames(unit)
            if missing_frames:
                note(
                    json_ptr("layer", "stages", unit_index, "evaluation", "judge"),
                    f"unit {unit.id} judges frame(s) {list(missing_frames)} with no "
                    f"required claim moment. {UNIT_JUDGE_CLAIM_COVERAGE_RULE}",
                )
            unearned_frames = unearned_look_judge_frames(unit)
            if unearned_frames:
                note(
                    json_ptr("layer", "stages", unit_index, "look_capabilities"),
                    f"unit {unit.id} declares look_capabilities "
                    f"{list(unit.look_capabilities)} but judge frame(s) "
                    f"{list(unearned_frames)} have no required image-domain claim. "
                    f"{LOOK_REQUIRES_IMAGE_DOMAIN_RULE}",
                )
            for claim in unit.evaluation.claims:
                for binding in claim.evidence:
                    declared = getattr(binding, "moments", None)
                    contract_frame = frame_by_id.get(str(binding.id))
                    if (
                        declared is not None
                        and contract_frame is not None
                        and contract_frame not in declared
                    ):
                        note(
                            json_ptr("layer", "stages", unit_index, "evaluation", "claims"),
                            f"unit {unit.id} claim {claim.id} binds {binding.id} at moments "
                            f"{sorted(declared)}, but that contract measures frame "
                            f"{contract_frame} — a binding due when it cannot be produced",
                        )

    required_bindings: set[tuple[str, str]] = set()
    if layer is not None:
        required_bindings = {
            (binding.kind, binding.id)
            for unit in layer.stages
            for claim in unit.evaluation.claims
            if claim.required
            for binding in claim.evidence
        }
        # Composition context rows are producer bindings, same as claim evidence.
        required_bindings.update(
            ("scene_contract", str(contract_id))
            for unit in layer.stages
            if unit.evaluation.composition_context is not None
            for contract_id in unit.evaluation.composition_context.contract_ids
        )
    missing_claims = sorted(
        contract_id
        for contract_id, (kind, _row) in all_contracts.items()
        if (kind, contract_id) not in required_bindings
    )
    if missing_claims and layer is not None:
        note(
            json_ptr("layer", "stages"),
            "materialized contracts lack required producing claims: "
            + ", ".join(missing_claims),
        )

    # A mesh metric needs polygons under the roles it selects. Units declare `geometry`
    # when their roles carry meshes; a mesh metric aimed anywhere else reads None
    # forever, which is a binding defect no build can repair. Only enforced once some
    # unit in the layer declares anything, so legacy units are not judged on a
    # declaration they never had the chance to make.
    if layer is not None:
        MESH_KINDS = {"smooth_fraction", "mesh_vertex_count", "radial_inward_fraction"}
        declares_anything = any(unit.provides for unit in layer.stages)
        geometry_roles = {
            role
            for unit in layer.stages
            if "geometry" in unit.provides
            for role in unit.mutates.roles
        }
        if declares_anything:
            for index, row in enumerate(scene_rows):
                if str(row.get("kind")) not in MESH_KINDS:
                    continue
                roles = [str(r) for r in (row.get("roles") or [])]
                if roles and not any(
                    any(
                        fnmatch.fnmatchcase(role, owned) or fnmatch.fnmatchcase(owned, role)
                        for owned in geometry_roles
                    )
                    for role in roles
                ):
                    note(
                        json_ptr("scene_contracts", index, "kind"),
                        f"contract {row.get('id')} uses mesh metric {row.get('kind')!r} on "
                        f"roles {roles}, but no unit declaring provides:[\"geometry\"] owns "
                        f"them (geometry roles: {sorted(geometry_roles) or 'none declared'}); "
                        "it can only read None. Bind a metric that applies to these roles, "
                        "or declare the unit that gives them polygons.",
                    )

    # A metric may only close a claim it can actually support. Counting rim modules
    # proves they exist, not that they chase; radial closure proves an aperture is shut,
    # not that it reads as machined metal. Run 20260823T154920Z shipped both.

    if layer is not None:
        unit_index_by_id = {unit.id: index for index, unit in enumerate(layer.stages)}
        payable_properties = sorted(payable_image_property_kinds(METRICS))
        for gap in image_property_vocabulary_gaps(layer.stages, METRICS):
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "evaluation",
                    "claims",
                ),
                f"unit {gap.unit_id} required image claim {gap.claim_id} uses "
                f"unpayable property {gap.property!r} for {list(gap.contract_ids)}. "
                f"Accepted image properties: {payable_properties}. "
                + IMAGE_PROPERTY_VOCABULARY_RULE,
            )
        for unit_index, unit in enumerate(layer.stages):
            # Every mutated role needs a required claim answering for it — the gate's
            # claim-closure rule, enforced HERE so the write-hook reports it in-session.
            # Run 20260825T015307Z-bc9109 published a materialization the validator called
            # clean and the gate then blocked with 7 role-closure findings the session
            # could no longer see.
            closure_roles = {
                role
                for claim in unit.evaluation.claims
                if claim.required
                for role in claim.subject_roles
            }
            for mutation_role in [*unit.mutates.roles, *unit.mutates.dresses]:
                if not any(
                    fnmatch.fnmatchcase(role, mutation_role)
                    or fnmatch.fnmatchcase(mutation_role, role)
                    for role in closure_roles
                ):
                    note(
                        json_ptr("layer", "stages", unit_index, "mutates"),
                        f"unit {unit.id}: mutation role {mutation_role!r} has no required "
                        "claim; every mutated or dressed role needs a required claim whose "
                        "subject_roles cover it, or must be dropped from mutates",
                    )
            for claim in unit.evaluation.claims:
                if not claim.required:
                    continue
                owner = next(
                    (item for item in layer.stages if item.id == claim.repair_owner),
                    unit,
                )
                for binding in claim.evidence:
                    if binding.kind != "scene_contract":
                        continue
                    bound = all_contracts.get(binding.id)
                    if not bound or bound[0] != "scene_contract":
                        continue
                    vis_row = bound[1]
                    if (
                        str(vis_row.get("kind") or "") in PROJECTED_ORIGIN_KINDS
                        and "camera" not in owner.provides
                    ):
                        note(
                            json_ptr(
                                "layer", "stages", unit_index, "evaluation", "claims"
                            ),
                            f"unit {unit.id} binds point-projection metric "
                            f"{vis_row.get('kind')} {binding.id}, but repair_owner "
                            f"{owner.id} does not provide camera. "
                            + PROJECTED_ORIGIN_REPAIR_RULE,
                        )
                    if str(vis_row.get("kind") or "") in SURFACE_PROJECTED_KINDS:
                        roles = [str(value) for value in vis_row.get("roles") or []]
                        mutates_measured_role = any(
                            fnmatch.fnmatchcase(role, selector)
                            or fnmatch.fnmatchcase(selector, role)
                            for role in roles
                            for selector in owner.mutates.roles
                        )
                        dresses_measured_role = any(
                            fnmatch.fnmatchcase(role, selector)
                            or fnmatch.fnmatchcase(selector, role)
                            for role in roles
                            for selector in owner.mutates.dresses
                        )
                        if (
                            mutates_measured_role
                            and "geometry" not in owner.provides
                            and not dresses_measured_role
                        ):
                            note(
                                json_ptr(
                                    "layer", "stages", unit_index, "evaluation", "claims"
                                ),
                                f"unit {unit.id} binds surface metric "
                                f"{vis_row.get('kind')} {binding.id} on its mutated roles "
                                f"{roles}, but repair_owner {owner.id} does not provide "
                                "geometry or dress those roles. A control/Empty host has "
                                "no rendered surface. Use projected_origin_x/"
                                "projected_origin_y for a point interface, or split a "
                                "genuine geometry provider; do not add proxy mesh only "
                                "to satisfy bbox/visible_fraction.",
                            )
                    kind = str(vis_row.get("kind") or "")
                    owner_layer = str(vis_row.get("owner_layer") or "")
                    activates_at = str(vis_row.get("activates_at") or owner_layer)
                    deferred_row = bool(
                        owner_layer and activates_at and activates_at != owner_layer
                    )
                    observation_only = "camera" in owner.provides and (
                        kind == "visible_fraction" or kind in PROJECTED_ORIGIN_KINDS
                    )
                    if not deferred_row:
                        if observation_only:
                            primary_keys: tuple[str, ...] = ()
                        elif kind in TWO_SIDED_MEASUREMENT_KINDS:
                            primary_keys = ("roles",)
                        else:
                            primary_keys = ("roles", "compare_roles")
                        selected_roles = {
                            str(value)
                            for key in primary_keys
                            for value in vis_row.get(key) or []
                        }
                        mutable_roles = [*unit.mutates.roles, *unit.mutates.dresses]
                        undeclared_roles = sorted(
                            selector
                            for selector in selected_roles
                            if not plan_selector_declared(selector, mutable_roles)
                        )
                        if undeclared_roles:
                            note(
                                json_ptr(
                                    "layer", "stages", unit_index, "evaluation", "claims"
                                ),
                                f"unit {unit.id} contract {binding.id} selects roles "
                                "outside mutation authority: "
                                + ", ".join(undeclared_roles)
                                + ". "
                                + ROLE_SELECTOR_CLOSURE_RULE,
                            )
                    if kind != "visible_fraction":
                        continue
                    unrepaired = vis_roles_unrepairable_by(
                        provides=owner.provides,
                        mutation_roles=[*owner.mutates.roles, *owner.mutates.dresses],
                        vis_roles=vis_row.get("roles") or [],
                    )
                    if unrepaired:
                        note(
                            json_ptr(
                                "layer", "stages", unit_index, "evaluation", "claims"
                            ),
                            f"unit {unit.id} claim {claim.id} binds visible_fraction "
                            f"{binding.id} on {list(unrepaired)}; repair_owner "
                            f"{owner.id} neither provides camera nor mutates/dresses "
                            f"those roles. {VIS_REPAIR_OWNER_RULE}",
                        )
                note_required_claim_metric_domains(
                    note=note,
                    unit=unit,
                    unit_index=unit_index,
                    claim=claim,
                    all_contracts=all_contracts,
                )

    return complete_validated_layer(
        note=note,
        findings=findings,
        payload=payload,
        scene_rows=scene_rows,
        image_rows=image_rows,
        all_contracts=all_contracts,
        image_debt_ids=image_debt_ids,
        requirement_contract_ids=requirement_contract_ids,
        reserved=reserved,
        jit=jit,
        layer_id=layer_id,
        layer=layer,
        layer_row=layer_row,
        root=root,
        resolutions_path=resolutions_path,
        expected_bundle_hash=expected_bundle_hash,
        base_requirements_path=base_requirements_path,
    )
