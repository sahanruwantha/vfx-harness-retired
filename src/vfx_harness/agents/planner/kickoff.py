"""Stage 2 — the PLAN harness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.agents.plan_guardrails import target_validation_feedback
from vfx_harness.domain.judgment_debt_models import JUDGMENT_DEBT_PROPERTIES
from vfx_harness.domain.plan_records import load_active_structured_decisions, roles_match_reserved
from vfx_harness.domain.work_units import (
    CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
    DEFERRED_SUBJECT_ACTIVATION_RULE,
    allowed_unit_provides,
    compile_deferred_subject_activation,
    compile_frame_authority,
)
from vfx_harness.orchestration import layer_publication
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.jit_materialization.candidate import (
    load_materialization_candidate,
)
from vfx_harness.orchestration.jit_materialization.overlay_base import read_overlay_base
from vfx_harness.orchestration.jit_materialization.schema import (
    materialization_base_selection,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.plan_authoring import expand_mapping, validate_mapping


@dataclass(frozen=True, slots=True)
class _MaterializationKickoffAuthority:
    selected: ResolvedSelectedAuthority
    bundle_root: Path
    bundle_hash: str
    selected_layers: Path


def _require_same_selection(expected, observed, *, boundary: str) -> None:
    try:
        require_matching_authority_selection_token(expected, observed)
    except AuthoritySelectionConflict as exc:
        raise ValueError(f"{boundary}: {exc}") from exc


def _materialization_kickoff_authority(
    shot_folder: Path,
    bundle,
    rel_target: str,
    *,
    overlay_root: str | Path | None,
    selected_authority: ResolvedSelectedAuthority | None,
) -> _MaterializationKickoffAuthority:
    """Bind one kickoff card to the exact candidate, plan, and effective view."""

    shot = Path(shot_folder).resolve()
    try:
        selected = resolve_selected_authority(shot) if selected_authority is None else selected_authority
    except SelectedAuthorityResolutionError as exc:
        raise ValueError(str(exc)) from exc

    # Pointerless test/legacy authoring has no selected generation to mix. Production
    # materialization always enters the strict branch below after candidate seeding.
    if selected.plan is None:
        bundle_root = Path(bundle.root).resolve()
        selected_layers = (
            Path(overlay_root).resolve() / "layers.json" if overlay_root is not None else bundle_root / "layers.json"
        )
        return _MaterializationKickoffAuthority(
            selected=selected,
            bundle_root=bundle_root,
            bundle_hash=str(bundle.content_hash),
            selected_layers=selected_layers,
        )

    selected_bundle = selected.plan.bundle
    if (
        Path(bundle.root).resolve() != selected_bundle.root.resolve()
        or str(bundle.content_hash) != selected_bundle.content_hash
    ):
        raise ValueError("materialization kickoff bundle is not the selected global plan")
    try:
        selected_bundle.root.relative_to(shot)
    except ValueError as exc:
        raise ValueError("materialization kickoff snapshot belongs to another shot") from exc

    candidate = (shot / rel_target).resolve()
    try:
        candidate.relative_to(shot)
    except ValueError as exc:
        raise ValueError("materialization kickoff target escapes the shot root") from exc
    payload = load_materialization_candidate(
        candidate,
        expected_bundle_hash=selected_bundle.content_hash,
    )
    candidate_base = materialization_base_selection(payload)
    _require_same_selection(
        candidate_base,
        selected.selection_token,
        boundary="materialization kickoff candidate base selection is stale",
    )

    if overlay_root is None:
        try:
            selected_layers = selected.artifact_paths["layers.json"]
        except KeyError as exc:
            raise ValueError("selected materialization authority omits 'layers.json'") from exc
    else:
        overlay = Path(overlay_root).resolve()
        overlay_bundle, overlay_base = read_overlay_base(overlay)
        if overlay_bundle != selected_bundle.content_hash:
            raise ValueError("materialization kickoff overlay belongs to another global bundle")
        _require_same_selection(
            overlay_base,
            selected.selection_token,
            boundary="materialization kickoff overlay base selection is stale",
        )
        selected_layers = overlay / "layers.json"

    return _MaterializationKickoffAuthority(
        selected=selected,
        bundle_root=selected_bundle.root,
        bundle_hash=selected_bundle.content_hash,
        selected_layers=selected_layers,
    )


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

    reserved = tuple(str(pattern) for pattern in (getattr(getattr(layer, "jit", None), "reserved_roles", None) or []))
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
    *,
    selected_layers_path: Path,
    selected_authority: ResolvedSelectedAuthority,
) -> str:
    """Compile only dependency status and explicitly required evidence (HIR-0054)."""
    jit_row = global_row.get("jit") if isinstance(global_row.get("jit"), dict) else {}
    depends: list[str] = []
    required: list[dict[str, str]] = []
    layer_jit = getattr(layer, "jit", None)
    if layer_jit is not None:
        depends = [str(item) for item in (getattr(layer_jit, "depends_on_layers", None) or [])]
        required = [
            {"kind": str(kind), "id": str(oid)} for kind, oid in (getattr(layer_jit, "required_outcomes", None) or [])
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
    available_layers = load_layers_from_path(selected_layers_path)
    required_bindings = frozenset((row["kind"], row["id"]) for row in required)
    outcomes: list[dict] = []
    for dep in depends:
        dependency_layer = available_layers.get(dep)
        if dependency_layer is None:
            raise ValueError(f"dependency {dep} is absent from the selected executable consumer view")
        try:
            publication = layer_publication.require_current_layer_publication(
                shot_folder,
                dependency_layer,
                selected_authority,
            )
        except layer_publication.LayerPublicationConflict as exc:
            raise ValueError(
                f"dependency {dep} has no current receipt-backed publication: {exc}"
            ) from exc
        sealed = publication.outcome
        outcomes.append(
            {
                "layer": dep,
                "status": sealed.status,
                "script": sealed.script,
                "finalization_receipt_digest": sealed.receipt_digest,
                "required_evidence": list(sealed.required_evidence(required_bindings)),
            }
        )
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


def _judgment_property_authority_block(global_row: dict) -> str:
    """Compile the closed judgment-property vocabulary and this layer's legal choice.

    Every layer-1 materialization of room_1046_opening first bound its camera-layer
    judgment debt as ``subject_appearance`` or ``reference_identity`` and paid one or
    two turns for the rejection that names the rule; a camera-providing layer can own
    only ``camera_framing`` judgment, and the rule is derivable from the sparse row.
    """

    raw_provides = (global_row.get("jit") or {}).get("provides") or {}
    camera_owner = isinstance(raw_provides, dict) and "camera" in raw_provides
    properties = sorted(JUDGMENT_DEBT_PROPERTIES)
    if camera_owner:
        rule = (
            "This layer provides camera, so every requirement_bindings decision.judgment."
            "property must be 'camera_framing' with a camera-providing fault_owner unit; "
            "'subject_appearance' and 'reference_identity' belong to the form or look "
            "layer that owns the subject and are refused here."
        )
    else:
        rule = (
            "This layer does not provide camera: 'camera_framing' belongs to the camera "
            "owner; choose 'subject_appearance' or 'reference_identity' whose subject "
            "selectors the fault_owner unit mutates or dresses."
        )
    return (
        f"Judgment property authority (closed vocabulary {properties}): {rule}\n"
        "Claim authority at staging: executable_required or advisory. "
        "qualified_qualitative_required is minted by the harness for approved_start / "
        "planner_start judgment debt, never authored on a unit; appearance judged at "
        "build time is executable_required with asserts image.\n"
    )


def _owned_requirements_block(bundle_root: Path, global_row: dict) -> str:
    owned = {str(value) for value in ((global_row.get("jit") or {}).get("owned_requirements") or [])}
    if not owned:
        return "Compiled requirements owned by this layer: none.\n"
    document = json.loads((bundle_root / "requirements.json").read_text(encoding="utf-8"))
    rows = [
        row for row in document.get("requirements") or [] if isinstance(row, dict) and str(row.get("id") or "") in owned
    ]
    return (
        "Compiled requirements owned by this layer (complete; do not read the global "
        f"requirements register):\n{json.dumps(rows, indent=1)}\n"
    )


def _upstream_interfaces_block(
    global_row: dict,
    *,
    selected_layers_path: Path,
) -> str:
    """Compile dependency grants without exposing all selected layer claims."""

    dependencies = {str(value) for value in ((global_row.get("jit") or {}).get("depends_on_layers") or [])}
    if not dependencies:
        return "Compiled upstream semantic interfaces: none (dependency root).\n"
    document = json.loads(selected_layers_path.read_text(encoding="utf-8"))
    interfaces: list[dict] = []
    for row in document.get("layers") or []:
        if not isinstance(row, dict) or str(row.get("id") or "") not in dependencies:
            continue
        interfaces.append(
            {
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
            }
        )
    return (
        "Compiled upstream semantic interfaces and owner-granted dressable selectors "
        f"(complete; do not read the layer catalog):\n{json.dumps(interfaces, indent=1)}\n"
    )


def _deferred_subject_activation_block(global_layers: list, owner_layer_id: str) -> str:

    card = compile_deferred_subject_activation(global_layers, owner_layer_id)
    obligations = card.get("framing_obligations") or []
    obligation_text = (
        "Framing obligations this camera layer must author now (one persistent bbox_* row per "
        "line, target measured from the named still, bound through composition_context): "
        + "; ".join(
            f"f{row['frame']} layer {row['layer_id']} {', '.join(row['reserved_roles'])} ← {row['ref']}"
            for row in obligations
        )
        + ". The camera unit proves them jointly feasible before it freezes; a row it cannot "
        "author is a vocabulary-gap escalation, not an omission.\n"
        if obligations
        else ""
    )
    return (
        "Deferred subject-composition activation compiled from the selected DAG "
        "(do not ask_supervisor for layer occupancy):\n"
        f"{json.dumps(card, indent=1)}\n"
        f"{DEFERRED_SUBJECT_ACTIVATION_RULE}.\n"
        + obligation_text
    )


def _materialization_kickoff(
    shot_folder: Path,
    layer,
    bundle,
    rel_target: str,
    replacing: str | None = None,
    *,
    overlay_root: str | Path | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> str:
    """The session must copy its global layer row exactly and close owned requirements,
    so the kickoff carries the row verbatim and the READABLE paths that hold the rest.
    Run 20260823T125746Z-9cd0b8 got only the bundle hash: it probed six plausible bundle
    locations, was denied by the path scope, reconstructed the row from prose, and
    failed structural validation on every field."""
    authority = _materialization_kickoff_authority(
        shot_folder,
        bundle,
        rel_target,
        overlay_root=overlay_root,
        selected_authority=selected_authority,
    )
    rows = json.loads((authority.bundle_root / "layers.json").read_text(encoding="utf-8"))
    global_row = next(row for row in rows.get("layers", []) if str(row.get("id")) == str(layer.id))
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
    upstream_interfaces = _upstream_interfaces_block(
        global_row,
        selected_layers_path=authority.selected_layers,
    )
    sealed_outcomes = _sealed_outcomes_block(
        shot_folder,
        layer,
        global_row,
        selected_layers_path=authority.selected_layers,
        selected_authority=authority.selected,
    )
    return (
        f"{replacement}"
        f"Materialize deferred layer {layer.id} ({layer.title}).\n"
        f"Selected bundle hash: {authority.bundle_hash}\n"
        f"This kickoff is the complete compiled authority card. Do not read the brief, "
        f"global registers, layer catalogs, decision ledger, or prior materializations.\n"
        f"Your exact global layer row — copy the structural fields verbatim into the "
        f"replacement layer:\n{json.dumps(global_row, indent=1)}\n"
        f"{_owned_requirements_block(authority.bundle_root, global_row)}"
        f"{upstream_interfaces}"
        f"{_frame_authority_block(global_row)}"
        f"{_unit_capability_authority_block(global_row)}"
        f"{_judgment_property_authority_block(global_row)}"
        f"{_deferred_subject_activation_block(rows.get('layers') or [], str(layer.id))}"
        f"{_CONSTRUCTION_ROUTE_BLOCK}"
        f"{_TWO_SIDED_CONTRACT_BINDING}"
        f"{_binding_decisions_block(shot_folder, layer, authority.bundle_hash)}"
        f"{sealed_outcomes}"
        f"{_PUBLISH_CONSUME_EXAMPLE}"
        f"Document shape (generic minimal-valid example — replace every placeholder, "
        f"add stages/contracts/claims as the layer needs):\n{_MATERIALIZATION_EXAMPLE}\n"
        f"Output: {rel_target}"
    )
