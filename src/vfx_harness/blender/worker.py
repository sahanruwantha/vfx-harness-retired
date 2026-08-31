"""Runs INSIDE Blender (`blender --background --python worker.py -- --artifacts DIR`).

A warm scene server: boots once, holds the scene in memory, and serves JSON-RPC
requests over stdin/stdout so each tool call is just the operation — no Blender
restart, no scene reload. Responses are framed with a sentinel so Blender's own
stdout chatter (render logs, warnings) is ignored by the client.
"""

import contextlib
import hashlib
import io
import json
import math
import math as _math
import os
import random as _r
import re
import sys
import time
import traceback
from typing import Any

import bpy

# Blender runs this file by path, so its own directory is NOT importable by default.
# `checks` and `render_ext` are siblings and are imported lazily by their handlers.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SENT = "@@VFXH@@"


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

def _bvfx_emission(name, color=(1, 1, 1), strength=1.0, **_) -> "bpy.types.Material":
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (*color, 1.0)
    em.inputs["Strength"].default_value = strength
    nt.links.new(em.outputs[0], out.inputs["Surface"])
    return m


def _bvfx_scatter_emissive(count, area=200.0, z_range=(0.0, 6.0), color=(1.0, 0.7, 0.35),
                           strength=3.0, seed=0, dot=0.6, name="scatter",
                           **_) -> "bpy.types.Object":
    """A carpet of `count` emissive points as ONE vertex-instanced object (fast for
    thousands). Returns the instancer. Use this instead of a per-light loop."""
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
                           vol_color=(0.05, 0.3, 0.1), density=0.006, role=None,
                           node_role=None, control=None, owner_layer=None,
                           **_) -> "bpy.types.World":
    """Near-black tinted sky + volume-scatter haze (the billowing-glow look).

    WARNING: once a world Volume is linked, a SUN contributes essentially nothing — it is
    infinitely distant, so its light is fully extinguished crossing an unbounded volume.
    Measured here: a white 0.8 body under a sun at energy 25 reads 6.74/255 with the
    volume linked and 216 without. No volumetric setting changes it. Key your subjects
    with LOCAL lights (AREA/POINT/SPOT) placed near them.
    """
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
    if role:
        _bvfx_role(w, role, owner_layer)
    if node_role:
        _bvfx_role(vol, node_role, owner_layer)
    if control:
        _bvfx_control(vol, control, owner_layer)
    return w


def _bvfx_glare_bloom(threshold=0.6, size=0.75, strength=0.7, gtype="Bloom",
                      role="compositor.glare.baseline", owner_layer=None,
                      **_) -> "bpy.types.Node":
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
    _bvfx_role(ng, "compositor.pipeline", owner_layer)
    _bvfx_role(rl, "compositor.input.render_layers", owner_layer)
    _bvfx_role(glare, role, owner_layer)
    _bvfx_role(gout, "compositor.output.image", owner_layer)
    for sock, val in (("Type", gtype), ("Threshold", threshold), ("Size", size),
                      ("Strength", strength)):
        s = glare.inputs.get(sock)
        if s is not None:
            with contextlib.suppress(Exception):
                s.default_value = val
    ng.links.new(rl.outputs["Image"], glare.inputs["Image"])
    ng.links.new(glare.outputs["Image"], gout.inputs[0])
    sc.compositing_node_group = ng
    sc.render.use_compositing = True
    return glare


def _bvfx_vector_blur(samples=16, shutter=0.5, role="lookdev.motion_blur_node",
                      control=None, owner_layer=None, **_) -> "bpy.types.Node":
    """Create and wire Blender 5.x's unified-compositor Vector Blur atomically.

    Blender 5.x exposes Samples/Shutter as input sockets, uses a Group Output rather
    than ``CompositorNodeComposite``, and only exposes Vector after the view-layer pass
    is enabled and the Render Layers node is refreshed.  Keep those coupled facts in
    one helper so a builder never has to discover them through partial scene edits.
    """
    sc = bpy.context.scene
    sc.render.engine = _eevee_engine()
    view_layer = sc.view_layers[0]
    view_layer.use_pass_vector = True
    view_layer.use_pass_z = True

    ng = sc.compositing_node_group
    if ng is None:
        ng = bpy.data.node_groups.new("bvfx_compositor", "CompositorNodeTree")
        sc.compositing_node_group = ng
    sc.render.use_compositing = True

    output_socket = next(
        (
            item
            for item in ng.interface.items_tree
            if getattr(item, "item_type", "SOCKET") == "SOCKET"
            and getattr(item, "in_out", None) == "OUTPUT"
            and item.name == "Image"
        ),
        None,
    )
    if output_socket is None:
        ng.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")

    nodes = ng.nodes
    links = ng.links
    render_layers = next((node for node in nodes if node.type == "R_LAYERS"), None)
    # Pass sockets are materialized when the node is created.  An older node created
    # before enabling Vector must be refreshed or it will still expose only Image/Alpha/Z.
    if render_layers is not None and render_layers.outputs.get("Vector") is None:
        location = tuple(render_layers.location)
        nodes.remove(render_layers)
        render_layers = nodes.new("CompositorNodeRLayers")
        render_layers.location = location
    elif render_layers is None:
        render_layers = nodes.new("CompositorNodeRLayers")

    group_output = next((node for node in nodes if node.type == "GROUP_OUTPUT"), None)
    if group_output is None:
        group_output = nodes.new("NodeGroupOutput")
    vector_blur = next((node for node in nodes if node.type == "VECBLUR"), None)
    if vector_blur is None:
        vector_blur = nodes.new("CompositorNodeVecBlur")
        vector_blur.name = "BVFX Vector Blur"

    for socket_name, value in (("Samples", samples), ("Shutter", shutter)):
        socket = vector_blur.inputs.get(socket_name)
        if socket is None:
            available = ", ".join(input_socket.name for input_socket in vector_blur.inputs)
            raise ValueError(
                f"bvfx_vector_blur: {socket_name!r} input is unavailable; inputs: {available}"
            )
        socket.default_value = value

    for source_name, target_name in (
        ("Image", "Image"),
        ("Vector", "Speed"),
        ("Depth", "Depth"),
    ):
        source = render_layers.outputs.get(source_name)
        target = vector_blur.inputs.get(target_name)
        if source is None or target is None:
            raise ValueError(
                "bvfx_vector_blur: missing required socket "
                f"Render Layers.{source_name} -> Vector Blur.{target_name}"
            )
        links.new(source, target)
    image_output = group_output.inputs.get("Image") or group_output.inputs[0]
    links.new(vector_blur.outputs["Image"], image_output)

    _bvfx_role(vector_blur, role, owner_layer)
    if control:
        _bvfx_control(vector_blur, control, owner_layer)
    return vector_blur


def _bvfx_light(name, light_type="POINT", location=(0.0, 0.0, 0.0), energy=None,
                color=None, role=None, control=None, owner_layer=None, aim_target=None,
                size=None, spot_size=None, spot_blend=None, **_) -> "bpy.types.Object":
    """Create or reconfigure one semantic light without stale RNA-subtype handles.

    Changing ``Light.type`` in Blender can leave the existing Python reference typed as
    the old PointLight/AreaLight subclass.  Re-fetch the data-block before touching
    type-specific properties so POINT -> SPOT/AREA conversions are one closed-loop edit.
    """
    obj = bpy.data.objects.get(name)
    created = obj is None
    if created:
        if not role:
            raise ValueError("bvfx_light: role is required when creating a persistent light")
        data = bpy.data.lights.new(name + "_data", type=str(light_type).upper())
        obj = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(obj)
    elif obj.type != "LIGHT":
        raise ValueError(f"bvfx_light: existing object {name!r} is {obj.type}, not LIGHT")

    data_name = obj.data.name
    obj.data.type = str(light_type).upper()
    # Important: discard the pre-conversion RNA wrapper and resolve the new subtype.
    data = bpy.data.lights[data_name]
    if energy is not None:
        data.energy = float(energy)
    if color is not None:
        data.color = tuple(float(value) for value in color)
    obj.location = tuple(float(value) for value in location)
    if size is not None and hasattr(data, "size"):
        data.size = float(size)
    if spot_size is not None:
        if data.type != "SPOT":
            raise ValueError("bvfx_light: spot_size requires light_type='SPOT'")
        data.spot_size = float(spot_size)
    if spot_blend is not None:
        if data.type != "SPOT":
            raise ValueError("bvfx_light: spot_blend requires light_type='SPOT'")
        data.spot_blend = float(spot_blend)
    if aim_target is not None:
        _bvfx_aim(obj, target=aim_target)

    effective_role = role or str(obj.get("bvfx_role") or "")
    if effective_role:
        _bvfx_role(obj, effective_role, owner_layer)
    if control:
        _bvfx_control(obj, control, owner_layer)
    return obj


