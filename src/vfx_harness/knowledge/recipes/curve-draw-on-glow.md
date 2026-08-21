---
name: curve-draw-on-glow
tags: [curve, animation, growth, emission, glow, trail, reveal, transparent, material]
blender: "5.2+"
when: "a line must DRAW ITSELF on screen — a data path, a route trace, a beam, a wire, a signature — and needs a soft halo that lights the surface it lies on without occluding it"
verified: true
---
A bevelled curve animated on `bevel_factor_end` is the cheapest reliable "line draws itself"
in Blender: no geometry nodes, no build modifier, no per-frame Python. Model the route as a
POLY spline with per-point `radius` (thickness ramps along the run), give it a ROUND bevel,
and key `bevel_factor_end` 0→1 over the draw beat.

The halo is a SECOND curve on the same points, fatter bevel, flattened, sitting a hair above
the first — a "skirt". Shade it with Emission→MixShader→Transparent driven by the bevel
profile's UV so it fades to nothing at its rim. That soft-edged spill is what sells the line
as *emitting* rather than as a glowing tube sitting on top of the plate, and it costs one
transparent surface instead of a volume or a row of lights.

The same UV gives you a free along-the-run gradient: UV.x runs 0→1 along the spline, so a
Map Range on it makes the trail dim where it starts and hot where you want the eye.

GOTCHAS:
- Set `bevel_factor_mapping_end = 'SPLINE'`. The default maps growth per control-point
  segment, so a long segment and a short one take the same time and the draw visibly
  stutters at every corner. SPLINE maps by arc length = constant speed.
- Force LINEAR interpolation on the draw keys. Default BEZIER eases in and out, and a line
  that slows down as it arrives reads as hesitant, not deliberate. Note 5.x fcurves live in
  per-slot channelbags, not `action.fcurves` — see `blender-5-api`.
- Key on the CURVE DATA (`curve.data.bevel_factor_end`), not the object. It's a data
  property; keying the object silently no-ops.
- `animation_data_clear()` before re-keying — milestone deltas re-run and keys otherwise
  stack into a mess of duplicate frames.
- Flatten a round bevel into a floor ribbon with object `scale.z`, not a custom profile
  object — scale keeps the bevel UVs intact, which is what the rim falloff depends on.
- The glow material MUST be `surface_render_method = 'BLENDED'` or the Transparent BSDF
  renders black in EEVEE. Also `use_backface_culling = False`, or the skirt disappears from
  whichever side the camera happens to be on.
- Offset the skirt in Z by ~0.01–0.02 (and give the core a tiny Z too) — coplanar with the
  floor is z-fighting, and it flickers only in the render, not the viewport.
- Both curves must share the SAME points and the SAME draw keys, or the halo leads or trails
  the stroke by a few frames and the whole effect falls apart.

