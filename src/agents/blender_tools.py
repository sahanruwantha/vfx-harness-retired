"""In-process SDK tools that hand a live Blender to the scene agent — its desk.

Until now the scene builder was blind: it wrote one bpy script, the harness ran it, and the agent
only ever saw a canned text digest fed back. These tools flip that — they bind a live
:class:`~scene.bridge.BlenderBridge` and expose it *to the agent* so it can work the way a real
technical artist does: probe the live API before writing (``introspect``), build incrementally and
read the traceback itself (``run_bpy``), LOOK at what it made (``render`` returns the image + its
stats), and check the actual scene state (``scene_graph``).

``create_blender_server(bridge)`` returns a server to hand to ``ClaudeAgentOptions(mcp_servers=…)``;
the agent then sees the tools as ``mcp__blender__run_bpy`` etc. One bridge is one Blender process
and one socket — NOT concurrency-safe — so a server backs a single agent desk at a time; the caller
owns the bridge lifecycle (matching every other bridge consumer).

The single biggest win is ``introspect``: it turns the old "guess bpy → crash → self-heal from the
traceback" loop into "verify the call exists → write it right", which is exactly where Blender 5.x's
moved APIs (slotted actions, the compositor node group, EEVEE ids) kept burning iterations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from scene.bridge import BridgeError
from scene.diagnostics import scene_digest

# The agent's own quick looks are cheap and frequent — low samples, preview resolution. This is the
# desk monitor, not the final render (the orchestrator renders the deliverable at full quality).
_PREVIEW_RESOLUTION = (640, 360)
_PREVIEW_SAMPLES = 16

RUN_BPY_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "Blender Python (bpy) to run in the live session. State persists across calls "
            "in this desk. Set `result = {...}` to return values. Do NOT call render operators — use the "
            "render tool. Returns stdout and either the result or the full traceback.",
        }
    },
    "required": ["code"],
}

INTROSPECT_SCHEMA = {
    "type": "object",
    "properties": {
        "expr": {
            "type": "string",
            "description": "A Python expression evaluated against live `bpy`, e.g. "
            "'bpy.context.scene.eevee', 'bpy.types.Object.bl_rna.properties.keys()', "
            "'[s.name for s in node.inputs]'. Returns its type, repr, public members (dir) and docstring "
            "— use it to VERIFY an attribute/socket/API exists before you write code against it.",
        }
    },
    "required": ["expr"],
}

RENDER_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "Optional short filename stem for this look, e.g. 'lit-pass'."},
        "frame": {"type": "integer", "description": "Optional frame to set before rendering (for animations)."},
        "samples": {"type": "integer", "description": f"Optional sample count (default {_PREVIEW_SAMPLES}, higher = slower/cleaner)."},
        "engine": {"type": "string", "description": "Optional 'CYCLES' or 'EEVEE'. Default is fast EEVEE for iterating; "
                   "render a 'CYCLES' check to see the true path-traced look (materials, volumetrics, lighting) that "
                   "the final and the critic are judged in — do this before you submit."},
    },
    "required": [],
}

VIEWPORT_SNAPSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "Optional short filename stem, e.g. 'blocking'."},
    },
    "required": [],
}

SCENE_GRAPH_SCHEMA = {"type": "object", "properties": {}, "required": []}

ACQUIRE_ASSET_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "The object to acquire, e.g. 'a tall green neon "
                        "data tower' or 'an antique brass typewriter'. A real 3D mesh is pulled from the "
                        "library or GENERATED (AI image → image-to-3D) and imported into the scene."},
    },
    "required": ["description"],
}
LIST_ASSETS_SCHEMA = {"type": "object", "properties": {}, "required": []}

# Tool names the desk sees when an asset library is wired in.
ASSET_TOOL_NAMES = ["mcp__blender__acquire_asset", "mcp__blender__list_assets"]


def _text(payload: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": payload}]}
    if is_error:
        result["is_error"] = True
    return result


# Runs INSIDE Blender via run_python: eval the expression and describe the value structurally, so a
# raised exception (attribute missing) comes back as data, not a transport error — the agent learns
# "that API doesn't exist" the same way it learns any other fact.
_INTROSPECT_CODE = """
import bpy  # noqa: F401  (available to the eval)
import traceback
__expr = {expr!r}
try:
    __val = eval(__expr, {{"bpy": bpy}})
    __members = [n for n in dir(__val) if not n.startswith("__")]
    result = {{
        "ok": True,
        "type": type(__val).__name__,
        "repr": repr(__val)[:1000],
        "members": __members[:400],
        "member_count": len(__members),
        "doc": (getattr(__val, "__doc__", "") or "").strip()[:400],
    }}