def _bvfx_volume(name="volume", center=(0, 0, 150), size=(400, 160, 240), density=None,
                 optical_depth=0.4, color=(0.05, 0.3, 0.1), emission_strength=0.0,
                 noise_scale=2.4, noise_detail=9.0, noise_roughness=0.72,
                 contrast=(0.42, 0.72), stretch=(1.0, 1.0, 1.0), edge_falloff=True,
                 role=None, material_role=None, node_role=None, control=None,
                 owner_layer=None, **_) -> "bpy.types.Object":
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
    # primitive_cube_add(size=1) creates a unit-width mesh. Object scale is therefore
    # the requested full dimension, not its half-extent.
    dom.scale = (size[0], size[1], size[2])
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
        with contextlib.suppress(Exception):
            ec.default_value = (*color, 1.0)
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
    if role:
        _bvfx_role(dom, role, owner_layer)
    if material_role:
        _bvfx_role(m, material_role, owner_layer)
    if node_role:
        _bvfx_role(vol, node_role, owner_layer)
    if control:
        _bvfx_control(vol, control, owner_layer)
    # Flush scale and graph changes before the helper returns so the next observation
    # sees truthful dimensions and tagged hosts without needing an undocumented update.
    bpy.context.view_layer.update()
    return dom


def _bvfx_emissive_windows(obj, window_color=(0.12, 1.0, 0.38), strength=8.0,
                           density=7.0, aspect=2.0, mortar=0.22, body_glow=0.06,
                           **_) -> "bpy.types.Material":
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
        # DESTRUCTIVE, and for a textured asset almost certainly wrong — see the warning
        # this raises. Kept as-is for procedural/untextured meshes, where it is the point.
        _warn_if_textured(obj)
        obj.data.materials.clear()
        obj.data.materials.append(m)
    return m


def _warn_if_textured(obj):
    """Shout if we are about to throw away a baked facade.

    sr2_tower ships three 2048x2048 maps (base colour, normal, ORM) that reproduce the
    design plate almost exactly: dense window cells in strips, the dark recessed core,
    the ribbed piers, the stepped podium, and the "Silk Road 2.0" sign with glowing
    letters. Measured against the plate, the native texture scores facade L1 0.184 and
    outer/core 4.41 against the plate's 3.37.

    bvfx_emissive_windows() clears the material slots and replaces all of it with a
    procedural grid that scores L1 0.242 and outer/core 1.91 — the WRONG polarity. Layer 1
    called it on the hero, so from the first stage onward the real facade was gone, and
    layer 2 then spent five attempts and $78.76 rebuilding by hand what had been discarded
    one layer earlier, including the sign, which is in the texture.

    Use bvfx_emissive_from_texture() on a textured asset instead: it keeps the maps and
    makes their bright cells emit.
    """
    try:
        imgs = [n.image.name
                for m in obj.data.materials if m and m.node_tree
                for n in m.node_tree.nodes
                if n.type == "TEX_IMAGE" and n.image]
    except Exception:
        return
    if imgs:
        print(f"WARNING bvfx_emissive_windows is about to DISCARD {len(imgs)} image "
              f"texture(s) on {obj.name!r} ({', '.join(sorted(set(imgs)))}). This asset "
              f"has a baked facade. Use bvfx_emissive_from_texture(obj) to keep it and "
              f"make the lit cells emit.")


def _bvfx_emissive_from_texture(obj, threshold=0.55, soft=0.10, strength=6.0,
                                tint=(1.0, 1.0, 1.0), body_glow=0.0,
                                **_) -> "list[str]":
    """Make the BRIGHT cells of an asset's OWN base-colour texture emit, keeping the
    texture, the normal map and every bit of baked detail.

    This is the right tool for an imported asset that already looks correct in daylight
    and needs to read at night: windows glow, the body stays dark, and the sign lights up
    because it is painted bright in the same map. Contrast with bvfx_emissive_windows,
    which builds a procedural grid from scratch and destroys the maps.

    `threshold` is on texture luminance (0..1) — above it a texel is treated as a lit
    cell; `soft` is the width of the ramp so cell edges do not alias; `strength` scales
    the emission; `tint` colours it (leave white to keep the texture's own colour);
    `body_glow` adds a faint floor everywhere, for a facade that should not go pure black.
    """
    mats = [m for m in getattr(getattr(obj, "data", None), "materials", []) or []
            if m and m.use_nodes and m.node_tree]
    if not mats:
        raise RuntimeError(f"{getattr(obj, 'name', obj)!r} has no node-based material; "
                           f"bvfx_emissive_from_texture needs the asset's own material")
    touched = []
    for m in mats:
        nt = m.node_tree
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
        surf = next((l for l in nt.links
                     if l.to_node == out and l.to_socket.name == "Surface"), None)
        if out is None or surf is None:
            continue
        base = surf.from_node
        # The base-colour image: prefer the one feeding Base Color, else any image node.
        tex = None
        if base.type == "BSDF_PRINCIPLED":
            tex = next((l.from_node for l in nt.links
                        if l.to_node == base and l.to_socket.name == "Base Color"
                        and l.from_node.type == "TEX_IMAGE"), None)
        if tex is None:
            tex = next((n for n in nt.nodes if n.type == "TEX_IMAGE" and n.image), None)
        if tex is None:
            continue

        lum = nt.nodes.new("ShaderNodeRGBToBW")
        nt.links.new(tex.outputs["Color"], lum.inputs["Color"])
        # Smooth ramp rather than GREATER_THAN: a hard step aliases badly on a 2048 map
        # viewed at 0.1 of frame width, which is exactly how this asset is framed.
        ramp = nt.nodes.new("ShaderNodeMapRange")
        ramp.inputs["From Min"].default_value = max(0.0, threshold - soft)
        ramp.inputs["From Max"].default_value = min(1.0, threshold + soft)
        ramp.inputs["To Min"].default_value = 0.0
        ramp.inputs["To Max"].default_value = 1.0
        ramp.clamp = True
        nt.links.new(lum.outputs[0], ramp.inputs["Value"])

        stg = nt.nodes.new("ShaderNodeMath"); stg.operation = "MULTIPLY_ADD"
        stg.inputs[1].default_value = float(strength)
        stg.inputs[2].default_value = float(body_glow)
        nt.links.new(ramp.outputs[0], stg.inputs[0])

        emi = nt.nodes.new("ShaderNodeEmission")
        # Emit the texture's OWN colour so lit cells keep their painted hue (the sign is
        # cyan in this asset), tinted only if the caller asks.
        if tuple(tint) == (1.0, 1.0, 1.0):
            nt.links.new(tex.outputs["Color"], emi.inputs["Color"])
        else:
            emi.inputs["Color"].default_value = (*tint, 1.0)
        nt.links.new(stg.outputs[0], emi.inputs["Strength"])

        # ADD, not mix: emission sits on top of the shaded surface, so the body still
        # takes light and self-shadows while the windows glow.
        add = nt.nodes.new("ShaderNodeAddShader")
        nt.links.new(base.outputs[0], add.inputs[0])
        nt.links.new(emi.outputs[0], add.inputs[1])
        nt.links.new(add.outputs[0], out.inputs["Surface"])
        touched.append(m.name)
    if not touched:
        raise RuntimeError(f"no image-textured material found on "
                           f"{getattr(obj, 'name', obj)!r}; use bvfx_emissive_windows "
                           f"for an untextured mesh")
    return touched


_PROMOTED_GLB = re.compile(r"^build/construction/[0-9a-f]{64}\.glb$")
_CONSTRUCTION_PIN = "construction_import.json"


