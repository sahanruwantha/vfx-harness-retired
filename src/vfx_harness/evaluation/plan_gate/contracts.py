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

import json
from pathlib import Path

from vfx_harness.domain.contracts import load_document
from vfx_harness.domain.work_units import DEFERRED_CONTRACT_FRAME_AUTHORITY_RULE, read_document
from vfx_harness.evaluation.plan_gate.types import (
    Finding,
    _global_authority_layers,
    _global_executable_checks_apply,
    _materialized_view,
)
from vfx_harness.evaluation.plan_gate.unit_deps import _check_unit_dependencies
from vfx_harness.evidence.claim_evidence import validate_claim_closure
from vfx_harness.evidence.scene_checks import (
    BBOX_KINDS,
    SUBJECT_COMPOSITION_RULE,
    schedule_smoothness_contradictions,
    validate_row_set,
)
from vfx_harness.evidence.scene_checks import validate_row as validate_scene_check
from vfx_harness.orchestration.ledger import load_layers


def _cross_row_contract_findings(scene_rows: list) -> list[Finding]:
    """Split grandfathered advisory lint from jointly unsatisfiable published pairs.

    Auto-socket ``control_render_response`` vs a pinned sibling is refused at
    authoring; a published view that already measures honestly stays advisory.
    A ``keyframe_schedule`` whose consecutive samples already exceed a same-role
    ``curve_derivative_max.hi`` cannot pass (HIR-0030) — that finding blocks.
    """

    scene_dicts = [row for row in scene_rows if isinstance(row, dict)]
    out: list[Finding] = []
    pair_messages: set[str] = set()
    for pair in schedule_smoothness_contradictions(scene_dicts):
        pair_messages.add(pair["message"])
        out.append(Finding(
            "contracts",
            True,
            "scene_checks.json",
            pair["message"],
            "raise hi, widen the sample span, or reduce Δ — interpolation cannot invent a third option",
        ))
    for cross_row_finding in validate_row_set(scene_dicts):
        if cross_row_finding in pair_messages:
            continue
        out.append(Finding(
            "contracts",
            False,
            "scene_checks.json",
            cross_row_finding,
            "declare the response row's socket at this layer's next materialization",
        ))
    return out


