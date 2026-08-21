"""Phase 2 render knobs — passes, shade modes, crops, light groups.

Called from `h_render`. Every path restores the scene settings it touches.
Captions are produced here so a visual channel never ships without text
(measured −10.6pp when it does).
"""

from __future__ import annotations

import contextlib

# `bpy` is imported INSIDE the functions that touch the scene, never at module scope.
# The captions and the socket-name table are the two things most worth pinning in the
# no-Blender test suite (a wrong socket name renders a perfect copy of the beauty frame
# under a caption promising isolation), and a module-level `import bpy` would put them
# out of the suite's reach.

_PASS_FLAGS = {
    "diffuse_direct": "use_pass_diffuse_direct",
    "emit": "use_pass_emit",
    "shadow": "use_pass_shadow",
    "ao": "use_pass_ambient_occlusion",
    "normal": "use_pass_normal",
    "depth": "use_pass_z",
    "crypto": "use_pass_cryptomatte_object",
}

# The compositor socket names, MEASURED on Blender 5.2 rather than remembered. Enabling
# use_pass_diffuse_direct/use_pass_emit adds sockets named "Diffuse Direct" and
# "Emission" — not the "DiffDir"/"Emit" of older docs and older muscle memory. Ordered
# most-likely-first; every candidate is tried and a miss reports what WAS available,
# because a silently unrouted pass renders as a perfect copy of the beauty frame and the
# caption then promises an isolation that did not happen.
_PASS_SOCKETS = {
    "beauty": ("Image", "Combined"),
    "diffuse_direct": ("Diffuse Direct", "DiffDir"),
    "emit": ("Emission", "Emit"),
    "shadow": ("Shadow",),
    "ao": ("Ambient Occlusion", "AO"),
    "normal": ("Normal",),
    "depth": ("Depth", "Z"),
    "crypto": ("CryptoObject00", "CryptoObject", "Cryptomatte"),
}

_CAPTIONS = {
    "beauty": "BEAUTY render — the finished look. Judge colour, light and composition.",
    "diffuse_direct": "DIFFUSE DIRECT pass — modelling by light only. Emission must not appear.",
    "emit": "EMIT pass — only self-lit surfaces. Everything else should be black.",
    "shadow": "SHADOW pass — contact and self-shadow. Look for missing or inverted shadows.",
    "ao": "AO pass — cavity/contact darkening, no lighting.",
    "normal": "NORMAL pass — surface orientation. Texture and grade are out of scope.",
    "depth": "DEPTH pass — camera distance. Near is light, far is dark (or the reverse).",
    "crypto": "CRYPTOMATTE — object IDs. Use this to confirm isolation, not look.",
    "clay": "CLAY shade — mid-grey override. Form only; materials are disabled.",
    "silhouette": "SILHOUETTE shade — white on black. Read the outline, ignore interior.",
}


def caption_for(pass_name: str, shade: str, light: str | None, crop: list | None,
                res_pct: int | None, kept: list | None = None,
                hidden: list | None = None) -> str:
    """What to look for in this image, in words.

    Never optional. A visual channel added with no text to read it by measured WORSE
    than not adding the channel at all, so a mode without a caption is a regression.
    """
    key = shade if shade and shade != "beauty" and not shade.startswith("matcap:") else pass_name
    if shade.startswith("matcap:"):
        text = (f"MATCAP {shade.split(':', 1)[1]} — a Workbench diagnostic. "
                f"Look at the surface treatment the matcap is designed to reveal.")
    else:
        text = _CAPTIONS.get(key, f"{key} render.")
    extra = []
    if light:
        # State what was actually done, not what was requested. This used to claim a
        # light-group isolation that EEVEE never performed.
        if hidden:
            extra.append(f"lit by {', '.join(kept or [light])} ALONE — "
                         f"{len(hidden)} other light(s) hidden from the render "
                         f"({', '.join(hidden)}); emissive materials and the world "
                         f"still contribute")
        else:
            extra.append(f"lit by {', '.join(kept or [light])}, which is/are the ONLY "
                         f"light object(s) in the scene — so this is identical to the "
                         f"beauty render, not an isolation")
    if crop:
        extra.append(f"optical crop {crop} (normalized frame, origin top-left)")
    if res_pct and res_pct > 100:
        extra.append(f"resolution_percentage={res_pct} (real zoom, not an upscale)")
    if extra:
        text += " " + "; ".join(extra) + "."
    return text


def _eevee():
    import bpy
    items = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
    for cand in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        if cand in items:
            return cand
    return bpy.context.scene.render.engine


def _ensure_media_type(settings) -> None:
    """Blender 5.x: media_type must be set before file_format."""
    if hasattr(settings, "media_type"):
        with contextlib.suppress(Exception):
            settings.media_type = "IMAGE"


def _socket(rl, names: tuple[str, ...]):
    for n in names:
        sock = rl.outputs.get(n) if hasattr(rl.outputs, "get") else None
        if sock is not None:
            return sock
    for s in rl.outputs:
        if s.name in names:
            return s
    return None


