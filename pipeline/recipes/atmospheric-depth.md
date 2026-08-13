---
name: atmospheric-depth
tags: [atmosphere, haze, fog, volumetric, depth, environment, night]
blender: "5.2+"
when: "the render is uniformly sharp from foreground to horizon and reads flat — the ref has the far city dissolving into haze"
verified: true
---
The single biggest difference between a reference night exterior and ours is that the
reference RECEDES: distant lights are dimmer, lower-contrast and shifted toward the sky
colour, and the hero's base sits in mist. Without it every layer sits at the same apparent
distance and the frame reads as a diagram.

Three cues, cheapest first. Do at least the first two.

GOTCHAS:
- **MEASURED TRAP — a linked WORLD Volume kills your sky in EEVEE.** With anything connected
  to `World Output.inputs['Volume']`, EEVEE renders the sky as pure unlit volume and the
  Background surface **never reaches camera**: a red sky at `strength=20` rendered BLACK, and
  `strength` 1.4 vs 8.0 gave *byte-identical* frames. You will chase the Background colour for
  a whole round with zero pixels changing. Unlink the Volume socket, drive the sky from the
  Background, and put haze in a BOUNDED domain box instead (see snippet 0 below). This is the
  single most expensive miss in this recipe — it silently invalidates every sky note.
- **EEVEE 5.x has no `use_volumetric_lights` and no `use_bloom`** (both `MISSING` on 5.2 —
  they were 4.x). Volumetrics are a world/object volume shader; bloom is the compositor
  Glare node (see `emissive-halation`).
- `scene.eevee.volumetric_start` / `volumetric_end` DO exist (defaults 0.1 / 100.0) and
  clip the volume. If your city is 400 units deep and `volumetric_end` is 100, the far
  half gets NO haze and you will chase the wrong knob for a round. Set it past your
  farthest geometry.
- `volumetric_samples` defaults to 64; drop to 24-32 for iteration, restore for the final.
- `ShaderNodeVolumePrincipled` inputs are `Color, Density, Anisotropy, Emission Strength,
  Emission Color, …` — **Anisotropy is the one that matters for night haze**: 0.3-0.6
  forward-scatters, so lights bloom into the fog instead of it sitting as flat grey milk.
- A world volume applies EVERYWHERE including in front of the hero. If the foreground goes
  soft, use a bounded domain box around the mid/background instead of `world.volume`.

```python
# 0) FIRST — guarantee the sky is drawn by the Background, not eaten by a world volume.
def unlink_world_volume(scene):
    """EEVEE: any link into World Output.Volume makes the Background unreachable."""
    nt = scene.world.node_tree
    for lk in list(nt.nodes['World Output'].inputs['Volume'].links):
        nt.links.remove(lk)

sc = bpy.context.scene
w = bpy.data.worlds.new('sky'); sc.world = w; w.use_nodes = True
nt = w.node_tree
bg = nt.nodes['Background']
bg.inputs['Color'].default_value = (0.016, 0.036, 0.032, 1.0)   # the sky you actually see
bg.inputs['Strength'].default_value = 0.55
unlink_world_volume(sc)                 # <- do this even if you never added a volume node

# 1) haze as a BOUNDED domain box around the mid/background — art-directable, keeps the
#    foreground crisp, and leaves the sky alone.
def haze_box(name, size, center, color=(0.06, 0.07, 0.11), density=0.0025, anisotropy=0.45):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=center)
    ob = bpy.context.object
    ob.name = ob.data.name = name
    ob.scale = size                                  # (x, y, z) extent of the haze
    mat = bpy.data.materials.new(name + '_mat'); mat.use_nodes = True
    mnt = mat.node_tree
    for n in list(mnt.nodes):                        # strip the default surface
        if n.type != 'OUTPUT_MATERIAL':
            mnt.nodes.remove(n)
    vol = mnt.nodes.new('ShaderNodeVolumePrincipled')
    vol.inputs['Color'].default_value = (*color, 1.0)     # night-blue, not grey
    vol.inputs['Density'].default_value = density         # START LOW — 0.02 is soup
    vol.inputs['Anisotropy'].default_value = anisotropy   # forward scatter = glow
    mnt.links.new(vol.outputs['Volume'], mnt.nodes['Material Output'].inputs['Volume'])
    ob.data.materials.append(mat)
    return ob

haze_box('haze_mid', size=(8000, 8000, 400), center=(0, 1500, 200), density=0.0022)
sc.eevee.volumetric_start = 1.0
sc.eevee.volumetric_end = 4000.0       # MUST exceed your farthest geometry (default 100!)
sc.eevee.volumetric_samples = 32

# 2) mist pass as a check (not a look): confirms your depth range is sane
bpy.context.view_layer.use_pass_mist = True
w.mist_settings.start, w.mist_settings.depth = 40.0, 500.0
```

Tuning rule: raise Density until the FARTHEST lights lose about half their contrast, then
stop. If the hero loses contrast too, your box is too close to camera — pull its near face
back past the foreground rather than lowering density.

Sky as a story beat: the Background `Color`/`Strength` are keyable
(`bg.inputs['Color'].keyframe_insert('default_value', frame=f)`), so you can shift the whole
sky — e.g. green-teal storm → violet night — across a blackout or whip where the frame is
near-black, and the change cannot be pointed at. Set those keys to LINEAR, and remember the
world node tree has its own slotted action to walk (see `blender-5-api`).
