"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import json

import anyio
from claude_agent_sdk import tool

from vfx_harness.blender.session import BlenderError
from vfx_harness.blender.tools.payment import _text
from vfx_harness.blender.tools.reports import _check_args_error, _check_report
from vfx_harness.evidence.checks import layer_evidence as image_layer_evidence
from vfx_harness.evidence.scene_checks import irreversible_deferred_subject_forecast_failures, layer_evidence, load_rows
from vfx_harness.orchestration.ledger import load_layers


def register_contracts(
    session,
    _call,
    comparison_state,
    comparison_locks,
    shot_dir,
    layer_id,
    assets_dir,
    feedback_policy,
    mutation_roles,
    scope_baseline,
    unit_scope,
    _black_frame_note,
    _black_search_stop,
    _register_candidate,
):
    @tool(
        "check_scene",
        "JUDGMENT-FREE checks on the scene itself — no critic, no cost, no render (except "
        "`passes`). This is the whole class of defect a beauty render CANNOT show: "
        "kind='visibility' reports the canonical on-screen surface visible_fraction "
        "without inventing a pass threshold (use contract_result for the bound target), "
        "'framing' gives the NDC bbox/width/centre via "
        "world_to_camera_view, 'projection' maps proposed [x,y,z] world points through "
        "the evaluated active camera without creating marker objects, 'motion' gives max "
        "speed/accel/jerk and whether the move is "
        "unbroken, 'mesh' counts non-manifold edges, loose verts, n-gons, poles and "
        "disconnected islands, 'scale' checks dimensions and that scale is applied, "
        "'passes' checks the render buffer for NaN/Inf/negative pixels, 'bbox' returns the "
        "oracle crop box to hand to render_pass. Address subjects with role= (bvfx_role); "
        "a shared role that matches several hosts is not a miss — pass object= with one "
        "of the named hosts. object= is the display-name fallback. A miss names present "
        "roles and names. "
        "Required arguments: visibility=role-or-object+frame; "
        "framing=role-or-object+(frame or frames); projection=frame+points; "
        "motion=role-or-object+2+ frames; "
        "mesh/scale=role-or-object; passes=frame; bbox=role-or-object+frame. For "
        "intentional open shells, mesh accepts allow_boundary=true and still rejects "
        "branch/wire edges.",
        {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "visibility",
                        "framing",
                        "projection",
                        "motion",
                        "mesh",
                        "scale",
                        "passes",
                        "bbox",
                    ],
                },
                "role": {
                    "type": "string",
                    "description": (
                        "bvfx_role selector (preferred); fnmatch; exactly one host — "
                        "if several share the role, pass object="
                    ),
                },
                "object": {
                    "type": "string",
                    "description": "display name; use role= instead when you know the semantic role",
                },
                "frame": {"type": "integer"},
                "frames": {"type": "array", "items": {"type": "integer"}},
                "points": {
                    "type": "array",
                    "description": (
                        "projection only: proposed world-space points; read-only and "
                        "does not require temporary scene objects"
                    ),
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 3,
                        "maxItems": 3,
                    },
                    "minItems": 1,
                    "maxItems": 64,
                },
                "scale": {"type": "number"},
                "allow_boundary": {
                    "type": "boolean",
                    "description": "mesh only: permit intentional open-shell "
                    "boundary edges while still rejecting wire "
                    "and >2-face branch edges",
                },
            },
            "required": ["kind"],
        },
    )
    async def check_scene(args):
        kind = args["kind"]
        args_error = _check_args_error(kind, args)
        if args_error:
            return _text(args_error, is_error=True)
        payload = {k: v for k, v in args.items() if k != "kind" and v is not None}
        try:
            r = await _call("check", kind=kind, **payload)
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(_check_report(kind, r))

    @tool(
        "contract_result",
        "Evaluate one contract bound to the ACTIVE unit by exact id, or one compiled "
        "deferred-subject forecast. Scene contracts "
        "use the canonical evaluator (including multi-role visible_fraction logical AND) "
        "and report per-role details. Image contracts require image_handle from an "
        "eevee render at that frame. Forecast rows cannot pay the owner contract; an "
        "irreversible partial-union miss is nevertheless a current-unit freeze blocker. "
        "Use this instead of recreating contract math in run_bpy or "
        "guessing from a beauty render.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "image_handle": {"type": "string"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    )
    async def contract_result(args):
        if not shot_dir or not layer_id:
            return _text("contract_result needs an active shot layer", is_error=True)
        cid = str(args["id"])
        scene_ids = set(comparison_state.get("active_evidence_ids") or [])
        diagnostic_ids = set(comparison_state.get("diagnostic_evidence_ids") or [])
        image_ids = set(comparison_state.get("active_image_evidence_ids") or [])
        if cid not in scene_ids | diagnostic_ids | image_ids:
            present = sorted(scene_ids | diagnostic_ids | image_ids)
            return _text(
                f"contract {cid!r} is neither bound nor a compiled forecast for this "
                "unit; available ids: " + (", ".join(present) if present else "none"),
                is_error=True,
            )
        if cid in scene_ids | diagnostic_ids:
            try:

                source = next(row for row in load_rows(shot_dir) if str(row.get("id")) == cid)
                frames = source.get("frames") or [source.get("frame", comparison_state.get("frame", 1))]
                rows: list[dict] = []
                for frame in frames:
                    measured = await anyio.to_thread.run_sync(
                        lambda f=int(frame): layer_evidence(shot_dir, str(layer_id), frame=f, session=session)
                    )
                    rows.extend(row for row in measured if str(row.get("id")) == cid)
            except (StopIteration, OSError, ValueError, BlenderError) as exc:
                return _text(f"could not evaluate scene contract {cid}: {exc}", is_error=True)
            if cid in diagnostic_ids:

                blockers = {
                    str(row.get("id")): row for row in irreversible_deferred_subject_forecast_failures([source], rows)
                }
                rows = [
                    blockers.get(str(row.get("id")))
                    or {
                        **row,
                        "diagnostic_only": True,
                        "acceptance_evidence": False,
                    }
                    for row in rows
                ]
            return _text(json.dumps(rows, indent=2, sort_keys=True))

        handle = str(args.get("image_handle") or "")
        record = (comparison_state.get("image_artifacts") or {}).get(handle)
        if not isinstance(record, dict) or record.get("role") != "live_candidate":
            return _text(
                f"image contract {cid!r} requires a current IMAGE EVIDENCE HANDLE; "
                "call render_frame(mode='eevee', scale=0.5) at its owed frame",
                is_error=True,
            )
        try:

            layer = load_layers(type("ShotRef", (), {"folder": shot_dir})())[str(layer_id)]
            ref = dict(layer.judges).get(int(record["frame"]), "")
            rows = image_layer_evidence(
                shot_dir,
                str(layer_id),
                frame=int(record["frame"]),
                ref=str(ref),
                render=str(record["path"]),
                stage=("post_grade" if any("grade" in axis.lower() for axis in layer.owns) else "pre_grade"),
            )
        except (OSError, ValueError, KeyError) as exc:
            return _text(f"could not evaluate image contract {cid}: {exc}", is_error=True)
        return _text(json.dumps([row for row in rows if str(row.get("id")) == cid], indent=2, sort_keys=True))

    return check_scene, contract_result
