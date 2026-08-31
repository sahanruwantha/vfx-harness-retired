"""Stage 2 — the PLAN harness."""

from __future__ import annotations

import json
from pathlib import Path

from vfx_harness.agents.plan_guardrails import target_validation_feedback
from vfx_harness.domain.layer_outcomes import (
    LayerOutcomeContractError,
    parse_sealed_layer_outcome,
)
from vfx_harness.domain.plan_records import load_active_structured_decisions, roles_match_reserved
from vfx_harness.domain.work_units import (
    CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
    DEFERRED_SUBJECT_ACTIVATION_RULE,
    allowed_unit_provides,
    compile_deferred_subject_activation,
    compile_frame_authority,
)
from vfx_harness.orchestration.jit_materialization import selected_view_artifact
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.plan_authoring import expand_mapping, validate_mapping
from vfx_harness.orchestration.plan_authority import resolve_current
from vfx_harness.orchestration.revalidation import current_outcome_eligibility


def mapping_expander(workspace: Path, registry, mapping_path: Path):
    """The warm authoring loop: each mapping write is validated with enumerated errors
    and, when valid, expanded into the full authority surface immediately — so the
    session's `run_gate` always measures fresh artifacts and `plans/global.md` exists
    exactly when the mapping is publishable."""

    def _expand_or_errors() -> list[str]:

        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"ownership_mapping.json is not readable JSON: {exc}"]
        errors = validate_mapping(mapping, registry, workspace / "refs")
        if errors:
            return errors
        try:
            expand_mapping(workspace, mapping)
        except (ValueError, OSError) as exc:
            return [str(exc)]
        return []

    return _expand_or_errors


def _with_target_feedback(hooks: dict, target: Path, validate) -> dict:
    """Append warm write-time validation of one target file to a planner hook set."""

    hooks = dict(hooks)
    hooks["PostToolUse"] = [
        *hooks.get("PostToolUse", []),
        target_validation_feedback(target, validate),
    ]
    return hooks


# The unit-ticket tool exposes the closed WorkUnit schema. This remains only the outer
# materialization document shape — every value is a placeholder, no shot vocabulary.
_MATERIALIZATION_EXAMPLE = """{
 "schema": "<materialization schema id from your instructions>",
 "bundle_hash": "<selected bundle hash>",
 "layer": {
  "<structural fields copied verbatim from your global row>": "...",
  "execution": "ready",
  "stages": [{
   "id": "example_unit", "title": "Example unit", "plan": "plans/units/example_unit.md",
   "depends_on": [],
   "mutates": {"mode": "scoped", "roles": ["example_role.part"], "controls": ["example_control"],
               "control_roles": {"example_control": ["example_role.part"]},
               "script_spans": ["build/units/01/example_unit.py"]},
   "protects": {"selector": "all_active_upstream_interfaces",
                "resolve_to_explicit_ids_at": "freeze"},
   "look_capabilities": ["<the appearance families THIS unit answers for>"],
   "provides": ["<capabilities this unit gives the scene: camera, geometry>"],
   "evaluation": {"primary_judge": 1, "judge": [{"frame": 1, "ref": "refs/<a judge ref>.png"}],
                  "temporal_evidence": "none",
                  "claims": [{
                   "id": "example-claim", "proposition": "one testable sentence",
                   "axis": "<an axis this layer owns>", "property": "<contract kind>",
                   "subject_roles": ["example_role.part"], "subject_controls": [],
                   "moments": [1], "kind": "atomic", "required": true,
                   "authority": "executable_required", "repair_owner": "example_unit",
                   "asserts": "<scene|temporal|projected_composition|image|human>",
                   "evidence": [{"kind": "scene_contract", "id": "example-contract"}]}]},
   "completion": "all_required_claims_and_protected_contracts_pass"}]
 },
 "scene_contracts": [{
  "id": "example-contract", "kind": "<contract kind>", "owner_layer": "<this layer id>",
  "fault_owner": "<this layer id>", "activates_at": "<this layer id>", "lifecycle": "layer",
  "axis": "<an owned axis>", "op": "max", "hi": 0.01},
  {"id": "example-subject-bbox-later", "kind": "bbox_height",
   "owner_layer": "<this camera layer id>", "fault_owner": "<this camera layer id>",
   "activates_at": "<compiled earliest_geometry_layer>", "lifecycle": "persistent",
   "axis": "<this camera layer axis>", "roles": ["<subject role that layer will create>"],
   "frame": 1, "op": "band", "lo": 0.35, "hi": 0.55}],
 "image_contracts": [],
 "requirement_bindings": [
 {"requirement_id": "<owned id>", "contract_ids": ["example-contract"],
    "decision": {"statement": "one-sentence provisional qualitative debt",
    "decision_strength": "approved_start",
    "judgment": {"claim_kind": "atomic", "property": "<closed judgment property>",
     "fault_owner": "<exact work-unit id>", "lifecycle": "persistent",
     "subject_roles": ["<rendered subject selector>"],
     "axes": ["<one owned axis>"], "moments": [1],
     "carrier_families": ["mesh"],
     "observation_medium": "workbench_solid"}}}],
 "acceptance": []
}"""

