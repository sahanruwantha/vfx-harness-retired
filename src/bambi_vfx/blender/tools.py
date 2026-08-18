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
from ..script_map import find_lines as _find_lines
from ..script_map import outline as _outline
from .session import BlenderError, BlenderSession

SERVER_NAME = "blender"

# Renders are downscaled to JPEG before base64 so a single tool-result message stays
# well under the SDK's stdio buffer (a detailed 960px PNG base64s to >1MB and crashes
# it).
#
# 1024 was chosen against the stdio limit alone and is too small for the judgement it
# feeds. Claude tokenises images in 28x28 px patches: fine discrimination is measured at
# 0.00 accuracy for features spanning <=2 patches (56px) and 1.00 at >=4 patches (112px).
# At 1024px wide, this shot's hero facade piers span ~6px — a fifth of ONE patch, i.e.
# no representational slot in the encoder at all. 2048 doubles every feature's patch
# count and a 2048x1024 JPEG q85 base64s to ~300-450KB, comfortably inside the buffer.
_MAX_W = 2048
# The side-by-side sheet is two images wide, so it needs its own ceiling or the same
# height that is right for ONE frame doubles the payload.
_SHEET_MAX_W = 3072
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


def _warn_suffix(r: dict) -> str:
    """Scene-state warnings from the worker, attached to the render they describe.

    These are conditions where the render looks entirely plausible while silently doing
    the opposite of what was asked — a sun inside a world volume being the one that cost
    five layer-2 attempts. There is nothing in the picture to prompt suspicion, so the
    warning has to travel with it.
    """
    w = r.get("warnings") or []
    return "".join(f"\n⚠ {x}" for x in w)


def subtract_png(a_path: str, b_path: str, dest: str) -> dict:
    """|A − B| as an image, plus how much the two frames actually differ.

    Client-side: Blender's bundled Python has no Pillow, and two PNGs already on disk
    do not need a scene to be subtracted.

    `did_work` answers the question a side-by-side cannot: a near-black diff means the
    edit changed nothing. The 1.5/255 threshold is above PNG quantisation and well below
    any visible change.
    """
    from PIL import ImageChops, ImageStat
    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    resized = a.size != b.size
    if resized:
        b = b.resize(a.size, Image.LANCZOS)
    diff = ImageChops.difference(a, b)
    st = ImageStat.Stat(diff.convert("L"))
    mean = st.mean[0]
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    diff.save(dest)
    return {
        "image_path": dest,
        "mean_delta": round(mean, 2),
        "max_delta": int(st.extrema[0][1]),
        "did_work": mean > 1.5,
        # A resample introduces differences of its own, so a diff across two sizes
        # cannot answer "did my edit do anything". Say so rather than returning a
        # number that looks like the same measurement.
        "resized": resized,
    }