def _bvfx_import_construction() -> "list[str]":
    """Import the harness-promoted generate-construction GLB pinned for this unit.

    Replay reads only hash-verified bytes under build/construction/<sha256>.glb.
    Generate units call this with no arguments. Procedural units must author mesh
    in-session; this helper raises without a pin.
    """
    pin = os.path.join(ARTIFACTS, _CONSTRUCTION_PIN)
    if not os.path.isfile(pin):
        raise RuntimeError(
            "bvfx_import_construction has no pin; generate construction is prepared "
            "before the live session. Procedural units must author mesh in-session."
        )
    with open(pin, encoding="utf-8") as handle:
        payload = json.load(handle)
    rel = str(payload.get("glb") or "")
    expected = str(payload.get("sha256") or "")
    if not _PROMOTED_GLB.fullmatch(rel):
        raise RuntimeError(
            f"construction pin names illegal path {rel!r}; replay imports only "
            "build/construction/<sha256>.glb"
        )
    snapshot_glb = payload.get("snapshot_glb")
    if snapshot_glb is None:
        glb = os.path.abspath(rel)
    else:
        if not isinstance(snapshot_glb, str) or not os.path.isabs(snapshot_glb):
            raise RuntimeError("construction snapshot path must be absolute")
        glb = os.path.abspath(snapshot_glb)
        if os.path.realpath(glb) != glb:
            raise RuntimeError("construction snapshot path must contain no symlinks")
        scratch = os.path.realpath(os.path.dirname(ARTIFACTS))
        try:
            relative_snapshot = os.path.relpath(glb, scratch)
        except ValueError as exc:
            raise RuntimeError("construction snapshot is outside run scratch") from exc
        parts = relative_snapshot.split(os.sep)
        if (
            not parts
            or not parts[0].startswith("final-render-chain-")
            or relative_snapshot == os.pardir
            or relative_snapshot.startswith(os.pardir + os.sep)
        ):
            raise RuntimeError("construction snapshot is outside final-render replay authority")
    if not os.path.isfile(glb):
        raise FileNotFoundError(f"promoted construction {rel} is missing")
    with open(glb, "rb") as handle:
        raw = handle.read()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"promoted construction hash mismatch for {rel}: pin {expected} file {actual}"
        )
    before = set(bpy.data.objects.keys())
    bpy.ops.import_scene.gltf(filepath=glb)
    new = [n for n in bpy.data.objects if n not in before]
    for n in new:
        bpy.data.objects[n].rotation_mode = "XYZ"
    return new


def _bvfx_import_asset(name) -> "list[str]":
    """Import a committed, normalized asset (assets/<name>/model.glb) into the live scene
    and return the new object names. Use this in build scripts — the `import_asset` TOOL
    is not in scope inside run_bpy / a build script, but this helper is."""
    pin = os.path.join(ARTIFACTS, _CONSTRUCTION_PIN)
    if os.path.isfile(pin):
        raise RuntimeError(
            "generate construction units call bvfx_import_construction(); "
            "import_asset is not a construction route"
        )
    if not ASSETS_DIR:
        raise RuntimeError("no assets dir configured for this session")
    glb = os.path.join(ASSETS_DIR, name, "model.glb")
    if not os.path.exists(glb):
        avail = sorted(os.listdir(ASSETS_DIR)) if os.path.isdir(ASSETS_DIR) else []
        raise FileNotFoundError(f"no asset {name!r}; available: {avail}")
    before = set(bpy.data.objects.keys())
    bpy.ops.import_scene.gltf(filepath=glb)
    new = [n for n in bpy.data.objects if n not in before]
    # glTF import leaves objects in QUATERNION rotation mode, and in that mode Blender
    # ignores `rotation_euler` ENTIRELY — assigning to it is a silent no-op, not an error.
    # A lookdev turntable written against it produced four "different" angles that were
    # identical to the pixel, and bvfx_aim() below would fail the same silent way on an
    # imported object. Euler is what every build script and helper here actually writes,
    # so normalise on the way in rather than leaving the trap behind the asset boundary.
    for n in new:
        bpy.data.objects[n].rotation_mode = "XYZ"
    return new


def _bvfx_aim(obj, target=(0.0, 0.0, 0.0), up="Y") -> "bpy.types.Object":
    """Point obj (e.g. a camera) at `target`. Accepts tuples OR Vectors safely — avoids
    the `unary -: tuple` footgun of hand-rolled aim math."""
    import mathutils
    bpy.context.view_layer.update()  # matrix_world is stale right after setting .location
    t = mathutils.Vector(target)
    loc = obj.matrix_world.translation
    # Writing rotation_euler on a QUATERNION-mode object does nothing at all, so aiming
    # would silently leave the object pointing wherever it already was.
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = (t - loc).to_track_quat("-Z", up).to_euler()
    return obj


def _bvfx_fcurves(target) -> "list[bpy.types.FCurve]":
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


def _bvfx_interp(target, mode="LINEAR", const=("hide_render", "hide_viewport")) -> "int":
    """Force interpolation on everything keyed on `target` and its data-block; visibility
    goes CONSTANT so a swap is a hard cut, never a half-hidden in-between frame. A string
    resolves first as an object name and then as an exact semantic role.

    Returns the number of curves touched — 0 means you keyed something other than what you
    think you did, which is the failure this exists to make visible. Bezier overshoot on a
    fast ramp is what makes a delta layer non-idempotent, and can drive a one-frame value
    negative between keys that are both positive.
    """
    if isinstance(target, str):
        named = bpy.data.objects.get(target)
        targets = [named] if named is not None else [
            obj for obj in bpy.context.scene.objects
            if str(obj.get("bvfx_role") or "") == target
        ]
    else:
        targets = [target]
    expanded = []
    seen = set()
    for item in targets:
        for candidate in (item, getattr(item, "data", None)):
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            expanded.append(candidate)

    n = 0
    for item in expanded:
        for fc in _bvfx_fcurves(item):
            m = "CONSTANT" if any(c in fc.data_path for c in const) else mode
            for kp in fc.keyframe_points:
                kp.interpolation = m
                if m == "BEZIER":
                    kp.handle_left_type = kp.handle_right_type = "AUTO_CLAMPED"
            fc.update()
            n += 1
    return n


def _bvfx_camera_rig(name="cam_rig", lens=35.0, sensor=36.0, clip=(0.5, 20000.0),
                     spine=(), ladder=(), display=4.0, role=None,
                     owner_layer=None) -> "tuple[bpy.types.Object, bpy.types.Object]":
    """The two-object camera: an EMPTY owns location+pitch, the camera child owns ROLL on
    its own local Z. On a bare camera `rotation_euler[2]` is world YAW and swings the
    subject out of frame; under the rig the view axis IS local Z, so the frame rotates
    about its own centre. Returns (rig, cam).

      spine:  [(frame, distance, altitude, pitch_up_deg), ...] subject on +Y at origin.
              Keyed BEZIER — one smooth travel; a velocity step reads as a camera bump.
      ladder: [(frame, roll_deg), ...] keyed LINEAR — a roll ladder's segment RATES are
              the look, and bezier drags the peak rate off the frames you keyed it on.

    Pitch is `radians(90 + p)`: a camera looks down its own -Z, so LEVEL is 90, not 0."""
    # Both objects persist, so both need a semantic role or a scoped unit inherits a
    # canonical-replay rejection it cannot repair (the helper recreates them on every
    # call). The role is REQUIRED and never derived from `name`: a display name is a
    # label, not authority — defaulting to it tagged run 20260824T052204Z's objects
    # `CAM_spine`/`CAM_spine.camera` while its declared scope was `cam_rig`. Checked
    # before anything is created, so a refused call leaves the scene untouched.
    if not role:
        raise ValueError(
            "bvfx_camera_rig(role=...) is required: both objects it creates persist in "
            "the scene and must carry a semantic role inside your unit's declared "
            "mutation scope. Pass the role your unit owns, not the display name."
        )
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
    _bvfx_role(rig, role, owner_layer)
    _bvfx_role(cam, f"{role}.camera", owner_layer)
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


def _bvfx_role(obj, role, owner_layer=None) -> Any:
    """Attach a stable semantic contract role to any Blender custom-property host.

    Object names remain useful labels, but they are not an API: Blender suffixes names
    during duplication and artists rename objects while iterating.  Custom properties
    survive both operations and let scene contracts address intent instead of spelling.
    """
    if isinstance(obj, str):
        obj = bpy.data.objects.get(obj)
    if obj is None:
        raise ValueError("bvfx_role: object does not exist")
    import checks

    role = checks.validate_role_token(role)
    # Semantic identity belongs to its owner. Run 20260826 (build14) rewrote a layer-1
    # marker's role to lookdev.* so an assignment-fraction denominator would shrink —
    # a pass-by-theft no closure could see, because the audit is creation-based and the
    # owner's contracts select by the very role being stolen. Dressing (ADR-0007)
    # grants material assignment, never identity.
    current_owner = obj.get("bvfx_owner_layer")
    if (
        current_owner is not None
        and owner_layer is not None
        and str(current_owner) != str(owner_layer)
    ):
        raise ValueError(
            f"bvfx_role: {getattr(obj, 'name', '<node>')!r} is owned by layer "
            f"{current_owner} (role {obj.get('bvfx_role')!r}); layer {owner_layer} may "
            "not retag it — another layer's semantic identity is never yours to rewrite"
        )
    obj["bvfx_role"] = role
    if owner_layer is not None:
        obj["bvfx_owner_layer"] = str(owner_layer)
    return obj


