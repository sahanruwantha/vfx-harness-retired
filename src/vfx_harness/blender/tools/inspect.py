"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import json

from claude_agent_sdk import tool

from vfx_harness.blender.session import BlenderError
from vfx_harness.blender.tools.payment import _text
from vfx_harness.blender.tools.reports import _object_or_role_error


def register_inspect(
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
        "unit_scope",
        "Compiled scope of the ACTIVE work unit: mutation roles/controls/dresses/"
        "script_spans, bound contract ids (kind, roles, frame), claims, judge frames, "
        "and every bvfx_* helper injected into run_bpy. Same card as kickoff and "
        "CLAUDE.md. Query this; do not inspect.getsource or guess a sibling unit.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def unit_scope_tool(_args):
        if not unit_scope:
            return _text(
                "no active work unit — unit_scope is compiled per unit",
                is_error=True,
            )
        return _text(json.dumps(unit_scope, indent=2, sort_keys=True))

    @tool(
        "inspect_scene",
        "Read the scene as text (Tier-1, free, no render): objects+transforms+modifiers"
        "+particle systems, materials with object-slot consumers, world, lights, color "
        "management, compositor, and "
        "render settings. section='lights' reports energy/color/visibility/location so "
        "light setup never needs a read-only run_bpy probe; section='cameras' reports "
        "freshly evaluated world pose, forward vector, lens, and sensor. Every call "
        "re-evaluates the current frame; pass frame= to select another frame. Each object line includes "
        "local/evaluated world location, dimensions, evaluated world_bbox_min/"
        "world_bbox_max, visibility, role, and owner. Pass "
        "role= to filter by semantic bvfx_role "
        "(literal namespace or fnmatch). Use this to verify structure before spending a render.",
        {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["all", "objects", "materials", "world", "render", "lights", "cameras"],
                },
                "frame": {"type": "integer", "description": "optional evaluation frame"},
                "role": {
                    "type": "string",
                    "description": (
                        "optional bvfx_role selector (literal namespace or fnmatch); miss names present roles and names"
                    ),
                },
            },
            "required": [],
        },
    )
    async def inspect_scene(args):
        try:
            r = await _call(
                "inspect",
                section=args.get("section", "all"),
                **({"frame": int(args["frame"])} if args.get("frame") is not None else {}),
                **({"role": args["role"]} if args.get("role") else {}),
            )
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(r["text"])

    @tool(
        "inspect_nodes",
        "Dump a NODE GRAPH as text — every node's type, unlinked input values, all "
        "output socket names, and links. target = 'compositor' (the bloom/glare graph — 5.x has "
        "NO scene.node_tree, it's scene.compositing_node_group), 'world', a material "
        "name, or an object name (its active material). Use this to DEBUG shaders/"
        "compositor directly instead of rendering over and over to guess.",
        {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
    )
    async def inspect_nodes(args):
        try:
            r = await _call("nodes", target=args.get("target", "world"))
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(r["text"])

    @tool(
        "list_keyframes",
        "The Graph-Editor read (Tier-1, free): every F-curve on an object AND its "
        "data-block (Light/Camera energy lives on data.energy) as frame→value pairs "
        "with interpolation. Address by role= (bvfx_role); a shared role lists every "
        "host (including hide_render). object= is the display-name fallback for one "
        "host. Use to verify easing/timing without rendering. A keyframe_schedule "
        "sample path energy matches data.energy; a custom ['energy'] is a different path.",
        {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "bvfx_role selector (preferred)"},
                "object": {"type": "string", "description": "display name; use role= instead"},
            },
            "required": [],
        },
    )
    async def list_keyframes(args):
        selector = _object_or_role_error(args, "list_keyframes")
        if selector:
            return _text(selector, is_error=True)
        payload = {k: v for k, v in args.items() if k in ("role", "object") and v}
        try:
            r = await _call("keyframes", **payload)
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(r["text"])

    return unit_scope_tool, inspect_scene, inspect_nodes, list_keyframes