def _apply_shade(sc, shade: str) -> list:
    """Returns undo thunks.

    EVERY previous value is read BEFORE anything is assigned. An earlier draft captured
    the previous world AFTER installing a placeholder, so a scene that had NO world got
    one handed back and every plain render afterwards came out 47.8/255 brighter — a
    diagnostic mode silently re-lighting the canonical render the critic scores.
    """
    import bpy
    undo = []
    vl = sc.view_layers[0]
    if shade == "beauty":
        return undo

    if shade in ("clay", "silhouette"):
        prev_override = vl.material_override      # read first
        prev_world = sc.world                     # may legitimately be None
        mat = bpy.data.materials.new("_bvfx_shade")
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        black = None
        if shade == "silhouette":
            em = nt.nodes.new("ShaderNodeEmission")
            em.inputs["Color"].default_value = (1, 1, 1, 1)
            em.inputs["Strength"].default_value = 1.0
            nt.links.new(em.outputs[0], out.inputs["Surface"])
            black = bpy.data.worlds.new("_bvfx_silhouette_bg")
            black.use_nodes = True
            bg = next((n for n in black.node_tree.nodes if n.type == "BACKGROUND"), None)
            if bg is not None:
                bg.inputs[0].default_value = (0, 0, 0, 1)
                bg.inputs[1].default_value = 0.0
            sc.world = black
        else:
            d = nt.nodes.new("ShaderNodeBsdfDiffuse")
            d.inputs["Color"].default_value = (0.18, 0.18, 0.18, 1)
            nt.links.new(d.outputs[0], out.inputs["Surface"])
        vl.material_override = mat

        def _undo_shade():
            vl.material_override = prev_override
            sc.world = prev_world                 # None restores None
            if black is not None and black.users == 0:
                bpy.data.worlds.remove(black)
            if mat.users == 0:
                bpy.data.materials.remove(mat)

        undo.append(_undo_shade)
        return undo

    if shade.startswith("matcap:"):
        name = shade.split(":", 1)[1]
        sh = sc.display.shading
        prev_engine = sc.render.engine
        prev_light = sh.light
        prev_studio = getattr(sh, "studio_light", "")
        prev_type = sh.type
        sc.render.engine = "BLENDER_WORKBENCH"
        sh.type = "SOLID"
        sh.light = "MATCAP"
        # studio_light is a filename ('check_normal+y.exr'), so accept a bare name too.
        lights = [getattr(sl, "name", "") for sl in
                  getattr(bpy.context.preferences, "studio_lights", [])]
        chosen = next((c for c in (name, name + ".exr", name + ".jpg") if c in lights),
                      None) or next((n for n in lights if name and name in n), None)
        if chosen:
            sh.studio_light = chosen

        def _undo_matcap():
            sc.render.engine = prev_engine
            sh.type = prev_type
            sh.light = prev_light
            if prev_studio:
                sh.studio_light = prev_studio

        undo.append(_undo_matcap)
        return undo

    raise ValueError(f"unknown shade {shade!r} — use beauty, clay, silhouette or "
                     f"matcap:<name>")


def isolate_lights(sc, light: str) -> tuple[list, list, list]:
    """Render with ONLY the named light object(s) contributing. Returns
    (undo thunks, kept names, hidden names).

    NOT light groups. Blender's `lightgroup` is a Cycles feature: on this pipeline's
    engine (BLENDER_EEVEE, the only engine this build offers) the render layer exposes
    exactly `Image` and `Alpha` with a light group assigned, so routing a light-group
    pass returns the full beauty frame. The first version of this did exactly that and
    captioned it "light group 'key' only — other lights are excluded", which is the worst
    outcome available: a confident claim about an isolation that never happened. Two
    lights would have caught it; one light made beauty and "isolated" identical.

    Hiding the other light objects from the render is isolation EEVEE actually performs.
    Emissive materials and the world still contribute — the caption says so, because they
    are usually the point of the comparison.
    """
    undo: list = []
    if not light:
        return undo, [], []
    wanted = {n.strip() for n in light.split(",") if n.strip()}
    lights = [o for o in sc.objects if o.type == "LIGHT"]
    if not lights:
        raise RuntimeError("there are no light objects in this scene to isolate")
    known = {o.name for o in lights}
    missing = wanted - known
    if missing:
        raise RuntimeError(f"no light object named {sorted(missing)}; "
                           f"the scene's lights are {sorted(known)}")
    kept, hidden = [], []
    for obj in lights:
        if obj.name in wanted:
            kept.append(obj.name)
            continue
        prev = obj.hide_render
        obj.hide_render = True
        hidden.append(obj.name)
        undo.append(lambda o=obj, p=prev: setattr(o, "hide_render", p))
    return undo, kept, hidden


