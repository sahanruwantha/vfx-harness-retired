"""Runs INSIDE Blender (`blender --background --python worker.py -- --artifacts DIR`).

A warm scene server: boots once, holds the scene in memory, and serves JSON-RPC
requests over stdin/stdout so each tool call is just the operation — no Blender
restart, no scene reload. Responses are framed with a sentinel so Blender's own
stdout chatter (render logs, warnings) is ignored by the client.
"""

import contextlib
import io
import json
import os
import sys
import time
import traceback

import bpy

SENT = "@@BVFX@@"


def _argv_after_ddash() -> list[str]:
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def _opt(name: str, default: str = "") -> str:
    a = _argv_after_ddash()
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


ARTIFACTS = os.path.abspath(_opt("--artifacts", "."))
os.makedirs(ARTIFACTS, exist_ok=True)
ASSETS_DIR = os.path.abspath(_opt("--assets")) if _opt("--assets") else ""


def _send(obj: dict) -> None:
    sys.stdout.write("\n" + SENT + json.dumps(obj) + SENT + "\n")
    sys.stdout.flush()


def _eevee_engine() -> str:
    items = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
    for cand in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        if cand in items:
            return cand
    return bpy.context.scene.render.engine


# ---- scene stats ------------------------------------------------------------

def _scene_stats() -> dict:
    sc = bpy.context.scene
    meshes = [o for o in sc.objects if o.type == "MESH"]
    verts = sum(len(o.data.vertices) for o in meshes)
    tris = sum(sum(max(0, len(p.vertices) - 2) for p in o.data.polygons) for o in meshes)
    return {"objects": len(sc.objects), "mesh_objects": len(meshes), "verts": verts, "tris": tris}


# ---- bvfx helpers (injected into run_bpy scope) -----------------------------
# Performant, house-look primitives so the build agent never hand-rolls slow
# per-object loops. Available in every run_bpy script (like `bpy`).

def _bvfx_emission(name, color=(1, 1, 1), strength=1.0, **_):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (*color, 1.0)
    em.inputs["Strength"].default_value = strength
    nt.links.new(em.outputs[0], out.inputs["Surface"])
    return m


def _bvfx_scatter_emissive(count, area=200.0, z_range=(0.0, 6.0), color=(1.0, 0.7, 0.35),
                           strength=3.0, seed=0, dot=0.6, name="scatter", **_):
    """A carpet of `count` emissive points as ONE vertex-instanced object (fast for
    thousands). Returns the instancer. Use this instead of a per-light loop."""
    import random as _r
    rng = _r.Random(seed)
    me = bpy.data.meshes.new(name + "_pts")
    verts = [(rng.uniform(-area, area), rng.uniform(-area, area),
              rng.uniform(z_range[0], z_range[1])) for _ in range(count)]
    me.from_pydata(verts, [], [])
    inst = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(inst)
    h = dot / 2.0
    src_me = bpy.data.meshes.new(name + "_dot")
    src_me.from_pydata([(-h, -h, 0), (h, -h, 0), (h, h, 0), (-h, h, 0)], [], [[0, 1, 2, 3]])
    src_me.update()
    src = bpy.data.objects.new(name + "_src", src_me)
    src.data.materials.append(_bvfx_emission(name + "_mat", color, strength))
    bpy.context.scene.collection.objects.link(src)
    src.parent = inst
    inst.instance_type = "VERTS"
    return inst


def _bvfx_volumetric_world(color=(0.02, 0.05, 0.03), bg_strength=0.3,
                           vol_color=(0.05, 0.3, 0.1), density=0.006, **_):
    """Near-black tinted sky + volume-scatter haze (the billowing-glow look)."""
    sc = bpy.context.scene
    w = sc.world or bpy.data.worlds.new("World"); sc.world = w
    w.use_nodes = True; nt = w.node_tree; nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.inputs["Color"].default_value = (*color, 1.0)
    bg.inputs["Strength"].default_value = bg_strength
    nt.links.new(bg.outputs[0], out.inputs["Surface"])
    vol = nt.nodes.new("ShaderNodeVolumeScatter")
    vol.inputs["Color"].default_value = (*vol_color, 1.0)
    vol.inputs["Density"].default_value = density
    nt.links.new(vol.outputs[0], out.inputs["Volume"])
    return w


