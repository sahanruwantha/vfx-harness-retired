"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import anyio
from claude_agent_sdk import create_sdk_mcp_server, tool
from PIL import Image, ImageDraw

from ..escalate import ask as _ask
from ..script_map import find_lines as _find_lines, outline as _outline
from .session import BlenderError, BlenderSession

SERVER_NAME = "blender"

# Renders are downscaled to JPEG before base64 so a single tool-result message stays
# well under the SDK's stdio buffer (a detailed 960px PNG base64s to >1MB and crashes
# it). 1024px JPEG q85 ≈ 150-250KB base64 — plenty to judge look, far cheaper in tokens.
_MAX_W = 1024
_JPEG_Q = 85


def _text(s: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], **({"is_error": True} if is_error else {})}


def _load(path: str) -> Image.Image:
    im = Image.open(path).convert("RGB")
    if im.width > _MAX_W:
        im = im.resize((_MAX_W, round(im.height * _MAX_W / im.width)))
    return im


def _b64(im: Image.Image) -> str:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=_JPEG_Q)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _stats(im: Image.Image) -> str:
    """Objective exposure readout so the agent stops eyeballing blowout/whiteout."""
    px = list(im.convert("L").getdata())
    n = len(px) or 1
    total = clipped = black = 0
    for p in px:
        total += p
        if p >= 250:
            clipped += 1
        elif p <= 4:
            black += 1
    line = (f"exposure: mean {total / n:.0f}/255 · clipped(blown) {100 * clipped / n:.0f}% · "
            f"black {100 * black / n:.0f}%")
    if clipped / n > 0.12:
        line += "  ⚠ highlights BLOWN — lower emission/light strength or exposure"
    if black / n > 0.85:
        line += "  ⚠ frame almost entirely black — add light/emission or open exposure"
    return line


