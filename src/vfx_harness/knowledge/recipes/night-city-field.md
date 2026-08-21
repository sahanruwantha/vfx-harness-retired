---
name: night-city-field
tags: [city, environment, instancing, emission, windows, background, night]
blender: "5.2+"
when: "a night city / skyline behind the hero — and the render looks like grey boxes with a regular dot grid instead of a city"
verified: true
---
The default construction — instanced cubes with one emission-masked window grid — is THE
tell that a render is amateur. Every tower gets identical window spacing, identical
colour, identical density, so the eye reads "repeated texture", not "city". Reference
night cities are irregular in three independent ways: which windows are lit, what colour
each building's lights are, and how much of the field survives distance.

MEASURED on a 120-tower test (640×360, EEVEE 5.2), naive grid vs this recipe:

| | warm↔cool spread σ across lit pixels |
|---|---|
| one shared emission material | **0.37** (i.e. none) |
| per-instance `ObjectInfo.Random` → ramp | **16.38** |

GOTCHAS:
- **White noise must be SNAPPED to the window cell.** Feeding the raw mapping vector into
  `ShaderNodeTexWhiteNoise` samples per-POINT, so you get speckle *inside* each window
  instead of whole windows switching off. Verified: without the snap the lit fraction
  barely moves (47.75% → 47.41%); with it the gate actually bites. Use
  `ShaderNodeVectorMath` `operation='SNAP'` with increment `1/brick_scale` on all three
  axes BEFORE the white noise.
- **`ObjectInfo.Random` is per-OBJECT, not per-instance-of-a-mesh.** Real separate objects
  each get their own value. If you instance via geometry nodes you need the instance's
  own random attribute instead.
- Drive COLOUR from `ObjectInfo.Random` and ON/OFF from the snapped white noise. Using one
  source for both correlates them and the city looks banded.
- Do NOT set emission strength to a uniform 1.0 (the test render above still reads too
  hot). Scale strength down with distance from camera as well as fading density, or the
  horizon is as bright as the foreground and all depth dies. See `atmospheric-depth`.
- `GREATER_THAN` threshold ≈ 0.45 leaves roughly half the windows lit; 0.6–0.7 reads as a
  quieter, later-night city.

```python
def bvfx_window_mat(name, scale=14.0, lit_fraction=0.55,
                    warm=(1.00, 0.72, 0.38), cool=(0.75, 0.86, 1.00)):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    em  = nt.nodes.new('ShaderNodeEmission')
    tc  = nt.nodes.new('ShaderNodeTexCoord')
    mp  = nt.nodes.new('ShaderNodeMapping')
    brk = nt.nodes.new('ShaderNodeTexBrick')
    brk.inputs['Scale'].default_value = scale
    brk.inputs['Mortar Size'].default_value = 0.18      # the dark frame between windows
    nt.links.new(tc.outputs['Object'], mp.inputs['Vector'])
    nt.links.new(mp.outputs['Vector'], brk.inputs['Vector'])

    snap = nt.nodes.new('ShaderNodeVectorMath'); snap.operation = 'SNAP'
    cell = 1.0 / scale
    snap.inputs[1].default_value = (cell, cell, cell)   # one noise value PER WINDOW
    wn = nt.nodes.new('ShaderNodeTexWhiteNoise')
    nt.links.new(mp.outputs['Vector'], snap.inputs[0])
    nt.links.new(snap.outputs['Vector'], wn.inputs['Vector'])

    gate = nt.nodes.new('ShaderNodeMath'); gate.operation = 'GREATER_THAN'
    gate.inputs[1].default_value = 1.0 - lit_fraction
    mul  = nt.nodes.new('ShaderNodeMath'); mul.operation = 'MULTIPLY'
    nt.links.new(wn.outputs['Value'], gate.inputs[0])
    nt.links.new(gate.outputs[0], mul.inputs[0])
    nt.links.new(brk.outputs['Fac'], mul.inputs[1])
    nt.links.new(mul.outputs[0], em.inputs['Strength'])

    oi   = nt.nodes.new('ShaderNodeObjectInfo')          # per-BUILDING colour temp
    ramp = nt.nodes.new('ShaderNodeValToRGB')
    ramp.color_ramp.elements[0].color = (*warm, 1)
    ramp.color_ramp.elements[1].color = (*cool, 1)
    nt.links.new(oi.outputs['Random'], ramp.inputs['Fac'])
    nt.links.new(ramp.outputs['Color'], em.inputs['Color'])
    nt.links.new(em.outputs['Emission'], out.inputs['Surface'])
    return m
```

Placement matters as much as the shader — vary footprint, height and spacing, and thin the
field with distance so the horizon is sparse:

```python
import random, math
random.seed(7)
for i in range(N):
    a, r = random.uniform(0, math.tau), random.uniform(r_min, r_max)
    far = (r - r_min) / (r_max - r_min)          # 0 near … 1 far
    if random.random() < far * 0.6:              # thin the distant field
        continue
    h = random.uniform(3, 26) * (1.0 - 0.4 * far)
    ...
```
