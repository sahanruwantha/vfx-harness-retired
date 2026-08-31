"""The plan gate — the deterministic bar a plan must clear before anything is built on it.

The plan is the specification every later stage is judged against, and it was the only
artifact nothing checked. `ledger.load_layers` reads `layers.json` for shape; past that, a
plan could claim anything and the first thing to notice would be a layer thrashing against
a target that was never reachable.

The organising finding, measured across both shots and all four plan documents:

    spike citations      PRECISE and TRUE. `spike_05.out` line 9 is cited for "the default
                         is 100" and line 9 reads `volumetric_start/end: ... 100.0`.
    web research         0 URLs. Step 6 of the planner prompt requires "source links in
                         the ticket". Not one plan contains a link.
    ask_supervisor       never fired across two shots.
    [unknown] tags       0, in every document, and research is gated on that tag.

The difference is not diligence. `spike` WRITES A FILE to the lab directory; `WebSearch`
leaves nothing behind unless the model chooses to type a URL. Evidence a tool physically
deposits survives and stays checkable. Evidence the model is merely asked to record does
not. Every check here is therefore a check on an ARTIFACT, never on a claim about one.

The checks, each free and each with a known-bad fixture in the test suite:

    grounded      every stated fingerprint number re-derives from the plate it describes
                  (eval.grounding — 153/160 on the shipped plans; the 7 failures were one
                  broken metric, not an inventive planner)
    citations     every file a plan cites RESOLVES, and a cited line number exists in it.
                  Not fabrication — link rot: `server_to_hansa/plan.md` cites
                  `../barrel_roll/logs/plan_lab_draft/spike_05.out` five times and that
                  path stopped existing when the shot was archived. The claims are true and
                  a build agent reading them today gets nothing.
    evidence      a ticket claiming `✓spiked` must cite a lab file; a ticket claiming
                  research must leave a source link. A confidence tag is a self-assessment
                  and worth exactly the artifact under it.
    done-checks   do the plan's own checks WORK — is each satisfiable by the reference it
                  names, and does it REJECT a known-bad render? Grounding asks whether the
                  numbers are real; this asks whether the checks can fail anything. Both are
                  needed: this plan scored 95/95 on grounding while carrying a check its own
                  plate cannot pass. See checks.py — the contract, re-run by the gate.
    contracts     plans/global.md, the next just-in-time work-unit plan, layers.json,
                  acceptance.json, critic_axes.json and live-scene
                  contracts must agree: an axis a layer OWNS must exist in the rubric,
                  every ref must be real, and a scene fact must belong to a frame/axis its
                  layer actually judges. A layer owning an axis the critic does not score
                  cannot be judged on it, and nothing else in the pipeline notices.

Severity is either `blocking` (a build on this plan is aiming at something that is not
there) or `warn` (the plan is weaker than it claims, but buildable).

Exit codes: 0 clean · 3 at least one blocking finding
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from vfx_harness.domain.atomicity import ATOMICITY_RULE, atomicity_gaps
from vfx_harness.domain.construction import CONSTRUCTION_ROUTE_RULE
from vfx_harness.domain.construction_routes import construction_route_gaps
from vfx_harness.domain.contracts import load_document
from vfx_harness.domain.dressing import DRESSING_CLOSURE_FIX, SAME_LAYER_DRESS_RULE, same_layer_dress_gaps
from vfx_harness.domain.image_debts import (
    IMAGE_PROPERTY_VOCABULARY_RULE,
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
from vfx_harness.domain.work_units import (
    DEFERRED_CONTRACT_CONTEXT_RULE,
    GEOMETRY_VIS_CYCLE_RULE,
    GEOMETRY_VIS_DEPENDENCY_RULE,
    PROJECTED_ORIGIN_REPAIR_RULE,
    VIS_REPAIR_OWNER_RULE,
    WorkUnit,
    geometry_vis_dependency_cycles,
    geometry_vis_dependency_gaps,
    point_projection_interface_gaps,
    read_document,
    vis_roles_unrepairable_by,
)
from vfx_harness.evaluation.plan_gate.types import (
    Finding,
    _global_authority_layers,
    _global_executable_checks_apply,
)
from vfx_harness.evaluation.plan_gate.unit_deps import (
    _camera_only_host_roles,
    _deferred_subject_framing_covers,
    _is_subject_framing_row,
)
from vfx_harness.evidence.checks import METRICS
from vfx_harness.evidence.scene_checks import (
    CAMERA_REQUIRED_KINDS,
    PROJECTED_ORIGIN_KINDS,
    SUBJECT_COMPOSITION_RULE,
    SURFACE_PROJECTED_KINDS,
    TEMPORAL_KINDS,
    deferred_subject_composition_payment_gaps,
)


def _check_evidence_coherence(folder: Path) -> tuple[list[Finding], dict]:
    """Check temporal, composition, and mutation ownership coverage across contracts."""

    try:
        layers = read_document(folder / "layers.json")
    except (OSError, ValueError):
        return [], {}
    try:
        scene_rows = load_document(folder / "scene_checks.json", "contracts")
    except (OSError, ValueError, json.JSONDecodeError):
        scene_rows = []
    try:
        image_rows = load_document(folder / "checks.json", "checks")
    except (OSError, ValueError, json.JSONDecodeError):
        image_rows = []

    out: list[Finding] = []
    temporal_ids = {
        str(row.get("id"))
        for row in scene_rows
        if isinstance(row, dict) and row.get("kind") in TEMPORAL_KINDS | {"frame_delta"}
    }
    composition_tokens = ("camera", "composition", "framing", "staging")
    scene_by_id = {
        str(row.get("id")): row for row in scene_rows if isinstance(row, dict) and row.get("id")
    }
    image_by_id = {
        str(row.get("id")): row for row in image_rows if isinstance(row, dict) and row.get("id")
    }
    # every role namespace the PLAN declares anywhere: unit mutation authority plus
    # deferred reservations — the universe a two-sided contract's measurement side may
    # observe (its own repair authority still closes on the primary selectors)
    plan_declared_roles: set[str] = set()
    plan_declared_controls: set[str] = set()
    global_layers = _global_authority_layers(folder, layers)
    for layer_row in global_layers:
        if not isinstance(layer_row, dict):
            continue
        for pattern in (layer_row.get("jit") or {}).get("reserved_roles") or []:
            plan_declared_roles.add(str(pattern))
    for layer_row in layers:
        if not isinstance(layer_row, dict):
            continue
        for stage_row in layer_row.get("stages") or []:
            if isinstance(stage_row, dict):
                for pattern in (stage_row.get("mutates") or {}).get("roles") or []:
                    plan_declared_roles.add(str(pattern))
                for pattern in (stage_row.get("mutates") or {}).get("controls") or []:
                    plan_declared_controls.add(str(pattern))

    def _selector_declared(selector: str, declarations: set[str]) -> bool:
        return any(
            selector == declared
            or selector.startswith(f"{declared}.")
            or fnmatch.fnmatchcase(selector, declared)
            or fnmatch.fnmatchcase(declared, selector)
            for declared in declarations
        )

    motion_units = 0
    earlier_camera_available = False
    earlier_image_signal_available = False
    earlier_image_subject_available = False
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        lid = str(layer.get("id") or "")
        owns = [str(axis).lower() for axis in layer.get("owns") or []]
        judges = [
            int(row["frame"])
            for row in layer.get("judge") or []
            if isinstance(row, dict) and isinstance(row.get("frame"), int)
        ]
        domains = layer.get("evidence_domains")
        if not _global_executable_checks_apply(layer):
            # Deferred layers reserve authority but deliberately have no executable units,
            # claims, composition contexts, or controls until their JIT materialization gate.
            # Their structural shape is validated by the typed layer loader and meta gate.
            continue
        owns_composition = (
            "projected_composition" in domains
            if isinstance(domains, list)
            else any(token in axis for axis in owns for token in composition_tokens)
        )
        stages = {
            str(unit.get("id")): unit
            for unit in layer.get("stages") or []
            if isinstance(unit, dict) and unit.get("id")
        }
        # HIR-0132: vis activates at its typed repair owner; every later geometry
        # provider must carry that owner in its dependency closure.
        try:

            typed_stages = tuple(
                WorkUnit.parse(unit, f"layer {lid}.stages[{index}]")
                for index, unit in enumerate(layer.get("stages") or [])
            )
        except (TypeError, ValueError):
            # Typed layer validation owns malformed units. Avoid duplicating its
            # partial-shape findings here.
            typed_stages = ()
        for gap in deferred_subject_composition_payment_gaps(
            scene_rows, typed_stages, lid
        ):
            out.append(
                Finding(
                    "deferred-composition-payer",
                    True,
                    f"layer {lid} deferred contract {gap.contract_id}",
                    f"roles {', '.join(gap.roles)} have overlapping geometry producers "
                    f"{', '.join(gap.producer_ids) or '(none)'} but no unit dependency "
                    "closure contains the complete subject",
                    "order the truthful geometry write clusters so the first complete "
                    "cumulative subject pays the camera-owned bbox; do not evaluate a "
                    "future subject at layer-start preflight",
                )
            )
        vis_gaps = geometry_vis_dependency_gaps(typed_stages, scene_rows, lid)
        vis_cycles = geometry_vis_dependency_cycles(typed_stages, scene_rows, lid)
        cyclic_edges = {edge for cycle in vis_cycles for edge in cycle.edges}
        for cycle in vis_cycles:
            edge_text = ", ".join(f"{source}->{target}" for source, target in cycle.edges)
            out.append(
                Finding(
                    "geometry-vis-cycle",
                    True,
                    f"layer {lid} units {', '.join(cycle.unit_ids)}",
                    f"mutually protect contracts {', '.join(cycle.contract_ids)} on "
                    f"roles {', '.join(cycle.roles)} through producer edges {edge_text}",
                    GEOMETRY_VIS_CYCLE_RULE,
                )
            )
        for gap in vis_gaps:
            if any((gap.unit_id, producer) in cyclic_edges for producer in gap.producer_ids):
                continue
            out.append(
                Finding(
                    "geometry-vis-dependency",
                    True,
                    f"layer {lid} unit {gap.unit_id}",
                    f"provides geometry and therefore protects visible_fraction "
                    f"{gap.contract_id}, but typed repair owner(s) / role producer(s) "
                    f"{', '.join(gap.producer_ids)} are outside its dependency closure "
                    f"for role {gap.role!r}",
                    GEOMETRY_VIS_DEPENDENCY_RULE,
                )
            )
        for gap in point_projection_interface_gaps(typed_stages, scene_rows):
            if gap.reason == "owner_mutation":
                what = (
                    f"camera owner also mutates observed selector {gap.selector!r} "
                    f"for point-projection contract {gap.contract_id}"
                )
            else:
                what = (
                    f"point-projection contract {gap.contract_id} observes selector "
                    f"{gap.selector!r} produced by {', '.join(gap.producer_ids)}, but "
                    "the camera owner consumes no compatible typed interface"
                )
            out.append(
                Finding(
                    "point-projection-interface",
                    True,
                    f"layer {lid} unit {gap.unit_id}",
                    what,
                    PROJECTED_ORIGIN_REPAIR_RULE,
                )
            )

        raw_stages = tuple(
            unit
            for unit in layer.get("stages") or []
            if isinstance(unit, dict)
        )
        for gap in atomicity_gaps(
            typed_stages, scene_rows, layer_id=lid, raw_stages=raw_stages
        ):
            extra = ""
            if gap.considered_exceptions:
                extra = (
                    " Exceptions considered and insufficient: "
                    + ", ".join(gap.considered_exceptions)
                    + "."
                )
            out.append(
                Finding(
                    "unit-atomicity",
                    True,
                    f"layer {lid} unit {gap.unit_id}",
                    gap.detail + extra,
                    "split the unit, consume a typed assembly interface, bind dressing, "
                    "or reassign evidence. " + ATOMICITY_RULE,
                )
            )
        for gap in construction_route_gaps(typed_stages, scene_rows):
            out.append(
                Finding(
                    "construction-route",
                    True,
                    f"layer {lid} unit {gap.unit_id}",
                    gap.detail,
                    CONSTRUCTION_ROUTE_RULE,
                )
            )

        signal_provider_ids = image_signal_provider_ids(typed_stages, scene_rows)
        for gap in image_signal_dependency_gaps(
            typed_stages,
            scene_rows,
            earlier_signal_available=earlier_image_signal_available,
        ):
            available = (
                " Same-layer signal provider(s) exist but are outside the dependency "
                f"closure: {', '.join(gap.available_provider_ids)}."
                if gap.available_provider_ids
                else " No same-layer unit currently derives a signal family."
            )
            out.append(
                Finding(
                    "image-signal-bootstrap",
                    True,
                    f"layer {lid} unit {gap.unit_id}",
                    "required image-contract debt is due before optical signal is "
                    f"available: {', '.join(gap.contract_ids)}."
                    + available,
                    "Registered write-kind witnesses: "
                    + image_signal_witness_guidance()
                    + ". "
                    + IMAGE_SIGNAL_DEPENDENCY_RULE,
                )
            )
        if signal_provider_ids:
            earlier_image_signal_available = True
        subject_provider_ids = image_subject_provider_ids(typed_stages, scene_rows)
        for gap in image_subject_dependency_gaps(
            typed_stages,
            scene_rows,
            earlier_subject_available=earlier_image_subject_available,
        ):
            available = (
                " Same-layer rendered-carrier unit(s) exist but are outside the "
                f"dependency closure: {', '.join(gap.available_provider_ids)}."
                if gap.available_provider_ids
                else " No same-layer unit currently derives a mesh, volume, or compositor family."
            )
            out.append(
                Finding(
                    "image-subject-bootstrap",
                    True,
                    f"layer {lid} unit {gap.unit_id}",
                    "required image-contract debt is due before a rendered carrier is "
                    f"available: {', '.join(gap.contract_ids)}."
                    + available,
                    IMAGE_SUBJECT_DEPENDENCY_RULE,
                )
            )
        if subject_provider_ids:
            earlier_image_subject_available = True

        payable_properties = sorted(payable_image_property_kinds(METRICS))
        for gap in image_property_vocabulary_gaps(typed_stages, METRICS):
            out.append(
                Finding(
                    "image-property-vocabulary",
                    True,
                    f"layer {lid} unit {gap.unit_id} claim {gap.claim_id}",
                    f"required image-contract debt {', '.join(gap.contract_ids)} uses "
                    f"unpayable property {gap.property!r}",
                    f"accepted image properties: {payable_properties}. "
                    + IMAGE_PROPERTY_VOCABULARY_RULE,
                )
            )
        # Camera availability is typed authority. Role names such as camera.target are
        # semantic selectors, not capabilities, and cannot bootstrap projected evidence.
        camera_units = {
            uid
            for uid, unit in stages.items()
            if "camera" in (unit.get("provides") or [])
        }

        def _camera_available_to(
            uid: str,
            earlier: bool = earlier_camera_available,
            available: frozenset[str] = frozenset(camera_units),
            layer_stages: dict[str, dict] = stages,
        ) -> bool:
            if earlier:
                return True
            seen: set[str] = set()
            frontier = [uid]
            while frontier:
                current = frontier.pop()
                if current in seen:
                    continue
                seen.add(current)
                if current in available:
                    return True
                frontier.extend(
                    str(dep) for dep in layer_stages.get(current, {}).get("depends_on") or []
                )
            return False

        camera_only_roles = _camera_only_host_roles(stages)
        if owns_composition:
            for frame in judges:
                covered = False
                for unit in stages.values():
                    evaluation = unit.get("evaluation") or {}
                    # A required claim bound straight to a subject bbox at this judge
                    # frame IS executable projected context. projected_origin of a
                    # camera-only host is alignment, not framing (HIR-0127).
                    if any(
                        isinstance(claim, dict)
                        and claim.get("required")
                        and frame in (claim.get("moments") or [])
                        and any(
                            isinstance(binding, dict)
                            and binding.get("kind") == "scene_contract"
                            and scene_by_id.get(str(binding.get("id")), {}).get("frame")
                            == frame
                            and _is_subject_framing_row(
                                scene_by_id.get(str(binding.get("id")), {}),
                                camera_only_roles,
                            )
                            for binding in claim.get("evidence") or []
                        )
                        for claim in evaluation.get("claims") or []
                    ):
                        covered = True
                        break
                    context = evaluation.get("composition_context") or {}
                    if frame not in (context.get("frames") or []):
                        continue
                    contract_ids = {str(value) for value in context.get("contract_ids") or []}
                    if any(
                        cid in scene_by_id
                        and scene_by_id[cid].get("frame") == frame
                        and _is_subject_framing_row(scene_by_id[cid], camera_only_roles)
                        for cid in contract_ids
                    ):
                        covered = True
                        break
                    source_id = str(context.get("source_unit") or "")
                    source = stages.get(source_id)
                    if source and source_id in {str(value) for value in unit.get("depends_on") or []}:
                        source_contracts = {
                            str(binding.get("id"))
                            for claim in (source.get("evaluation") or {}).get("claims") or []
                            if isinstance(claim, dict)
                            for binding in claim.get("evidence") or []
                            if isinstance(binding, dict) and binding.get("kind") == "scene_contract"
                        }
                        source_frames = {
                            row.get("frame")
                            for row in (source.get("evaluation") or {}).get("judge") or []
                            if isinstance(row, dict)
                        }
                        if frame in source_frames and any(
                            str(row.get("id")) in source_contracts
                            and row.get("frame") == frame
                            and str(row.get("activates_at") or "") == lid
                            and _is_subject_framing_row(row, camera_only_roles)
                            for row in scene_rows
                            if isinstance(row, dict)
                        ):
                            covered = True
                            break
                if not covered and _deferred_subject_framing_covers(
                    scene_rows, lid, frame, camera_only_roles
                ):
                    covered = True
                if not covered:
                    out.append(
                        Finding(
                            "composition-coverage",
                            True,
                            f"layer {lid} judge f{frame}",
                            "camera/composition owner has no executable subject framing",
                            SUBJECT_COMPOSITION_RULE,
                        )
                    )
        for uid, unit in stages.items():
            evaluation = unit.get("evaluation") or {}
            bound_ids = {
                str(binding.get("id"))
                for claim in evaluation.get("claims") or []
                if isinstance(claim, dict)
                for binding in claim.get("evidence") or []
                if isinstance(binding, dict) and binding.get("kind") == "scene_contract"
            }
            context = evaluation.get("composition_context") or {}
            bound_ids.update(str(value) for value in context.get("contract_ids") or [])
            camera_required_ids = sorted(
                cid
                for cid in bound_ids
                if cid in scene_by_id
                and scene_by_id[cid].get("kind") in CAMERA_REQUIRED_KINDS
            )
            if camera_required_ids and not _camera_available_to(uid):
                kinds = sorted(
                    {
                        str(scene_by_id[cid].get("kind"))
                        for cid in camera_required_ids
                    }
                )
                out.append(
                    Finding(
                        "composition-bootstrap",
                        True,
                        f"layer {lid} unit {uid}",
                        "camera-dependent evidence is due before any declared camera is "
                        f"available: {', '.join(kinds)} ({', '.join(camera_required_ids)})",
                        "provide the camera in this unit or depend on a unit that declares "
                        "`provides: [\"camera\"]`; projected and rendered evidence cannot "
                        "be evaluated through a camera owned only by a later unit or layer",
                    )
                )

        typed_stages = [
            row for row in (layer.get("stages") or []) if isinstance(row, dict)
        ]
        same_layer = {
            gap.unit_id: set(gap.selectors) for gap in same_layer_dress_gaps(typed_stages)
        }
        for gap in same_layer_dress_gaps(typed_stages):
            out.append(
                Finding(
                    "same-layer-dress",
                    True,
                    f"layer {lid} unit {gap.unit_id}",
                    "dresses same-layer mutation roles "
                    + ", ".join(gap.selectors)
                    + f" produced by {', '.join(gap.producer_ids)}",
                    SAME_LAYER_DRESS_RULE,
                )
            )
        for unit in layer.get("stages") or []:
            if not isinstance(unit, dict):
                continue
            uid = str(unit.get("id") or "<missing>")
            evaluation = unit.get("evaluation") or {}
            mutates = unit.get("mutates") or {}
            # dressed selectors carry appearance authority (ADR-0007), so contracts and
            # claims about the dressed surfaces close through them like mutation roles
            mutable_roles = {str(value) for value in mutates.get("roles") or []} | {
                str(value) for value in mutates.get("dresses") or []
            }
            mutable_controls = {str(value) for value in mutates.get("controls") or []}
            declared_dressable_elsewhere = {
                str(selector)
                for other in layers
                if isinstance(other, dict) and str(other.get("id")) != lid
                for selector in other.get("dressable") or []
            }
            undeclared_dresses = sorted(
                str(value)
                for value in mutates.get("dresses") or []
                if str(value) not in declared_dressable_elsewhere
                and str(value) not in same_layer.get(uid, set())
            )
            if undeclared_dresses:
                out.append(
                    Finding(
                        "dressing-closure",
                        True,
                        f"layer {lid} unit {uid}",
                        "dresses selectors no other layer declares dressable: "
                        + ", ".join(undeclared_dresses),
                        DRESSING_CLOSURE_FIX,
                    )
                )
            for claim in evaluation.get("claims") or []:
                if not isinstance(claim, dict) or not claim.get("required"):
                    continue
                for binding in claim.get("evidence") or []:
                    if not isinstance(binding, dict) or not binding.get("id"):
                        continue
                    evidence_id = str(binding["id"])
                    if binding.get("kind") == "image_contract":
                        check = image_by_id.get(evidence_id)
                        if check and check.get("stage") == "post_grade" and lid != str(layers[-1].get("id")):
                            out.append(
                                Finding(
                                    "unit-evidence-due",
                                    True,
                                    f"layer {lid} unit {uid} claim {claim.get('id', '?')}",
                                    f"required image contract {evidence_id} is post_grade and "
                                    "cannot execute at this unit boundary",
                                    "either author and prove an any/pre_grade contract for this "
                                    "unit, or make the final-plate requirement a typed obligation "
                                    "due after the grade-owning dependency; required unit evidence "
                                    "may not be deferred implicitly",
                                )
                            )
                    if binding.get("kind") != "scene_contract":
                        continue
                    contract = scene_by_id.get(evidence_id) or {}
                    owner_layer = str(contract.get("owner_layer") or "")
                    activates_at = str(contract.get("activates_at") or owner_layer)
                    if owner_layer and activates_at and activates_at != owner_layer:

                        out.append(
                            Finding(
                                "unit-evidence-due",
                                True,
                                f"layer {lid} unit {uid} claim "
                                f"{claim.get('id', '?')} contract {evidence_id}",
                                f"directly binds a deferred scene contract owned by "
                                f"layer {owner_layer} and active at layer {activates_at}",
                                DEFERRED_CONTRACT_CONTEXT_RULE,
                            )
                        )
                        continue
                    if str(contract.get("kind") or "") in PROJECTED_ORIGIN_KINDS:
                        owner_id = str(claim.get("repair_owner") or uid)
                        owner = stages.get(owner_id) or unit
                        if "camera" not in (owner.get("provides") or []):
                            out.append(
                                Finding(
                                    "point-projection-owner",
                                    True,
                                    f"layer {lid} unit {uid} claim "
                                    f"{claim.get('id', '?')} contract {evidence_id}",
                                    f"point-projection metric {contract.get('kind')} is "
                                    f"repaired by {owner_id}, which does not provide camera",
                                    PROJECTED_ORIGIN_REPAIR_RULE,
                                )
                            )
                    if str(contract.get("kind") or "") in SURFACE_PROJECTED_KINDS:
                        owner_id = str(claim.get("repair_owner") or uid)
                        owner = stages.get(owner_id) or unit
                        owner_mutates = owner.get("mutates") or {}
                        roles = [str(value) for value in contract.get("roles") or []]
                        owned_roles = [
                            str(value) for value in owner_mutates.get("roles") or []
                        ]
                        dressed_roles = [
                            str(value) for value in owner_mutates.get("dresses") or []
                        ]

                        def _matches(left: str, right: str) -> bool:
                            return fnmatch.fnmatchcase(left, right) or fnmatch.fnmatchcase(
                                right, left
                            )

                        mutates_measured_role = any(
                            _matches(role, selector)
                            for role in roles
                            for selector in owned_roles
                        )
                        dresses_measured_role = any(
                            _matches(role, selector)
                            for role in roles
                            for selector in dressed_roles
                        )
                        if (
                            mutates_measured_role
                            and "geometry" not in (owner.get("provides") or [])
                            and not dresses_measured_role
                        ):
                            out.append(
                                Finding(
                                    "surface-evidence-owner",
                                    True,
                                    f"layer {lid} unit {uid} claim "
                                    f"{claim.get('id', '?')} contract {evidence_id}",
                                    f"surface metric {contract.get('kind')} targets "
                                    f"mutated role(s) {roles}, but repair owner {owner_id} "
                                    "does not provide geometry or dress those surfaces",
                                    "use projected_origin_x/projected_origin_y for an "
                                    "Empty/control point, or split a genuine geometry "
                                    "provider; do not create proxy mesh to pay bbox/visibility",
                                )
                            )
                    # Two-sided measurement kinds observe the OTHER side of a relation:
                    # clearance obstacles and parallax far-groups are inherently other
                    # layers' roles (a persistent clearance contract exists precisely to
                    # measure against geometry its owner will never mutate). The primary
                    # selectors remain strictly inside mutation authority — repair
                    # authority closes there; the compare side is a measurement subject
                    # that must still name a role namespace the PLAN declares somewhere,
                    # so a control id smuggled into compare_roles stays a violation.
                    two_sided = str(contract.get("kind") or "") in {
                        "path_clearance_min",
                        "parallax_displacement_profile",
                        "onset_order",
                    }
                    # visible_fraction is repaired by a unit that can change the rays
                    # (HIR-0051). The camera exception remains: a provides:["camera"]
                    # owner may observe plan-declared geometry it does not mutate
                    # (HIR-0019). A volume-only unit cannot bind mesh vis as required
                    # repair — that hole let atmosphere own proxy_core raycasts.
                    vis_kind = str(contract.get("kind") or "") == "visible_fraction"
                    point_kind = str(contract.get("kind") or "") in PROJECTED_ORIGIN_KINDS
                    observation_only = False
                    if vis_kind:

                        owner_id = str(claim.get("repair_owner") or uid)
                        owner = stages.get(owner_id) or unit
                        owner_mutates = owner.get("mutates") or {}
                        unrepaired = vis_roles_unrepairable_by(
                            provides=owner.get("provides") or [],
                            mutation_roles=[
                                *(owner_mutates.get("roles") or []),
                                *(owner_mutates.get("dresses") or []),
                            ],
                            vis_roles=contract.get("roles") or [],
                        )
                        if unrepaired:
                            out.append(
                                Finding(
                                    "vis-repair-owner",
                                    True,
                                    f"layer {lid} unit {uid} claim {claim.get('id', '?')} "
                                    f"contract {evidence_id}",
                                    "visible_fraction roles are not repairable by "
                                    f"{owner_id}: " + ", ".join(unrepaired),
                                    "bind vis on a unit that provides camera or mutates/"
                                    "dresses every named role; "
                                    + VIS_REPAIR_OWNER_RULE,
                                )
                            )
                        observation_only = "camera" in {
                            str(item) for item in (owner.get("provides") or [])
                        }
                    if point_kind:
                        owner_id = str(claim.get("repair_owner") or uid)
                        owner = stages.get(owner_id) or unit
                        observation_only = "camera" in {
                            str(item) for item in (owner.get("provides") or [])
                        }
                    if observation_only:
                        primary_keys: tuple[str, ...] = ()
                    elif two_sided:
                        primary_keys = ("roles",)
                    else:
                        primary_keys = ("roles", "compare_roles")
                    selected_roles = {
                        str(value)
                        for key in primary_keys
                        for value in contract.get(key) or []
                    }
                    undeclared_roles = sorted(
                        selector
                        for selector in selected_roles
                        if not _selector_declared(selector, mutable_roles)
                    )
                    if two_sided or observation_only:
                        measurement_key = "roles" if observation_only else "compare_roles"
                        undeclared_roles.extend(sorted(
                            selector
                            for selector in {
                                str(value) for value in contract.get(measurement_key) or []
                            }
                            if not _selector_declared(selector, plan_declared_roles)
                        ))
                    if undeclared_roles:
                        out.append(
                            Finding(
                                "role-selector-closure",
                                True,
                                f"layer {lid} unit {uid} contract {evidence_id}",
                                "selects roles outside mutation authority: "
                                + ", ".join(undeclared_roles),
                                "use role selectors inside mutates.roles, or use typed "
                                "control_roles/compare_control_roles for bvfx_control ids; "
                                "required evidence and repair authority must close together",
                            )
                        )
                    observed_controls = (
                        {str(value) for value in contract.get("control_roles") or []}
                        if point_kind and observation_only
                        else set()
                    )
                    selected_controls = set() if point_kind and observation_only else {
                        str(value)
                        for key in ("control_roles", "compare_control_roles")
                        for value in contract.get(key) or []
                    }
                    undeclared = sorted(
                        selector
                        for selector in selected_controls
                        if not _selector_declared(selector, mutable_controls)
                    )
                    undeclared.extend(sorted(
                        selector
                        for selector in observed_controls
                        if not _selector_declared(selector, plan_declared_controls)
                    ))
                    if undeclared:
                        repair = (
                            PROJECTED_ORIGIN_REPAIR_RULE
                            if observed_controls
                            else "declare each selected control in mutates.controls and map it "
                            "through control_roles; contract selectors and mutation authority "
                            "must share the same typed ids"
                        )
                        out.append(
                            Finding(
                                "control-selector-closure",
                                True,
                                f"layer {lid} unit {uid} contract {evidence_id}",
                                "selects undeclared semantic controls: " + ", ".join(undeclared),
                                repair,
                            )
                        )
            if evaluation.get("temporal_evidence") == "motion":
                motion_units += 1
                bound = {
                    str(binding.get("id"))
                    for claim in evaluation.get("claims") or []
                    if isinstance(claim, dict)
                    for binding in claim.get("evidence") or []
                    if isinstance(binding, dict) and binding.get("kind") == "scene_contract"
                }
                if not bound & temporal_ids:
                    out.append(
                        Finding(
                            "temporal-coverage",
                            True,
                            f"layer {lid} unit {uid}",
                            "declares temporal_evidence='motion' but binds no temporal executable contract",
                            "bind onset_order, radial_distance_trend, transform_return_delta, or frame_delta evidence",
                        )
                    )
            controls = {str(value) for value in mutates.get("controls") or []}
            mapping = mutates.get("control_roles")
            if controls and not mapping:
                out.append(
                    Finding(
                        "ownership",
                        False,
                        f"layer {lid} unit {uid}",
                        f"declares {len(controls)} mutable control(s) without control_roles mapping",
                        "map each control to the semantic roles it governs so scope coherence is checkable",
                    )
                )
        earlier_camera_available = earlier_camera_available or bool(camera_units)

    fault_owners = [
        str(row.get("fault_owner") or "") for row in image_rows if isinstance(row, dict)
    ]
    owner_layers = {
        str(row.get("owner_layer") or "") for row in image_rows if isinstance(row, dict)
    }
    if len(fault_owners) >= 4:
        counts = {owner: fault_owners.count(owner) for owner in set(fault_owners)}
        concentrated, count = max(counts.items(), key=lambda item: item[1])
        final_layer = str(layers[-1].get("id") or "") if layers and isinstance(layers[-1], dict) else ""
        if (
            concentrated
            and count / len(fault_owners) >= 0.9
            and (len(owner_layers) >= 2 or concentrated == final_layer)
        ):
            out.append(
                Finding(
                    "ownership",
                    False,
                    "checks.json fault_owner distribution",
                    f"{count}/{len(fault_owners)} image checks route failures to layer {concentrated}",
                    "separate activation from fault ownership and route each failure to the earliest answerable layer",
                )
            )
    return out, {"motion_units": motion_units, "temporal_contracts": len(temporal_ids)}