def _region_metrics(im: Image.Image) -> dict:
    """Perceptual metrics per horizontal band (top/mid/bottom thirds) + halation.
    Look-agnostic: measures image properties (structure, halation), not content."""
    g = im.convert("L")
    w, h = g.size
    px = list(g.getdata())
    bands = {}
    for name, (y0, y1) in (("top", (0, h // 3)), ("mid", (h // 3, 2 * h // 3)),
                           ("bot", (2 * h // 3, h))):
        vals = [px[y * w + x] for y in range(y0, y1, 2) for x in range(0, w, 2)]
        n = len(vals) or 1
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        bands[name] = (mean, var ** 0.5)
    # halation: how much area glows dimmer around the hottest pixels (bloom spread).
    bright = sum(1 for p in px if p >= 240)
    halo = sum(1 for p in px if 120 <= p < 240)
    halation = round(halo / bright, 1) if bright else 0.0
    return {"bands": bands, "halation": halation}


def _metrics_line(im: Image.Image, ref: Image.Image | None = None) -> str:
    """`structure` = local stdev per band (fog wall = LOW; wispy/structured = HIGH).
    `halation` = glow-area : hot-core ratio (hard dots = LOW; bloomy = HIGH).
    With a ref, report deltas so tuning becomes numeric convergence, not guessing."""
    mm = _region_metrics(im)
    parts = []
    for band in ("top", "mid", "bot"):
        mean, sd = mm["bands"][band]
        parts.append(f"{band} μ{mean:.0f}/σ{sd:.0f}")
    line = f"structure: {' · '.join(parts)} · halation {mm['halation']}"
    if ref is not None:
        rm = _region_metrics(ref)
        deltas = []
        for band in ("top", "mid", "bot"):
            sd, rsd = mm["bands"][band][1], rm["bands"][band][1]
            if rsd > 4 and sd < rsd * 0.45:
                deltas.append(f"{band} σ{sd:.0f} vs ref σ{rsd:.0f} → needs ~{rsd / max(sd, 1):.1f}× more structure")
        if rm["halation"] > 1 and mm["halation"] < rm["halation"] * 0.45:
            deltas.append(f"halation {mm['halation']} vs ref {rm['halation']} → crank bloom")
        if deltas:
            line += "\nref gap: " + "; ".join(deltas)
    return line


def _image(path: str, caption: str) -> dict:
    im = _load(path)
    return {"content": [
        {"type": "text", "text": f"{caption}\n{_stats(im)}\n{_metrics_line(im)}"},
        {"type": "image", "data": _b64(im), "mimeType": "image/jpeg"},
    ]}


# Metrics are computed at ONE fixed size for every comparison, whatever `scale` the
# render was made at. Previously both images were forced to height 512 AFTER the render
# had already been shrunk by `scale`, so the two travelled different resampling paths:
# the reference (always the full 1920x960 plate) was downscaled 0.53x and stayed sharp,
# while a render at the default scale=0.4 was 768x384 and got UPSCALED 1.33x. Detail and
# structure therefore read as systematically softer than they were, and the builder chased
# sharpness it already had. Worse, two calls at different scales were not comparable to
# each other, yet the builder varies scale freely between them — in one layer it compared
# the same frame at 0.6 and then at 1.0 while editing a node in between, making the
# reading uninterpretable. (This is the same artifact that produced a "detail 46% high"
# finding I had to retract; it was in the tool, not just the analysis.)
# 320, NOT 512, and the number matters. The smallest scale the builder uses (0.35 of a
# 1920x960 shot) renders 336px tall, so a 512 metric height would still UPSCALE the render
# while the full-res reference downscaled — the very asymmetry this is fixing, just moved.
# Measuring below every accepted render height means neither image is ever upscaled.
_METRIC_H = 320
_DISPLAY_H = 512

# (frame, ref) -> the (mode, scale) it was last measured at, so a change is announced.
_LAST_COMPARE: dict = {}


def _to_metric_size(im: Image.Image) -> Image.Image:
    """One canonical geometry for measurement, independent of render scale.

    Downscale only. Upscaling invents no detail but redistributes it, which is what made
    a render read softer than the reference it was being compared against.
    """
    w = max(1, round(im.width * _METRIC_H / im.height))
    return im.resize((w, _METRIC_H), Image.LANCZOS)


def _to_display_size(im: Image.Image) -> Image.Image:
    w = max(1, round(im.width * _DISPLAY_H / im.height))
    return im.resize((w, _DISPLAY_H), Image.LANCZOS)


def _compare_image(cand_path: str, ref_path: Path, caption: str) -> dict:
    cand_raw = Image.open(cand_path).convert("RGB")
    ref_raw = Image.open(ref_path).convert("RGB")

    # MEASURE on identically-resampled copies…
    cand_m, ref_m = _to_metric_size(cand_raw), _to_metric_size(ref_raw)
    text = f"{caption}\n{_stats(cand_m)}\n{_metrics_line(cand_m, ref_m)}"

    # …and state the SIGNED GAP, not just the two numbers. The builder was reading its own
    # absolute values ("exposure: mean 22/255") and having to remember or re-derive the
    # reference's, so it searched instead of solving: across one layer f440's mean went
    # 22 → 74 → 44, overshooting and correcting, ~10 compare calls in 19 minutes. The full
    # signed, banded delta already existed in metrics.compare() and was used by the
    # acceptance stage — the tool the builder actually calls dozens of times per layer
    # simply never called it.
    try:
        from ..metrics import compare as _mcompare, look_vector as _lv
        deltas = _mcompare(_lv(cand_path), _lv(str(ref_path)))
        if deltas:
            text += ("\ngap vs reference (signed — fix the sign, not just the number):\n"
                     + "\n".join(f"  {d}" for d in deltas[:6]))
            if len(deltas) > 6:
                text += f"\n  … and {len(deltas) - 6} smaller gap(s)"
        else:
            text += "\ngap vs reference: every tracked metric is within tolerance"
    except Exception as e:
        # Never break a comparison over the extra readout — but say it is missing, or a
        # builder silently loses its only objective signal and nobody can tell.
        text += f"\n(signed gap unavailable: {type(e).__name__}: {str(e)[:70]})"
    if abs(cand_raw.width / max(cand_raw.height, 1)
           - ref_raw.width / max(ref_raw.height, 1)) > 0.02:
        # Different aspect means the metric bands are not describing the same regions;
        # say so rather than reporting a confident number about mismatched frames.
        text += (f"\n⚠ aspect mismatch: render {cand_raw.width}x{cand_raw.height} vs "
                 f"reference {ref_raw.width}x{ref_raw.height} — band metrics compare "
                 f"different parts of the frame")

    if cand_raw.height < _METRIC_H:
        text += (f"\n⚠ render is only {cand_raw.height}px tall — below the {_METRIC_H}px "
                 f"measurement height, so it had to be upscaled and detail metrics read "
                 f"soft. Re-render at a higher scale before trusting them.")

    # …and build the side-by-side from the ORIGINALS, purely for looking at. Display
    # resizing is separate on purpose: it must never feed back into the numbers.
    cand_d, ref_d = _to_display_size(cand_raw), _to_display_size(ref_raw)
    sheet = Image.new("RGB", (cand_d.width + ref_d.width, _DISPLAY_H), (18, 18, 22))
    sheet.paste(cand_d, (0, 0)); sheet.paste(ref_d, (cand_d.width, 0))
    d = ImageDraw.Draw(sheet)
    d.text((6, 6), "YOURS", fill=(255, 255, 0))
    d.text((cand_d.width + 6, 6), "REFERENCE", fill=(0, 255, 255))
    return {"content": [
        {"type": "text", "text": text},
        {"type": "image", "data": _b64(sheet), "mimeType": "image/jpeg"},
    ]}


def build_blender_tools(session: BlenderSession, assets_dir: str | Path | None = None,
                        shot_dir: str | Path | None = None, layer_id: str | None = None):
    """Wire the warm session as SDK tools. `assets_dir` enables `import_asset`;
    `shot_dir` enables `compare_frame` to resolve reference paths (e.g. refs/…)."""
    assets_dir = Path(assets_dir) if assets_dir else None
    shot_dir = Path(shot_dir) if shot_dir else None

    async def _call(cmd, **args):
        return await anyio.to_thread.run_sync(lambda: session.call(cmd, **args))

    @tool(
        "run_bpy",
        "Execute Python (with `bpy` in scope) against the live scene — the hands. "
        "Model, shade, key, set render config. Set `RESULT` to return JSON data; "
        "print()s are captured. Returns TIMING + scene-delta so you can feel cost. "
        "Pre-injected helpers (use them, don't hand-roll slow loops): "
        "bvfx_scatter_emissive(count,...) for light/greeble carpets (ONE instanced "
        "object, fast for 1000s); bvfx_volume(center,size,density,color,...) for a "
        "bounded volumetric domain (clouds/nebula/fog); bvfx_volumetric_world(...) for "
        "a tinted haze sky; bvfx_glare_bloom(...) for EEVEE-Next bloom; "
        "bvfx_emission(name,color,strength). To DEBUG a material/world, use inspect_nodes "
        "instead of rendering repeatedly to guess.",
        {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
    )
    async def run_bpy(args):
        try:
            r = await _call("run", code=args["script"])
        except BlenderError as e:
            return _text(str(e), is_error=True)
        out = r.get("stdout", "")
        res = r.get("result")
        el, oa, va = r.get("elapsed_s"), r.get("objects_added"), r.get("verts_added")
        sc = r.get("scene", {})
        body = (out + (f"\nRESULT: {res}" if res is not None else "")).strip() or "ok"
        meta = (f"\n⏱ {el}s · +{oa} objects · +{va} verts · "
                f"scene now {sc.get('objects', '?')} objs / {sc.get('tris', '?')} tris")
        warn = ""
        if isinstance(el, (int, float)) and el > 20:
            warn += (f"\n⚠ that took {el}s — too slow. Never create repeated elements in a "
                     f"Python loop; use bvfx_scatter_emissive / instancing / bmesh.")
        if isinstance(oa, int) and oa > 200:
            warn += (f"\n⚠ +{oa} objects — collapse to ONE instanced mesh "
                     f"(bvfx_scatter_emissive) instead of per-object creation.")
        return _text(body + meta + warn)

    @tool(
        "inspect_scene",
        "Read the scene as text (Tier-1, free, no render): objects+transforms+modifiers"
        "+particle systems, materials, world, and render settings. Use this to verify "
        "structure before spending a render.",
        {"type": "object",
         "properties": {"section": {"type": "string", "enum": ["all", "objects", "materials", "world", "render"]}},
         "required": []},
    )
    async def inspect_scene(args):
        r = await _call("inspect", section=args.get("section", "all"))
        return _text(r["text"])

    @tool(
        "inspect_nodes",
        "Dump a NODE GRAPH as text — every node's type, its unlinked input socket "
        "values, and the links. target = 'compositor' (the bloom/glare graph — 5.x has "
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
        "The Graph-Editor read (Tier-1, free): every F-curve on an object as "
        "frame→value pairs with interpolation. Use to verify easing/timing without rendering.",
        {"type": "object", "properties": {"object": {"type": "string"}}, "required": ["object"]},
    )
    async def list_keyframes(args):
        try:
            r = await _call("keyframes", object=args["object"])
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(r["text"])

    @tool(
        "render_frame",
        "See one frame. mode='solid'/'wire' = fast Workbench (~0.1s, composition/"
        "silhouette); mode='draft' = fast low-sample EEVEE (~0.5s, quick look checks — "
        "use this while iterating); mode='eevee' = full-quality look (~1-3s, for final "
        "judging). scale is 0..1 (default 0.4). Returns the image + an EXPOSURE readout "
        "(mean/clipped/black) so you can catch blowout objectively.",
        {"type": "object",
         "properties": {
             "frame": {"type": "integer"},
             "mode": {"type": "string", "enum": ["solid", "wire", "draft", "eevee"]},
             "scale": {"type": "number", "description": "0..1 res scale, default 0.4"},
         },
         "required": ["frame"]},
    )
    async def render_frame(args):
        try:
            r = await _call("render", frame=int(args["frame"]),
                            mode=args.get("mode", "eevee"), scale=float(args.get("scale", 0.4)))
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _image(r["image_path"], f"frame {r['frame']} ({r['mode']})")

    @tool(
        "compare_frame",
        "Render a frame and place it SIDE-BY-SIDE with a reference image (YOURS | "
        "REFERENCE) so you judge the match directly — composition, palette, atmosphere. "
        "Pass `reference` as a path under the shot folder (e.g. refs/M1_green.jpg). Same "
        "mode/scale as render_frame; returns one image + your render's exposure readout.",
        {"type": "object",
         "properties": {
             "frame": {"type": "integer"},
             "reference": {"type": "string", "description": "e.g. refs/M1_green.jpg"},
             "mode": {"type": "string", "enum": ["solid", "wire", "draft", "eevee"]},
             "scale": {"type": "number"},
         },
         "required": ["frame", "reference"]},
    )
    async def compare_frame(args):
        ref = args["reference"]
        ref_path = (shot_dir / ref) if (shot_dir and not Path(ref).is_absolute()) else Path(ref)
        if not ref_path.is_file():
            return _text(f"reference not found: {ref_path}", is_error=True)
        mode = args.get("mode", "eevee")
        scale = float(args.get("scale", 0.4))
        try:
            r = await _call("render", frame=int(args["frame"]), mode=mode, scale=scale)
        except BlenderError as e:
            return _text(str(e), is_error=True)
        out = _compare_image(r["image_path"], ref_path,
                             f"frame {r['frame']} ({mode})  vs  {ref}")
        # Structure and exposure are now scale-invariant, but halation is not and cannot
        # be: a 672px render genuinely holds less high-frequency detail than a 1920px one.
        # So changing mode or scale between two readings of the SAME frame moves the
        # numbers for reasons that have nothing to do with the edit in between — which is
        # exactly what happened when one layer compared f45 at 0.6, then at 1.0, having
        # altered a shader node in between, and could not tell which change moved what.
        key = (int(args["frame"]), str(ref))
        prev = _LAST_COMPARE.get(key)
        _LAST_COMPARE[key] = (mode, scale)
        if prev and prev != (mode, scale):
            out["content"][0]["text"] += (
                f"\n⚠ last comparison of f{key[0]} used mode={prev[0]} scale={prev[1]}, "
                f"this one mode={mode} scale={scale}. Halation is not comparable across "
                f"that change — hold mode and scale FIXED while iterating, or you cannot "
                f"tell your edit from the render settings.")
        return out

    @tool(
        "render_frames",
        "See several frames at once (the milestone contact sheet) — pass the frame list, "
        "e.g. [1,24,32,48]. Same mode/scale semantics as render_frame. Returns one image "
        "per frame so you can compare timing/colour against the refs.",
        {"type": "object",
         "properties": {
             "frames": {"type": "array", "items": {"type": "integer"}},
             "mode": {"type": "string", "enum": ["solid", "wire", "eevee"]},
             "scale": {"type": "number"},
         },
         "required": ["frames"]},
    )
    async def render_frames(args):
        mode = args.get("mode", "eevee")
        scale = float(args.get("scale", 0.35))
        blocks: list[dict] = []
        for f in args["frames"]:
            try:
                r = await _call("render", frame=int(f), mode=mode, scale=scale)
                im = _load(r["image_path"])
                blocks.append({"type": "text",
                               "text": f"frame {r['frame']} ({mode})\n{_stats(im)}"})
                blocks.append({"type": "image", "data": _b64(im),
                               "mimeType": "image/jpeg"})
            except BlenderError as e:
                blocks.append({"type": "text", "text": f"frame {f}: ERROR {e}"})
        return {"content": blocks}

    @tool(
        "import_asset",
        "Import a committed, normalized 3D asset into the live scene. The mesh was "
        "frozen upstream by the asset stage (assets/<name>/model.glb), base at the "
        "origin and scaled to a known height — deterministic. Returns the names of the "
        "objects it added, which you then shade/emit, place and animate. Prefer this "
        "over modelling a bespoke hero prop by hand.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    async def import_asset(args):
        if assets_dir is None:
            return _text("import_asset unavailable: no assets_dir configured", is_error=True)
        name = args["name"]
        glb = assets_dir / name / "model.glb"
        meta = assets_dir / name / "meta.json"
        if not glb.is_file():
            avail = sorted(p.name for p in assets_dir.iterdir()
                           if (p / "model.glb").is_file()) if assets_dir.is_dir() else []
            hint = f" Available assets: {avail}" if avail else " No assets are prepared."
            return _text(f"no asset {name!r}.{hint}", is_error=True)
        script = (
            "import bpy\n"
            "before = set(bpy.data.objects.keys())\n"
            f"bpy.ops.import_scene.gltf(filepath={str(glb)!r})\n"
            "RESULT = [n for n in bpy.data.objects.keys() if n not in before]\n"
        )
        try:
            r = await _call("run", code=script)
        except BlenderError as e:
            return _text(str(e), is_error=True)
        dims = json.loads(meta.read_text()).get("bbox_dims") if meta.is_file() else None
        return _text(f"imported {name}: objects={r.get('result')} bbox_dims={dims}")

    tools = [run_bpy, inspect_scene, inspect_nodes, list_keyframes, render_frame, render_frames]
    if shot_dir is not None:
        tools.append(compare_frame)
    if assets_dir is not None:
        tools.append(import_asset)
    @tool(
        "script_map",
        "STRUCTURAL INDEX of a build script — functions, sections, and which lines create "
        "or reference each named object/material. Use this INSTEAD of reading the whole "
        "file: a 536-line script maps to ~380 tokens. Then Read just that span and Edit "
        "it. Never rewrite a script you only need to change in one place.",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "required": ["path"]},
    )
    async def script_map(args):
        p = Path(args["path"])
        if not p.is_absolute() and shot_dir:
            p = shot_dir / p
        return {"content": [{"type": "text", "text": _outline(p)}]}

    @tool(
        "find_in_script",
        "Locate a name/value inside a build script with surrounding context, so you can "
        "Read the right span instead of the whole file. Give the object name, material "
        "name, or literal you want to change.",
        {"type": "object",
         "properties": {"path": {"type": "string"}, "needle": {"type": "string"}},
         "required": ["path", "needle"]},
    )
    async def find_in_script(args):
        p = Path(args["path"])
        if not p.is_absolute() and shot_dir:
            p = shot_dir / p
        return {"content": [{"type": "text",
                             "text": _find_lines(p, args["needle"])}]}

    @tool(
        "ask_supervisor",
        "Raise a question you cannot resolve from the brief, the stills or the plan — an "
        "ambiguity, a contradiction, or a judgement call that is genuinely the client's. "
        "This does NOT block: state the assumption you will proceed on and keep building. "
        "Use it INSTEAD of guessing silently, and instead of tuning against a target you "
        "are not sure about. Do not use it for things you could measure or spike.",
        {"type": "object",
         "properties": {"question": {"type": "string"},
                        "assumption": {"type": "string"},
                        "why_it_matters": {"type": "string"}},
         "required": ["question", "assumption"]},
    )
    async def ask_supervisor(args):
        if not shot_dir:
            return {"content": [{"type": "text", "text": "no shot folder — cannot ask"}]}
        qid = _ask(shot_dir, layer=layer_id or "?", question=args["question"],
                   assumption=args["assumption"],
                   why_it_matters=args.get("why_it_matters", ""))
        return {"content": [{"type": "text", "text":
                f"Recorded as Q{qid}. Continue on your stated assumption: "
                f"{args['assumption']}"}]}

    @tool(
        "worklist",
        "Your build checklist ON DISK — it survives context compaction and process death, "
        "which your memory does not. Call with items=[...] to (re)write it, or done=[...] "
        "to tick things off; call with neither to read it back. Write it once at the start "
        "from your layer's tickets, then tick as you go. Layer G was killed at turn 121 with "
        "the work half-finished and no record of what remained.",
        {"type": "object",
         "properties": {"items": {"type": "array", "items": {"type": "string"}},
                        "done": {"type": "array", "items": {"type": "string"}},
                        "note": {"type": "string"}},
         "required": []},
    )
    async def worklist(args):
        if not shot_dir:
            return {"content": [{"type": "text", "text": "no shot folder"}]}
        wl = shot_dir / "logs" / f"worklist_{layer_id or 'layer'}.json"
        wl.parent.mkdir(parents=True, exist_ok=True)
        state = json.loads(wl.read_text()) if wl.is_file() else {"items": [], "done": [], "notes": []}
        if args.get("items"):
            state["items"] = list(args["items"])
        for d in args.get("done", []):
            if d not in state["done"]:
                state["done"].append(d)
        if args.get("note"):
            state["notes"].append(args["note"])
        wl.write_text(json.dumps(state, indent=2))
        left = [i for i in state["items"] if i not in state["done"]]
        body = ("\n".join(f"  [x] {i}" for i in state["items"] if i in state["done"]) + "\n" +
                "\n".join(f"  [ ] {i}" for i in left)).strip()
        return {"content": [{"type": "text", "text":
                f"{len(state['done'])}/{len(state['items'])} done, {len(left)} left\n{body}"}]}

    # ask_supervisor is deliberately PLAN-ONLY: a layer that discovers an
    # ambiguity is already building on earlier layers' answer to it.
    tools = tools + [script_map, find_in_script, worklist]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tools)
    names = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    return server, names