def _check_report(kind: str, r: dict) -> str:
    """A check's answer as text, with the ISSUES first.

    Deliberately not a JSON dump. The failure this class of check exists to catch is
    "the number was measured, written into a comment, and verified by nothing" — so
    the finding has to read as a finding, not as a payload to be re-derived.
    """
    issues = r.get("issues") or []
    head = f"check {kind}: " + ("PASS ✅" if r.get("ok") else "ISSUES ✗")
    lines = [head]
    for i in issues:
        lines.append(f"  ✗ {i}")
    if kind == "visibility":
        lines.append(f"  visible fraction {r.get('visible_fraction')} "
                     f"({r.get('hits')} hit · {r.get('occluded')} occluded · "
                     f"{r.get('missed')} missed of {r.get('samples')} rays)")
    elif kind == "framing":
        for fr in r.get("frames", []):
            lines.append(f"  f{fr.get('frame')}: bbox {fr.get('bbox')} · "
                         f"w {fr.get('width')} h {fr.get('height')} · "
                         f"centre {fr.get('centre')} · on-screen {fr.get('on_screen')}")
    elif kind == "motion":
        lines.append(f"  max speed {r.get('max_speed')} u/f (f{r.get('peak_speed_frame')}) · "
                     f"max |accel| {r.get('max_accel')} u/f^2 · "
                     f"max |jerk| {r.get('max_jerk')} u/f^3 · "
                     f"{'one unbroken move' if r.get('unbroken') else 'BROKEN move'}")
    elif kind == "mesh":
        c = r.get("counts", {})
        lines.append(f"  {c.get('verts')} verts · {c.get('edges')} edges · "
                     f"{c.get('faces')} faces · {c.get('islands')} island(s)")
        lines.append(f"  non-manifold {c.get('nonmanifold_edges')} · loose "
                     f"{c.get('loose_verts')} · degenerate {c.get('degenerate_faces')} · "
                     f"n-gons {c.get('ngons')} · poles {c.get('poles')}")
    elif kind == "scale":
        lines.append(f"  scale {r.get('scale')} · dimensions {r.get('dimensions')} · "
                     f"units {r.get('unit_system')}")
    elif kind == "passes":
        lines.append(f"  {r.get('channels')} channels · NaN {r.get('nan')} · "
                     f"Inf {r.get('inf')} · negative {r.get('negative')} · "
                     f"passes {r.get('passes_enabled')}")
    elif kind in ("bbox", "subject_bbox"):
        lines.append(f"  bbox {r.get('bbox')} (0..1, origin BOTTOM-LEFT) · "
                     f"w {r.get('width')} h {r.get('height')} · centre {r.get('centre')}")
        lines.append("  hand this straight to render_pass(crop=…, res_pct=400) — an "
                     "oracle crop measures far better than a guessed one.")
    if not issues and kind in ("visibility", "framing", "motion", "mesh", "scale"):
        lines.append("  nothing to fix on this check — the numbers above are the record.")
    return "\n".join(lines)


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
# DISPLAY is a SEPARATE question from METRIC and the two must not be reconciled.
#
# _METRIC_H exists to stop resampling asymmetry: measure below every accepted render
# height so neither image is ever upscaled. Lower is SAFER there.
#
# _DISPLAY_H is what a vision model gets to look at, where lower is strictly worse.
# Claude tokenises in 28x28 px patches, so a feature must clear ~4 patches (112px) to be
# discriminable and is at chance below ~2 (56px). At 512px tall, this shot's hero tower
# facade piers land near 6px — 0.21 of one patch, five times below the floor. The render
# was already paid for at full resolution; shipping it shrunk is throwing away the only
# signal the judge has. 1024 is the height at which a 2:1 frame still passes Claude's
# high-resolution tier unresized (~2400 tokens, ~$0.012 a call).
#
# The reasoning for METRIC leaked into DISPLAY once already. They answer different
# questions; do not "unify" them.
_DISPLAY_H = 1024

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
        from ..metrics import compare as _mcompare
        from ..metrics import look_pair as _lp
        deltas = _mcompare(*_lp(cand_path, str(ref_path)))
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
    # Two frames side by side hit the stdio ceiling at half the height one frame does.
    # Shrink only if the join is genuinely too wide, and shrink the SHEET — never the
    # copies the numbers above were computed from.
    if sheet.width > _SHEET_MAX_W:
        h = max(1, round(sheet.height * _SHEET_MAX_W / sheet.width))
        sheet = sheet.resize((_SHEET_MAX_W, h), Image.LANCZOS)
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
        cap = f"frame {r['frame']} ({r['mode']})" + _warn_suffix(r)
        return _image(r["image_path"], cap)

    @tool(
        "render_pass",
        "Render one frame as a DIAGNOSTIC rather than a beauty shot, so you can see the "
        "thing you are actually being judged on. `pass` isolates a render pass — use "
        "'diffuse_direct' to see MODELLING BY LIGHT with emission removed (a render "
        "setting, not something to squint past), 'emit' to see only self-lit surfaces, "
        "'shadow'/'ao'/'normal'/'depth'/'crypto' for the rest. `shade` overrides "
        "materials: 'clay' for form, 'silhouette' for outline, 'matcap:<name>' for a "
        "Workbench diagnostic. `light='<LightObject>'` (comma list allowed) renders with "
        "ONLY those light objects and hides the rest, so you can see what one lamp "
        "actually contributes. `crop` is "
        "[x0,y0,x1,y1] in 0..1 from the BOTTOM-LEFT and is a true optical zoom, so pair "
        "it with res_pct (e.g. 400) to see fine detail at real resolution instead of "
        "upscaling a thumbnail. Every mode returns a caption saying what to look for.",
        {"type": "object",
         "properties": {
             "frame": {"type": "integer"},
             "pass": {"type": "string",
                      "enum": ["beauty", "diffuse_direct", "emit", "shadow", "ao",
                               "normal", "depth", "crypto"]},
             "shade": {"type": "string",
                       "description": "beauty | clay | silhouette | matcap:<name>"},
             "light": {"type": "string",
                       "description": "light OBJECT name(s), comma-separated; all other "
                                      "lights are hidden for this render"},
             "crop": {"type": "array", "items": {"type": "number"},
                      "description": "[x0,y0,x1,y1] in 0..1, origin BOTTOM-LEFT"},
             "res_pct": {"type": "integer",
                         "description": "resolution percentage; >100 zooms a crop"},
             "scale": {"type": "number"},
         },
         "required": ["frame"]},
    )
    async def render_pass(args):
        crop = args.get("crop")
        bad_crop = crop is not None and (
            len(crop) != 4
            or not all(0.0 <= float(v) <= 1.0 for v in crop)
            or not (crop[0] < crop[2] and crop[1] < crop[3])
        )
        if bad_crop:
            return _text("crop must be [x0,y0,x1,y1] in 0..1 with x0<x1 and y0<y1 "
                         "(origin BOTTOM-LEFT)", is_error=True)
        try:
            r = await _call("render", frame=int(args["frame"]),
                            mode="eevee",
                            scale=float(args.get("scale", 0.5)),
                            **{"pass": args.get("pass", "beauty")},
                            shade=args.get("shade", "beauty"),
                            light=args.get("light"),
                            crop=crop,
                            res_pct=args.get("res_pct"))
        except BlenderError as e:
            return _text(str(e), is_error=True)
        # The caption is the point: a visual channel with no text measured WORSE than no
        # extra channel at all. It travels in the same text block as the readouts.
        cap = (f"frame {r['frame']} · {r.get('caption', '')}"
               + f"\nsettings: pass={r.get('pass')} shade={r.get('shade')} "
                 f"light={r.get('light')} crop={r.get('crop')} res_pct={r.get('res_pct')}"
               + _warn_suffix(r))
        return _image(r["image_path"], cap)

    @tool(
        "check_scene",
        "JUDGMENT-FREE checks on the scene itself — no critic, no cost, no render (except "
        "`passes`). This is the whole class of defect a beauty render CANNOT show: "
        "kind='visibility' ray-casts the camera to the object (a hero behind a wall looks "
        "fine until you look for it), 'framing' gives the NDC bbox/width/centre via "
        "world_to_camera_view, 'motion' gives max speed/accel/jerk and whether the move is "
        "unbroken, 'mesh' counts non-manifold edges, loose verts, n-gons, poles and "
        "disconnected islands, 'scale' checks dimensions and that scale is applied, "
        "'passes' checks the render buffer for NaN/Inf/negative pixels, 'bbox' returns the "
        "oracle crop box to hand to render_pass. Use these to VERIFY a claim you would "
        "otherwise write in a comment.",
        {"type": "object",
         "properties": {
             "kind": {"type": "string",
                      "enum": ["visibility", "framing", "motion", "mesh", "scale",
                               "passes", "bbox"]},
             "object": {"type": "string"},
             "frame": {"type": "integer"},
             "frames": {"type": "array", "items": {"type": "integer"}},
             "samples": {"type": "integer"},
             "scale": {"type": "number"},
         },
         "required": ["kind"]},
    )
    async def check_scene(args):
        kind = args["kind"]
        payload = {k: v for k, v in args.items() if k != "kind" and v is not None}
        try:
            r = await _call("check", kind=kind, **payload)
        except BlenderError as e:
            return _text(str(e), is_error=True)
        return _text(_check_report(kind, r))

    @tool(
        "diff_frames",
        "Subtract one render from another and SEE the difference. Give two image paths "
        "from earlier render calls. A near-black diff means nothing changed — which is the "
        "answer to 'did my edit do anything' that a side-by-side cannot give you. Reports "
        "mean and max delta alongside the image.",
        {"type": "object",
         "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
         "required": ["a", "b"]},
    )
    async def diff_frames(args):
        a, b = Path(args["a"]), Path(args["b"])
        for p in (a, b):
            if not p.is_file():
                return _text(f"{p} does not exist — pass the image paths from two "
                             f"earlier render calls", is_error=True)
        dest = a.with_name(f"diff_{a.stem}_vs_{b.stem}.png")
        try:
            r = await anyio.to_thread.run_sync(
                lambda: subtract_png(str(a), str(b), str(dest)))
        except OSError as e:
            return _text(f"could not subtract those images: {e}", is_error=True)
        verdict = ("the two renders DIFFER" if r["did_work"] else
                   "the two renders are essentially IDENTICAL — whatever you changed had "
                   "no visible effect at this frame")
        cap = (f"|A − B| · mean delta {r['mean_delta']}/255 · max {r['max_delta']}/255 — "
               f"{verdict}. Bright regions are where the two renders disagree.")
        if r["resized"]:
            cap += (" ⚠ the two frames were DIFFERENT SIZES, so one was resampled and "
                    "part of this difference is the resample, not your edit. Re-render "
                    "both at the same scale before trusting it.")
        return _image(r["image_path"], cap)

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
                             f"frame {r['frame']} ({mode})  vs  {ref}" + _warn_suffix(r))
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
                               "text": f"frame {r['frame']} ({mode})\n{_stats(im)}"
                                       + _warn_suffix(r)})
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

    tools = [run_bpy, inspect_scene, inspect_nodes, list_keyframes, render_frame,
             render_frames, render_pass, check_scene, diff_frames]
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

    @tool(
        "measure_regions",
        "Measure named RECTANGLES of a rendered frame and compare them. Use this to "
        "prove a structural claim numerically instead of eyeballing it — 'the outer "
        "window strips are brighter than the recessed core', 'the sign LETTERS are "
        "brighter than the panel behind them'. Regions are in NORMALISED frame "
        "coordinates [x0,y0,x1,y1], each 0..1, origin TOP-LEFT. Returns mean/σ/max/lit%% "
        "per region plus every pairwise brightness ratio, so you never slice pixels "
        "yourself.",
        {"type": "object",
         "properties": {
             "frame": {"type": "integer"},
             "regions": {
                 "type": "object",
                 "description": 'name -> [x0,y0,x1,y1] in 0..1, e.g. '
                                '{"left_strip":[0.42,0.2,0.46,0.8], '
                                '"core":[0.47,0.2,0.53,0.8]}',
                 "additionalProperties": {"type": "array", "items": {"type": "number"}}},
             "mode": {"type": "string", "enum": ["solid", "wire", "draft", "eevee"]},
             "scale": {"type": "number"},
         },
         "required": ["frame", "regions"]},
    )
    async def measure_regions(args):
        """Exists because the builder was writing its own measurement rig every layer.

        Asked to prove 'the outer quarters are brighter than the central half', it had no
        tool for it, so it hand-rolled `bpy.ops.render.render(write_still=True)` plus numpy
        pixel slicing INSIDE run_bpy — which produced two distinct crashes in one layer
        (a zero-size reduction and a 28-vs-31 concatenation), bypassed the session's render
        path so the metrics hook never saw those frames, and mutated
        scene.render.resolution_* on the live scene, where an exception between set and
        restore leaves the deliverable rendering at the wrong size.
        """
        regions = args.get("regions") or {}
        if not regions:
            return _text("no regions given", is_error=True)
        bad = [n for n, r in regions.items()
               if not (isinstance(r, list) and len(r) == 4
                       and all(isinstance(v, (int, float)) and 0.0 <= v <= 1.0 for v in r)
                       and r[0] < r[2] and r[1] < r[3])]
        if bad:
            return _text(f"regions must be [x0,y0,x1,y1] in 0..1 with x0<x2 and y0<y1; "
                         f"bad: {bad}", is_error=True)
        try:
            r = await _call("render", frame=int(args["frame"]),
                            mode=args.get("mode", "eevee"),
                            scale=float(args.get("scale", 0.5)))
        except BlenderError as e:
            return _text(str(e), is_error=True)

        im = Image.open(r["image_path"]).convert("RGB")
        g = im.convert("L")
        W, H = g.size
        out = {}
        for name, (x0, y0, x1, y1) in regions.items():
            box = (max(0, int(x0 * W)), max(0, int(y0 * H)),
                   min(W, max(int(x1 * W), int(x0 * W) + 1)),
                   min(H, max(int(y1 * H), int(y0 * H) + 1)))
            px = list(g.crop(box).getdata())
            n = len(px) or 1
            mean = sum(px) / n
            sd = (sum((p - mean) ** 2 for p in px) / n) ** 0.5
            out[name] = {"mean": round(mean, 1), "sd": round(sd, 1), "max": max(px),
                         "lit_pct": round(100 * sum(1 for p in px if p >= 120) / n, 1),
                         "px": n}
        lines = [f"frame {r['frame']} ({r['mode']}) — {W}x{H}"]
        for name, v in out.items():
            lines.append(f"  {name:<16} mean {v['mean']:>5} · σ {v['sd']:>5} · "
                         f"max {v['max']:>3} · lit {v['lit_pct']:>5}% · {v['px']}px")
        names = list(out)
        if len(names) > 1:
            lines.append("  ratios (a/b by mean brightness):")
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    ma, mb = out[a]["mean"], out[b]["mean"]
                    rel = ma / mb if mb > 0.5 else float("inf")
                    verdict = ("BRIGHTER" if ma > mb * 1.05 else
                               "DARKER" if mb > ma * 1.05 else "about EQUAL")
                    lines.append(f"    {a} is {verdict} than {b}  "
                                 f"({ma} vs {mb}, ×{rel:.2f})")
        for name, v in out.items():
            if v["px"] < 64:
                lines.append(f"  ⚠ {name} is only {v['px']}px — too small to measure "
                             f"reliably; widen the region or raise scale")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    # ask_supervisor is deliberately PLAN-ONLY: a layer that discovers an
    # ambiguity is already building on earlier layers' answer to it.
    tools = [*tools, script_map, find_in_script, worklist, measure_regions]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tools)
    names = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    return server, names
