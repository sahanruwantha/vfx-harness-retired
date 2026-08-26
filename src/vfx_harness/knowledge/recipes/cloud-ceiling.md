---
name: cloud-ceiling
tags: [atmosphere, clouds, sky, ceiling, dome, nebula, storm, backdrop, eevee]
blender: "5.2+"
when: "the ref has a DENSE ROLLING CLOUD CEILING / storm-cloud canopy filling the upper frame (not thin wisps)"
verified: false
---
A rolling cloud CEILING cannot be a volume slab. PHYSICS: viewed edge-on, a wide slab's
optical depth along the VIEW ray is huge (density × hundreds of units ≈ 10+) → fully
saturated → noise/billow structure integrates away to a SMOOTH DOME no matter what the
noise params are (verified: noise_scale 1.6→5.5 produced pixel-identical renders).

THE TECHNIQUE — structure from a TEXTURED DOME, depth from a THIN volume:
1. Giant flattened emission-shaded dome (noise → ramp → emission color). Surface shading
   can't saturate, so billows always read.
2. A thin `bvfx_volume` haze band in front of it for depth (short view path — no saturation).

GOTCHAS (each cost a render to learn):
- Dome must be HUGE and flattened (radius ~3000, z-scale ~0.22, center z ≈ -150) so its
  silhouette edge stays below the visible horizon — a small dome shows a dark arc.
- Flip the dome's normals inward (camera is inside it).
- Ramp bright-stop calibration: (0.045, 0.16, 0.085) reads clearly but slightly bright
  (top-band μ~70 vs ref μ~24); (0.02, 0.075, 0.04) is too dark (billows fade). Start
  ~(0.032, 0.11, 0.06) and converge on the ref's top-band μ/σ via compare_frame's
  structure readout.
- Mapping z-scale ~2.6 stretches noise into horizontal cloud banks; noise Scale 6-7,
  Detail 10, Roughness 0.68.
- Keep the haze volume THIN along the view axis (e.g. 60u deep) — that's what keeps it
  from saturating like the slab.
- COVERAGE IS SCORED: the billow texture must wrap the ENTIRE upper third — both sides of
  the hero, no flat-gradient patches anywhere in the sky (critics call half-covered skies
  "nearly absent"). And the hero's glow halo must DISSOLVE into haze — a hard halo edge
  against clean sky reads as no atmosphere. Check the full sky region, not just above
  the hero.

```python
import bpy, bmesh
# 1) the textured cloud dome (structure)
bpy.ops.mesh.primitive_uv_sphere_add(radius=3000, location=(0, 0, -150),
                                     segments=48, ring_count=24)
dome = bpy.context.object; dome.name = 'cloud_dome'; dome.scale = (1, 1, 0.22)
m = bpy.data.materials.new('cloud_dome_mat'); m.use_nodes = True
nt = m.node_tree; nt.nodes.clear()
out = nt.nodes.new('ShaderNodeOutputMaterial'); tc = nt.nodes.new('ShaderNodeTexCoord')
mp = nt.nodes.new('ShaderNodeMapping'); mp.inputs['Scale'].default_value = (1, 1, 2.6)
no = nt.nodes.new('ShaderNodeTexNoise')
no.inputs['Scale'].default_value = 7.0; no.inputs['Detail'].default_value = 10.0
rp = nt.nodes.new('ShaderNodeValToRGB')
rp.color_ramp.elements[0].position = 0.44
rp.color_ramp.elements[0].color = (0.001, 0.004, 0.002, 1)   # gaps: near-black
rp.color_ramp.elements[1].position = 0.75
rp.color_ramp.elements[1].color = (0.032, 0.11, 0.06, 1)     # billows: tune to ref tint/μ
em = nt.nodes.new('ShaderNodeEmission'); em.inputs['Strength'].default_value = 1.0
nt.links.new(tc.outputs['Generated'], mp.inputs['Vector'])
nt.links.new(mp.outputs['Vector'], no.inputs['Vector'])
nt.links.new(no.outputs['Fac'], rp.inputs['Fac'])
nt.links.new(rp.outputs['Color'], em.inputs['Color'])
nt.links.new(em.outputs[0], out.inputs['Surface'])
dome.data.materials.append(m)
bm = bmesh.new(); bm.from_mesh(dome.data)
for f in bm.faces: f.normal_flip()
bm.to_mesh(dome.data); bm.free()

# 2) thin haze band for depth (short view path — never saturates)
bvfx_volume(name='haze', center=(0, 120, 150), size=(700, 60, 200),
            optical_depth=0.35, color=(0.05, 0.18, 0.10), emission_strength=0.1,
            noise_scale=2.5, contrast=(0.35, 0.7), stretch=(0.5, 1.0, 1.6))
bpy.context.scene.eevee.volumetric_end = 600.0
```
