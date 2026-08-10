"""Scene builder: the agent-driven **desk** that builds ONE cinematic shot in a live Blender.

:func:`build_scene` spawns an LLM technical artist that holds live Blender tools
(``introspect``/``run_bpy``/``render``/``scene_graph`` from :mod:`agents.blender_tools`) and works
the way a real technical artist does: probe the API before writing, build incrementally in a
persistent session, LOOK at its own renders, fix, and submit when the shot reads. The scene it
leaves live IS the deliverable; an independent critic reviews it afterwards (in :mod:`scene.harness`
for stills, :mod:`scene.animate` for motion) and its notes come back into the SAME session for a
revision (``resume``).

The hard-won Blender constraints (EEVEE watts, emissive cubes for distant lights, no compositor,
5.x moved APIs) live in :data:`CONSTRAINTS`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from _sdk import MAX_BUFFER_SIZE, VISUAL_EFFORT, VISUAL_MODEL

# NOTE: agents.blender_tools is imported lazily inside build_scene. It pulls scene.bridge, whose
# package __init__ imports scene.animate → agents.scene_builder; a top-level import here would close
# that cycle. Deferring it to call time keeps this module import-safe from within the scene package.

MODEL = VISUAL_MODEL
EFFORT = VISUAL_EFFORT

DESK_MAX_TURNS = 40  # a real build is many probe→build→render→fix steps; give it room
WRAP_UP_MAX_TURNS = 2
DESK_BUDGET_TOKENS = 120_000
BLENDER_TOOLS = [
    "mcp__blender__introspect",
    "mcp__blender__run_bpy",
    "mcp__blender__render",
    "mcp__blender__viewport_snapshot",
    "mcp__blender__scene_graph",
]

ReferenceImage = tuple[str, str]  # (media_type, base64)

RECOVERABLE_SUBTYPES = {"error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"}

# The hard constraints — common failure modes discovered by hand — shared by both prompts.
CONSTRAINTS = """Hard constraints (common failure modes — when unsure an API exists, introspect it, do not guess):
- EEVEE light energy is in Watts: area/spot lights over a large scene need thousands to tens of
  thousands. But watch exposure — emission STRENGTH above ~8 clips to white; keep glows coloured.
- A distant field of lights (city lights, stars) MUST be small emissive CUBES, never flat planes:
  from a low camera a horizontal plane goes edge-on and disappears.
- Volumetric fog: a large cube whose material feeds a Principled Volume into the Volume output, low
  density (~0.004-0.008); set bpy.context.scene.eevee.volumetric_end past the far geometry.
- Principled BSDF inputs are named: "Base Color","Metallic","Roughness","Emission Color","Emission Strength".
- Do NOT use the compositor. Blender 5.x removed scene.node_tree / old use_nodes; a mis-wired
  compositor renders the frame BLACK. Get glow/bloom from EMISSIVE materials — never a Glare node.
- A BLACK render is almost never missing geometry: it's emission/exposure too low, a camera framing
  nothing, or a leftover compositor. Raise emission and key-light energy, verify the camera points at
  the lit subject, use the 'Standard' or 'AgX' view transform at exposure 0 — then re-render.