def _bvfx_control(target, role, owner_layer=None) -> Any:
    """Tag a host as a control without replacing its existing semantic role."""
    if isinstance(target, str):
        target = bpy.data.objects.get(target)
    if target is None:
        raise ValueError("bvfx_control: target does not exist")
    import checks
    from semantic_tags import tag_control

    return tag_control(
        target,
        role,
        owner_layer,
        validate=checks.validate_role_token,
    )


_HELPERS = {
    "bvfx_emission": _bvfx_emission,
    "bvfx_emissive_windows": _bvfx_emissive_windows,
    "bvfx_emissive_from_texture": _bvfx_emissive_from_texture,
    "bvfx_import_asset": _bvfx_import_asset,
    "bvfx_import_construction": _bvfx_import_construction,
    "bvfx_aim": _bvfx_aim,
    "bvfx_scatter_emissive": _bvfx_scatter_emissive,
    "bvfx_volumetric_world": _bvfx_volumetric_world,
    "bvfx_volume": _bvfx_volume,
    "bvfx_glare_bloom": _bvfx_glare_bloom,
    "bvfx_vector_blur": _bvfx_vector_blur,
    "bvfx_light": _bvfx_light,
    "bvfx_fcurves": _bvfx_fcurves,
    "bvfx_interp": _bvfx_interp,
    "bvfx_camera_rig": _bvfx_camera_rig,
    "bvfx_role": _bvfx_role,
    "bvfx_control": _bvfx_control,
}


# ---- handlers ---------------------------------------------------------------

def h_ping(a: dict) -> dict:
    return {"blender": bpy.app.version_string, "eevee": _eevee_engine()}


# Blender 4.x attribute -> the 5.x way. Surfaced inline so a wrong guess costs one
# tool call instead of a retry loop.
_ATTR_HINTS = {
    "FromMesh": "BVHTree.FromMesh is not the 5.x constructor (use FromBMesh) and is "
                "not the clearance instrument. Use the bound path_clearance_min "
                "contract / closest_point_on_mesh, or check_scene(kind='motion') "
                "for smoothness — do not invent a nearest-point probe in run_bpy.",
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
    "'Action' object has no attribute 'fcurves'":
        "5.x actions are SLOTTED — curves live at action.layers[*].strips[*]"
        ".channelbags[*].fcurves, and a channelbag binds one slot (match it to the "
        "target via adt.action_slot). Walk that path, or read curves through the "
        "existing helpers (bvfx_fcurves(target) inside worker scripts; the "
        "milestone-delta-keying recipe shows the full pattern).",
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

    if isinstance(e, RuntimeError) and "Node type" in msg and "undefined" in msg and "Compositor" in msg:
        # 5.x unified node trees: the value/math/ramp utility nodes exist only under
        # their Shader* names, and sessions reach for CompositorNodeValue/Math/
        # ValToRGB/MixColor by habit — four sessions burned turns on this exact
        # RuntimeError before one thought to poll node_type_poll.
        return RuntimeError(
            f"{msg}\nHINT: 5.x compositor trees are UNIFIED — utility nodes keep their "
            "Shader* idnames inside compositor groups: use ShaderNodeValue, "
            "ShaderNodeMath, ShaderNodeValToRGB, ShaderNodeMix. Compositor-specific "
            "nodes (Glare, ColorBalance, AlphaOver, RGBToBW) keep CompositorNode* "
            "names. When unsure: nt.bl_rna ... node_type_poll, or inspect_nodes."
        )

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
    # `limit` is a snapshot's write-ahead `journal_index`: the calls accepted up to that
    # checkpoint. Dumping the FULL journal after restoring an earlier checkpoint writes
    # calls the scene no longer contains — run 20260823T154920Z restored round 1 and
    # published round 2's rejected key light and tunnel taper into the unit script.
    start = max(0, int(a.get("start", 0)))
    limit = a.get("limit")
    stop = len(_JOURNAL) if limit is None else min(len(_JOURNAL), int(limit))
    stop = max(start, stop)
    entries = _JOURNAL[start:stop]
    dropped_before = min(start, len(_JOURNAL))
    dropped = len(_JOURNAL) - stop
    body = "\n\n# ---- next accepted run_bpy call ----\n".join(entries)
    path = a.get("path")
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# JOURNAL — {len(entries)} active-unit run_bpy calls, in order.\n"
                     + (f"# Excluded {dropped_before} inherited setup/prior call(s).\n"
                        if dropped_before else "")
                     + (f"# Truncated to the selected checkpoint: {dropped} later call(s) "
                        f"from discarded rounds excluded.\n" if dropped else "")
                     + "# Superseded tweaks and probes included: PRUNE, do not paste.\n\n"
                     + body + "\n")
    return {
        "calls": len(entries),
        "chars": len(body),
        "path": path,
        "start": start,
        "dropped_before": dropped_before,
        "dropped": dropped,
    }


def h_run(a: dict) -> dict:
    """Exec arbitrary bpy code. Set `RESULT = <json-able>` to return data.

    `bvfx_*` helpers, `mathutils`, `Vector` and `math` are pre-injected into scope (each
    call is a FRESH namespace — imports don't persist between calls, which is why raw-
    tuple math kept recurring). Returns timing + scene-delta so the agent feels cost."""

    import mathutils as _mathutils
    ns: dict = {"bpy": bpy, "math": _math, "mathutils": _mathutils,
                "Vector": _mathutils.Vector, **_HELPERS}
    before = _scene_stats()
    buf = io.StringIO()
    t0 = time.monotonic()
    rollback_path = None
    if a.get("transactional"):
        rollback_path = os.path.join(ARTIFACTS, "run_bpy_rollback.blend")
        # Blender background mode cannot use bpy.ops.ed.undo (its poll fails without an
        # editor context).  A same-worker copy is the deterministic transaction boundary.
        bpy.ops.wm.save_as_mainfile(
            filepath=rollback_path,
            compress=False,
            copy=True,
        )
    with contextlib.redirect_stdout(buf):
        try:
            exec(a["code"], ns)
        except Exception as e:
            if rollback_path:
                try:
                    bpy.ops.wm.open_mainfile(filepath=rollback_path)
                except Exception as restore_error:
                    raise RuntimeError(
                        "run_bpy mutation failed and rollback also failed: "
                        f"{type(restore_error).__name__}: {restore_error}"
                    ) from e
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(rollback_path)
            better = _hinted(e)
            if better is None:
                raise
            # `from None` keeps the traceback tail on OUR line: the enriched message is
            # the last thing printed, which is also the part the log now preserves.
            raise better.with_traceback(e.__traceback__) from None
    if rollback_path:
        with contextlib.suppress(OSError):
            os.unlink(rollback_path)
    elapsed = time.monotonic() - t0
    # Only successful code is journalled: the builder currently re-authors the whole
    # layer from memory at finalize (~28KB of live calls -> a 23KB script), which is
    # duplicated effort AND the only reason live and canonical can diverge.
    # Measurements use the same execution engine but are not authored scene mutations.
    # Journalling them bloats finalisation input and makes a canonical replay run probes
    # that were never part of the build. Callers mark those with journal=False.
    if a.get("journal", True):
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


def _refresh_inspection_scene(frame=None):
    """Return one freshly evaluated scene/depsgraph for typed inspection.

    Blender may leave ``matrix_world`` at its pre-script value after a direct local
    transform assignment until the dependency graph is evaluated. Re-setting even the
    current frame is intentional: omitted ``frame=`` means "inspect now", not "accept
    whatever evaluation another tool happened to trigger" (HIR-0116).
    """
    sc = bpy.context.scene
    selected_frame = int(sc.frame_current if frame is None else frame)
    sc.frame_set(selected_frame)
    bpy.context.view_layer.update()
    return sc, bpy.context.evaluated_depsgraph_get()