def _apply_crop(sc, crop, res_pct, scale) -> list:
    undo = []
    r = sc.render
    prev = (r.use_border, r.use_crop_to_border,
            r.border_min_x, r.border_min_y, r.border_max_x, r.border_max_y,
            r.resolution_percentage)
    undo.append(lambda: (
        setattr(r, "use_border", prev[0]),
        setattr(r, "use_crop_to_border", prev[1]),
        setattr(r, "border_min_x", prev[2]),
        setattr(r, "border_min_y", prev[3]),
        setattr(r, "border_max_x", prev[4]),
        setattr(r, "border_max_y", prev[5]),
        setattr(r, "resolution_percentage", prev[6]),
    ))
    if crop:
        x0, y0, x1, y1 = [float(v) for v in crop]
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise ValueError("crop must be [x0,y0,x1,y1] in 0..1, origin top-left")
        r.use_border = True
        r.use_crop_to_border = True
        # Blender render borders use bottom-left. Keep that implementation detail here so
        # every planner/builder/check API can use the same image-space convention.
        r.border_min_x, r.border_min_y = x0, 1.0 - y1
        r.border_max_x, r.border_max_y = x1, 1.0 - y0
    if res_pct is not None:
        r.resolution_percentage = max(1, min(1000, int(res_pct)))
    else:
        r.resolution_percentage = max(1, min(100, int(float(scale) * 100)))
    return undo


def _route_pass(sc, pass_name: str) -> list:
    """Route one render pass to the scene output. Restores the previous compositor.

    Blender 5.x has no `CompositorNodeComposite`: the compositor is a NODE GROUP on
    `scene.compositing_node_group` and its result is whatever reaches the group's
    INTERFACE output. A `NodeGroupOutput` node starts with only a virtual socket, so
    linking to `inputs[0]` on a group whose interface is empty links to nothing, the
    group outputs nothing, and Blender falls back to the raw Combined pass — a render
    bit-identical to beauty, under a caption claiming emission had been removed. The
    interface socket must be declared FIRST; that is what makes the link real.
    """
    import bpy
    undo = []
    if pass_name in ("", "beauty", None):
        return undo
    flag = _PASS_FLAGS.get(pass_name)
    vl = sc.view_layers[0]
    if flag and hasattr(vl, flag):
        prev_flag = getattr(vl, flag)
        setattr(vl, flag, True)
        undo.append(lambda: setattr(vl, flag, prev_flag))
    elif flag:
        raise RuntimeError(f"this Blender's view layer has no {flag}")

    prev_ng = getattr(sc, "compositing_node_group", None)
    prev_comp = getattr(sc.render, "use_compositing", None)
    ng = bpy.data.node_groups.new("_bvfx_pass", "CompositorNodeTree")
    ng.interface.new_socket(name="Image", in_out="OUTPUT",
                            socket_type="NodeSocketColor")
    rl = ng.nodes.new("CompositorNodeRLayers")
    go = ng.nodes.new("NodeGroupOutput")
    sock = _socket(rl, _PASS_SOCKETS.get(pass_name, (pass_name,)))
    if sock is None:
        available = [s.name for s in rl.outputs]
        bpy.data.node_groups.remove(ng)
        raise RuntimeError(f"pass {pass_name!r} has no compositor socket after enabling "
                           f"{flag}; available: {available}")
    dest_in = go.inputs.get("Image") or (go.inputs[0] if go.inputs else None)
    if dest_in is None:
        bpy.data.node_groups.remove(ng)
        raise RuntimeError("the compositor group output has no usable input socket")
    ng.links.new(sock, dest_in)
    if not ng.links:
        bpy.data.node_groups.remove(ng)
        raise RuntimeError(f"pass {pass_name!r} could not be linked to the group output")
    sc.compositing_node_group = ng
    if prev_comp is not None:
        sc.render.use_compositing = True

    def _undo():
        sc.compositing_node_group = prev_ng
        if prev_comp is not None:
            sc.render.use_compositing = prev_comp
        if ng.users == 0:
            bpy.data.node_groups.remove(ng)

    undo.append(_undo)
    return undo


def configure(sc, *, pass_name="beauty", light=None, shade="beauty",
              crop=None, res_pct=None, scale=0.5) -> tuple[list, str, dict]:
    """Apply Phase 2 knobs. Returns (undo thunks, caption, what was actually done)."""
    undo: list = []
    undo += _apply_shade(sc, shade or "beauty")
    light_undo, kept, hidden = isolate_lights(sc, light)
    undo += light_undo
    undo += _apply_crop(sc, crop, res_pct, scale)
    undo += _route_pass(sc, pass_name or "beauty")
    cap = caption_for(pass_name or "beauty", shade or "beauty", light, crop, res_pct,
                      kept=kept, hidden=hidden)
    return undo, cap, {"lights_kept": kept, "lights_hidden": hidden}


def restore(undos: list) -> None:
    for fn in reversed(undos):
        with contextlib.suppress(Exception):
            fn()


def image_size(path: str) -> list:
    """Pixel dimensions of a file already on disk, via bpy.

    Not PIL: Blender ships its own Python and it has no Pillow. The image subtraction
    that used to live here moved to `tools._subtract_png` for the same reason — it never
    needed Blender, and running it here meant a crash instead of a diff.
    """
    import bpy
    img = None
    try:
        img = bpy.data.images.load(path, check_existing=False)
        return [int(img.size[0]), int(img.size[1])]
    finally:
        if img is not None:
            bpy.data.images.remove(img)
