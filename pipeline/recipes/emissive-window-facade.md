---
name: emissive-window-facade
tags: [material, emission, windows, building, tower, greeble, hero, overlay, transparent, noise]
blender: "5.2+"
when: "a building/tower needs to read as a dark facade with a GRID OF GLOWING WINDOWS — or an imported/glb facade needs EXTRA lit cells added without repainting it"
verified: true
---
Use `bvfx_emissive_windows(obj, ...)`. Do NOT apply a uniform emission material to a hero
building — it blows out into a featureless glow and the window/greeble detail disappears
(this is the #1 reason hero_tower_detail scores low). The helper mixes a DARK Principled
body with an Emission masked to grid cells (a Brick texture: bricks = lit windows, thick
mortar = dark body between them).

GOTCHAS:
- Import the committed hero mesh first (`import_asset('<name>')`), THEN shade it — the mesh
  carries the silhouette/greeble; this material makes the windows read.
- Balance with the render's exposure readout: if `clipped(blown)` climbs, lower `strength`.
- Control density with `Scale` (the `density` arg) — do NOT set the brick's Brick Width/
  Row Height (that breaks the tiling → flat glow). `aspect` makes cells taller (windows).

```python
tower = bpy.data.objects[import_result_name]   # from import_asset
bvfx_emissive_windows(
    tower,
    window_color=(0.15, 1.0, 0.40),   # match the ref's window tint
    strength=8.0,                     # window glow (watch exposure — don't blow out)
    density=7.0,                      # ~columns of windows across (Scale)
    aspect=2.0,                       # taller-than-wide cells → window shapes
    mortar=0.12,                      # dark gap thickness; bigger = smaller windows
)
```

## Variant: ADDITIVE transparent window shell over an existing facade

When the hero mesh already has a good textured facade (e.g. an imported glb) and you only
want to ADD lit windows — never occlude the texture — wrap it in a slightly-larger cube
shell shaded with `bvfx_emissive_windows`, then rewire the material so mortar and "off"
cells are TRANSPARENT (only lit cells contribute). Add a per-cell random on/off mask with
SNAP → White Noise → GREATER_THAN: snapping object coords to the cell pitch gives one
noise value per cell, so lit/unlit is stable per cell and fully deterministic (no seed,
no Python randomness).

GOTCHAS:
- The material must use `surface_render_method = 'BLENDED'` or the transparent BSDF
  renders black.
- SNAP increments must match the brick cell pitch (cell size = texture scale factors),
  or lit cells and mask cells drift apart.
- GREATER_THAN threshold ≈ 1 − fraction lit (0.58 → ~42% of cells on).
- Parent the shell to the tower and set `matrix_parent_inverse` so it tracks any
  animation of the underlying mesh.

```python
bpy.ops.mesh.primitive_cube_add(size=1, location=tower_center)
shell = bpy.context.active_object
shell.scale = shell_dims                       # slightly larger than the facade
bpy.ops.object.transform_apply(scale=True)     # so object coords = world units
bvfx_emissive_windows(shell, window_color=(0.4, 0.8, 1.0),
                      strength=6.0, density=10, aspect=1.6, mortar=0.12)

nt = bpy.data.materials[shell.name + '_windows'].node_tree
n, l = nt.nodes, nt.links
# per-cell deterministic on/off mask
snap = n.new('ShaderNodeVectorMath'); snap.operation = 'SNAP'
snap.inputs[1].default_value = cell_pitch      # e.g. (0.79, 0.79, 1.25)
l.new(n['Texture Coordinate'].outputs['Object'], snap.inputs[0])
wn = n.new('ShaderNodeTexWhiteNoise'); wn.noise_dimensions = '3D'
l.new(snap.outputs['Vector'], wn.inputs['Vector'])
gt = n.new('ShaderNodeMath'); gt.operation = 'GREATER_THAN'
gt.inputs[1].default_value = 0.58              # 1 - fraction_lit
l.new(wn.outputs['Value'], gt.inputs[0])
mul = n.new('ShaderNodeMath'); mul.operation = 'MULTIPLY'
l.new(n['Math.010'].outputs[0], mul.inputs[0])   # the brick window mask
l.new(gt.outputs[0], mul.inputs[1])
# off-cells + mortar → transparent (shell only ADDS light)
trans = n.new('ShaderNodeBsdfTransparent')
mix = n.new('ShaderNodeMixShader')
l.new(mul.outputs[0], mix.inputs['Fac'])
l.new(trans.outputs['BSDF'], mix.inputs[1])
l.new(n['Emission'].outputs['Emission'], mix.inputs[2])
l.new(mix.outputs['Shader'], nt.nodes['Material Output'].inputs['Surface'])
bpy.data.materials[shell.name + '_windows'].surface_render_method = 'BLENDED'

shell.parent = tower
shell.matrix_parent_inverse = tower.matrix_world.inverted()
```