def _bvfx_glare_bloom(threshold=0.6, size=0.75, strength=0.7, gtype="Bloom", **_):
    """EEVEE-Next has no bloom toggle — add a compositor Glare so emission blooms.
    Blender 5.x: the compositor is a NODE GROUP on scene.compositing_node_group whose
    output is a Group Output node, and the Glare node's settings are INPUT SOCKETS
    (Type is a menu: 'Bloom'/'Fog Glow'/'Streaks'/…; Threshold/Size/Strength are floats)."""
    sc = bpy.context.scene
    ng = bpy.data.node_groups.new("bvfx_compositor", "CompositorNodeTree")
    ng.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    rl = ng.nodes.new("CompositorNodeRLayers")
    glare = ng.nodes.new("CompositorNodeGlare")
    gout = ng.nodes.new("NodeGroupOutput")
    for sock, val in (("Type", gtype), ("Threshold", threshold), ("Size", size),
                      ("Strength", strength)):
        s = glare.inputs.get(sock)
        if s is not None:
            try:
                s.default_value = val
            except Exception:
                pass
    ng.links.new(rl.outputs["Image"], glare.inputs["Image"])
    ng.links.new(glare.outputs["Image"], gout.inputs[0])
    sc.compositing_node_group = ng
    sc.render.use_compositing = True
    return glare