def _observation_json_value(value):
    """Return a compact, deterministic JSON value for a Blender RNA scalar/vector."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("observation environment contains a non-finite value")
        return round(value, 8)
    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes, dict)):
        return [_observation_json_value(item) for item in value]
    return str(value)


def _observation_matrix(matrix):
    return [
        [_observation_json_value(float(matrix[row][column])) for column in range(4)]
        for row in range(4)
    ]


def _observation_node_tree(tree):
    if tree is None:
        return None
    nodes = []
    for node in sorted(tree.nodes, key=lambda item: str(item.name)):
        inputs = []
        for socket in sorted(node.inputs, key=lambda item: str(item.name)):
            value = None
            if not socket.is_linked and hasattr(socket, "default_value"):
                with contextlib.suppress(TypeError, ValueError):
                    value = _observation_json_value(socket.default_value)
            inputs.append({
                "name": str(socket.name),
                "linked": bool(socket.is_linked),
                "value": value,
            })
        nodes.append({
            "name": str(node.name),
            "type": str(node.bl_idname),
            "role": str(node.get("bvfx_role") or ""),
            "mute": bool(getattr(node, "mute", False)),
            "inputs": inputs,
        })
    links = sorted(
        (
            {
                "from_node": str(link.from_node.name),
                "from_socket": str(link.from_socket.name),
                "to_node": str(link.to_node.name),
                "to_socket": str(link.to_socket.name),
            }
            for link in tree.links
        ),
        key=lambda item: (
            item["from_node"], item["from_socket"], item["to_node"], item["to_socket"]
        ),
    )
    return {"name": str(tree.name), "nodes": nodes, "links": links}


def _observation_bbox(obj):
    from mathutils import Vector

    if obj.type not in {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME"}:
        return None
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    if not corners:
        return None
    return {
        "min": [round(min(float(point[index]) for point in corners), 8) for index in range(3)],
        "max": [round(max(float(point[index]) for point in corners), 8) for index in range(3)],
    }


def _observation_subject(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    return {
        "name": str(obj.name),
        "role": str(obj.get("bvfx_role") or ""),
        "type": str(evaluated.type),
        "matrix_world": _observation_matrix(evaluated.matrix_world),
        "bbox_world": _observation_bbox(evaluated),
        "hide_render": bool(obj.hide_render),
        "hide_viewport": bool(obj.hide_viewport),
        "visible_view_layer": bool(obj.visible_get()),
        "visible_camera": bool(getattr(obj, "visible_camera", True)),
        "materials": [
            {
                "name": str(slot.material.name),
                "role": str(slot.material.get("bvfx_role") or ""),
                "use_nodes": bool(slot.material.use_nodes),
            }
            for slot in obj.material_slots
            if slot.material is not None
        ],
    }


def _observation_light(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    data = evaluated.data
    return {
        "name": str(obj.name),
        "role": str(obj.get("bvfx_role") or ""),
        "type": str(data.type),
        "matrix_world": _observation_matrix(evaluated.matrix_world),
        "energy": _observation_json_value(float(data.energy)),
        "color": _observation_json_value(data.color),
        "shape": str(getattr(data, "shape", "")),
        "size": _observation_json_value(getattr(data, "size", None)),
        "size_y": _observation_json_value(getattr(data, "size_y", None)),
        "use_shadow": bool(getattr(data, "use_shadow", False)),
        "hide_render": bool(obj.hide_render),
        "visible_camera": bool(getattr(obj, "visible_camera", True)),
    }


def _observation_node_carrier(node, *, family, graph):
    return {
        "family": family,
        "host_kind": f"{graph}_node",
        "graph": graph,
        "name": str(node.name),
        "role": str(node.get("bvfx_role") or ""),
        "type": str(node.bl_idname),
    }


def _observation_carriers(sc, depsgraph, roles, families, checks):
    """Resolve debt subjects only through their declared carrier-family authority."""
    carriers = []
    world_tree = sc.world.node_tree if sc.world and sc.world.use_nodes else None
    compositor_tree = getattr(sc, "compositing_node_group", None)
    for role in roles:
        matched = []
        if "mesh" in families:
            matched.extend(
                {
                    "family": "mesh",
                    "host_kind": "object",
                    **_observation_subject(obj, depsgraph),
                }
                for obj in sc.objects
                if obj.type == "MESH"
                and checks.match_semantic(str(obj.get("bvfx_role") or ""), [role])
            )
        if "volume" in families:
            matched.extend(
                {
                    "family": "volume",
                    "host_kind": "object",
                    **_observation_subject(obj, depsgraph),
                }
                for obj in sc.objects
                if obj.type == "VOLUME"
                and checks.match_semantic(str(obj.get("bvfx_role") or ""), [role])
            )
            if world_tree is not None:
                matched.extend(
                    _observation_node_carrier(node, family="volume", graph="world")
                    for node in world_tree.nodes
                    if node.type in {"VOLUME_SCATTER", "PRINCIPLED_VOLUME", "VOLUME_PRINCIPLED"}
                    and checks.match_semantic(str(node.get("bvfx_role") or ""), [role])
                )
        if "compositor" in families and compositor_tree is not None:
            if checks.match_semantic(str(compositor_tree.get("bvfx_role") or ""), [role]):
                matched.append({
                    "family": "compositor",
                    "host_kind": "compositor_group",
                    "graph": "compositor",
                    "name": str(compositor_tree.name),
                    "role": str(compositor_tree.get("bvfx_role") or ""),
                    "type": "NODE_GROUP",
                })
            for node in compositor_tree.nodes:
                node_role = str(node.get("bvfx_role") or "")
                group_role = str(
                    (getattr(node, "node_tree", None) and node.node_tree.get("bvfx_role")) or ""
                )
                if checks.match_semantic(node_role, [role]):
                    matched.append(_observation_node_carrier(node, family="compositor", graph="compositor"))
                elif group_role and checks.match_semantic(group_role, [role]):
                    matched.append({
                        "family": "compositor",
                        "host_kind": "compositor_group_instance",
                        "graph": "compositor",
                        "name": str(node.name),
                        "role": group_role,
                        "type": str(node.bl_idname),
                    })
        if not matched:
            inventory = checks.object_inventory()
            raise ValueError(
                checks.format_object_miss(inventory=inventory, role=role)
                + "; no matching rendered carrier exists in allowed families "
                + ", ".join(families)
            )
        carriers.extend(matched)
    return sorted(
        carriers,
        key=lambda row: (
            str(row["family"]), str(row["role"]), str(row["host_kind"]), str(row["name"])
        ),
    )


def h_observation_environment(a: dict) -> dict:
    """Read the canonical scene inputs of a qualitative observation without rendering."""
    import checks
    from observation_environment import (
        SCHEMA,
        canonical_observation_environment,
        validate_observation_request,
    )

    frame, roles, medium, families = validate_observation_request(
        a.get("frame"),
        a.get("subject_roles"),
        a.get("observation_medium"),
        a.get("carrier_families"),
    )
    sc, depsgraph = _refresh_inspection_scene(frame)
    if sc.camera is None or sc.camera.type != "CAMERA":
        raise ValueError("canonical observation requires an active camera")

    carriers = _observation_carriers(sc, depsgraph, roles, families, checks)

    camera = sc.camera.evaluated_get(depsgraph)
    camera_data = camera.data
    render = sc.render
    image_settings = render.image_settings
    view = sc.view_settings
    display = sc.display_settings
    world = sc.world
    eevee = getattr(sc, "eevee", None)
    workbench = getattr(getattr(sc, "display", None), "shading", None)
    snapshot = {
        "schema": SCHEMA,
        "frame": frame,
        "observation_medium": medium,
        "subject_roles": list(roles),
        "carrier_families": list(families),
        "blender": {
            "version": list(bpy.app.version),
            "version_string": str(bpy.app.version_string),
            "build_hash": str(getattr(bpy.app, "build_hash", "")),
            "build_branch": str(getattr(bpy.app, "build_branch", "")),
            "build_type": str(getattr(bpy.app, "build_type", "")),
        },
        "render": {
            "engine": str(render.engine),
            "resolution": [int(render.resolution_x), int(render.resolution_y)],
            "resolution_percentage": int(render.resolution_percentage),
            "film_transparent": bool(getattr(render, "film_transparent", False)),
            "fps": int(render.fps),
            "motion_blur": bool(getattr(render, "use_motion_blur", False)),
            "image_settings": {
                "file_format": str(image_settings.file_format),
                "color_mode": str(image_settings.color_mode),
                "color_depth": str(image_settings.color_depth),
            },
        },
        "color_management": {
            "display_device": str(getattr(display, "display_device", "")),
            "view_transform": str(getattr(view, "view_transform", "")),
            "look": str(getattr(view, "look", "")),
            "exposure": _observation_json_value(float(getattr(view, "exposure", 0.0))),
            "gamma": _observation_json_value(float(getattr(view, "gamma", 1.0))),
            "sequencer_colorspace": str(
                getattr(getattr(view, "sequencer_colorspace_settings", None), "name", "")
            ),
        },
        "camera": {
            "name": str(camera.name),
            "role": str(sc.camera.get("bvfx_role") or ""),
            "matrix_world": _observation_matrix(camera.matrix_world),
            "type": str(camera_data.type),
            "lens": _observation_json_value(float(camera_data.lens)),
            "sensor_width": _observation_json_value(float(camera_data.sensor_width)),
            "sensor_height": _observation_json_value(float(camera_data.sensor_height)),
            "shift": [
                _observation_json_value(float(camera_data.shift_x)),
                _observation_json_value(float(camera_data.shift_y)),
            ],
            "clip": [
                _observation_json_value(float(camera_data.clip_start)),
                _observation_json_value(float(camera_data.clip_end)),
            ],
        },
        "subject_carriers": carriers,
        "world": {
            "name": str(world.name) if world is not None else None,
            "use_nodes": bool(world and world.use_nodes),
            "color": _observation_json_value(world.color) if world is not None else None,
            "nodes": _observation_node_tree(world.node_tree) if world and world.use_nodes else None,
        },
        "compositor": _observation_node_tree(getattr(sc, "compositing_node_group", None)),
        "view_layers": [
            {
                "name": str(layer.name),
                "use": bool(getattr(layer, "use", True)),
                "use_pass_z": bool(getattr(layer, "use_pass_z", False)),
                "use_pass_vector": bool(getattr(layer, "use_pass_vector", False)),
                "use_pass_normal": bool(getattr(layer, "use_pass_normal", False)),
            }
            for layer in sorted(sc.view_layers, key=lambda item: str(item.name))
        ],
        "active_view_layer": str(bpy.context.view_layer.name),
        "lights": [
            _observation_light(obj, depsgraph)
            for obj in sorted(
                (item for item in sc.objects if item.type == "LIGHT"),
                key=lambda item: (str(item.get("bvfx_role") or ""), item.name),
            )
        ],
        "eevee": {
            key: _observation_json_value(getattr(eevee, key, None))
            for key in (
                "taa_render_samples",
                "taa_samples",
                "use_gtao",
                "gtao_distance",
                "gtao_factor",
                "use_bloom",
                "volumetric_start",
                "volumetric_end",
                "volumetric_samples",
                "use_volumetric_shadows",
            )
        },
        "workbench": {
            key: _observation_json_value(getattr(workbench, key, None))
            for key in (
                "light",
                "studio_light",
                "color_type",
                "single_color",
                "background_type",
                "background_color",
                "show_shadows",
                "show_cavity",
                "cavity_type",
                "show_object_outline",
            )
        },
    }
    return canonical_observation_environment(snapshot)


def h_inspect(a: dict) -> dict:
    import checks

    section = a.get("section", "all")
    role_filter = str(a.get("role") or "").strip() or None
    sc, depsgraph = _refresh_inspection_scene(a.get("frame"))
    out: list[str] = []
    if section in ("all", "render"):
        from scene_report import render_status_lines

        out.extend(render_status_lines(sc, _scene_stats()))
    if section in ("all", "world"):
        if sc.world:
            out.append(f"world: {sc.world.name} use_nodes={sc.world.use_nodes}")
        elif section == "world":
            out.append("world: (none)")
    if section in ("all", "objects"):
        from mathutils import Vector

        out.append("objects:")
        shown = 0
        for o in sc.objects:
            role = str(o.get("bvfx_role") or "")
            if role_filter and not checks.match_semantic(role, [role_filter]):
                continue
            shown += 1
            evaluated = o.evaluated_get(depsgraph)
            owner = str(o.get("bvfx_owner_layer") or "-")
            loc = tuple(round(v, 2) for v in o.location)
            world_loc = tuple(
                round(v, 2) for v in evaluated.matrix_world.translation
            )
            dims = tuple(round(v, 2) for v in evaluated.dimensions)
            world_bbox_min = world_bbox_max = None
            if evaluated.type in {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME"}:
                corners = [evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box]
                if corners:
                    world_bbox_min = tuple(
                        round(min(float(point[index]) for point in corners), 4)
                        for index in range(3)
                    )
                    world_bbox_max = tuple(
                        round(max(float(point[index]) for point in corners), 4)
                        for index in range(3)
                    )
            mods = ",".join(m.type for m in o.modifiers) or "-"
            psys = ",".join(p.name for p in getattr(o, "particle_systems", [])) or "-"
            out.append(
                f"  {o.name} [{o.type}] role={role or '-'} owner={owner} "
                f"loc={loc} world_loc={world_loc} dims={dims} "
                f"world_bbox_min={world_bbox_min or '-'} "
                f"world_bbox_max={world_bbox_max or '-'} "
                f"hide_render={bool(o.hide_render)} mods={mods} psys={psys}"
            )
        if role_filter and shown == 0:
            raise ValueError(
                checks.format_object_miss(inventory=checks.object_inventory(), role=role_filter)
            )
    if section in ("all", "materials"):
        from scene_report import material_status_lines

        out.extend(material_status_lines(sc, bpy.data.materials))
    if section in ("all", "lights"):
        from scene_report import light_status_line

        out.append("lights:")
        lights = [o for o in sc.objects if o.type == "LIGHT"]
        for o in lights:
            out.append(light_status_line(o.evaluated_get(depsgraph)))
        if not lights:
            out.append("  (none)")
    if section in ("all", "cameras"):
        from mathutils import Vector

        out.append("cameras:")
        cameras = [o for o in sc.objects if o.type == "CAMERA"]
        for o in cameras:
            evaluated = o.evaluated_get(depsgraph)
            world = evaluated.matrix_world
            forward = (world.to_quaternion() @ Vector((0.0, 0.0, -1.0))).normalized()
            out.append(
                f"  {o.name} role={o.get('bvfx_role') or '-'!s} "
                f"owner={o.get('bvfx_owner_layer') or '-'!s} "
                f"world_loc={tuple(round(float(v), 4) for v in world.translation)} "
                f"forward={tuple(round(float(v), 6) for v in forward)} "
                f"lens={round(float(evaluated.data.lens), 4)} "
                f"sensor_width={round(float(evaluated.data.sensor_width), 4)} "
                f"active={o == sc.camera}"
            )
        if not cameras:
            out.append("  (none)")
    return {"text": "\n".join(out)}


def h_nodes(a: dict) -> dict:
    """Dump a node tree as text — nodes, socket values/names, and links.
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
    from node_report import format_node_tree

    return {"text": format_node_tree(nt, title)}