- Blender 5.x moved APIs (verify with introspect, don't trust memory): Action.fcurves is GONE
  (slotted actions) — use obj.keyframe_insert(data_path, frame=f). EEVEE's engine id is 'BLENDER_EEVEE'.
- Compose deliberately: match the reference's camera height/angle, subject scale-in-frame, palette,
  and where light and darkness sit. Prefer a dark scene with concentrated glow over a flat wash."""

DESK_PROMPT = (
    """You are an expert Blender technical artist working at your DESK on ONE cinematic shot for a
faceless documentary, to match a REFERENCE image and a BRIEF. Blender 5.2.

Your preview renders (the render tool) are fast EEVEE, but the FINAL deliverable is path-traced in
CYCLES with a compositor bloom/grade pass. So build for path tracing: physically-plausible PBR
materials and real light energy that hold up in Cycles — do NOT rely on EEVEE-only tricks, and do
NOT bake glow into materials to fake bloom (comp adds the bloom; over-bright emission just clips).

Work the way a real studio does — VIEWPORT while you build, DAILIES at the checkpoint — not like a
REPL that re-renders after every line. You are judged by an independent critic (dailies) AFTER you
submit; you do not need to perfect the frame yourself, you need to build a complete, coherent pass.

Your senses, cheapest first — reach for the free ones constantly and spend looks sparingly:
- scene_graph(): FREE and unlimited — object/light/camera counts, positions, bounds, energy, engine.
  This is your VIEWPORT for structure. Use it liberally to confirm what got built (operators fail
  silently) without spending a look. Most "did that work?" questions are answered here, not by a render.
- introspect(expr): FREE — verify an attribute/socket/API exists on live bpy BEFORE writing code
  against it. Blender 5.x moved many APIs — check, don't trust memory.
- run_bpy(code): run bpy in the live scene; state PERSISTS. Build a WHOLE coherent pass per call or
  few (all the geometry; then all the materials; then the full light rig) — not one nudge at a time.
  Read any traceback and fix it yourself.
- viewport_snapshot(): a fast flat solid-shaded grab — costs a LOOK. For a quick "does the blocking/
  composition/silhouette read?" glance. Cheaper than render, but still a look, so don't spam it.
- render(): the CHECKPOINT — the true lit/material preview, and a CYCLES check (engine='CYCLES')
  before you SUBMIT because the path-traced final + critic differ from EEVEE. Costs a look.

You have a small BUDGET of visual looks (viewport_snapshot + render combined) — you may be told the
number. Treat it like real render time: build the whole pass by reasoning + scene_graph, then spend a
look or two at the milestones (blocking done; lit; a Cycles check before submit). If you run out of
looks, verify with scene_graph and submit — dailies will render and judge it.

Do NOT dump one giant script and hope, and do NOT render after every edit — build in coherent passes
and check structure symbolically. The scene starts EMPTY for a fresh shot (already prepared for you);
on a revision you are editing the scene you already built — do not clear it. The live scene you leave
IS the deliverable — do not save files, and do not call render operators except via the tools.

You may be given PROJECT CONTEXT (decisions and scene facts from earlier stages/rounds) and LESSONS
(known Blender 5.x gotchas). READ them first: build on the prior decisions, do not contradict them,
reuse the named objects/values, and do not waste a pass rediscovering a listed gotcha.

NARRATE your decisions so they are legible — this is required:
- Begin with ONE short line "PLAN: …" naming, in order, the parts of the shot you will build.
- Immediately BEFORE every run_bpy, write one plain sentence saying what you are about to build or
  change and WHY (what in the last render or the reference prompted it). Keep it to a sentence.

When the shot genuinely matches the reference and brief — composition, camera, subject, lighting,
palette — stop and write ONE short line beginning "SUBMIT:" summarising what you built and the key
scene facts the next department needs (object names, camera, key values, what you left for later).
If you discovered a Blender 5.x gotcha worth remembering, add a separate line "LESSON: <the gotcha>".
An independent critic then reviews it; if it returns notes, you will revise this same live scene.

"""
    + CONSTRAINTS
)

WRAP_UP_PROMPT = (
    "You've reached your build limit, so your tools have been removed. In one or two sentences, state "
    "what you built in the live scene and whether the shot is ready for review. Do not claim anything "
    "you did not actually build."
)

# Prepare a fresh, empty scene before the desk starts a new shot: clear objects AND reset the render
# pipeline state that leaks across a persistent Blender session (compositor group, use_nodes,
# transparent film, view transform/exposure), then set EEVEE. The orchestrator will own this in
# Phase 2; for now the desk prepares its own clean slate on a fresh (non-resume) build.
CLEAN_SLATE_CODE = """
import bpy
for _o in list(bpy.data.objects):
    bpy.data.objects.remove(_o, do_unlink=True)
scene = bpy.context.scene
for _attr, _val in (("use_nodes", False),):
    try: setattr(scene, _attr, _val)
    except Exception: pass
try: scene.compositing_node_group = None
except Exception: pass
try: scene.render.film_transparent = False
except Exception: pass
try:
    scene.view_settings.view_transform = 'AgX'; scene.view_settings.exposure = 0.0; scene.view_settings.look = 'None'
except Exception: pass
try: scene.render.engine = 'BLENDER_EEVEE'
except Exception: pass
result = {"clean_slate": True}
"""


# ======================================================================================
# Shared helpers
# ======================================================================================


def animation_block(frames: int) -> str:
    """The extra instruction that turns the builder from a still into an animator."""
    return (
        f"ANIMATION MODE — this is a {frames}-frame MOVING shot, not a still:\n"
        f"- set bpy.context.scene.frame_start = 1 and bpy.context.scene.frame_end = {frames}\n"
        "- KEYFRAME the motion: for every moving property — camera location and rotation_euler, "
        "object transforms, material/world values — set the value at a frame and call "
        "keyframe_insert(<data_path>, frame=f) across frames 1.." + str(frames) + ".\n"
        "- design the motion ARC across the timeline (start pose → middle → end pose); the shot is "
        "judged on start, middle and end frames, so make each read.\n"
        "- BLOCK THE WHOLE ARC FIRST: rough in the entire shot end to end — every key pose/world and "
        "the camera move across ALL frames — and confirm start/middle/end read, BEFORE you polish the "
        "look of any single moment. Do NOT perfect the first pose before the motion exists; the "
        "transition and continuity between poses are the hardest part and need the most passes.\n"
        "- do NOT call any render operator — the harness renders the sequence."
    )


def _image_block(media_type: str, data: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


async def _stream_one(blocks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "user", "session_id": "", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


# ======================================================================================
# Agent-driven desk (the new path)
# ======================================================================================


@dataclass(frozen=True)
class DeskResult:
    """The outcome of a desk session. The live Blender scene is the real deliverable; this carries
    the metadata the orchestrator needs to render it, judge it, and resume for a revision."""

    submitted: bool  # True when the agent finished on its own (vs running out of turns/budget)
    note: str  # the agent's final message (its SUBMIT line, or its wrap-up on a limit)
    session_id: str  # resume the SAME desk with dailies notes
    outcome: Literal["complete", "wrapped_up"]
    stop_reason: str
    turns: int
    cost_usd: float
    bpy_log: tuple[str, ...] = ()  # the bpy the agent actually ran, in order (repro + trace)


def desk_content(
    *,
    brief: str,
    reference_images: list[ReferenceImage],
    animation_frames: int | None = None,
    existing_scene: bool = False,
    context: str | None = None,
    lessons: str | None = None,
    assets_available: bool = False,
    look_budget: int | None = None,
) -> list[dict[str, Any]]:
    """The opening message for a fresh desk build: the brief, the reference board, the submit rule.

    ``existing_scene`` (a later department stage opening the prior stage's .blend): the scene is NOT
    empty; tell the desk to inspect it and build only on top, never clear it. ``context`` (per-shot
    turnover notes) and ``lessons`` (durable Blender gotchas) are injected so a fresh session starts
    informed instead of re-deriving."""
    blocks: list[dict[str, Any]] = [{"type": "text", "text": f"BRIEF — build this shot at your desk:\n{brief.strip()}"}]
    if existing_scene:
        blocks.append({
            "type": "text",
            "text": "NOTE: the scene is NOT empty — it already holds the previous department's work. "
            "Run scene_graph() first to see what exists, then build ONLY your layer on top of it. Do "
            "NOT clear the scene or rebuild what is already there.",
        })
    if assets_available:
        blocks.append({
            "type": "text",
            "text": "ASSETS: you have acquire_asset(description) and list_assets(). For the HERO SUBJECT, "
            "prefer acquiring a real 3D mesh (a library asset or a generated one) over building it from "
            "primitives — then position, scale and light it. Build from geometry only for simple/"
            "parametric elements or if acquisition fails.",
        })
    if lessons and lessons.strip():
        blocks.append({"type": "text", "text": lessons.strip()})
    if context and context.strip():
        blocks.append({"type": "text", "text": context.strip()})
    if animation_frames:
        blocks.append({"type": "text", "text": animation_block(animation_frames)})
    if look_budget is not None:
        blocks.append({
            "type": "text",
            "text": f"LOOK BUDGET: {look_budget} visual look(s) this pass (viewport_snapshot + render "
            "combined). scene_graph and introspect are free and unlimited — lean on them. Build the "
            "whole pass, then spend your look(s) at the milestone(s); a Cycles render is the one to keep "
            "for before you SUBMIT.",
        })
    blocks.append({"type": "text", "text": f"REFERENCE — match this look ({len(reference_images)} image(s)):"})
    for media_type, data in reference_images:
        blocks.append(_image_block(media_type, data))
    blocks.append({
        "type": "text",
        "text": "Build the whole pass in coherent chunks, checking structure with scene_graph as you go; "
        "spend a look at the milestone(s). Finish with a line starting 'SUBMIT:' when it reads.",
    })
    return blocks


def revision_content(feedback: str) -> list[dict[str, Any]]:
    """The message for a revision pass: dailies notes into the same live scene."""
    return [{
        "type": "text",
        "text": "DAILIES — an independent critic reviewed your shot. Revise the LIVE scene to address "
        "ONLY these notes; keep what already works. Make the fix in a coherent pass, spend ONE look to "
        "confirm it, then finish with a line starting 'SUBMIT:'.\n\n" + feedback.strip(),
    }]


async def _run_desk(
    prompt: Any,
    *,
    server,
    tools: list[str],
    max_turns: int,
    resume: str | None,
    task_budget_tokens: int | None,
    on_message: Callable[[object], None] | None,
) -> ResultMessage:
    options = ClaudeAgentOptions(
        max_buffer_size=MAX_BUFFER_SIZE, model=MODEL, effort=EFFORT, system_prompt=DESK_PROMPT,
        tools=tools, allowed_tools=tools, mcp_servers={"blender": server},
        max_turns=max_turns, permission_mode="dontAsk", setting_sources=[], resume=resume,
        task_budget={"total": task_budget_tokens} if task_budget_tokens is not None else None,
    )
    terminal: ResultMessage | None = None
    try:
        async for message in query(prompt=prompt, options=options):
            if on_message is not None:
                on_message(message)
            if isinstance(message, ResultMessage):
                terminal = message
    except Exception:
        if terminal is None:
            raise
    if terminal is None:
        raise RuntimeError("scene desk ended without returning a result")
    return terminal


async def build_scene(
    *,
    bridge,
    brief: str,
    reference_images: list[ReferenceImage],
    feedback: str | None = None,
    resume: str | None = None,
    animation_frames: int | None = None,
    fresh: bool = True,
    context: str | None = None,
    lessons: str | None = None,
    preview_engine: str = "BLENDER_EEVEE_NEXT",
    asset_library=None,
    look_budget: int | None = None,
    max_turns: int = DESK_MAX_TURNS,
    task_budget_tokens: int = DESK_BUDGET_TOKENS,
    on_message: Callable[[object], None] | None = None,
) -> DeskResult:
    """Build (or revise) the shot at the agent's desk against a live ``bridge``.

    Fresh build (``resume`` is None, ``fresh`` True): the scene is cleared, then the agent builds it
    from the brief + reference. Department stage (``resume`` None, ``fresh`` False): the caller has
    already ``open_blend``-ed the prior stage's scene — it is NOT cleared, and the desk is told to
    build only its layer on top. Revision (``resume`` set): the agent re-opens that session and edits
    the same live scene from the dailies ``feedback``. Either way the deliverable is the live scene;
    the returned :class:`DeskResult` carries the note, the resumable session id, and the bpy log.
    """
    from agents.blender_tools import ASSET_TOOL_NAMES, create_blender_server  # lazy: breaks the scene↔agents cycle

    bpy_log: list[str] = []
    server = create_blender_server(bridge, on_run_bpy=bpy_log.append, default_render_engine=preview_engine,
                                   asset_library=asset_library, look_budget=look_budget)
    tools = [*BLENDER_TOOLS, *(ASSET_TOOL_NAMES if asset_library is not None else [])]

    if resume is None:
        if fresh:
            bridge.run_python(CLEAN_SLATE_CODE)  # fresh shot → empty, pipeline-clean scene
        prompt: Any = _stream_one(desk_content(
            brief=brief, reference_images=reference_images, animation_frames=animation_frames,
            existing_scene=not fresh, context=context, lessons=lessons, assets_available=asset_library is not None,
            look_budget=look_budget,
        ))
    else:
        prompt = _stream_one(revision_content(feedback or "Address the critic's notes."))

    run = await _run_desk(
        prompt, server=server, tools=tools, max_turns=max_turns,
        resume=resume, task_budget_tokens=(None if resume else task_budget_tokens), on_message=on_message,
    )

    if not run.is_error:
        return DeskResult(
            submitted=True, note=run.result or "", session_id=run.session_id,
            outcome="complete", stop_reason=run.terminal_reason or "completed",
            turns=run.num_turns, cost_usd=run.total_cost_usd or 0.0, bpy_log=tuple(bpy_log),
        )

    if run.subtype not in RECOVERABLE_SUBTYPES:
        errors = "\n".join(run.errors or [])
        raise RuntimeError(f"scene desk failed: {run.subtype}{(': ' + errors) if errors else ''}")

    # Ran out of turns/budget — the scene is still live and (partly) built. Get a final note.
    wrap = await _run_desk(
        WRAP_UP_PROMPT, server=server, tools=[], max_turns=WRAP_UP_MAX_TURNS,
        resume=run.session_id, task_budget_tokens=None, on_message=on_message,
    )
    return DeskResult(
        submitted=False, note=wrap.result or "", session_id=wrap.session_id or run.session_id,
        outcome="wrapped_up", stop_reason=run.terminal_reason or run.subtype,
        turns=run.num_turns + wrap.num_turns,
        cost_usd=(run.total_cost_usd or 0.0) + (wrap.total_cost_usd or 0.0), bpy_log=tuple(bpy_log),
    )

