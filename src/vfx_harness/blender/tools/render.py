"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

from claude_agent_sdk import tool

from vfx_harness.blender.session import BlenderError
from vfx_harness.blender.tools.images import _image
from vfx_harness.blender.tools.payment import _text
from vfx_harness.blender.tools.reports import _warn_suffix, preview_render_mode


def register_render(
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
        "render_frame",
        "See one frame. mode='solid'/'wire' = fast Workbench (~0.1s, composition/"
        "silhouette); mode='draft' = fast low-sample EEVEE (~0.5s, quick look checks — "
        "use this while iterating); mode='eevee' = full-quality look (~1-3s, for final "
        "judging). On a unit with no look capabilities the default is solid (geometry "
        "without lights). Pass mode='eevee' only when you need beauty. scale is 0..1 "
        "(default 0.4). Returns the image + an EXPOSURE readout (mean/clipped/black) "
        "so you can catch blowout objectively.",
        {
            "type": "object",
            "properties": {
                "frame": {"type": "integer"},
                "mode": {"type": "string", "enum": ["solid", "wire", "draft", "eevee"]},
                "scale": {"type": "number", "description": "0..1 res scale, default 0.4"},
            },
            "required": ["frame"],
        },
    )
    async def render_frame(args):
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
        mode = preview_render_mode(feedback_policy["look_actions"], args.get("mode"), look_default="eevee")
        try:
            r = await _call("render", frame=int(args["frame"]), mode=mode, scale=float(args.get("scale", 0.4)))
        except BlenderError as e:
            return _text(str(e), is_error=True)
        # h_render does not echo scale because its pixel resolution is the executable
        # setting. Preserve the caller value for provenance equality with the harness-
        # captured pre-unit adversary.
        r["scale"] = float(args.get("scale", 0.4))
        handle = _register_candidate(r)
        cap = f"frame {r['frame']} ({r['mode']})" + _warn_suffix(r)
        if handle:
            cap += f"\nIMAGE EVIDENCE HANDLE: {handle}"
        if not feedback_policy["look_actions"] and not args.get("mode"):
            cap += (
                " · Workbench default for an executable-only unit "
                "(pass mode='eevee' for beauty; lighting is not this unit's scope)"
            )
        cap += await _black_frame_note(r)
        return _image(r["image_path"], cap, feedback_groups=feedback_policy["groups"])

    @tool(
        "inspect_view",
        "Read-only 3D form inspection aimed at one semantic role namespace. Renders "
        "a temporary Workbench camera, restores the sealed shot camera and every "
        "temporary visibility change, and returns no image-evidence handle. Use "
        "view='orbit' with orbit_degrees, a shot-relative elevation "
        "(front/right/back/left/top), or through_camera. isolate=true solos matching "
        "rendered hosts transactionally. This diagnostic can teach a mutation but can "
        "never pay a contract.",
        {
            "type": "object",
            "properties": {
                "frame": {"type": "integer"},
                "role": {
                    "type": "string",
                    "description": "semantic namespace, e.g. building or building.mass.tower",
                },
                "view": {
                    "type": "string",
                    "enum": ["through_camera", "orbit", "front", "right", "back", "left", "top"],
                },
                "orbit_degrees": {
                    "type": "integer",
                    "enum": [-60, -30, 30, 60],
                    "description": "required only for view='orbit'; positive tumbles right",
                },
                "mode": {"type": "string", "enum": ["solid", "wire"]},
                "isolate": {"type": "boolean"},
                "scale": {"type": "number", "minimum": 0.1, "maximum": 1.0},
            },
            "required": ["frame", "role", "view"],
            "additionalProperties": False,
        },
    )
    async def inspect_view(args):
        role = str(args.get("role") or "").strip()
        if not role:
            return _text("inspect_view requires a semantic role namespace", is_error=True)
        view = str(args.get("view") or "")
        orbit_degrees = args.get("orbit_degrees")
        if view == "orbit" and orbit_degrees not in {-60, -30, 30, 60}:
            return _text(
                "inspect_view view='orbit' requires orbit_degrees from [-60, -30, 30, 60]",
                is_error=True,
            )
        if view != "orbit" and orbit_degrees is not None:
            return _text(
                "inspect_view orbit_degrees is legal only when view='orbit'",
                is_error=True,
            )
        try:
            r = await _call(
                "inspect_view",
                frame=int(args["frame"]),
                role=role,
                view=view,
                **({"orbit_degrees": int(orbit_degrees)} if orbit_degrees is not None else {}),
                mode=str(args.get("mode") or "solid"),
                isolate=bool(args.get("isolate", False)),
                scale=float(args.get("scale", 0.5)),
            )
        except BlenderError as e:
            return _text(str(e), is_error=True)
        cap = (
            f"DIAGNOSTIC ONLY — cannot pay contracts · role {role!r} · "
            f"{r['view']} at frame {r['frame']} · sealed camera restored"
        )
        if r.get("isolated"):
            cap += f" · isolated {r.get('subject_count', 0)} matching hosts"
        return _image(r["image_path"], cap, feedback_groups=[])

    @tool(
        "render_pass",
        "Render one frame as a DIAGNOSTIC rather than a beauty shot, so you can see the "
        "thing you are actually being judged on. `pass` isolates a render pass — use "
        "'diffuse_direct' to see MODELLING BY LIGHT with emission removed (a render "
        "setting, not something to squint past), 'emit' to see only self-lit surfaces, "
        "'light_coverage' with `light=` to render clay under that local light while "
        "transactionally suppressing World lighting/volume (separates placement from "
        "atmospheric extinction), "
        "'shadow'/'ao'/'normal'/'depth'/'crypto' for the rest. `shade` overrides "
        "materials: 'clay' for form, 'silhouette' for outline, 'matcap:<name>' for a "
        "Workbench diagnostic. `light='<LightObject>'` (comma list allowed) renders with "
        "ONLY those light objects and hides the rest, so you can see what one lamp "
        "actually contributes. `crop` is "
        "[x0,y0,x1,y1] in 0..1 from the TOP-LEFT and is a true optical zoom, so pair "
        "it with res_pct (e.g. 400) to see fine detail at real resolution instead of "
        "upscaling a thumbnail. Every mode returns a caption saying what to look for.",
        {
            "type": "object",
            "properties": {
                "frame": {"type": "integer"},
                "pass": {
                    "type": "string",
                    "enum": [
                        "beauty",
                        "light_coverage",
                        "diffuse_direct",
                        "emit",
                        "shadow",
                        "ao",
                        "normal",
                        "depth",
                        "crypto",
                    ],
                },
                "shade": {"type": "string", "description": "beauty | clay | silhouette | matcap:<name>"},
                "light": {
                    "type": "string",
                    "description": "light OBJECT name(s), comma-separated; all other lights are hidden for this render",
                },
                "crop": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[x0,y0,x1,y1] in 0..1, origin TOP-LEFT",
                },
                "res_pct": {"type": "integer", "description": "resolution percentage; >100 zooms a crop"},
                "scale": {"type": "number"},
            },
            "required": ["frame"],
        },
    )
    async def render_pass(args):
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
        crop = args.get("crop")
        bad_crop = crop is not None and (
            len(crop) != 4
            or not all(0.0 <= float(v) <= 1.0 for v in crop)
            or not (crop[0] < crop[2] and crop[1] < crop[3])
        )
        if bad_crop:
            return _text(
                "crop must be [x0,y0,x1,y1] in 0..1 with x0<x1 and y0<y1 (origin TOP-LEFT; x right, y down)",
                is_error=True,
            )
        if args.get("pass") == "light_coverage" and not str(args.get("light") or "").strip():
            return _text(
                "pass='light_coverage' requires light='<LightObject>' so the causal isolation has one named subject",
                is_error=True,
            )
        diagnostic_args = dict(args)
        if not feedback_policy["look_actions"]:
            # Layout owns form, not beauty. One stable material/lighting-independent
            # diagnostic prevents the builder from tuning albedo or lamps to a finished
            # reference whose appearance belongs to later layers.
            diagnostic_args["pass"] = "beauty"
            diagnostic_args["shade"] = "matcap:check_normal+y"
            diagnostic_args["light"] = None
        try:
            r = await _call(
                "render",
                frame=int(args["frame"]),
                mode="eevee",
                scale=float(diagnostic_args.get("scale", 0.5)),
                **{"pass": diagnostic_args.get("pass", "beauty")},
                shade=diagnostic_args.get("shade", "beauty"),
                light=diagnostic_args.get("light"),
                crop=crop,
                res_pct=args.get("res_pct"),
            )
        except BlenderError as e:
            return _text(str(e), is_error=True)
        # The caption is the point: a visual channel with no text measured WORSE than no
        # extra channel at all. It travels in the same text block as the readouts.
        cap = (
            f"frame {r['frame']} · {r.get('caption', '')}" + f"\nsettings: pass={r.get('pass')} "
            f"shade={r.get('effective_shade') or r.get('shade')} "
            f"light={r.get('light')} crop={r.get('crop')} res_pct={r.get('res_pct')}" + _warn_suffix(r)
        )
        if r.get("pass") != "light_coverage":
            cap += await _black_frame_note(r)
        return _image(r["image_path"], cap, feedback_groups=feedback_policy["groups"])

    return render_frame, inspect_view, render_pass
