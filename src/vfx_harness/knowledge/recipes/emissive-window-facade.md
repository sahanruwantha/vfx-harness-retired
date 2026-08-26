---
name: emissive-window-facade
tags: [material, emission, windows, building, tower, greeble, hero, overlay, transparent, noise]
blender: "5.2+"
when: "a building/tower needs to read as a dark facade with a GRID OF GLOWING WINDOWS — or an imported/glb facade needs EXTRA lit cells added without repainting it"
verified: false
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

## Retuning the helper's graph: find nodes by WALKING, never by name

Every variant below reaches back into the graph `bvfx_emissive_windows` built. Do **not**
address those nodes as `n['Math.010']` / `n['Math.011']` — those names are assigned by
Blender in *creation order*, so they shift the moment the helper changes, and the failure is
silent-ish garbage (`KeyError`, or worse, you retune the wrong Math node). Find nodes by
their `type`, then follow `node_tree.links` outward from a known anchor.

GOTCHAS:
- **`next(...)` with no default raises bare `StopIteration`** — an empty traceback that says
  nothing about which node was missing. Always pass a default and assert, or the next builder
  burns a turn on `StopIteration:` with no message.
- **Compare bpy nodes with `==`, never `is`.** `bpy_struct` wrappers are re-created on every
  attribute access, so `link.from_node is node` is `False` even when they are the same node.
  bpy overloads `==` to compare the underlying data. This one silently returns empty lists.
- Anchor on types that are unique in the graph (`'EMISSION'`, `'SEPXYZ'`, `'BRICK'`), then
  walk one hop at a time. Sockets are addressed by name (`'X'`, `'Strength'`), which IS stable.
- Walking UPSTREAM from a consumer socket (`l.to_node == emi and l.to_socket.name ==
  'Strength'`) is how you grab the node feeding a value without knowing its name at all.

```python
nt = obj.data.materials[0].node_tree

def find(node_type):
    n = next((x for x in nt.nodes if x.type == node_type), None)   # default -> no StopIteration
    assert n is not None, f'no {node_type} node in {nt.name}'
    return n

def downstream(node, out_name='Value'):
    """Nodes fed by node.outputs[out_name].  == not `is` — wrappers are recreated."""
    return [l.to_node for l in nt.links
            if l.from_node == node and l.from_socket.name == out_name]

def upstream(node, in_name):
    """The node feeding node.inputs[in_name], or None."""
    return next((l.from_node for l in nt.links
                 if l.to_node == node and l.to_socket.name == in_name), None)

sep = find('SEPXYZ')                       # object coords split
emi = find('EMISSION')
strength = upstream(emi, 'Strength')       # the MULTIPLY_ADD driving window vs body
strength.inputs[1].default_value = LIT     # lit-window emission,   e.g. 3.0
strength.inputs[2].default_value = DARK    # dark body floor,       e.g. 0.008

xf, zf = downstream(sep, 'X')[0], downstream(sep, 'Z')[0]   # per-axis cell frequency
xf.inputs[1].default_value = COLS          # ~window columns across the face
zf.inputs[1].default_value = ROWS          # ~window rows up the shaft
for freq, lo, hi in ((xf, 0.40, 0.60), (zf, 0.30, 0.70)):   # duty cycle per axis
    fract = downstream(freq)[0]
    for t in downstream(fract):            # the band's two threshold nodes
        t.inputs[1].default_value = lo if t.inputs[1].default_value < 0.5 else hi
```

Pick ROWS/COLS against the *rendered* size, not the model: a facade that fills 0.10 of frame
width downscales its cells hard, so ~2 bright columns and ~30 rows read better than a dense
grid that aliases into flat glow.

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
  animation of the underlying mesh. Call `bpy.context.view_layer.update()` FIRST — the
  inverse must be built from the parent's *current* evaluated matrix, or the child lands
  hundreds of units off (this bites any parent that was moved/scaled earlier in the script).

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
bpy.context.view_layer.update()                  # before reading matrix_world!
shell.matrix_parent_inverse = tower.matrix_world.inverted()
```

## Variant: per-cell BRIGHTNESS variation (not just on/off)

A binary on/off mask still reads as a uniform sheet at distance. Feeding the same
SNAP → White Noise through a Map Range instead of GREATER_THAN gives each cell a stable
brightness multiplier, so the facade reads as *occupied* — some floors bright, some dim.
Still fully deterministic (no seed, no Python randomness). Use this on the hero; keep the
cheaper on/off mask for background buildings.

```python
snap = n.new('ShaderNodeVectorMath'); snap.operation = 'SNAP'
snap.inputs[1].default_value = (1.0 / density_x, 1.0 / density_y, 1.0 / density_z)
l.new(n['Texture Coordinate'].outputs['Object'], snap.inputs[0])
wn = n.new('ShaderNodeTexWhiteNoise'); wn.noise_dimensions = '3D'
l.new(snap.outputs['Vector'], wn.inputs['Vector'])
var = n.new('ShaderNodeMapRange')
var.inputs['To Min'].default_value = 0.30        # dimmest cell (never 0 → keeps texture)
var.inputs['To Max'].default_value = 1.25        # brightest cell
l.new(wn.outputs['Value'], var.inputs['Value'])
mul = n.new('ShaderNodeMath'); mul.operation = 'MULTIPLY'
l.new(n['Math.010'].outputs[0], mul.inputs[0])   # the brick window mask
l.new(var.outputs['Result'], mul.inputs[1])
l.new(mul.outputs[0], n['Math.011'].inputs[0])   # → emission strength
```

## Variant: vertical band silhouette ("two bright strips, dark core")

Real towers don't glow evenly across their width — the camera-facing face is bright, the
core seam and the raking side faces fall off. Layer the window mask on `|X|` of the object
coords (a GREATER_THAN × LESS_THAN band), then MULTIPLY_ADD so the excluded faces keep a
floor instead of going black. This is what turns a lit box into a building.

GOTCHAS:
- Keep the MULTIPLY_ADD offset ≥ ~0.3 — side faces at 0 read as holes, and they may be
  the camera-facing faces in an earlier/later milestone.
- Band edges are in OBJECT coords, so they're scale-invariant — reusable across shots.
- Also lift the emission's own MULTIPLY_ADD offset a little (≈0.05) so the dark body is
  never a pure-black cutout against a haze-lit sky.

```python
ab = n.new('ShaderNodeMath'); ab.operation = 'ABSOLUTE'
gt = n.new('ShaderNodeMath'); gt.operation = 'GREATER_THAN'; gt.inputs[1].default_value = CORE_SEAM
lt = n.new('ShaderNodeMath'); lt.operation = 'LESS_THAN';    lt.inputs[1].default_value = FACE_EDGE
band = n.new('ShaderNodeMath'); band.operation = 'MULTIPLY'
madd = n.new('ShaderNodeMath'); madd.operation = 'MULTIPLY_ADD'
madd.inputs[1].default_value = 0.62              # band contrast
madd.inputs[2].default_value = 0.38              # floor for off-band faces
l.new(n['Separate XYZ'].outputs['X'], ab.inputs[0])
l.new(ab.outputs[0], gt.inputs[0]); l.new(ab.outputs[0], lt.inputs[0])
l.new(gt.outputs[0], band.inputs[0]); l.new(lt.outputs[0], band.inputs[1])
l.new(band.outputs[0], madd.inputs[0])
# then MULTIPLY madd against the window mask before the variation stage above
```