def h_black_context(a: dict) -> dict:
    """Scene facts that disambiguate a nearly black beauty render."""
    from black_frame_report import format_black_frame_context

    sc = bpy.context.scene
    frame = int(a.get("frame", sc.frame_current))
    sc.frame_set(frame)
    world = sc.world
    volume_rows = []
    background_strength = None
    nt = world.node_tree if (world and world.use_nodes) else None
    if nt is not None:
        for node in nt.nodes:
            if node.type == "BACKGROUND":
                socket = node.inputs.get("Strength")
                if socket is not None and not socket.is_linked:
                    background_strength = float(socket.default_value)
            if node.type not in {"VOLUME_SCATTER", "PRINCIPLED_VOLUME", "VOLUME_PRINCIPLED"}:
                continue
            socket = node.inputs.get("Density")
            if socket is None:
                continue
            volume_rows.append(
                {
                    "name": node.name,
                    "role": str(node.get("bvfx_role") or ""),
                    "linked": bool(socket.is_linked),
                    "density": None if socket.is_linked else float(socket.default_value),
                }
            )
    camera = sc.camera
    camera_location = camera.matrix_world.translation if camera is not None else None
    subjects = []
    for obj in sc.objects:
        role = str(obj.get("bvfx_role") or "")
        if obj.type != "MESH" or not role:
            continue
        distance = (
            float((obj.matrix_world.translation - camera_location).length)
            if camera_location is not None
            else 0.0
        )
        subjects.append(
            {"name": obj.name, "role": role, "camera_distance": distance}
        )
    lights = []
    for obj in sc.objects:
        if obj.type != "LIGHT":
            continue
        distance = (
            float((obj.matrix_world.translation - camera_location).length)
            if camera_location is not None
            else 0.0
        )
        lights.append(
            {
                "name": obj.name,
                "role": str(obj.get("bvfx_role") or ""),
                "energy": float(obj.data.energy),
                "camera_distance": distance,
            }
        )
    camera_clip_end = float(camera.data.clip_end) if camera is not None else None
    eevee = getattr(sc, "eevee", None)
    volumetric_start = getattr(eevee, "volumetric_start", None)
    volumetric_end = getattr(eevee, "volumetric_end", None)
    return {
        "text": format_black_frame_context(
            frame=frame,
            camera_clip_end=camera_clip_end,
            volume_rows=volume_rows,
            background_strength=background_strength,
            lights=lights,
            volumetric_start=volumetric_start,
            volumetric_end=volumetric_end,
            subjects=subjects,
        ),
        "camera_clip_end": camera_clip_end,
        "volume_rows": volume_rows,
        "background_strength": background_strength,
        "lights": lights,
        "volumetric_start": volumetric_start,
        "volumetric_end": volumetric_end,
        "subjects": subjects,
    }


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
    import bpy
    import checks

    role = str(a.get("role") or "").strip() or None
    name = str(a.get("object") or "").strip() or None
    inventory = checks.object_inventory()
    hits = checks.pick_objects(inventory, role=role, name=name)
    if not hits:
        raise ValueError(checks.format_object_miss(inventory=inventory, role=role, name=name))
    lines: list[str] = []
    if role and len(hits) > 1:
        lines.append(
            f"role {role!r} matched {len(hits)} objects; listing every host "
            "(check_scene still needs object= for a single-subject kind)"
        )
    for row in hits:
        obj = bpy.data.objects.get(row["name"])
        if obj is None:
            continue
        listed = False
        ad = obj.animation_data
        if ad and ad.action:
            listed = True
            lines.append(f"{obj.name}: action {ad.action.name}")
            for fc in _action_fcurves(obj, ad.action):
                keys = [(round(k.co[0], 1), round(k.co[1], 3), k.interpolation) for k in fc.keyframe_points]
                lines.append(f"  {fc.data_path}[{fc.array_index}]: {keys}")
        data = getattr(obj, "data", None)
        dad = getattr(data, "animation_data", None) if data is not None else None
        if dad and dad.action:
            listed = True
            lines.append(f"{obj.name}.data: action {dad.action.name}")
            for fc in _action_fcurves(data, dad.action):
                keys = [(round(k.co[0], 1), round(k.co[1], 3), k.interpolation) for k in fc.keyframe_points]
                path = fc.data_path
                shown = path if str(path).startswith("data.") else f"data.{path}"
                lines.append(f"  {shown}[{fc.array_index}]: {keys}")
        if not listed:
            lines.append(f"{obj.name}: no animation")
    return {"text": "\n".join(lines)}


