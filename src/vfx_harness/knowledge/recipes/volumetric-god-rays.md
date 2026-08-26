---
name: volumetric-god-rays
tags: [atmosphere, volumetric, god-rays, light-shaft, beam, shaft, haze, scale, radiating,
       occluder, slab, trapezoid, feather, depth]
blender: "5.2+"
when: "the critic wants shafts/beams radiating through atmosphere and what renders instead is
       a hard-edged bright slab or trapezoid, a flat near-uniform haze with dead-straight
       geometric limits, or an isotropic halo that does not couple to the medium"
verified: true
---
Distilled from five failed build sessions on one unit (layer-2 `atmosphere`, 2026-08-26): every
session tried to PAINT shafts into a density field, and every render came back "flat bounded
slab, no radiating structure". The physics they were missing:

**A light shaft is a shadow phenomenon.** Rays radiating through a medium are the LIT gaps
between OCCLUDED wedges. Light + volume alone gives you a uniform glow (or a hard projected
slab — see trap 1). Light + volume + **blockers between the source and the camera** gives you
shafts, because EEVEE carves the beam wherever geometry shadows the medium. No amount of
density tuning creates radiating structure; only occlusion does.

```python
sc = bpy.context.scene
sc.eevee.use_volumetric_shadows = True      # without this, occluders do NOT carve the medium
sc.eevee.volumetric_shadow_samples = 16     # raise if shafts look chunky
# volumetric_start/end must bracket BOTH the medium and the occluders (see atmospheric-depth)
```

Then give the light something to shine PAST. Thin slats / spokes / a ring of blockers around
the bright core, shadow-casting, camera-invisible if you only want their shadows:

```python
def shaft_blocker(name, location, rotation, scale=(0.05, 2.0, 0.6)):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location, rotation=rotation)
    ob = bpy.context.object
    ob.name = ob.data.name = name
    ob.scale = scale
    ob.visible_camera = False        # shadow-only: carves the medium, never seen directly
    return ob
# a fan of 5-9 of these around the core, uneven angles/gaps -> uneven, organic ray spread
```

SCOPE RULE: blockers are new objects — they are legal only under a semantic role YOUR unit
declares. If your scope has no such role, do not invent one: position your medium so geometry
that ALREADY exists in the scene (another layer's spokes, pillars, masses) sits between the
light and the camera, and let it do the carving. Never move or retag another layer's objects.

GOTCHAS (each one cost a real session):

1. **MEASURED TRAP — a rectangular AREA light through a volume projects a hard-edged bright
   trapezoid.** It looks exactly like a glowing box of geometry; a matcap pass will show no
   geometry there. Per-light isolation (render with each light solo) is the fast diagnosis.
   If the area light belongs to another unit you cannot resize it — break the projection up
   instead: noise-modulate the medium density (snippet below) and put blockers across the
   beam so its silhouette stops being a clean quadrilateral.
2. **Uniform density reads as a slab, not an atmosphere.** `Volume Principled` with constant
   Density is a filled box. Drive Density with large-scale noise (Texture Coordinate →
   Noise → ColorRamp → Density): mottle breaks the beam edge into wisps and gives the
   critic's "medium filling the space" instead of "bounded slab".
3. **A domain box face that crosses the frame draws a dead-straight edge.** Either extend the
   box past the camera frustum on every visible side, or fade density to zero before the
   walls (Object coords → Gradient/absolute-value ramp multiplied into Density). A visible
   box limit is the #1 "geometric edges cut across the frame" critique.
4. **Depth-thicken or it reads small.** Multiply a second ramp along the view axis so far
   haze is denser than near haze; scale cue = accumulation with distance.
5. **Particulates must grade with depth.** Even specks at even brightness read as noise on
   glass. Near: few, larger, brighter; far: many, smaller, dimmer — instance with a size/
   emission falloff, don't scatter one particle everywhere.

```python
# noise-modulated, wall-feathered density for a bounded haze domain
def haze_volume_material(name, base_density=0.02, noise_scale=0.35):
    mat = bpy.data.materials.new(name); mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        if n.type != 'OUTPUT_MATERIAL':
            nt.nodes.remove(n)
    out = nt.nodes['Material Output']
    vol = nt.nodes.new('ShaderNodeVolumePrincipled')
    tex = nt.nodes.new('ShaderNodeTexCoord')
    noise = nt.nodes.new('ShaderNodeTexNoise')
    noise.inputs['Scale'].default_value = noise_scale          # LARGE features: 0.2-0.6
    ramp = nt.nodes.new('ShaderNodeValToRGB')
    ramp.color_ramp.elements[0].position = 0.35                # cut floor -> real gaps
    math = nt.nodes.new('ShaderNodeMath'); math.operation = 'MULTIPLY'
    math.inputs[1].default_value = base_density
    nt.links.new(tex.outputs['Object'], noise.inputs['Vector'])
    nt.links.new(noise.outputs['Fac'], ramp.inputs['Fac'])
    nt.links.new(ramp.outputs['Color'], math.inputs[0])
    nt.links.new(math.outputs['Value'], vol.inputs['Density'])
    nt.links.new(vol.outputs['Volume'], out.inputs['Volume'])
    return mat
# wall feathering: add (1 - smoothed |Object coord|) ramps per axis, multiply into Density
```

Order of operations when the critic says "flat / no shafts / hard edges": first confirm what
is actually projecting (per-light isolation + matcap), then occluders + volumetric shadows,
then noise-modulated density, then wall feathering, then depth thickening, then particulate
grading. Occlusion first — everything else is seasoning on a shaft that must exist before it
can be seasoned.