def _bvfx_volume(name="volume", center=(0, 0, 150), size=(400, 160, 240), density=None,
                 optical_depth=0.4, color=(0.05, 0.3, 0.1), emission_strength=0.0,
                 noise_scale=2.4, noise_detail=9.0, noise_roughness=0.72,
                 contrast=(0.42, 0.72), stretch=(1.0, 1.0, 1.0), edge_falloff=True, **_):
    """A BOUNDED volumetric domain — clouds, nebula, fog, sandstorm, god-rays. THE
    primitive for atmosphere (do not hand-roll a cloud material/backdrop). Structured
    via noise so it reads as WISPS, not milk/smooth-glow, and density FADES TO 0 at the
    domain faces (`edge_falloff`) so you never see a hard box/wall. `stretch` scales the
    noise coords per axis — e.g. (0.3,1,1) for horizontal streaks. `contrast` narrows/
    widens the density ramp.

    DENSITY IS PHYSICS: what you see is optical depth ≈ density × path length through the
    domain. Leave `density=None` and it's derived from `optical_depth` (default 0.4 ≈
    clearly visible but never a fog wall; ~0.15 subtle haze, ~0.8 heavy) and the domain's
    smallest span — so ANY domain size starts near-right. Pass `density` to override.
    Place `center` behind/around the subject; extend scene.eevee.volumetric_end past it."""
    if density is None:
        path_len = max(1.0, min(float(size[0]), float(size[1]), float(size[2])))
        # noise ramp thins the volume to roughly ~1/3 average occupancy → compensate 3×
        density = 3.0 * float(optical_depth) / path_len
    bpy.ops.mesh.primitive_cube_add(size=1, location=center)
    dom = bpy.context.object
    dom.name = name
    dom.scale = (size[0] / 2, size[1] / 2, size[2] / 2)
    dom.display_type = "WIRE"
    m = bpy.data.materials.new(name + "_vol"); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    tex = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")  # anisotropic stretch → streaked wisps
    mapping.inputs["Scale"].default_value = stretch
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = noise_scale
    noise.inputs["Detail"].default_value = noise_detail
    _r = noise.inputs.get("Roughness")
    if _r is not None:
        _r.default_value = noise_roughness
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = contrast[0]
    ramp.color_ramp.elements[1].position = contrast[1]
    # A 0..1 "shape" mask (noise → contrast ramp), optionally faded to 0 at the domain
    # faces. BOTH density and emission are driven by it, so emission also vanishes at the
    # edges — otherwise Principled Volume's uniform emission glows a solid box/wall.
    shape_out = ramp.outputs["Color"]
    if edge_falloff:
        sep = nt.nodes.new("ShaderNodeSeparateXYZ")
        nt.links.new(tex.outputs["Generated"], sep.inputs["Vector"])

        def _axis_fade(idx):  # 1 - |2g-1|: 1 at centre, 0 at the two faces on this axis
            ma = nt.nodes.new("ShaderNodeMath"); ma.operation = "MULTIPLY_ADD"
            ma.inputs[1].default_value = 2.0; ma.inputs[2].default_value = -1.0
            nt.links.new(sep.outputs[idx], ma.inputs[0])
            ab = nt.nodes.new("ShaderNodeMath"); ab.operation = "ABSOLUTE"
            nt.links.new(ma.outputs[0], ab.inputs[0])
            sub = nt.nodes.new("ShaderNodeMath"); sub.operation = "SUBTRACT"
            sub.inputs[0].default_value = 1.0
            nt.links.new(ab.outputs[0], sub.inputs[1])
            return sub

        fx, fy, fz = _axis_fade(0), _axis_fade(1), _axis_fade(2)
        w1 = nt.nodes.new("ShaderNodeMath"); w1.operation = "MULTIPLY"
        nt.links.new(fx.outputs[0], w1.inputs[0]); nt.links.new(fy.outputs[0], w1.inputs[1])
        w2 = nt.nodes.new("ShaderNodeMath"); w2.operation = "MULTIPLY"
        nt.links.new(w1.outputs[0], w2.inputs[0]); nt.links.new(fz.outputs[0], w2.inputs[1])
        shp = nt.nodes.new("ShaderNodeMath"); shp.operation = "MULTIPLY"  # ramp * window
        nt.links.new(ramp.outputs["Color"], shp.inputs[0]); nt.links.new(w2.outputs[0], shp.inputs[1])
        shape_out = shp.outputs[0]

    dmul = nt.nodes.new("ShaderNodeMath"); dmul.operation = "MULTIPLY"
    dmul.inputs[1].default_value = density
    nt.links.new(shape_out, dmul.inputs[0])

    vol = nt.nodes.new("ShaderNodeVolumePrincipled")
    vol.inputs["Color"].default_value = (*color, 1.0)
    ec = vol.inputs.get("Emission Color")
    if ec is not None:
        try:
            ec.default_value = (*color, 1.0)
        except Exception:
            pass
    nt.links.new(dmul.outputs[0], vol.inputs["Density"])
    es = vol.inputs.get("Emission Strength")
    if es is not None and emission_strength > 0:
        emul = nt.nodes.new("ShaderNodeMath"); emul.operation = "MULTIPLY"
        emul.inputs[1].default_value = emission_strength
        nt.links.new(shape_out, emul.inputs[0])
        nt.links.new(emul.outputs[0], es)

    nt.links.new(tex.outputs["Generated"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(vol.outputs[0], out.inputs["Volume"])
    dom.data.materials.append(m)
    return dom


def _bvfx_emissive_windows(obj, window_color=(0.12, 1.0, 0.38), strength=8.0,
                           density=7.0, aspect=2.0, mortar=0.22, body_glow=0.06, **_):
    """A glowing circuit-board facade: lit window cells on a DARK grid, as emission (NOT
    one uniform emission material, which washes out all detail). Uses an explicit
    coordinate-modulo grid (mesh-independent — TexBrick sampled all-mortar on real GLBs).
    `density` = window COLUMNS across (~4-20; NOT a 0-1 fraction); `aspect` = rows/cols
    (taller cells); `mortar` (0..0.45) = dark gap fraction; `body_glow` = faint facade glow
    in the gaps. Put it on a hero building mesh."""
    density = max(3.0, min(40.0, float(density)))
    aspect = max(0.5, min(6.0, float(aspect)))
    cols, rows = density, density * aspect
    mfrac = max(0.05, min(0.45, float(mortar)))
    # Use OBJECT coords normalized by the mesh's own bbox (imported GLBs often have a
    # degenerate texspace → Generated collapses to a point → all-mortar/black).
    bb = [tuple(c) for c in getattr(obj, "bound_box", [])] if obj is not None else []
    if bb:
        xs = [c[0] for c in bb]; zs = [c[2] for c in bb]
        xlo, xsz = min(xs), max(1e-6, max(xs) - min(xs))
        zlo, zsz = min(zs), max(1e-6, max(zs) - min(zs))
        coord_kind = "Object"
    else:  # generic (e.g. a unit cube) — Generated 0..1 is fine
        xlo, xsz, zlo, zsz, coord_kind = 0.0, 1.0, 0.0, 1.0, "Generated"

    m = bpy.data.materials.new((getattr(obj, "name", "win")) + "_windows"); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    tc = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(tc.outputs[coord_kind], sep.inputs["Vector"])

    def _cell_band(coord_socket, count, lo, size):
        # frac((coord-lo)/size * count) → 1 inside the lit window band, 0 in the mortar gap
        freq = float(count) / size
        mul = nt.nodes.new("ShaderNodeMath"); mul.operation = "MULTIPLY_ADD"
        mul.inputs[1].default_value = freq
        mul.inputs[2].default_value = -lo * freq
        nt.links.new(coord_socket, mul.inputs[0])
        fr = nt.nodes.new("ShaderNodeMath"); fr.operation = "FRACT"
        nt.links.new(mul.outputs[0], fr.inputs[0])
        gt = nt.nodes.new("ShaderNodeMath"); gt.operation = "GREATER_THAN"
        gt.inputs[1].default_value = mfrac
        nt.links.new(fr.outputs[0], gt.inputs[0])
        lt = nt.nodes.new("ShaderNodeMath"); lt.operation = "LESS_THAN"
        lt.inputs[1].default_value = 1.0 - mfrac
        nt.links.new(fr.outputs[0], lt.inputs[0])
        band = nt.nodes.new("ShaderNodeMath"); band.operation = "MULTIPLY"
        nt.links.new(gt.outputs[0], band.inputs[0]); nt.links.new(lt.outputs[0], band.inputs[1])
        return band

    wx = _cell_band(sep.outputs["X"], cols, xlo, xsz)
    wz = _cell_band(sep.outputs["Z"], rows, zlo, zsz)
    mask = nt.nodes.new("ShaderNodeMath"); mask.operation = "MULTIPLY"  # 1 in window, 0 gap
    nt.links.new(wx.outputs[0], mask.inputs[0]); nt.links.new(wz.outputs[0], mask.inputs[1])
    stg = nt.nodes.new("ShaderNodeMath"); stg.operation = "MULTIPLY_ADD"  # mask*strength+glow
    stg.inputs[1].default_value = strength
    stg.inputs[2].default_value = body_glow
    nt.links.new(mask.outputs[0], stg.inputs[0])
    emis = nt.nodes.new("ShaderNodeEmission")
    emis.inputs["Color"].default_value = (*window_color, 1.0)
    nt.links.new(stg.outputs[0], emis.inputs["Strength"])
    nt.links.new(emis.outputs[0], out.inputs["Surface"])
    if obj is not None and hasattr(obj, "data") and hasattr(obj.data, "materials"):
        obj.data.materials.clear()
        obj.data.materials.append(m)
    return m


def _bvfx_import_asset(name):
    """Import a committed, normalized asset (assets/<name>/model.glb) into the live scene
    and return the new object names. Use this in build scripts — the `import_asset` TOOL
    is not in scope inside run_bpy / a build script, but this helper is."""
    if not ASSETS_DIR:
        raise RuntimeError("no assets dir configured for this session")
    glb = os.path.join(ASSETS_DIR, name, "model.glb")
    if not os.path.exists(glb):
        avail = sorted(os.listdir(ASSETS_DIR)) if os.path.isdir(ASSETS_DIR) else []
        raise FileNotFoundError(f"no asset {name!r}; available: {avail}")
    before = set(bpy.data.objects.keys())
    bpy.ops.import_scene.gltf(filepath=glb)
    return [n for n in bpy.data.objects.keys() if n not in before]


def _bvfx_aim(obj, target=(0.0, 0.0, 0.0), up="Y"):
    """Point obj (e.g. a camera) at `target`. Accepts tuples OR Vectors safely — avoids
    the `unary -: tuple` footgun of hand-rolled aim math."""
    import mathutils
    bpy.context.view_layer.update()  # matrix_world is stale right after setting .location
    t = mathutils.Vector(target)
    loc = obj.matrix_world.translation
    obj.rotation_euler = (t - loc).to_track_quat("-Z", up).to_euler()
    return obj


def _bvfx_fcurves(target):
    """EVERY f-curve keyed on `target` — object, material, world, node group, scene, or a
    raw animation_data.

    Promoted to a helper because 8 of 10 shipped build scripts walk channelbags by hand,
    in 5 distinct implementations, and 5 of those use the `action.layers[0].strips[0]`
    indexed form that raises on any ID nothing has been keyed on yet. 5.x actions are
    SLOTTED: `action.fcurves` is empty, so the obvious read finds nothing. Both failure
    modes are SILENT — you set interpolation on an empty list and ship the bezier.
    """
    ad = getattr(target, "animation_data", None)
    if ad is None and getattr(target, "node_tree", None) is not None:
        ad = target.node_tree.animation_data      # materials/worlds animate on the TREE
    if ad is None and hasattr(target, "action"):
        ad = target                               # already an animation_data
    if not ad or not ad.action:
        return []
    legacy = getattr(ad.action, "fcurves", None)
    if legacy and len(legacy):
        return list(legacy)                       # pre-4.4 action
    return [fc for layer in ad.action.layers for strip in layer.strips
            for cb in strip.channelbags for fc in cb.fcurves]


def _bvfx_interp(target, mode="LINEAR", const=("hide_render", "hide_viewport")):
    """Force interpolation on everything keyed on `target`; visibility goes CONSTANT so a
    swap is a hard cut, never a half-hidden in-between frame.

    Returns the number of curves touched — 0 means you keyed something other than what you
    think you did, which is the failure this exists to make visible. Bezier overshoot on a
    fast ramp is what makes a delta layer non-idempotent, and can drive a one-frame value
    negative between keys that are both positive.
    """
    n = 0
    for fc in _bvfx_fcurves(target):
        m = "CONSTANT" if any(c in fc.data_path for c in const) else mode
        for kp in fc.keyframe_points:
            kp.interpolation = m
            if m == "BEZIER":
                kp.handle_left_type = kp.handle_right_type = "AUTO_CLAMPED"
        fc.update()
        n += 1
    return n


def _bvfx_camera_rig(name="cam_rig", lens=35.0, sensor=36.0, clip=(0.5, 20000.0),
                     spine=(), ladder=(), display=4.0):
    """The two-object camera: an EMPTY owns location+pitch, the camera child owns ROLL on
    its own local Z. On a bare camera `rotation_euler[2]` is world YAW and swings the
    subject out of frame; under the rig the view axis IS local Z, so the frame rotates
    about its own centre. Returns (rig, cam).

      spine:  [(frame, distance, altitude, pitch_up_deg), ...] subject on +Y at origin.
              Keyed BEZIER — one smooth travel; a velocity step reads as a camera bump.
      ladder: [(frame, roll_deg), ...] keyed LINEAR — a roll ladder's segment RATES are
              the look, and bezier drags the peak rate off the frames you keyed it on.

    Pitch is `radians(90 + p)`: a camera looks down its own -Z, so LEVEL is 90, not 0."""
    import math                      # not a module-level import in this worker
    sc = bpy.context.scene
    for n in (name, "camera"):
        o = bpy.data.objects.get(n)
        if o:
            bpy.data.objects.remove(o, do_unlink=True)
    rig = bpy.data.objects.new(name, None)
    rig.empty_display_size = display
    sc.collection.objects.link(rig)
    camd = bpy.data.cameras.new("camera")
    camd.lens, camd.sensor_width, camd.sensor_fit = lens, sensor, "AUTO"
    camd.clip_start, camd.clip_end = clip
    cam = bpy.data.objects.new("camera", camd)
    sc.collection.objects.link(cam)
    cam.parent = rig
    cam.matrix_parent_inverse.identity()   # BEFORE location, or offsets are silently wrong
    cam.location = (0.0, 0.0, 0.0)
    cam.rotation_euler = (0.0, 0.0, 0.0)
    sc.camera = cam
    if spine:
        rig.animation_data_clear()
        for f, d, z, p in spine:
            rig.location = (0.0, -d, z)
            rig.rotation_euler = (math.radians(90.0 + p), 0.0, 0.0)
            rig.keyframe_insert("location", frame=f)
            rig.keyframe_insert("rotation_euler", frame=f)
        _bvfx_interp(rig, "BEZIER")
    if ladder:
        cam.animation_data_clear()
        for f, r in ladder:
            cam.rotation_euler[2] = math.radians(r)
            cam.keyframe_insert("rotation_euler", index=2, frame=f)
        _bvfx_interp(cam, "LINEAR")
    return rig, cam


_HELPERS = {
    "bvfx_emission": _bvfx_emission,
    "bvfx_emissive_windows": _bvfx_emissive_windows,
    "bvfx_import_asset": _bvfx_import_asset,
    "bvfx_aim": _bvfx_aim,
    "bvfx_scatter_emissive": _bvfx_scatter_emissive,
    "bvfx_volumetric_world": _bvfx_volumetric_world,
    "bvfx_volume": _bvfx_volume,
    "bvfx_glare_bloom": _bvfx_glare_bloom,
    "bvfx_fcurves": _bvfx_fcurves,
    "bvfx_interp": _bvfx_interp,
    "bvfx_camera_rig": _bvfx_camera_rig,
}


# ---- handlers ---------------------------------------------------------------

def h_ping(a: dict) -> dict:
    return {"blender": bpy.app.version_string, "eevee": _eevee_engine()}


# Blender 4.x attribute -> the 5.x way. Surfaced inline so a wrong guess costs one
# tool call instead of a retry loop.
_ATTR_HINTS = {
    "glare_type": "in 5.x Glare settings are INPUT SOCKETS, not attributes. Use the "
                  "helper: bvfx_glare_bloom(threshold=..., size=..., strength=...). To "
                  "read the graph use inspect_nodes('compositor').",
    "node_tree": "scene.node_tree is GONE in 5.x — the compositor is "
                 "scene.compositing_node_group. Prefer bvfx_glare_bloom / "
                 "inspect_nodes('compositor') over poking it directly.",
    "spot_size": "spot_size exists only on SPOT lights. Check light.type first "
                 "('AREA' uses size/size_y, 'SUN' uses angle).",
    "no attribute 'elements'": "ColorRamp stops live one level down: "
                               "node.color_ramp.elements (and .color_ramp.evaluate(t)), "
                               "not node.elements.",
    "default_value": "shader-type sockets carry a LINK, not a value — link a node into "
                     "it instead of assigning default_value.",
}


def _hinted(e: BaseException) -> BaseException | None:
    """Return a replacement exception carrying a HINT, or None if we can't improve it.

    Some Blender-script failures arrive with a message that is useless on its own —
    worst of all StopIteration, whose message is EMPTY, so the builder receives
    "StopIteration: " and has no node name, no collection, nothing to act on. Naming the
    fix at the point of failure turns a debug turn into a retry.
    """
    msg = str(e)

    if isinstance(e, StopIteration):
        return StopIteration(
            "a next(...) generator found NO match — StopIteration carries no message, "
            "so this is all Python can tell you. Never write bare "
            "`next(n for n in nt.nodes if ...)`: use "
            "`n = next((n for n in nt.nodes if ...), None)` and handle n is None, or "
            "call inspect_nodes('compositor'/'world'/<material>/<object>) first to see "
            "which node types actually exist. For the usual targets the helpers already "
            "do this: bvfx_emission / bvfx_emissive_windows (emission shaders), "
            "bvfx_glare_bloom (compositor glare), bvfx_volume (volume scatter)."
        )

    if isinstance(e, TypeError) and "tuple" in msg and ("operand" in msg or "unsupported" in msg):
        # the recurring footgun — make the fix instant instead of a debug turn
        return TypeError(
            f"{msg}\nHINT: you did vector math on a raw TUPLE. Wrap it first — "
            f"`Vector((x, y, z))` is already in scope (so are math/mathutils), "
            f"or use bvfx_aim(obj, target) for aiming.")

    if isinstance(e, AttributeError):
        # Blender-4 attributes that became input sockets in 5.x. Without a hint the
        # builder retries the same 4.x form (observed twice in 54s on glare_type),
        # so name the replacement at the point of failure.
        for attr, hint in _ATTR_HINTS.items():
            if attr in msg:
                return AttributeError(f"{msg}\nHINT: {hint}")

    if isinstance(e, KeyError):
        # bpy collections do say which key missed, but never what IS there, so the
        # builder's next move is another blind guess at the name.
        return KeyError(
            f"{msg} — no such key. HINT: a bpy collection lookup missed. Use "
            f".get(name) and check for None instead of [name], and list the real names "
            f"first: inspect_scene('objects'/'materials') for bpy.data, "
            f"inspect_nodes(<target>) for nodes and sockets. Socket names differ "
            f"between Blender versions and are localised by node type — never assume.")

    return None


_JOURNAL: list[str] = []


def h_replay(a: dict) -> dict:
    """Re-exec journal entries [start:] — the WAL half of snapshot recovery."""
    start = int(a.get("start", 0))
    entries = _JOURNAL[start:]
    done = 0
    for code in list(entries):
        h_run({"code": code})
        done += 1
    return {"replayed": done, "from": start}


def h_journal(a: dict) -> dict:
    """The accepted run_bpy calls, in order — a DRAFT of the build script.

    Not a build script by itself: it contains superseded tweaks, probes and dead ends.
    It is a transcript to prune, which is far cheaper than re-authoring from memory.
    """
    if a.get("clear"):
        _JOURNAL.clear()
        return {"cleared": True}
    body = "\n\n# ---- next accepted run_bpy call ----\n".join(_JOURNAL)
    path = a.get("path")
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# JOURNAL — {len(_JOURNAL)} accepted run_bpy calls, in order.\n"
                     f"# Superseded tweaks and probes included: PRUNE, do not paste.\n\n"
                     + body + "\n")
    return {"calls": len(_JOURNAL), "chars": len(body), "path": path}


def h_run(a: dict) -> dict:
    """Exec arbitrary bpy code. Set `RESULT = <json-able>` to return data.

    `bvfx_*` helpers, `mathutils`, `Vector` and `math` are pre-injected into scope (each
    call is a FRESH namespace — imports don't persist between calls, which is why raw-
    tuple math kept recurring). Returns timing + scene-delta so the agent feels cost."""
    import math as _math
    import mathutils as _mathutils
    ns: dict = {"bpy": bpy, "math": _math, "mathutils": _mathutils,
                "Vector": _mathutils.Vector, **_HELPERS}
    before = _scene_stats()
    buf = io.StringIO()
    t0 = time.monotonic()
    with contextlib.redirect_stdout(buf):
        try:
            exec(a["code"], ns)  # noqa: S102 — this is the point of the tool
        except Exception as e:  # noqa: BLE001 — re-raised; we only enrich the message
            better = _hinted(e)
            if better is None:
                raise
            # `from None` keeps the traceback tail on OUR line: the enriched message is
            # the last thing printed, which is also the part the log now preserves.
            raise better.with_traceback(e.__traceback__) from None
    elapsed = time.monotonic() - t0
    # Only successful code is journalled: the builder currently re-authors the whole
    # layer from memory at finalize (~28KB of live calls -> a 23KB script), which is
    # duplicated effort AND the only reason live and canonical can diverge.
    _JOURNAL.append(a["code"])
    after = _scene_stats()
    result = ns.get("RESULT")
    try:
        json.dumps(result)
    except TypeError:
        result = repr(result)
    return {"stdout": buf.getvalue(), "result": result,
            "elapsed_s": round(elapsed, 2),
            "objects_added": after["objects"] - before["objects"],
            "verts_added": after["verts"] - before["verts"],
            "scene": after}


def h_inspect(a: dict) -> dict:
    section = a.get("section", "all")
    sc = bpy.context.scene
    out: list[str] = []
    if section in ("all", "render"):
        r = sc.render
        out.append(
            f"render: engine={r.engine} res={r.resolution_x}x{r.resolution_y}"
            f"@{r.resolution_percentage}% frames={sc.frame_start}-{sc.frame_end}"
            f" fps={r.fps} motion_blur={r.use_motion_blur} camera={sc.camera.name if sc.camera else None}"
        )
        st = _scene_stats()
        out.append(f"scene: objects={st['objects']} mesh={st['mesh_objects']} "
                   f"verts={st['verts']} tris={st['tris']}")
    if section in ("all", "world") and sc.world:
        out.append(f"world: {sc.world.name} use_nodes={sc.world.use_nodes}")
    if section in ("all", "objects"):
        out.append("objects:")
        for o in sc.objects:
            loc = tuple(round(v, 2) for v in o.location)
            mods = ",".join(m.type for m in o.modifiers) or "-"
            psys = ",".join(p.name for p in getattr(o, "particle_systems", [])) or "-"
            out.append(f"  {o.name} [{o.type}] loc={loc} mods={mods} psys={psys}")
    if section in ("all", "materials"):
        out.append("materials: " + (", ".join(m.name for m in bpy.data.materials) or "-"))
    return {"text": "\n".join(out)}


def h_nodes(a: dict) -> dict:
    """Dump a node tree as text — nodes (type + unlinked socket values) + links.
    target: 'compositor' (the 5.x scene.compositing_node_group — NOT scene.node_tree,
    which is gone), 'world', a material name, or an object name (its active material)."""
    target = a.get("target", "world")
    nt = None
    title = target
    if target in ("compositor", "comp", "compositing"):
        nt = getattr(bpy.context.scene, "compositing_node_group", None)
        title = "compositor (scene.compositing_node_group)"
    elif target == "world":
        w = bpy.context.scene.world
        nt = w.node_tree if (w and w.use_nodes) else None
    else:
        mat = bpy.data.materials.get(target)
        if mat is None:
            obj = bpy.data.objects.get(target)
            if obj and obj.active_material:
                mat = obj.active_material
                title = f"{target} → {mat.name}"
        if mat and mat.use_nodes:
            nt = mat.node_tree
    if nt is None:
        return {"text": f"no node tree for {target!r} "
                        "(use 'compositor' / 'world' / <material> / <object>)"}
    lines = [f"nodes for {title}:"]
    for n in nt.nodes:
        vals = []
        for i in n.inputs:
            if not i.is_linked and hasattr(i, "default_value"):
                v = i.default_value
                try:
                    v = tuple(round(x, 3) for x in v) if hasattr(v, "__len__") else round(v, 3)
                except Exception:
                    pass
                vals.append(f"{i.name}={v}")
        lines.append(f"  [{n.type}] {n.name}" + (" | " + ", ".join(vals[:6]) if vals else ""))
    lines.append("links:")
    for lk in nt.links:
        lines.append(f"  {lk.from_node.name}.{lk.from_socket.name} → "
                     f"{lk.to_node.name}.{lk.to_socket.name}")
    return {"text": "\n".join(lines)}


def _action_fcurves(obj, action):
    """Get F-curves across Blender versions. 4.4+ 'slotted actions' removed
    `action.fcurves`; the curves live in per-slot channelbags on layer strips."""
    fcs = getattr(action, "fcurves", None)
    if fcs and len(fcs):
        return list(fcs)
    out = []
    slot = getattr(getattr(obj, "animation_data", None), "action_slot", None)
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            bags = []
            if slot is not None and hasattr(strip, "channelbag"):
                try:
                    cb = strip.channelbag(slot)
                    if cb is not None:
                        bags = [cb]
                except Exception:
                    bags = []
            if not bags:
                bags = list(getattr(strip, "channelbags", []))
            for cb in bags:
                out.extend(cb.fcurves)
    return out


def h_keyframes(a: dict) -> dict:
    name = a["object"]
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise KeyError(f"no object named {name!r}")
    ad = obj.animation_data
    if not ad or not ad.action:
        return {"text": f"{name}: no animation"}
    lines = [f"{name}: action {ad.action.name}"]
    for fc in _action_fcurves(obj, ad.action):
        keys = [(round(k.co[0], 1), round(k.co[1], 3), k.interpolation) for k in fc.keyframe_points]
        lines.append(f"  {fc.data_path}[{fc.array_index}]: {keys}")
    return {"text": "\n".join(lines)}


def h_render(a: dict) -> dict:
    sc = bpy.context.scene
    frame = int(a["frame"])
    mode = a.get("mode", "eevee")
    scale = float(a.get("scale", 0.5))
    engines = {"eevee": _eevee_engine(), "draft": _eevee_engine(),
               "solid": "BLENDER_WORKBENCH", "wire": "BLENDER_WORKBENCH"}
    sc.render.engine = engines.get(mode, _eevee_engine())
    if mode in ("solid", "wire"):
        sc.display.shading.type = "WIREFRAME" if mode == "wire" else "SOLID"
    if mode == "draft":  # fast, low-sample eevee for iteration (full 'eevee' for the layer)
        try:
            sc.eevee.taa_render_samples = int(a.get("samples", 8))
        except AttributeError:
            pass
    elif mode == "eevee" and "samples" in a:
        try:
            sc.eevee.taa_render_samples = int(a["samples"])
        except AttributeError:
            pass
    sc.render.resolution_percentage = max(1, min(100, int(scale * 100)))
    sc.frame_set(frame)
    path = os.path.join(ARTIFACTS, f"{mode}_f{frame:04d}.png")
    sc.render.filepath = path
    sc.render.image_settings.file_format = "PNG"
    bpy.ops.render.render(write_still=True)
    return {"image_path": path, "frame": frame, "mode": mode,
            "resolution": [sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage]}


def h_snapshot(a: dict) -> dict:
    """Save the scene as a .blend and record the journal position.

    snapshot + journal = checkpoint + write-ahead log: restore the .blend, replay the
    journal entries recorded after `journal_index`, and you have the exact scene even if
    the session died mid-turn far from the last checkpoint."""
    out_dir = a.get("dir") or ARTIFACTS
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"snapshot_{a.get('tag', 'x')}.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=True, copy=True)
    return {"blend": path, "journal_index": len(_JOURNAL)}


def h_restore(a: dict) -> dict:
    """Load a .blend snapshot — restores the exact scene state of that checkpoint."""
    bpy.ops.wm.open_mainfile(filepath=a["blend"])
    return {"restored": a["blend"], **_scene_stats()}


HANDLERS = {
    "ping": h_ping,
    "run": h_run,
    "inspect": h_inspect,
    "nodes": h_nodes,
    "keyframes": h_keyframes,
    "render": h_render,
    "snapshot": h_snapshot,
    "restore": h_restore,
    "journal": h_journal,
    "replay": h_replay,
}


def serve() -> None:
    _send({"event": "ready", "blender": bpy.app.version_string, "artifacts": ARTIFACTS})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, cmd, args = req.get("id"), req.get("cmd"), req.get("args", {})
        if cmd == "shutdown":
            _send({"id": rid, "ok": True, "result": {"bye": True}})
            break
        try:
            _send({"id": rid, "ok": True, "result": HANDLERS[cmd](args)})
        except Exception as e:  # noqa: BLE001 — report every failure to the client
            _send({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}",
                   "trace": traceback.format_exc()})


if __name__ == "__main__":
    serve()
