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
# 1) world haze — the depth cue that costs nothing to art-direct
w = bpy.data.worlds.new('sky'); bpy.context.scene.world = w; w.use_nodes = True
nt = w.node_tree
vol = nt.nodes.new('ShaderNodeVolumePrincipled')
vol.inputs['Color'].default_value = (0.06, 0.07, 0.11, 1)   # night-blue, not grey
vol.inputs['Density'].default_value = 0.0025                # START LOW — 0.02 is soup
vol.inputs['Anisotropy'].default_value = 0.45               # forward scatter = glow
nt.links.new(vol.outputs['Volume'], nt.nodes['World Output'].inputs['Volume'])
sc = bpy.context.scene
sc.eevee.volumetric_end = 600.0        # MUST exceed your farthest geometry
sc.eevee.volumetric_samples = 32

# 2) mist pass as a check (not a look): confirms your depth range is sane
bpy.context.view_layer.use_pass_mist = True
w.mist_settings.start, w.mist_settings.depth = 40.0, 500.0
```

Tuning rule: raise Density until the FARTHEST lights lose about half their contrast, then
stop. If the hero loses contrast too, your volume is unbounded — switch to a domain box.