```python
import bpy, math

def draw_on_curve(name, pts, keys, bevel=0.16, flatten=0.28,
                  color=(1.0, 0.52, 0.11), strength=30.0, radius_i=3):
    """pts: [(x, y, z, r_core, r_skirt), ...]; keys: [(frame, factor), ...]"""
    ob = bpy.data.objects.get(name)
    if ob is None:
        ob = bpy.data.objects.new(name, bpy.data.curves.new(name + "_c", "CURVE"))
        bpy.context.scene.collection.objects.link(ob)
    cu = ob.data
    cu.dimensions, cu.resolution_u, cu.fill_mode = "3D", 12, "FULL"
    cu.bevel_mode, cu.bevel_depth = "ROUND", bevel
    cu.bevel_factor_start = 0.0
    cu.bevel_factor_mapping_end = "SPLINE"      # constant-speed draw
    ob.scale = (1.0, 1.0, flatten)              # round tube -> floor ribbon

    cu.splines.clear()
    sp = cu.splines.new("POLY")
    sp.points.add(len(pts) - 1)
    for p, row in zip(sp.points, pts):
        p.co = (row[0], row[1], row[2], 1.0)
        p.radius = row[radius_i]                # thickness ramps along the run

    if cu.animation_data:
        cu.animation_data_clear()               # deltas re-run; don't stack keys
    for f, v in keys:
        cu.bevel_factor_end = v
        cu.keyframe_insert("bevel_factor_end", frame=f)
    act = cu.animation_data.action              # LINEAR or the draw reads hesitant
    for slot in act.slots:
        for fc in act.layers[0].strips[0].channelbag(slot).fcurves:
            for k in fc.keyframe_points:
                k.interpolation = "LINEAR"
            fc.update()
    return ob


def skirt_material(name, color, along=(0.16, 0.50, 0.45, 3.2), tightness=1.5):
    """Emission that fades to transparent at the bevel rim; brightens along the run.
    `along` = (from_min, from_max, strength_at_start, strength_at_end) over UV.x."""
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree; nt.nodes.clear()
    n, l = nt.nodes.new, nt.links.new
    out, mix = n("ShaderNodeOutputMaterial"), n("ShaderNodeMixShader")
    tr, em = n("ShaderNodeBsdfTransparent"), n("ShaderNodeEmission")
    em.inputs["Color"].default_value = (*color, 1.0)
    tc, sep = n("ShaderNodeTexCoord"), n("ShaderNodeSeparateXYZ")
    # UV.y wraps the bevel profile: (1 - |cos 2*pi*v|) = 1 at centre, 0 at the rim
    mul = n("ShaderNodeMath"); mul.operation = "MULTIPLY"; mul.inputs[1].default_value = 2 * math.pi
    cos = n("ShaderNodeMath"); cos.operation = "COSINE"
    ab  = n("ShaderNodeMath"); ab.operation = "ABSOLUTE"
    sub = n("ShaderNodeMath"); sub.operation = "SUBTRACT"
    sub.inputs[0].default_value = 1.0; sub.use_clamp = True
    pw  = n("ShaderNodeMath"); pw.operation = "POWER"; pw.inputs[1].default_value = tightness
    # UV.x runs along the spline -> gradient of emission strength
    rng = n("ShaderNodeMapRange"); rng.clamp = True
    for k, v in zip(("From Min", "From Max", "To Min", "To Max"), along):
        rng.inputs[k].default_value = v
    l(tc.outputs["UV"], sep.inputs["Vector"])
    l(sep.outputs["Y"], mul.inputs[0]); l(mul.outputs[0], cos.inputs[0])
    l(cos.outputs[0], ab.inputs[0]);    l(ab.outputs[0], sub.inputs[1])
    l(sub.outputs[0], pw.inputs[0]);    l(pw.outputs[0], mix.inputs["Fac"])
    l(sep.outputs["X"], rng.inputs["Value"])
    l(rng.outputs["Result"], em.inputs["Strength"])
    l(tr.outputs[0], mix.inputs[1]); l(em.outputs[0], mix.inputs[2])
    l(mix.outputs[0], out.inputs["Surface"])
    mat.surface_render_method = "BLENDED"       # else Transparent renders BLACK
    mat.use_backface_culling = False
    return mat


PTS  = [(0.0, 0.9, 0.02, 0.40, 0.40), (3.4, -0.35, 0.02, 0.40, 0.40),
        (0.0, 84.0, 0.02, 1.10, 0.95), (2.5, 150.0, 0.02, 1.90, 1.20)]
KEYS = [(1, 0.0), (200, 0.0), (264, 1.0), (480, 1.0)]   # hold, draw, hold

core  = draw_on_curve("data_path", PTS, KEYS, bevel=0.16, flatten=0.28, radius_i=3)
skirt = draw_on_curve("data_path_glow", PTS, KEYS, bevel=0.45, flatten=0.02, radius_i=4)
skirt.location = (0.0, 0.0, 0.014)              # a hair above the core — no z-fighting
skirt.data.materials.clear()
skirt.data.materials.append(skirt_material("path_glow", (1.0, 0.62, 0.25)))
```

Pair with a tight compositor Glare (Threshold ~1.0, Strength ~0.14, Size ~0.11) so only the
clipped core halates — a wide bloom smears the stroke back into mush and undoes the skirt.