def _check_contracts(folder: Path, *, require_scene_checks: bool = False) -> tuple[list[Finding], dict]:

    out = []
    missing = [n for n in ("layers.json", "critic_axes.json", "acceptance.json") if not (folder / n).is_file()]
    if missing:
        legacy = (
            " (this shot has gates.json — it predates the layer contract)" if (folder / "gates.json").is_file() else ""
        )
        return [
            Finding(
                "contracts",
                True,
                ", ".join(missing),
                f"the plan produced no {' / '.join(missing)}{legacy}",
                "the build stage reads these, so nothing can be built from this plan as it "
                "stands — re-plan, or port the legacy artifact forward",
            )
        ], {}
    try:
        layers_document = json.loads((folder / "layers.json").read_text())
        layers = read_document(folder / "layers.json")
        axes = json.loads((folder / "critic_axes.json").read_text())
        accept = json.loads((folder / "acceptance.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return [Finding("contracts", True, "plan artifacts", f"unreadable: {e}")], {}

    unit_first = isinstance(layers_document, dict) and layers_document.get("schema") == 5
    materialized_ids, pinned_overlays = _materialized_view(folder) if unit_first else (set(), set())
    if unit_first:
        global_layers = _global_authority_layers(folder, layers)
        capability_closure: dict[str, set[str]] = {}
        for index, layer in enumerate(global_layers):
            lid = str(layer.get("id") or "?")
            jit = layer.get("jit") or {}
            raw_provides = jit.get("provides")
            if not isinstance(raw_provides, dict):
                out.append(
                    Finding(
                        "global-capability",
                        True,
                        f"layers.json.layers[{index}].jit.provides",
                        "global layer omits its typed scene-capability declaration",
                        "declare a map binding `camera` to reserved roles on its producing "
                        "layer and {} on dependent layers; do not let a materialized unit invent "
                        "global camera ownership",
                    )
                )
                provided: set[str] = set()
            else:
                provided = {str(item) for item in raw_provides}
            inherited = {
                capability
                for dependency in jit.get("depends_on_layers") or []
                for capability in capability_closure.get(str(dependency), set())
            }
            capability_closure[lid] = provided | inherited
            if "camera" not in capability_closure[lid]:
                out.append(
                    Finding(
                        "global-capability",
                        True,
                        f"layer {lid}",
                        "judge visibility is due before a camera capability is available",
                        "move the camera-owning layer before this layer, declare "
                        "`jit.provides: {\"camera\": [\"<reserved role>\"]}` there, "
                        "and depend on it; the "
                        "materializer may not repair a global ownership/DAG omission",
                    )
                )
        ready = [
            str(layer.get("id") or "?")
            for layer in layers
            if layer.get("execution") != "jit_deferred"
            and str(layer.get("id") or "?") not in materialized_ids
        ]
        if ready:
            out.append(Finding(
                "global-preproduction",
                True,
                "layers.json",
                "schema-5 global authority contains ready layers: " + ", ".join(ready),
                "publish every layer as ownership-only jit_deferred authority; materialize "
                "the dependency-ready root after publication",
            ))
        if accept:
            out.append(Finding(
                "global-preproduction",
                True,
                "acceptance.json",
                "schema-5 global authority contains acceptance fingerprints",
                "leave global acceptance empty and measure a reference only when its owning "
                "layer materializes",
            ))

    axis_keys = {a["key"] for a in axes}
    layer_ids = {str(lay.get("id")) for lay in layers}
    layer_axes = {str(lay.get("id")): set(lay.get("owns") or []) for lay in layers}
    layer_frames = {
        str(lay.get("id")): {
            int(item.get("frame"))
            for item in ([lay.get("judge")] if isinstance(lay.get("judge"), dict) else lay.get("judge") or [])
            if item.get("frame") is not None
        }
        for lay in layers
    }
    published_frames = {
        frame
        for lay in layers
        if _global_executable_checks_apply(lay)
        for frame in layer_frames.get(str(lay.get("id")), set())
    }
    owned: set[str] = set()
    for lay in layers:
        lid = lay.get("id", "?")
        if not lay.get("owns"):
            out.append(
                Finding(
                    "contracts",
                    True,
                    f"layer {lid}",
                    "owns no critic axis; legacy unscoped judging is not supported",
                    "assign at least one visible, independently answerable axis",
                )
            )
        for ax in lay.get("owns", []):
            owned.add(ax)
            if ax not in axis_keys:
                out.append(
                    Finding(
                        "contracts",
                        True,
                        f"layer {lid}",
                        f"owns axis '{ax}', which critic_axes.json does not define",
                        "the critic can never score this axis, so the layer cannot be judged "
                        "on the thing it is responsible for — add the axis or fix the name",
                    )
                )
        judges = lay.get("judge")
        for j in [judges] if isinstance(judges, dict) else judges or []:
            ref = j.get("ref")
            if ref and not (folder / ref).is_file():
                out.append(
                    Finding(
                        "contracts",
                        True,
                        f"layer {lid} judge f{j.get('frame')}",
                        f"reference '{ref}' does not exist",
                        "the layer would be judged against a missing plate",
                    )
                )
    for ax in sorted(axis_keys - owned):
        out.append(
            Finding(
                "contracts",
                False,
                "critic_axes.json",
                f"axis '{ax}' is defined but no layer owns it",
                "either a layer should own it or it is dead weight in every rubric it "
                "appears in — an unowned axis has nobody to route a failure to",
            )
        )
    for m in accept:
        if m.get("frame") not in published_frames:
            out.append(Finding(
                "deferred-overplanning",
                True,
                f"acceptance {m.get('id')}",
                f"fingerprints frame {m.get('frame')} before its producing layer materializes",
                "global acceptance.json may contain only judge frames of ready published units; "
                "add later moments through the materialized acceptance overlay",
            ))
        ref = m.get("ref")
        if ref and not (folder / ref).is_file():
            out.append(
                Finding(
                    "contracts",
                    True,
                    f"acceptance {m.get('id')}",
                    f"reference '{ref}' does not exist",
                    "this moment cannot be judged at all",
                )
            )

    scene_path = folder / "scene_checks.json"
    scene_rows = []
    if not scene_path.is_file():
        out.append(
            Finding(
                "contracts",
                require_scene_checks,
                "scene_checks.json",
                "no live-scene contracts supplied",
                "exact dimensions, placements, counts and mesh-state facts will be left to a "
                "vision judge; add scene_checks.json for numeric scene clauses",
            )
        )
    else:
        try:

            scene_rows = load_document(scene_path, "contracts")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            out.append(Finding("contracts", True, "scene_checks.json", f"unreadable: {exc}"))
            scene_rows = []
        # Auto-socket lint stays advisory for grandfathered published views.
        # Unsatisfiable schedule/smoothness pairs block (HIR-0030).
        out.extend(_cross_row_contract_findings(scene_rows))
        # Advisory mirror of validate_materialization's hard rule (new materializations
        # cannot publish without it): a judge frame with no occlusion-true visibility
        # row judges subjects nobody proved are on screen. Run 20260825: layer 2's
        # every judged surface sat behind a solid proxy disc at both judge frames,
        # invisible to projection-only bbox rows, and no rule fired because
        # composition-coverage is scoped to camera-owning layers.
        for lid in sorted(materialized_ids):
            layer_row = next(
                (
                    row
                    for row in layers
                    if isinstance(row, dict) and str(row.get("id")) == lid
                ),
                {},
            )
            stages = [
                row for row in layer_row.get("stages") or [] if isinstance(row, dict)
            ]
            surface_visibility_due = any(
                "geometry" in (unit.get("provides") or [])
                or bool((unit.get("mutates") or {}).get("dresses"))
                or bool(unit.get("look_capabilities"))
                or any(
                    claim.get("required") and claim.get("asserts") == "image"
                    for claim in (unit.get("evaluation") or {}).get("claims") or []
                    if isinstance(claim, dict)
                )
                or any(
                    consume.get("kind") in {"asset_source", "instance_source"}
                    for consume in unit.get("consumes") or []
                    if isinstance(consume, dict)
                )
                for unit in stages
            )
            if not surface_visibility_due:
                continue
            for frame in sorted(layer_frames.get(lid, set())):
                if not any(
                    r.get("kind") == "visible_fraction" and r.get("frame") == frame
                    for r in scene_rows
                    if isinstance(r, dict)
                ):
                    out.append(Finding(
                        "composition-coverage",
                        False,
                        f"layer {lid} judge f{frame}",
                        "no occlusion-true visibility contract at this judge frame",
                        "add a visible_fraction row for the judged roles at this "
                        "layer's next materialization",
                    ))
        seen: set[str] = set()
        for row in scene_rows:
            rid = str(row.get("id") or "<missing>") if isinstance(row, dict) else "<invalid>"
            if not isinstance(row, dict):
                out.append(Finding("contracts", True, "scene_checks.json", "every record must be an object"))
                continue
            error = validate_scene_check(row)
            if error:
                out.append(Finding("contracts", True, rid, error, "fix the scene contract schema before building"))
            if rid in seen:
                out.append(Finding("contracts", True, rid, "duplicate scene-check id"))
            seen.add(rid)
            lid = str(row.get("fault_owner", ""))
            owner = str(row.get("owner_layer", ""))
            active = str(row.get("activates_at", ""))
            axis = str(row.get("axis", ""))
            if owner not in layer_ids:
                out.append(Finding("contracts", True, rid, f"owner_layer names nonexistent layer {owner!r}"))
            if active not in layer_ids:
                out.append(Finding("contracts", True, rid, f"activates_at names nonexistent layer {active!r}"))
            if lid not in layer_ids:
                out.append(Finding("contracts", True, rid, f"fault_owner names nonexistent layer {lid!r}"))
            elif axis not in layer_axes.get(lid, set()):
                out.append(
                    Finding(
                        "contracts",
                        True,
                        rid,
                        f"axis {axis!r} is not owned by layer {lid}",
                        "route the contract to the layer answerable for that property",
                    )
                )
            frame_authority = owner if owner != active and owner in layer_frames else active
            if row.get("frame") is not None and frame_authority in layer_frames:
                try:
                    frame = int(row["frame"])
                except (TypeError, ValueError):
                    out.append(Finding("contracts", True, rid, "frame is not an integer"))
                else:
                    if frame not in layer_frames[frame_authority]:
                        if frame_authority == owner and owner != active:

                            detail = (
                                f"frame {frame} is not judged by owner layer {owner}; "
                                f"the contract activates later at layer {active}"
                            )
                            fix = DEFERRED_CONTRACT_FRAME_AUTHORITY_RULE
                        else:
                            detail = (
                                f"frame {frame} is not judged by activation layer {active}"
                            )
                            fix = "add the frame to the layer judge list or move the contract"
                        out.append(
                            Finding(
                                "contracts",
                                True,
                                rid,
                                detail,
                                fix,
                            )
                        )
            try:
                owner_n = int(owner)
                active_n = int(active)
            except (TypeError, ValueError):
                owner_n = active_n = None
            if (
                owner_n is not None
                and active_n is not None
                and active_n > owner_n
                and str(row.get("kind") or "") in BBOX_KINDS
            ):
                if str(row.get("lifecycle") or "") != "persistent":
                    out.append(
                        Finding(
                            "deferred-composition-lifecycle",
                            True,
                            rid,
                            "subject composition due on a later layer must be persistent "
                            "so later layers keep the camera framed",
                            SUBJECT_COMPOSITION_RULE,
                        )
                    )
                if lid != owner:
                    out.append(
                        Finding(
                            "deferred-composition-fault",
                            True,
                            rid,
                            f"deferred subject composition must keep fault_owner={owner!r} "
                            f"(the camera owner), not {lid!r}",
                            SUBJECT_COMPOSITION_RULE,
                        )
                    )
            for temporal_frame in row.get("frames") or []:
                if (
                    frame_authority in layer_frames
                    and temporal_frame not in layer_frames[frame_authority]
                ):
                    if frame_authority == owner and owner != active:

                        detail = (
                            f"temporal frame {temporal_frame} is not judged by owner "
                            f"layer {owner}; the contract activates later at layer {active}"
                        )
                        fix = DEFERRED_CONTRACT_FRAME_AUTHORITY_RULE
                    else:
                        detail = (
                            f"temporal frame {temporal_frame} is not judged by "
                            f"activation layer {active}"
                        )
                        fix = (
                            "every temporal endpoint is a real judge frame; add it to "
                            "the layer and unit judge lists"
                        )
                    out.append(
                        Finding(
                            "contracts",
                            True,
                            rid,
                            detail,
                            fix,
                        )
                    )
    if unit_first and scene_rows:
        # contracts owned by a verifiably materialized layer ARE the layer boundary
        # materialization the remedy asks for; only the rest violate preproduction
        unmaterialized_contracts = [
            row
            for row in scene_rows
            if not (
                "scene_checks.json" in pinned_overlays
                and str(row.get("owner_layer") or "") in materialized_ids
            )
        ]
        if unmaterialized_contracts:
            out.append(Finding(
                "global-preproduction",
                True,
                "scene_checks.json",
                "schema-5 global authority contains concrete scene contracts",
                "materialize scene contracts at the owning layer boundary",
            ))

    if unit_first:
        try:
            image_rows = read_document(folder / "checks.json")
        except (OSError, json.JSONDecodeError, ValueError):
            image_rows = []
        if image_rows:
            out.append(Finding(
                "global-preproduction",
                True,
                "checks.json",
                "schema-5 global authority contains candidate-sensitive image checks",
                "propose image checks only after a producing unit has created a real candidate",
            ))
    closure_claims = 0
    dependency_findings = _check_unit_dependencies(folder)
    if dependency_findings:
        # Hierarchy already reports every invalid edge with a targeted repair. Claim
        # closure cannot be evaluated until those edges are fixed, and repeating the
        # typed loader's first exception here only adds a duplicate partial finding.
        return out, {
            "layers": len(layers),
            "axes": len(axis_keys),
            "moments": len(accept),
            "scene_checks": len(scene_rows),
            "claims": 0,
        }
    try:

        parsed_layers = tuple(load_layers(type("ShotRoot", (), {"folder": folder})()).values())
        closure_claims = sum(
            len(unit.evaluation.claims)
            for layer in parsed_layers
            for unit in layer.stages
        )
        closure = validate_claim_closure(folder, parsed_layers)
        for finding in closure.findings:
            out.append(
                Finding(
                    "claim-closure",
                    True,
                    finding.where,
                    finding.what,
                    "bind the atomic claim to exact typed evidence, or declare qualified "
                    "qualitative/human authority; aggregate 'all checks pass' claims are invalid",
                )
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        out.append(Finding("claim-closure", True, "layers.json", f"unreadable claim graph: {exc}"))
    return out, {
        "layers": len(layers),
        "axes": len(axis_keys),
        "moments": len(accept),
        "scene_checks": len(scene_rows),
        "claims": closure_claims,
    }