_PUBLISH_CONSUME_EXAMPLE = """Typed predecessor handoff (exact field names):
producer `publishes`: [{"id":"target.publish","kind":"placement_control",
"schema":"vfx-harness.publish-interface/v1",
"exports":{"role":"target.role","control":"target.control"}}]
successor `depends_on`: ["target"]
successor `consumes`: [{"producer":"target","interface_id":"target.publish",
"kind":"placement_control"}]
The successor observes exported selectors read-only; it does not add them to `mutates`.
Omit `composition_context` when required claims directly bind the evidence. If present,
it needs non-empty `frames` and exactly one of `source_unit` or non-empty `contract_ids`.
"""


def _binding_decisions_block(shot_folder: Path, layer, bundle_hash: str) -> str:
    """Compile the structured decisions this layer must copy — not the whole ledger.

    Remat5 copied a prior generation's A2 spine because the prompt said to scan
    ``state/plan-resolutions.jsonl``. The selected bundle is the key; other
    generations are inert (HIR-0028).
    """

    reserved = tuple(
        str(pattern)
        for pattern in (getattr(getattr(layer, "jit", None), "reserved_roles", None) or [])
    )
    active = load_active_structured_decisions(
        shot_folder / "state" / "plan-resolutions.jsonl",
        bundle_hash=bundle_hash,
    )
    binding = [
        {
            "id": decision.id,
            "decision": decision.decision,
            "values": {"contract": decision.contract},
        }
        for decision in active.values()
        if roles_match_reserved(
            [str(role) for role in (decision.contract.get("roles") or [])],
            reserved,
        )
    ]
    return (
        "Binding structured decisions for this layer on the selected bundle "
        "(copy each verbatim into scene_contracts with decision_id; ledger rows "
        "keyed to another generation, or retired by a later superseded/falsified "
        f"row, are inert):\n{json.dumps(binding, indent=1)}\n"
    )


_TWO_SIDED_CONTRACT_BINDING = (
    "Two-sided scene contracts (path_clearance_min, parallax_displacement_profile, "
    "onset_order): bind the contract on the unit that mutates the primary `roles`; "
    "`compare_roles` may name other plan-declared namespaces. visible_fraction is "
    "repaired by a unit that provides camera or mutates/dresses every named role "
    "on that row — not by any unit that can see a plan-declared selector. "
    "A volume-only unit cannot bind mesh vis as required repair.\n"
    "Each unit is one derived write-cluster (role-namespace × host class × instrument "
    "family). Do not author family/mutation_family/coherent_family fields. Dressing, "
    "visibility observation, bounded coordination, and consumed assembly interfaces "
    "are typed exceptions. Required claims share one repair_owner. Publish interfaces "
    "export only roles, controls, or sealed contract ids.\n"
    "Subject composition: a projected_composition owner covers each judge frame with "
    "bbox_* of a rendered subject, never projected_origin of a camera-only host "
    "(alignment, not framing). Vacuous normalized bands wider than half the frame are "
    "rejected. When the subject does not exist yet, author the bbox on this camera "
    "layer with activates_at equal to the compiled earliest_geometry_layer, lifecycle persistent, "
    "fault_owner this camera layer; bind the ids through composition_context. Do not "
    "seal those rows on the camera unit. Do not ask_supervisor which selected layer "
    "is earliest geometry occupancy.\n"
)

_CONSTRUCTION_ROUTE_BLOCK = (
    "Construction route: omit construction for procedural mesh. A unique source mesh "
    "may call mint_refobs on a refs/ crop (not a whole frame) then stage "
    "construction.route generate with those exact refobs-* ids. generate requires a "
    "mesh write family and cannot bind a required object_count whose minimum exceeds 1. "
    "retrieve is not wired. omit and abstain are not unit routes. Do not invent an "
    "assets department or builder-time import_asset.\n"
)