def _scene_warnings() -> list:
    """Scene states that render as a plausible picture while silently deleting the thing
    the builder just asked for. Returned with every render, because the render itself
    looks fine and gives no reason to suspect anything.

    THE SUN-IN-A-WORLD-VOLUME TRAP. A sun is infinitely distant, so its shadow ray through
    an unbounded homogeneous world volume accumulates unbounded optical depth and arrives
    at zero. Measured on this shot: a white 0.8-albedo body under a sun at energy 25
    renders at mean 6.74/255 with the world volume linked, and 216 without it. It is a
    cliff, not a gradient -- density 6e-6 is bit-identical to no volume, 6e-5 collapses --
    and NO volumetric setting rescues it (shadows off, custom end, 256 samples all render
    identically). A local AREA/POINT/SPOT light at the same atmosphere reads 154.

    This cost five layer-2 attempts and $78.76. Every builder that reached for a key light
    saw a black tower, concluded lighting does not work in this scene, and fell back to
    emissive geometry -- which cannot show faceting, which is what it was asked for.
    """
    out = []
    sc = bpy.context.scene
    w = sc.world
    vol_linked = bool(
        w and w.use_nodes and w.node_tree and any(
            l.to_socket.name == "Volume" and l.to_node.type == "OUTPUT_WORLD"
            for l in w.node_tree.links))
    if vol_linked:
        suns = [o.name for o in sc.objects
                if o.type == "LIGHT" and o.data.type == "SUN" and not o.hide_render]
        if suns:
            out.append(
                f"SUN + WORLD VOLUME: {', '.join(suns)} will contribute almost nothing. "
                f"A sun is infinitely distant, so its light is fully extinguished by an "
                f"unbounded world volume (measured on this shot: 6.74/255 vs 216 with the "
                f"volume unlinked). No volumetric setting fixes it. Use a LOCAL light "
                f"(AREA/POINT/SPOT) placed near the subject, or unlink the world Volume.")
    return out


def h_render(a: dict) -> dict:
    sc = bpy.context.scene
    image_settings = sc.render.image_settings
    original = {
        "engine": sc.render.engine,
        "resolution_percentage": sc.render.resolution_percentage,
        "filepath": sc.render.filepath,
        "frame": sc.frame_current,
        "shading_type": sc.display.shading.type,
        "file_format": image_settings.file_format,
        "media_type": getattr(image_settings, "media_type", None),
        "taa_render_samples": getattr(getattr(sc, "eevee", None), "taa_render_samples", None),
    }
    frame = int(a["frame"])
    mode = a.get("mode", "eevee")
    scale = float(a.get("scale", 0.5))
    engines = {"eevee": _eevee_engine(), "draft": _eevee_engine(),
               "solid": "BLENDER_WORKBENCH", "wire": "BLENDER_WORKBENCH"}
    sc.render.engine = engines.get(mode, _eevee_engine())
    if mode in ("solid", "wire"):
        sc.display.shading.type = "WIREFRAME" if mode == "wire" else "SOLID"
    if mode == "draft":  # fast, low-sample eevee for iteration (full 'eevee' for the layer)
        with contextlib.suppress(AttributeError):
            sc.eevee.taa_render_samples = int(a.get("samples", 8))
    elif mode == "eevee" and "samples" in a:
        with contextlib.suppress(AttributeError):
            sc.eevee.taa_render_samples = int(a["samples"])

    # Phase 2 knobs. Absent = the old behaviour, bit for bit: the critic's canonical
    # path must not move because a diagnostic mode exists.
    pass_name = a.get("pass") or "beauty"
    shade = a.get("shade") or "beauty"
    light = a.get("light") or None
    crop = a.get("crop") or None
    res_pct = a.get("res_pct")
    extended = not (pass_name == "beauty" and shade == "beauty"
                    and light is None and crop is None and res_pct is None)

    # Evaluate animated settings at the requested frame before a diagnostic snapshots or
    # overrides them. Otherwise frame_set below would overwrite a normalized light energy.
    sc.frame_set(frame)
    undo, caption, did = [], "", {}
    if extended:
        import render_ext  # same directory as this worker
        undo, caption, did = render_ext.configure(
            sc, pass_name=pass_name, light=light, shade=shade,
            crop=crop, res_pct=res_pct, scale=scale)
    else:
        sc.render.resolution_percentage = max(1, min(100, int(scale * 100)))

    tag = str(a.get("artifact_tag") or (
        mode if not extended else f"{mode}_{shade if shade != 'beauty' else pass_name}"
    ))
    tag = "".join(char if char.isalnum() or char in "_-" else "-" for char in tag)
    tag = tag.replace(":", "-")
    path = os.path.join(ARTIFACTS, f"{tag}_f{frame:04d}.png")
    sc.render.filepath = path
    _set_image_format(sc.render.image_settings, "PNG")
    # Read the settings the render ACTUALLY used, before restore puts them back. Reading
    # them afterwards reported resolution_percentage=50 for a render made at 400 and made
    # a working optical zoom look like a broken one.
    used = [sc.render.resolution_x, sc.render.resolution_y,
            sc.render.resolution_percentage]
    used_render_state = {
        "engine": str(sc.render.engine),
        "resolution": list(used),
        "image_settings": {
            "file_format": str(image_settings.file_format),
            "color_mode": str(image_settings.color_mode),
            "color_depth": str(image_settings.color_depth),
        },
        "workbench_shading": str(sc.display.shading.type),
        "eevee_render_samples": _observation_json_value(
            getattr(getattr(sc, "eevee", None), "taa_render_samples", None)
        ),
        "color_management": {
            "display_device": str(getattr(sc.display_settings, "display_device", "")),
            "view_transform": str(getattr(sc.view_settings, "view_transform", "")),
            "look": str(getattr(sc.view_settings, "look", "")),
            "exposure": _observation_json_value(
                float(getattr(sc.view_settings, "exposure", 0.0))
            ),
            "gamma": _observation_json_value(
                float(getattr(sc.view_settings, "gamma", 1.0))
            ),
        },
    }
    warnings = []
    try:
        bpy.ops.render.render(write_still=True)
        warnings = _scene_warnings()
    finally:
        if extended:
            import render_ext
            render_ext.restore(undo)
        # render_frame/render_pass/compare_frame are observations. Restore every
        # setting the common path touches, including Workbench engine and frame.
        sc.render.engine = original["engine"]
        sc.render.resolution_percentage = original["resolution_percentage"]
        sc.render.filepath = original["filepath"]
        sc.display.shading.type = original["shading_type"]
        if original["media_type"] is not None and hasattr(image_settings, "media_type"):
            with contextlib.suppress(Exception):
                image_settings.media_type = original["media_type"]
        with contextlib.suppress(Exception):
            image_settings.file_format = original["file_format"]
        if original["taa_render_samples"] is not None:
            with contextlib.suppress(AttributeError):
                sc.eevee.taa_render_samples = original["taa_render_samples"]
        sc.frame_set(original["frame"])
    out = {"image_path": path, "frame": frame, "mode": mode,
           "warnings": warnings,
           "resolution": used, "render_state": used_render_state}
    if extended:
        out.update({"pass": pass_name, "shade": shade, "light": light,
                    "crop": crop, "res_pct": res_pct, "caption": caption, **did})
        try:
            import render_ext
            out["pixels"] = render_ext.image_size(path)
        except Exception as e:
            out["pixels_error"] = str(e)[:120]
    return out


