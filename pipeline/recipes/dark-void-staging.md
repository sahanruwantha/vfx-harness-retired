---
name: dark-void-staging
tags: [lighting, world, floor, material, fresnel, emission, darkness, studio, spot, eevee]
blender: "5.2+"
when: "a hero object sits on near-black with emissive elements — the render reads as a cutout floating in a void with a hard silhouette edge"
verified: true
---
"Emission on darkness" fails in two specific ways, and both have cheap fixes that do NOT
raise overall exposure:

**1. The floor doesn't exist.** A flat near-zero emission blockout gives the hero a hard
silhouette cutoff at the ground line. Replace it with a **dielectric** near-black
Principled surface — base ~0.045, roughness ~0.12, **metallic 0**, IOR 1.5. Fresnel is the
whole point: at steep incidence it reflects ~5% (a dry black floor for high/close angles)
and at grazing incidence ~30% (a soft vertical reflection of the hero for low/far angles).
One material serves both beats. A METAL floor mirrors the world at every angle and lights
the beats that were supposed to stay black.

**2. The void is literally 0,0,0.** Pure black upper frame reads as a broken matte. Give the
world a ramp on the view direction's Z: black at and below the horizon, lifting to a faint
blue overhead (~0.015 linear). No sun, no skylight — the lift stays well under any practical
light, so the hero's key still dominates.

GOTCHAS:
- **Dielectric, not metal.** `metallic = 0.0` is the load-bearing line. Metal is the single
  most common way to accidentally light a shot that is supposed to be dark.
- Guard `'IOR' in bsdf.inputs` — the Principled socket set shifts across versions.
- Gradient by `Generated` Z through `SeparateXYZ` → `ColorRamp` → `Background`. Use `EASE`
  interpolation; a LINEAR ramp puts a visible band across the mid frame.
- Keep the first ramp stop black and place it slightly ABOVE 0 (~0.11) so the horizon and
  everything below stay true black; roll off the top stop back down a little so the zenith
  doesn't become the brightest thing in frame.
- **Soft shadows need samples.** A widened spot pool at `taa_render_samples = 8` is speckle,
  not shadow. Go to ~24 plus shadow rays. Guard the shadow attrs with `hasattr`.
- Widening a spot to kill its hard elliptical rim: change `spot_size`/`spot_blend` ONLY, keep
  energy and position. That moves hero exposure <5%, so an earlier gate's judged look survives.

```python
import bpy
sc = bpy.context.scene

# 1) dielectric studio floor — Fresnel does the angle-dependent work
mat = bpy.data.materials.new('floor_studio')
mat.use_nodes = True
b = mat.node_tree.nodes['Principled BSDF']
b.inputs['Base Color'].default_value = (0.045, 0.045, 0.048, 1.0)
b.inputs['Roughness'].default_value = 0.12
b.inputs['Metallic'].default_value = 0.0        # NOT metal — this is the whole trick
if 'IOR' in b.inputs:
    b.inputs['IOR'].default_value = 1.5
floor = bpy.data.objects['floor']
floor.data.materials.clear()
floor.data.materials.append(mat)

# 2) gradient void — black below the horizon, faint blue overhead
wnt = sc.world.node_tree
for n in list(wnt.nodes):
    if n.type != 'OUTPUT_WORLD':
        wnt.nodes.remove(n)
out = [n for n in wnt.nodes if n.type == 'OUTPUT_WORLD'][0]
bg   = wnt.nodes.new('ShaderNodeBackground')
tc   = wnt.nodes.new('ShaderNodeTexCoord')
sep  = wnt.nodes.new('ShaderNodeSeparateXYZ')
ramp = wnt.nodes.new('ShaderNodeValToRGB')
wnt.links.new(tc.outputs['Generated'], sep.inputs['Vector'])
wnt.links.new(sep.outputs['Z'], ramp.inputs['Fac'])
wnt.links.new(ramp.outputs['Color'], bg.inputs['Color'])
wnt.links.new(bg.outputs['Background'], out.inputs['Surface'])
cr = ramp.color_ramp
cr.interpolation = 'EASE'
cr.elements[0].position, cr.elements[0].color = 0.11, (0.0, 0.0, 0.0, 1.0)
cr.elements[1].position, cr.elements[1].color = 0.34, (0.0155, 0.0158, 0.0290, 1.0)
cr.elements.new(0.95).color = (0.0075, 0.0077, 0.0140, 1.0)   # roll the zenith back down
bg.inputs['Strength'].default_value = 1.0

# 3) spread the key pool past frame edge — falloff only, energy untouched
spot = bpy.data.objects['studio_cone'].data
spot.spot_size, spot.spot_blend = 1.00, 1.00

# 4) samples, or the soft pool is speckle
sc.eevee.taa_render_samples = 24
if hasattr(sc.eevee, 'shadow_rays'):
    sc.eevee.shadow_rays = 4
if hasattr(sc.eevee, 'shadow_steps'):
    sc.eevee.shadow_steps = 6
```
