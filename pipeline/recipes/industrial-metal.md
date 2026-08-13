---
name: industrial-metal
tags: [material, metal, chassis, hardware, rack, believability]
blender: "5.2+"
when: "a machined/manufactured object renders as flat light-grey clay instead of real hardware"
verified: true
---
An untextured Principled BSDF at default roughness on a light base colour is "clay", and a
critic will score it as a sci-fi prop or a placeholder. Reference hardware is DARK with
tight specular highlights along its edges — the brightness you see is reflection, not base
colour. Getting this wrong cost server_to_hansa its `rack_believability` read: our rack was
light matte grey where the reference is dark glossy metal.

GOTCHAS:
- **Base colour goes DOWN, not up.** Real dark anodised/brushed chassis sit around
  0.02-0.05 linear. If you brighten base colour to make it "visible" you destroy the metal
  read; brighten the LIGHT instead.
- `Metallic = 1.0` with high roughness looks like dirty plastic. Metal needs
  roughness 0.15-0.35 so it actually reflects something.
- Metal renders BLACK with nothing to reflect. In a dark set you must give it a source —
  a dim world, a large soft area light out of frame, or a reflection card. This is the
  usual reason a "correct" metal material looks wrong in an empty scene.
- Anisotropic roughness (brushed) reads far better on rack faces than isotropic, but only
  if UVs run along the brush direction.
- Break up the silhouette: bevel the edges. A 1-2 mm bevel catches a highlight line and is
  most of what separates "machined" from "box".

```python
def bvfx_metal(name, base=(0.035, 0.037, 0.040), rough=0.28, aniso=0.35):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes['Principled BSDF']
    b.inputs['Base Color'].default_value = (*base, 1)
    b.inputs['Metallic'].default_value = 1.0
    b.inputs['Roughness'].default_value = rough
    for n in ('Anisotropic', 'Anisotropy'):        # socket name varies by version
        if n in b.inputs:
            b.inputs[n].default_value = aniso
            break
    return m

# bevel every hard edge — cheap, and it is what catches the highlight
mod = obj.modifiers.new('bevel', 'BEVEL')
mod.width, mod.segments, mod.limit_method = 0.002, 2, 'ANGLE'
```