def _frame_authority_block(global_row: dict) -> str:
    """Compile this layer's judge subset rule — remat6 invented extra judge frames."""

    return (
        "Frame authority compiled from this layer's global row (do not invent "
        "frames; do not Read a prior materialization to guess them):\n"
        f"{json.dumps(compile_frame_authority(global_row), indent=1)}\n"
    )


def _unit_capability_authority_block(global_row: dict) -> str:
    """Compile the layer-local capability vocabulary from sparse DAG authority."""

    allowed = sorted(allowed_unit_provides(global_row))
    return (
        "Unit capability authority compiled from this sparse global layer row "
        f"(the stage_materialization_unit schema enforces it): {allowed}. "
        f"{CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE}.\n"
    )


def _sealed_outcomes_block(
    shot_folder: Path,
    layer,
    global_row: dict,
    bundle_hash: str,
    *,
    overlay_root: str | Path | None = None,
) -> str:
    """Compile only dependency status and explicitly required evidence (HIR-0054)."""
    jit_row = global_row.get("jit") if isinstance(global_row.get("jit"), dict) else {}
    depends: list[str] = []
    required: list[dict[str, str]] = []
    layer_jit = getattr(layer, "jit", None)
    if layer_jit is not None:
        depends = [str(item) for item in (getattr(layer_jit, "depends_on_layers", None) or [])]
        required = [
            {"kind": str(kind), "id": str(oid)}
            for kind, oid in (getattr(layer_jit, "required_outcomes", None) or [])
        ]
    if not depends:
        depends = [str(item) for item in (jit_row.get("depends_on_layers") or [])]
    if not required:
        for item in jit_row.get("required_outcomes") or []:
            if isinstance(item, dict) and item.get("kind") and item.get("id"):
                required.append({"kind": str(item["kind"]), "id": str(item["id"])})
    if not depends and not required:
        return (
            "Sealed upstream outcomes: none. This layer is a dependency root "
            "(empty depends_on_layers and required_outcomes).\n"
        )
    selected_layers_path = selected_view_artifact(
        shot_folder,
        "layers.json",
        bundle_hash,
        overlay_root=overlay_root,
    )
    if selected_layers_path is None:
        selected_layers_path = resolve_current(shot_folder).root / "layers.json"
    available_layers = load_layers_from_path(selected_layers_path)
    required_bindings = frozenset((row["kind"], row["id"]) for row in required)
    outcomes: list[dict] = []
    for dep in depends:
        path = layer_outcome_path(shot_folder, dep)
        if not path.is_file():
            outcomes.append({"layer": dep, "status": "missing"})
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        try:
            sealed = parse_sealed_layer_outcome(value, expected_layer_id=dep)
        except LayerOutcomeContractError as exc:
            raise ValueError(
                f"dependency {dep} has an invalid sealed outcome: {exc}"
            ) from exc
        dependency_layer = available_layers.get(dep)
        if dependency_layer is None:
            raise ValueError(
                f"dependency {dep} is absent from the selected executable consumer view"
            )
        eligible, reasons = current_outcome_eligibility(
            shot_folder,
            dependency_layer,
            value,
        )
        if not eligible:
            raise ValueError(
                f"dependency {dep} sealed outcome is stale: " + "; ".join(reasons)
            )
        outcomes.append({
            "layer": dep,
            "status": sealed.status,
            "script": sealed.script,
            "required_evidence": list(sealed.required_evidence(required_bindings)),
        })
    card = {
        "depends_on_layers": depends,
        "required_outcomes": required,
        "outcomes": outcomes,
    }
    return (
        "Compiled sealed upstream outcome card (this is the complete dependency input; "
        "do not read outcome files):\n"
        f"{json.dumps(card, indent=1)}\n"
    )


def _owned_requirements_block(bundle_root: Path, global_row: dict) -> str:
    owned = {
        str(value)
        for value in ((global_row.get("jit") or {}).get("owned_requirements") or [])
    }
    if not owned:
        return "Compiled requirements owned by this layer: none.\n"
    document = json.loads((bundle_root / "requirements.json").read_text(encoding="utf-8"))
    rows = [
        row
        for row in document.get("requirements") or []
        if isinstance(row, dict) and str(row.get("id") or "") in owned
    ]
    return (
        "Compiled requirements owned by this layer (complete; do not read the global "
        f"requirements register):\n{json.dumps(rows, indent=1)}\n"
    )


