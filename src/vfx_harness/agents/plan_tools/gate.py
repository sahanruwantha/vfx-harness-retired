"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
from claude_agent_sdk import tool

from vfx_harness.agents.plan_tools.media import _text
from vfx_harness.domain.brief import load_shot
from vfx_harness.evaluation import plan_gate
from vfx_harness.evidence.scene_checks import (
    FRAME_SCOPED_KINDS,
    KIND_DEFINITIONS,
    KIND_DOMAINS,
    OPERATOR_FIELDS,
    SUPPORTED_KINDS,
    WINDOW_KINDS,
)
from vfx_harness.observability.log import log
from vfx_harness.orchestration.escalate import ask as _ask
from vfx_harness.orchestration.jit_materialization import stage_candidate_view
from vfx_harness.orchestration.plan_authority import prepare_consumer_view


def register_gate_tools(**closed):

    shot_folder = closed["shot_folder"]
    _resolve = closed["_resolve"]
    _keep = closed["_keep"]
    candidate_materialization = closed["candidate_materialization"]
    overlay_root = closed["overlay_root"]
    layout = closed["layout"]
    ns = closed["ns"]
    gate_calls = ns.gate_calls
    prior_gate_signature = ns.prior_gate_signature
    prior_preview_signature = ns.prior_preview_signature
    materialization_requirement_statements = ns.materialization_requirement_statements
    @tool(
        "ask_supervisor",
        "Raise a question ONLY the client can settle — an ambiguity in the brief, a "
        "contradiction between the brief and the stills, or a taste call that is theirs. "
        "Does not block: state the assumption you will plan on and continue. A human "
        "answers before an AFFECTED layer starts. Name the affected layer ids and/or "
        "owned axes; use global_decision only when every layer truly depends on it. "
        "Do NOT use it for anything measure_ref or a spike could answer. Do NOT use it "
        "to ask which selected DAG layer is the earliest geometry successor; that id is "
        "compiled as earliest_geometry_layer on the kickoff card.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "assumption": {"type": "string"},
                "why_it_matters": {"type": "string"},
                "affected_layers": {"type": "array", "items": {"type": "string"}},
                "affected_axes": {"type": "array", "items": {"type": "string"}},
                "global_decision": {"type": "boolean"},
            },
            "required": ["question", "assumption", "affected_layers"],
        },
    )
    async def ask_supervisor(args):

        qid = _ask(
            shot_folder,
            layer="PLAN",
            question=args["question"],
            assumption=args["assumption"],
            why_it_matters=args.get("why_it_matters", ""),
            affected_layers=args.get("affected_layers") or [],
            affected_axes=args.get("affected_axes") or [],
            global_decision=bool(args.get("global_decision")),
        )
        return _text(f"Recorded as Q{qid}. Continue planning on: {args['assumption']}")

    @tool(
        "escalate_vocabulary_gap",
        "Record that NO evidence kind can express a claim you must close. This is the "
        "honest alternative to padding: a typed durable record of the requirement, the "
        "kinds you attempted, and why each cannot certify the claim. Close the "
        "requirement with an explicit decision resolution that references the returned "
        "gap id — never with a trivially-satisfiable contract (those are rejected at "
        "validation). Gaps are visible to the operator and to future planning sessions.",
        {
            "type": "object",
            "properties": {
                "requirement_id": {"type": "string"},
                "claim": {"type": "string"},
                "attempted": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "why_it_cannot_certify": {"type": "string"},
                        },
                        "required": ["kind", "why_it_cannot_certify"],
                    },
                    "minItems": 1,
                },
                "note": {"type": "string"},
            },
            "required": ["requirement_id", "claim", "attempted"],
        },
    )
    async def escalate_vocabulary_gap(args):
        record_dir = shot_folder / "state" / "plan-escalations"
        record_dir.mkdir(parents=True, exist_ok=True)
        path = record_dir / "vocabulary-gaps.jsonl"
        existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        gap_id = f"VG-{len(existing) + 1:03d}"
        record = {
            "schema": "vfx-harness.vocabulary-gap/v1",
            "id": gap_id,
            "requirement_id": str(args["requirement_id"]),
            "claim": str(args["claim"]),
            "attempted": [
                {
                    "kind": str(item.get("kind") or ""),
                    "why_it_cannot_certify": str(item.get("why_it_cannot_certify") or ""),
                }
                for item in args["attempted"]
            ],
            "note": str(args.get("note") or ""),
            "run_id": layout.run_id,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        log(f"plan-lab vocabulary gap {gap_id}: {args['requirement_id']} — {str(args['claim'])[:70]}", 1)
        authored = materialization_requirement_statements.get(
            str(args["requirement_id"]), ""
        )
        statement_rule = (
            f" Use the exact authored statement {authored!r}; materialization may not "
            "rewrite it."
            if authored
            else ""
        )
        return _text(
            f"Recorded {gap_id} for {args['requirement_id']}. Close the requirement with an "
            f"explicit decision resolution ({gap_id} is durable audit state; the "
            "decision carries the authored statement + decision_strength), not a contract."
            + statement_rule
            + " The gap is durable state: the harness "
            "grows the vocabulary against it, and a later generation re-binds the "
            "requirement to a real metric."
        )

    @tool(
        "run_gate",
        "Run the free deterministic plan gate against the current working artifacts. "
        "Use during draft, verify, or repair after a coherent artifact sweep so cross-file "
        "defects are fixed while context is warm. Read-only and capped at four calls per "
        "planning session.",
        {"type": "object", "properties": {}},
    )
    async def run_gate(args):
        nonlocal gate_calls, prior_gate_signature
        gate_calls += 1
        if gate_calls > 4:
            return _text(
                "run_gate call cap reached (4); finish the bounded planning sweep",
                is_error=True,
            )

        result = plan_gate.run(shot_folder, require_scene_checks=True)
        # The filesystem leaf is deliberately named ``plan-workspace``; reports and
        # feedback must retain the authored shot identity from brief.md instead.
        result.shot = load_shot(shot_folder).id
        body = plan_gate.report(result)
        signature = result.signature()
        if not result.clean and signature == prior_gate_signature:
            return _text(
                body
                + "\n\nGATE PLATEAU: findings are unchanged from the previous call. "
                "Stop editing and end this session; the outer deterministic loop owns "
                "any further repair.",
                is_error=True,
            )
        prior_gate_signature = signature
        repair = plan_gate.feedback(result)
        return _text(body + (f"\n\nREPAIR BRIEF\n{repair}" if repair else ""))

    @tool(
        "evidence_vocabulary",
        "The complete registry of scene-contract evidence kinds: definition, evidence "
        "domain, and the structural fields each kind requires. Call this BEFORE "
        "authoring contracts, and whenever a validator error mentions a kind, a "
        "property, or a vacuous target — run 20260824T153427Z-91b7c1 burned 8 write "
        "rounds guessing at a vocabulary this call returns in one turn. If no kind can "
        "express a claim, say so via ask_supervisor instead of padding with a "
        "trivially-satisfiable contract; padding shapes are rejected at validation.",
        {"type": "object", "properties": {}},
    )
    async def evidence_vocabulary(args):

        extra_fields = {
            "keyframe_schedule": [
                "samples: [{frame, values:{property: scalar|vector}}, …] (≥2, unique frames)"
            ],
            "object_property": [
                "frame",
                "property (Blender-evaluated path only — custom properties are "
                "self-certification and rejected; vector components use numeric paths "
                "such as location.2 or rotation_euler.1, not location.z)",
            ],
            "path_clearance_min": [
                "frames [a,b]",
                "compare_roles (obstacle roles, disjoint from roles)",
                "frame_step (optional)",
                "empty compare_roles match is not clearance (fail closed, not 1e9)",
            ],
            "parallax_displacement_profile": [
                "frames [a,b]",
                "compare_roles (far group, disjoint from roles)",
            ],
            "curve_derivative_max": ["frames [a,b]", "property (location|rotation_euler|scale)"],
            "onset_order": ["frames [a,b]", "compare_roles/compare_control_roles (disjoint)"],
            "transform_return_delta": ["frames [a,b]", "component (location|rotation|scale)"],
            "control_render_response": [
                "graph", "node_roles", "probe_values [lo,hi]", "region [x0,y0,x1,y1]",
                "frame (the render frame the sweep measures — the subject must be "
                "VISIBLE there; pair with a visible_fraction row)",
                "response_metric (mean_delta is luminance-only and reads ~0 for pure "
                "hue/tint shifts — palette semantics need mae)",
                "socket/socket_index (optional — otherwise resolution needs a socket "
                "literally named 'Value': the control tag belongs on a ShaderNodeValue, "
                "and the tagged control must stay FREE of drivers)",
            ],
            "frame_delta": ["frames [a,b]", "region (optional)"],
            "node_socket_value": [
                "graph (material|compositor|world)",
                "node_roles",
                "socket (name) or socket_index",
                "direction (input|output)",
                "component (optional, for vector sockets: channel index 0-3 or R/G/B/A)",
            ],
            "node_count": ["graph (material|compositor|world)", "node_roles"],
            "render_region_stat": [
                "stat (mean|stddev luminance, or mean_r/mean_g/mean_b channel means, "
                "all 0-255)",
                "region [x0,y0,x1,y1]",
                "op min/max/band with targets copied from measure_ref's reading of the "
                "judge reference — THE exposure anchor: every relative metric passes at "
                "any brightness, and luminance-only anchors pass a colorless frame "
                "(express 'amber' as mean_r above mean_b via two rows)",
            ],
            "visible_fraction": [
                "roles (the surfaces this judge frame is judged ON — occluders need no "
                "declaration, any closer surface counts)",
                "op min lo≈0.2–0.5 for must-be-seen; op max hi<1 for not-yet-revealed",
            ],
            "projected_origin_x": [
                "roles/control_roles selecting exactly one object (Empty/control is legal)",
                "op min/max/band in normalized camera coordinates; camera-alignment only, "
                "not visibility or subject composition coverage; repair_owner must provide "
                "camera; a band wider than half the frame is vacuous",
            ],
            "projected_origin_y": [
                "roles/control_roles selecting exactly one object (Empty/control is legal)",
                "op min/max/band in normalized top-left camera coordinates; camera-alignment "
                "only; repair_owner must provide camera; a band wider than half the frame "
                "is vacuous",
            ],
            "node_link_count": [
                "graph", "from_node_roles", "to_node_roles",
                "from_socket/to_socket (optional)",
            ],
        }
        entries = {}
        for kind in sorted(SUPPORTED_KINDS):
            fields = []
            if kind in WINDOW_KINDS and kind not in extra_fields:
                fields.append("frames [a,b]")
            if kind in FRAME_SCOPED_KINDS and kind != "object_property":
                fields.append("frame")
            fields.extend(extra_fields.get(kind, []))
            entries[kind] = {
                "definition": KIND_DEFINITIONS.get(kind, ""),
                "domain": KIND_DOMAINS.get(kind, "scene"),
                "fields": fields,
            }
        note = (
            "Projected bbox_* and projected_origin_* targets must lie inside the normalized frame; "
            "a band whose width is greater than half that frame is vacuous. "
            "bbox/visible_fraction require rendered surfaces, while projected_origin_* is "
            "the camera-owner alignment instrument for Empty/control hosts and does not "
            "cover subject composition. When the subject does not exist yet, author bbox_* "
            "with owner_layer on the camera layer, activates_at on the earliest geometry "
            "layer, lifecycle persistent, and fault_owner on the camera owner. "
            "The control "
            "producer proves fixed world state with scene evidence and publishes a typed "
            "placement_control; the camera successor depends on it, declares the exact "
            "consume, and owns projection without mutating the observed selector. "
            "path_clearance_min fails closed on an empty obstacle selection — persistent "
            "lifecycle re-evaluates as geometry arrives, it does not make absence a PASS."
        )
        return _text(
            json.dumps(
                {"kinds": entries, "operators": OPERATOR_FIELDS, "note": note},
                indent=1,
            )
        )

    gate_preview_calls = 0
    prior_preview_signature: str | None = None

    @tool(
        "gate_preview",
        "Run the deterministic plan gate against the CURRENT consumer view (selected "
        "bundle + materialized layers + staged unit plans) — the exact evaluation "
        "terminal publication will apply. Use it before finishing so findings become "
        "fixes in this session instead of a retracted artifact. Read-only; capped at "
        "three calls per session.",
        {"type": "object", "properties": {}},
    )
    async def gate_preview(args):
        nonlocal gate_preview_calls, prior_preview_signature
        gate_preview_calls += 1
        if gate_preview_calls > 3:
            return _text(
                "gate_preview call cap reached (3); finish the artifact and let the "
                "terminal gate decide",
                is_error=True,
            )

        try:
            view = await anyio.to_thread.run_sync(prepare_consumer_view, layout)
            candidate = Path(candidate_materialization) if candidate_materialization else None
            if candidate is not None and candidate.is_file():
                # stage the session's own unpublished payload so the gate previews the
                # POST-publication world — two generations published on a false CLEAN
                # because the preview saw the pre-publication view

                try:
                    await anyio.to_thread.run_sync(
                        lambda: stage_candidate_view(
                            shot_folder, candidate, view, overlay_root=overlay_root
                        )
                    )
                except (ValueError, OSError) as exc:
                    return _text(
                        f"candidate materialization does not validate, so the gate has "
                        f"nothing to preview: {exc}",
                        is_error=True,
                    )
            result = await anyio.to_thread.run_sync(
                lambda: plan_gate.run(view, require_scene_checks=False)
            )
        except Exception as exc:
            log(f"plan-lab ✗ gate_preview: {str(exc)[:120]}", 1)
            return _text(f"gate preview failed: {exc}", is_error=True)
        result.shot = load_shot(shot_folder).id
        body = plan_gate.report(result)
        log(f"plan-lab gate_preview → {'CLEAN' if result.clean else f'{len(result.blocking)} blocking'}", 1)
        signature = result.signature()
        if not result.clean and signature == prior_preview_signature:
            return _text(
                body
                + "\n\nGATE PLATEAU: findings are unchanged from the previous preview. "
                "Anything you cannot fix from inside this session (missing unit plan, "
                "another layer's authority) belongs to the outer flow — finish your "
                "artifact and report the residue.",
                is_error=True,
            )
        prior_preview_signature = signature
        repair = plan_gate.feedback(result)
        return _text(body + (f"\n\nREPAIR BRIEF\n{repair}" if repair else ""))

    return ask_supervisor, escalate_vocabulary_gap, run_gate, evidence_vocabulary, gate_preview
