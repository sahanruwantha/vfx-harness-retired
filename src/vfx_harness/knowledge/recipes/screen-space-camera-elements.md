---
name: screen-space-camera-elements
tags: [camera, parenting, screen-space, composition, instancing, emission, streaks, keyframes]
blender: "5.2+"
when: "an element must sit at a specific place IN FRAME (light streak, lens artifact, foreground blob, HUD/dirt element) and stay there while the camera rolls, whips or travels"
verified: false
---
Parent the element to the CAMERA (not the rig) with an identity parent-inverse. Its local
space then *is* camera space, so you can place it from a normalized screen coordinate
`(u, v)` — u=0 left, v=0 top — via the sensor/lens frustum at a chosen depth. The camera can
roll 70°/frame and the element does not arc away, because it is nailed to the frame.

This is the counterpart to `camera-parented-spill-lights` (which parents to the RIG precisely
so lighting does NOT follow the lens). Use the camera when you want frame-locked composition;
use the rig when you want world-anchored behaviour.

Give the element its OWN keyed travel: with the parent locked, all its motion blur comes from
its local movement, so a small keyed drift along the screen-tilt angle is what turns a blob
into a streak. Build them as one mesh reused by N objects (`bpy.data.objects.new(name, mesh)`)
— they are identical by design, so instancing is free.

GOTCHAS:
- `matrix_parent_inverse` is identity only for a *freshly created, unmoved* object. Set
  `ob.matrix_parent_inverse = Matrix.Identity(4)` explicitly BEFORE assigning `location`,
  or the placement math silently bakes in the old world transform.
- Camera local space looks down **−Z**: depth must be `-DIST`, and `+Y` is screen-up, so v
  inverts (`(0.5 - v)`). Getting either sign wrong puts the element behind the camera and it
  just never appears.
- World-space placement is the trap this replaces: under fast roll, anything static gets
  dragged into a long arc by motion blur and dissolves into nothing.
- Scale the mesh anisotropically so the streak is GEOMETRY, not just blur — blur alone
  disappears the moment the shutter or the roll rate changes. **~7:1, not 3:1**: 3:1 spheres
  plus a big glare render as round bokeh BALLS (a scored miss); ~7:1 along travel is what reads
  as an elongated cigar. Do not lean on the Glare to do the elongating.
- **Vary length AND flux per element, or the set reads synthetic** — N identical lamps look
  like an overlay. Instancing shares one MESH, so per-element brightness needs a per-OBJECT
  material override: `ob.material_slots[0].link = 'OBJECT'` then assign `ob.material_slots[0]
  .material`. The shared mesh must already have a slot to override (append any material to
  `mesh.materials` once), or there is no `material_slots[0]` to retarget and the assignment
  raises IndexError.
- Put one element slightly off-frame (u > 1.0) and let its keyed travel sweep it in; an
  all-visible-at-once set reads as a static overlay.
- `DIST` interacts with DOF and with any volume in the scene. Pick a depth outside your near
  haze domain, or the element gets fogged by geometry it should be in front of.

```python
import math, bpy
from mathutils import Matrix, Vector

def screen_place(ob, cam, u, v, dist, angle_deg=0.0, speed=0.0, travel=(), pivot=0):
    """Pin `ob` at normalized screen (u, v) at `dist` in front of `cam`.
    travel: frames to key a linear drift along `angle_deg` at `speed` units/frame,
    centered on `pivot` (the frame where the element sits exactly at (u, v))."""
    sc = bpy.context.scene
    hw = dist * (cam.data.sensor_width * 0.5) / cam.data.lens
    hh = hw * sc.render.resolution_y / sc.render.resolution_x
    ob.parent = cam
    ob.matrix_parent_inverse = Matrix.Identity(4)        # local space == screen space
    ob.rotation_euler = (0.0, 0.0, math.radians(angle_deg))
    base = Vector(((u - 0.5) * 2.0 * hw, (0.5 - v) * 2.0 * hh, -dist))   # -Z is forward
    if not travel:
        ob.location = base
        return ob
    d = Vector((math.cos(math.radians(angle_deg)),
                math.sin(math.radians(angle_deg)), 0.0)) * speed
    for f in travel:
        ob.location = base + d * float(f - pivot)
        ob.keyframe_insert('location', frame=f)
    return ob

def instanced_blobs(name, n, radius=1.0):
    """One mesh, n objects sharing it."""
    bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, segments=16, ring_count=8,
                                         location=(0, 0, 0))
    src = bpy.context.object
    src.name, src.data.name = '%s_0' % name, '%s_mesh' % name
    for p in src.data.polygons:
        p.use_smooth = True
    objs = [src] + [bpy.data.objects.new('%s_%d' % (name, i), src.data) for i in range(1, n)]
    for o in objs[1:]:
        bpy.context.scene.collection.objects.link(o)
    return objs


def set_object_material(ob, mesh, mat, first):
    """Per-OBJECT material on a SHARED mesh, so instances can differ in flux.
    `first`: True for the one object that seeds the mesh's (otherwise empty) slot."""
    if first:
        mesh.materials.append(mat)               # the slot to override must exist
    ob.material_slots[0].link = 'OBJECT'
    ob.material_slots[0].material = mat

# --- apply: warm streaks that survive a fast roll ----------------------------------------
#          u     v    long thick speed angle flux
SPECS = ((0.975, 0.115, 1.60, 0.205, 1.05, 30.0, 0.80),   # hero, clipped by the right edge
         (0.620, 0.925, 1.00, 0.155, 0.80, 34.0, 0.46),
         (1.120, 0.170, 0.90, 0.140, 0.50, 26.0, 0.62))   # off-frame at pivot; sweeps in
LVL = ((18, 11.0), (19, 72.0), (20, 34.0), (21, 80.0), (23, 5.0))   # keyed brightness
COL = (1.0, 0.70, 0.40)                          # warm sodium: core clips white, falloff amber

cam = bpy.data.objects['cam']
objs = instanced_blobs('streak', len(SPECS))
for i, (ob, (u, v, lng, thk, spd, ang, flux)) in enumerate(zip(objs, SPECS)):
    mat = bvfx_emission('streak_mat_%d' % i, COL, 30.0)
    em = mat.node_tree.nodes['Emission']
    for f, s in LVL:                             # flux scales this element's whole curve
        em.inputs['Strength'].default_value = s * flux
        em.inputs['Strength'].keyframe_insert('default_value', frame=f)
    set_object_material(ob, objs[0].data, mat, first=(i == 0))
    ob.scale = (lng, thk, thk)                   # ~7:1 -> geometry, not just blur
    screen_place(ob, cam, u, v, dist=40.0, angle_deg=ang, speed=spd,
                 travel=(17, 20, 24), pivot=20)
```