def h_inspect_view(a: dict) -> dict:
    """Render one transactional role-aimed artist view without touching shot authority."""

    import checks
    from mathutils import Matrix, Vector

    sc = bpy.context.scene
    original_camera = sc.camera
    original_frame = sc.frame_current
    role = str(a.get("role") or "").strip()
    view = str(a.get("view") or "through_camera")
    frame = int(a.get("frame", sc.frame_current))
    mode = str(a.get("mode") or "solid")
    scale = float(a.get("scale", 0.5))
    isolate = bool(a.get("isolate", False))
    if not role:
        raise ValueError("inspect_view requires a semantic role namespace")
    if mode not in {"solid", "wire"}:
        raise ValueError("inspect_view mode must be solid or wire")
    if view not in {"through_camera", "orbit", "front", "right", "back", "left", "top"}:
        raise ValueError(
            "inspect_view view must be through_camera, orbit, front, right, back, left, or top"
        )
    if original_camera is None:
        raise ValueError("inspect_view requires the selected scene camera")

    sc.frame_set(frame)
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    rendered_types = {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME"}
    inventory = [
        {"name": obj.name, "role": str(obj.get("bvfx_role") or "")}
        for obj in sc.objects
    ]
    subjects = []
    corners = []
    for obj in sc.objects:
        tag = str(obj.get("bvfx_role") or "")
        if not checks.match_semantic(tag, [role]) or obj.type not in rendered_types:
            continue
        evaluated = obj.evaluated_get(depsgraph)
        box = [evaluated.matrix_world @ Vector(point) for point in evaluated.bound_box]
        if box:
            subjects.append(obj)
            corners.extend(box)
    if not corners:
        raise ValueError(checks.format_object_miss(inventory=inventory, role=role))

    lo = Vector(tuple(min(float(point[i]) for point in corners) for i in range(3)))
    hi = Vector(tuple(max(float(point[i]) for point in corners) for i in range(3)))
    center = (lo + hi) * 0.5
    extent = hi - lo
    hidden = {obj.name: bool(obj.hide_render) for obj in sc.objects}
    temporary_camera = None
    temporary_data = None
    try:
        if isolate:
            selected = {obj.name for obj in subjects}
            for obj in sc.objects:
                if obj.type in rendered_types:
                    obj.hide_render = obj.name not in selected

        if view != "through_camera":
            temporary_data = original_camera.data.copy()
            temporary_data.name = "_bvfx_inspect_view_camera_data"
            temporary_camera = bpy.data.objects.new(
                "_bvfx_inspect_view_camera", temporary_data
            )
            sc.collection.objects.link(temporary_camera)

            source_location = original_camera.matrix_world.translation.copy()
            radial = source_location - center
            horizontal = Vector((radial.x, radial.y, 0.0))
            if horizontal.length < 1e-6:
                horizontal = Vector((0.0, -1.0, 0.0))
            base = horizontal.normalized()
            diagonal = max(float(extent.length), 0.01)

            if view == "orbit":
                angle = math.radians(int(a.get("orbit_degrees", 30)))
                direction = Matrix.Rotation(angle, 4, "Z") @ radial
                if direction.length < 1e-6:
                    direction = base
                half_angle = max(
                    0.05,
                    min(
                        float(getattr(temporary_data, "angle_x", temporary_data.angle)),
                        float(getattr(temporary_data, "angle_y", temporary_data.angle)),
                    )
                    * 0.5,
                )
                fit_distance = diagonal * 0.65 / math.tan(half_angle)
                direction.normalize()
                temporary_camera.location = center + direction * max(
                    float(radial.length), fit_distance
                )
                temporary_data.type = "PERSP"
            else:
                quarter_turns = {"front": 0, "right": 1, "back": 2, "left": -1}
                if view == "top":
                    direction = Vector((0.0, 0.0, 1.0))
                else:
                    direction = Matrix.Rotation(
                        math.radians(90 * quarter_turns[view]), 4, "Z"
                    ) @ base
                temporary_camera.location = center + direction * max(diagonal * 2.0, 1.0)
                temporary_data.type = "ORTHO"
                aspect = max(
                    0.1,
                    float(sc.render.resolution_x) / max(1.0, float(sc.render.resolution_y)),
                )
                vertical = max(float(extent.z), 0.01)
                horizontal_span = max(float(extent.x), float(extent.y), 0.01)
                temporary_data.ortho_scale = max(vertical, horizontal_span / aspect) * 1.3

            aim = center - temporary_camera.location
            temporary_camera.rotation_euler = aim.to_track_quat("-Z", "Y").to_euler()
            temporary_data.clip_start = min(
                float(temporary_data.clip_start), max(0.001, diagonal * 0.001)
            )
            temporary_data.clip_end = max(
                float(temporary_data.clip_end),
                float((temporary_camera.location - center).length + diagonal * 3.0),
            )
            sc.camera = temporary_camera

        orbit_suffix = f"-{int(a.get('orbit_degrees', 30))}" if view == "orbit" else ""
        result = h_render({
            "frame": frame,
            "mode": mode,
            "scale": scale,
            "artifact_tag": f"inspect-{view}{orbit_suffix}-{role}",
        })
        result.update({
            "diagnostic_only": True,
            "view": view + orbit_suffix,
            "role": role,
            "isolated": isolate,
            "subject_count": len(subjects),
            "subject_bbox_min": tuple(round(float(value), 4) for value in lo),
            "subject_bbox_max": tuple(round(float(value), 4) for value in hi),
        })
        return result
    finally:
        sc.camera = original_camera
        for obj in sc.objects:
            if obj.name in hidden:
                obj.hide_render = hidden[obj.name]
        if temporary_camera is not None:
            bpy.data.objects.remove(temporary_camera, do_unlink=True)
        if temporary_data is not None and temporary_data.users == 0:
            bpy.data.cameras.remove(temporary_data)
        sc.frame_set(original_frame)
        bpy.context.view_layer.update()


def _set_image_format(settings, fmt: str) -> None:
    """Blender 5.x requires media_type BEFORE file_format, or the enum rejects it."""
    if hasattr(settings, "media_type"):
        with contextlib.suppress(Exception):
            settings.media_type = "IMAGE"
    settings.file_format = fmt


def h_check(a: dict) -> dict:
    """Phase 1 — judgment-free scene checks (bmesh / ray_cast / camera projection)."""
    import checks
    kind = a.get("kind", "")
    if kind == "self_test":
        return checks.self_test()
    return checks.dispatch(kind, a)




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
    "observation_environment": h_observation_environment,
    "nodes": h_nodes,
    "black_context": h_black_context,
    "keyframes": h_keyframes,
    "render": h_render,
    "inspect_view": h_inspect_view,
    "snapshot": h_snapshot,
    "restore": h_restore,
    "journal": h_journal,
    "replay": h_replay,
    "check": h_check,
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
        except Exception as e:
            _send({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}",
                   "trace": traceback.format_exc()})


if __name__ == "__main__":
    serve()