def _upstream_interfaces_block(
    shot_folder: Path,
    global_row: dict,
    bundle_hash: str,
    *,
    overlay_root: str | Path | None = None,
) -> str:
    """Compile dependency grants without exposing all selected layer claims."""

    dependencies = {
        str(value)
        for value in ((global_row.get("jit") or {}).get("depends_on_layers") or [])
    }
    if not dependencies:
        return "Compiled upstream semantic interfaces: none (dependency root).\n"
    path = selected_view_artifact(
        shot_folder, "layers.json", bundle_hash, overlay_root=overlay_root
    )
    if path is None:

        path = resolve_current(shot_folder).root / "layers.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    interfaces: list[dict] = []
    for row in document.get("layers") or []:
        if not isinstance(row, dict) or str(row.get("id") or "") not in dependencies:
            continue
        interfaces.append({
            "id": str(row.get("id")),
            "title": row.get("title"),
            "dressable": list(row.get("dressable") or []),
            "units": [
                {
                    "id": unit.get("id"),
                    "provides": list(unit.get("provides") or []),
                    "roles": list((unit.get("mutates") or {}).get("roles") or []),
                    "dressable": list(unit.get("dressable") or []),
                }
                for unit in row.get("stages") or []
                if isinstance(unit, dict)
            ],
        })
    return (
        "Compiled upstream semantic interfaces and owner-granted dressable selectors "
        f"(complete; do not read the layer catalog):\n{json.dumps(interfaces, indent=1)}\n"
    )


def _deferred_subject_activation_block(global_layers: list, owner_layer_id: str) -> str:

    card = compile_deferred_subject_activation(global_layers, owner_layer_id)
    return (
        "Deferred subject-composition activation compiled from the selected DAG "
        "(do not ask_supervisor for layer occupancy):\n"
        f"{json.dumps(card, indent=1)}\n"
        f"{DEFERRED_SUBJECT_ACTIVATION_RULE}.\n"
    )


def _materialization_kickoff(
    shot_folder: Path,
    layer,
    bundle,
    rel_target: str,
    replacing: str | None = None,
    *,
    overlay_root: str | Path | None = None,
) -> str:
    """The session must copy its global layer row exactly and close owned requirements,
    so the kickoff carries the row verbatim and the READABLE paths that hold the rest.
    Run 20260823T125746Z-9cd0b8 got only the bundle hash: it probed six plausible bundle
    locations, was denied by the path scope, reconstructed the row from prose, and
    failed structural validation on every field."""
    rows = json.loads((bundle.root / "layers.json").read_text(encoding="utf-8"))
    global_row = next(
        row for row in rows.get("layers", []) if str(row.get("id")) == str(layer.id)
    )
    # A replacement designed in ignorance of why its predecessor was discarded repeats
    # the predecessor's mistakes: the first re-materialization of layer 1 put the camera
    # last and left the faceted housing unowned, both defects the operator was replacing.
    replacement = (
        f"REPLACING A DISCARDED MATERIALIZATION. The previous design of this layer was "
        f"rejected. Reason and requirements from the operator:\n{replacing}\n"
        f"Your design must satisfy those requirements explicitly; do not reproduce the "
        f"structure being replaced.\n\n"
        if replacing
        else ""
    )
    return (
        f"{replacement}"
        f"Materialize deferred layer {layer.id} ({layer.title}).\n"
        f"Selected bundle hash: {bundle.content_hash}\n"
        f"This kickoff is the complete compiled authority card. Do not read the brief, "
        f"global registers, layer catalogs, decision ledger, or prior materializations.\n"
        f"Your exact global layer row — copy the structural fields verbatim into the "
        f"replacement layer:\n{json.dumps(global_row, indent=1)}\n"
        f"{_owned_requirements_block(bundle.root, global_row)}"
        f"{_upstream_interfaces_block(shot_folder, global_row, bundle.content_hash, overlay_root=overlay_root)}"
        f"{_frame_authority_block(global_row)}"
        f"{_unit_capability_authority_block(global_row)}"
        f"{_deferred_subject_activation_block(rows.get('layers') or [], str(layer.id))}"
        f"{_CONSTRUCTION_ROUTE_BLOCK}"
        f"{_TWO_SIDED_CONTRACT_BINDING}"
        f"{_binding_decisions_block(shot_folder, layer, bundle.content_hash)}"
        f"{_sealed_outcomes_block(shot_folder, layer, global_row, bundle.content_hash, overlay_root=overlay_root)}"
        f"{_PUBLISH_CONSUME_EXAMPLE}"
        f"Document shape (generic minimal-valid example — replace every placeholder, "
        f"add stages/contracts/claims as the layer needs):\n{_MATERIALIZATION_EXAMPLE}\n"
        f"Output: {rel_target}"
    )
