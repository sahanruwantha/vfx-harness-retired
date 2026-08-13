---
name: camera-parented-spill-lights
tags: [lighting, camera, rig, parenting, area-light, travel, eevee, keyframes]
blender: "5.2+"
when: "a moving camera travels through a long/dark environment and the surfaces it passes must stay lit — corridor, tunnel, street, flythrough — without hand-placing lights along the whole route"
verified: true
---
Parent a small set of AREA lights to the CAMERA RIG (never the camera itself) and give them
locations in rig-local space. The lights travel with the shot, so every metre of a 400-unit
push is lit identically and you place three lights instead of thirty. Because they're on the
rig and not the camera, a camera shake/roll/lens move doesn't swing the lighting with it —
that swing is the tell that instantly reads as "lights are stuck to the lens".

Break the symmetry: make left and right different energies (e.g. 1500 / 1950) and add a
third pushed ahead of the camera. Equal L/R makes a corridor read as a flat-lit render;
unequal makes it read as photographed.

GOTCHAS:
- `obj.parent = rig` alone keeps the light's current WORLD position — Blender applies
  `matrix_parent_inverse`, which for a freshly-created object is identity, but for anything
  the script moved earlier it is not. Set `obj.matrix_parent_inverse.identity()` explicitly,
  THEN set `obj.location` — now the numbers you type are literally rig-local offsets
  (+Y ahead, +X right), which is the only way these values stay readable.
- `use_shadow = False` on travelling spill. These are cheats, not practicals — shadows from
  them contradict the real key lights and crawl as the rig moves. It's also most of the
  EEVEE cost of adding them.
- Big soft `size` (10–11) at moderate energy beats small+bright: a small travelling light
  produces a visible hot pool that slides along the floor.
- EEVEE needs `scene.eevee.use_raytracing = True` for these to bounce into the environment
  at all; without it they only key the surfaces they directly face.
- Key them OFF before the travel beat starts (see `stepped-visibility-swap`). A travelling
  spill sitting live during a static/black beat lifts the blacks and flattens it — this is
  the single most common note on these rigs.
- Energy is distance-squared sensitive: if the rig gets closer to the walls later in the
  move, the same light blows out. Check the exposure readout at the END of the travel, not
  the start.

```python
import bpy

def rig_spill(rig, specs, color=(1.0, 0.78, 0.50), live_from=None):
    """specs: [(name, (x, y, z) rig-local, (rx, ry, rz), energy, size), ...]"""
    sc = bpy.context.scene
    sc.eevee.use_raytracing = True
    made = []
    for name, loc, rot, energy, size in specs:
        ob = bpy.data.objects.get(name)
        if ob is None:
            ob = bpy.data.objects.new(name, bpy.data.lights.new(name, "AREA"))
            sc.collection.objects.link(ob)
        ld = ob.data
        ld.shape, ld.size = "SQUARE", size
        ld.color, ld.energy = color, energy
        ld.use_shadow = False                  # cheat light: no contradicting shadows
        ob.parent = rig
        ob.matrix_parent_inverse.identity()    # BEFORE location -> offsets are rig-local
        ob.location = loc
        ob.rotation_euler = rot
        if live_from is not None:              # dark before the travel beat
            if ob.animation_data:
                ob.animation_data_clear()
            ob.hide_render = ob.hide_viewport = True
            ob.keyframe_insert("hide_render", frame=live_from - 1)
            ob.keyframe_insert("hide_viewport", frame=live_from - 1)
            ob.hide_render = ob.hide_viewport = False
            ob.keyframe_insert("hide_render", frame=live_from)
            ob.keyframe_insert("hide_viewport", frame=live_from)
        made.append(ob)
    return made

rig_spill(
    bpy.data.objects["cam_rig"],
    [("spill_L", (-4.0, 1.5, 1.0), (0.0, -0.75, 0.0), 1500.0, 11.0),
     ("spill_R", ( 4.0, 1.5, 1.0), (0.0,  0.75, 0.0), 1950.0, 11.0),  # deliberate asymmetry
     ("spill_F", ( 0.0, 9.0, 1.4), (0.75, 0.0,  0.0), 1150.0, 10.0)], # ahead of the rig
    live_from=337,
)
```