except Exception as __exc:  # attribute/name errors are the POINT — report them as the answer
    result = {{
        "ok": False,
        "error": "".join(traceback.format_exception_only(type(__exc), __exc)).strip(),
    }}
"""


def _format_introspect(expr: str, info: dict[str, Any]) -> str:
    if not info.get("ok"):
        return f"introspect {expr!r} → does NOT resolve: {info.get('error', 'unknown error')}"
    lines = [
        f"introspect {expr!r} → {info.get('type')}",
        f"repr: {info.get('repr')}",
    ]
    doc = info.get("doc")
    if doc:
        lines.append(f"doc: {doc}")
    members = info.get("members") or []
    shown = ", ".join(members)
    total = info.get("member_count", len(members))
    lines.append(f"members ({total}): {shown}" + (" …" if total > len(members) else ""))
    return "\n".join(lines)


def _format_run(res: dict[str, Any]) -> dict[str, Any]:
    """A run_python response → tool result: surface stdout + result, or the traceback as an error."""
    stdout = (res.get("stdout") or "").strip()
    if res.get("ok"):
        parts = []
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        result = res.get("result")
        if result not in (None, {}):
            parts.append(f"result: {result}")
        return _text("\n".join(parts) if parts else "ran OK (no output).")
    error = str(res.get("error", "")).strip() or "unknown error"
    body = (f"stdout:\n{stdout}\n\n" if stdout else "") + f"ERROR:\n{error}"
    return _text(body, is_error=True)


def build_blender_tools(
    bridge,
    *,
    preview_resolution: tuple[int, int] = _PREVIEW_RESOLUTION,
    preview_samples: int = _PREVIEW_SAMPLES,
    render_subdir: str = "agent",
    default_render_engine: str = "BLENDER_EEVEE_NEXT",
    asset_library=None,
    acquire=None,
    look_budget: int | None = None,
    on_run_bpy: Callable[[str], None] | None = None,
) -> list:
    """The Blender tools bound to a live ``bridge``, as a list of ``SdkMcpTool``.

    Split out from :func:`create_blender_server` so the handlers can be unit-tested directly (the
    server object hides them). Renders the agent takes for its own eyes are written under
    ``bridge.render_dir/<render_subdir>``; blocking bridge calls run in a thread so the agent's event
    loop is never stalled. ``on_run_bpy``, if given, is called with each code string the agent runs —
    the desk uses it to keep an ordered log of the bpy actually executed (repro + trace).

    ``look_budget`` caps how many VISUAL looks (``viewport_snapshot`` + ``render`` combined) the desk
    may take this session. For an LLM every look costs a model turn regardless of how fast the pixels
    render, so the win is looking *sparingly* — build in coherent passes, sense structure with the free
    ``scene_graph``, and spend looks at checkpoints. Past the budget the visual tools return guidance
    instead of an image (``scene_graph`` and ``introspect`` stay free). ``None`` = unlimited.
    """
    out_dir = Path(bridge.render_dir) / render_subdir
    counter = {"n": 0}  # sequential filename stem
    looks = {"n": 0}  # visual looks spent (viewport_snapshot + render) — capped by look_budget

    def _look_denied() -> dict[str, Any] | None:
        """If the visual-look budget is spent, the message to return instead of rendering."""
        if look_budget is not None and looks["n"] >= look_budget:
            return _text(
                f"Visual-look budget spent ({looks['n']}/{look_budget}). Don't render again — verify "
                "what you built with scene_graph() (free), then finish with your 'SUBMIT:' line. The "
                "dailies critic renders and judges the result; you don't need another look to hand off.",
                is_error=True,
            )
        return None

    @tool(
        "run_bpy",
        "Run Blender Python in the live scene (state persists across calls). Set `result = {...}` to "
        "return values. Returns stdout and either the result or the full traceback so you can debug it "
        "yourself. Do NOT call render operators — use `render`.",
        RUN_BPY_SCHEMA,
    )
    async def run_bpy(args: dict[str, Any]) -> dict[str, Any]:
        code = str(args.get("code", "")).strip()
        if not code:
            return _text("`code` is required.", is_error=True)
        if on_run_bpy is not None:
            on_run_bpy(code)
        try:
            res = await asyncio.to_thread(bridge.run_python, code)
        except BridgeError as exc:
            return _text(f"bridge error: {exc}", is_error=True)
        return _format_run(res)

    @tool(
        "introspect",
        "Evaluate an expression against live `bpy` and report its type, repr, public members and doc. "
        "Use it to VERIFY an attribute, socket, or API exists BEFORE writing code against it — Blender "
        "5.x moved many APIs, so check instead of guessing.",
        INTROSPECT_SCHEMA,
    )
    async def introspect(args: dict[str, Any]) -> dict[str, Any]:
        expr = str(args.get("expr", "")).strip()
        if not expr:
            return _text("`expr` is required.", is_error=True)
        code = _INTROSPECT_CODE.format(expr=expr)
        try:
            res = await asyncio.to_thread(bridge.run_python, code)
        except BridgeError as exc:
            return _text(f"bridge error: {exc}", is_error=True)
        if not res.get("ok"):  # the introspection code itself failed to run — a real problem
            return _text(f"introspect failed to run: {res.get('error', 'unknown')}", is_error=True)
        return _text(_format_introspect(expr, res.get("result") or {}))

    @tool(
        "render",
        "CHECKPOINT render + LOOK — a real preview (EEVEE, or CYCLES to see the path-traced final) with "
        "brightness/colour stats. Costs a visual look from your budget, so render at milestones (a pass "
        "complete, a Cycles check before SUBMIT), NOT after every edit — sense structure with scene_graph.",
        RENDER_SCHEMA,
    )
    async def render(args: dict[str, Any]) -> dict[str, Any]:
        denied = _look_denied()
        if denied is not None:
            return denied
        out_dir.mkdir(parents=True, exist_ok=True)
        frame = args.get("frame")
        if frame is not None:
            try:
                await asyncio.to_thread(bridge.run_python, f"import bpy\nbpy.context.scene.frame_set(int({int(frame)}))\nresult={{'f':{int(frame)}}}")
            except BridgeError as exc:
                return _text(f"bridge error setting frame: {exc}", is_error=True)
        counter["n"] += 1
        looks["n"] += 1
        label = str(args.get("label") or "look").replace("/", "_")
        path = out_dir / f"{counter['n']:03d}_{label}.jpg"
        raw_engine = str(args.get("engine") or default_render_engine).upper()
        engine = "CYCLES" if raw_engine == "CYCLES" else ("BLENDER_EEVEE_NEXT" if raw_engine in ("EEVEE", "BLENDER_EEVEE_NEXT", "") else raw_engine)
        is_cycles = engine == "CYCLES"
        samples = int(args.get("samples") or (48 if is_cycles else preview_samples))
        try:
            shot = await asyncio.to_thread(
                bridge.render, path=str(path), format="JPEG", resolution=list(preview_resolution),
                samples=samples, engine=engine, denoise=is_cycles, return_base64=True,
            )
            stats = await asyncio.to_thread(bridge.image_stats, path=str(path))
        except BridgeError as exc:
            return _text(f"render failed: {exc}", is_error=True)
        b64 = shot.get("image_b64", "")
        tag = "CYCLES (path-traced, as the final/critic see it)" if is_cycles else "EEVEE preview"
        summary = (
            f"Rendered {path.name} at {preview_resolution[0]}x{preview_resolution[1]}, {samples} samples, {tag}. "
            f"luma_mean={stats.get('luma_mean')}, luma_std={stats.get('luma_std')}, mean_rgb={stats.get('mean_rgb')}. "
            + ("WARNING: near-black — check emission/exposure/camera, not geometry." if float(stats.get("luma_mean", 1.0)) < 0.03 else "Look at it below.")
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": summary}]
        if b64:
            content.append({"type": "image", "data": b64, "mimeType": "image/jpeg"})
        return {"content": content}

    @tool(
        "viewport_snapshot",
        "A FAST, flat solid-shaded look (Workbench, ~instant) to check blocking, composition, camera "
        "framing and silhouette — cheaper than render, but still costs a visual look. Use it for quick "
        "'does the layout read?' glances; use render for the true lit/material/Cycles look at checkpoints.",
        VIEWPORT_SNAPSHOT_SCHEMA,
    )
    async def viewport_snapshot(args: dict[str, Any]) -> dict[str, Any]:
        denied = _look_denied()
        if denied is not None:
            return denied
        out_dir.mkdir(parents=True, exist_ok=True)
        counter["n"] += 1
        looks["n"] += 1
        label = str(args.get("label") or "snapshot").replace("/", "_")
        path = out_dir / f"{counter['n']:03d}_{label}.jpg"
        try:  # Workbench: unlit solid shading, 1 sample — a viewport grab, not a render
            shot = await asyncio.to_thread(
                bridge.render, path=str(path), format="JPEG", resolution=list(preview_resolution),
                samples=1, engine="BLENDER_WORKBENCH", denoise=False, return_base64=True,
            )
        except BridgeError as exc:
            return _text(f"snapshot failed: {exc}. (Use render if Workbench is unavailable.)", is_error=True)
        b64 = shot.get("image_b64", "")
        content: list[dict[str, Any]] = [{"type": "text", "text": (
            f"Snapshot {path.name} — flat solid-shaded viewport (no lighting/materials), for blocking and "
            "composition only. Judge staging/silhouette/framing here; render for the lit look."
        )}]
        if b64:
            content.append({"type": "image", "data": b64, "mimeType": "image/jpeg"})
        return {"content": content}

    @tool(
        "scene_graph",
        "Report the live scene's ground truth: object/mesh/light/camera counts, active camera, total "
        "light energy, engine. FREE and unlimited — this is your primary 'viewport' for structure; use "
        "it liberally to confirm what actually got built (operators can silently fail) without spending a look.",
        SCENE_GRAPH_SCHEMA,
    )
    async def scene_graph(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            graph = await asyncio.to_thread(bridge.get_scene_graph)
        except BridgeError as exc:
            return _text(f"bridge error: {exc}", is_error=True)
        return _text(scene_digest(graph))

    tools = [run_bpy, introspect, render, viewport_snapshot, scene_graph]
    if asset_library is None:
        return tools

    _acquire = acquire
    if _acquire is None:
        from scene.assets import acquire_asset as _acquire  # lazy: assets pulls higgsfield/tripo

    @tool(
        "acquire_asset",
        "Pull or GENERATE a real 3D asset for a described object and import it into the scene. Use "
        "this for the hero subject instead of building it from primitives — you then position, scale "
        "and light it. Returns the imported object name(s); on failure, build from geometry instead.",
        ACQUIRE_ASSET_SCHEMA,
    )
    async def acquire_asset(args: dict[str, Any]) -> dict[str, Any]:
        description = str(args.get("description", "")).strip()
        if not description:
            return _text("`description` is required.", is_error=True)
        try:
            asset = await _acquire(description=description, library=asset_library)
        except Exception as exc:  # generation is external (network/credits) — degrade, don't crash
            return _text(f"asset generation failed for {description!r}: {exc}. Build it from geometry instead.", is_error=True)
        try:
            res = await asyncio.to_thread(bridge.import_glb, path=asset.path)
        except BridgeError as exc:
            return _text(f"asset generated ({asset.path}) but import failed: {exc}. Build it from geometry instead.", is_error=True)
        return _text(f"Acquired + imported {description!r} → {res}. It is in the scene now — position, scale "
                     "and light it to frame; you own the shot around it.")

    @tool(
        "list_assets",
        "List the assets already in the library you can import (by description) before generating a new one.",
        LIST_ASSETS_SCHEMA,
    )
    async def list_assets(_args: dict[str, Any]) -> dict[str, Any]:
        items = asset_library.list()
        if not items:
            return _text("Asset library is empty — use acquire_asset(description) to generate one.")
        return _text("Available assets:\n" + "\n".join(f"- {a.description} [{a.kind}]" for a in items))

    return [*tools, acquire_asset, list_assets]


def create_blender_server(
    bridge,
    *,
    preview_resolution: tuple[int, int] = _PREVIEW_RESOLUTION,
    preview_samples: int = _PREVIEW_SAMPLES,
    render_subdir: str = "agent",
    default_render_engine: str = "BLENDER_EEVEE_NEXT",
    asset_library=None,
    look_budget: int | None = None,
    on_run_bpy: Callable[[str], None] | None = None,
):
    """Build the ``blender`` SDK tool server bound to a live ``bridge``, ready for
    ``ClaudeAgentOptions(mcp_servers={"blender": create_blender_server(bridge)})``. The agent sees the
    tools as ``mcp__blender__run_bpy`` etc. One bridge backs one agent desk (not concurrency-safe).
    ``default_render_engine`` is what the desk's render tool uses when it doesn't name one — set it to
    the engine the stage is JUDGED in so the desk's eyes match the critic (avoids EEVEE↔Cycles drift).
    ``asset_library`` (optional) adds the acquire_asset/list_assets tools so the desk can source real
    assets instead of building from primitives."""
    tools = build_blender_tools(
        bridge, preview_resolution=preview_resolution, preview_samples=preview_samples,
        render_subdir=render_subdir, default_render_engine=default_render_engine,
        asset_library=asset_library, look_budget=look_budget, on_run_bpy=on_run_bpy,
    )
    return create_sdk_mcp_server("blender", tools=tools)
