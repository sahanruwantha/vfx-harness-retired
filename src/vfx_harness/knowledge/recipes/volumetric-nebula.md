---
name: volumetric-nebula
tags: [atmosphere, volume, clouds, nebula, fog, sky, eevee]
blender: "5.2+"
when: "colored nebulous glow / billowing clouds / haze behind or around a subject (not a flat sky)"
verified: false
---
SCOPE: this recipe is for WISPY haze/glow. If the ref shows a DENSE ROLLING CLOUD CEILING
(storm canopy filling the upper frame), use the `cloud-ceiling` recipe instead — an
edge-on volume slab saturates and can NEVER show billows (see that recipe's physics note).

ALWAYS use the `bvfx_volume` primitive — a bounded domain with noise-structured density,
so it reads as wisps not milk. Do NOT hand-roll a cloud material / emissive backdrop plane
(reads as flat smooth glow, scores low on atmosphere) and do NOT crank world VolumeScatter
density (>~0.008 whites out the whole frame and fogs the foreground).

For STREAKED wispy clouds (not uniform blobs), set `stretch` to compress the noise along
one axis — `stretch=(0.3, 1, 1)` makes horizontally-elongated wisps. Narrow `contrast`
(e.g. (0.45,0.68)) for punchier clumps; raise `noise_detail` for finer structure.

GOTCHAS:
- Place the domain BEHIND the subject and ABOVE the ground so the foreground stays clear.
- Don't set `density` by hand — set `optical_depth` (0.15 subtle · 0.4 default · 0.8 heavy)
  and the helper derives density from the domain size (what you see is density × path
  length — that's why big domains fog-wall at densities that looked fine on small ones).
- Watch compare_frame's structure readout: fog wall = low σ; converge on the ref's σ.
- The volume SCATTERS existing light — give it a light source (emissive subject / world) or
  add a little `emission_strength` so it glows.
- EEVEE only renders volume within its VOLUMETRIC RANGE. If the domain is far from camera it
  vanishes — extend the range to cover it:
  `sc.eevee.volumetric_start = 0.1; sc.eevee.volumetric_end = <past the domain, e.g. 450>`.
- Watch the render's exposure readout: if `clipped(blown)` climbs, emission is too high —
  it washes the volume to flat pale fog with no structure.

```python
# nebula sky behind a ~100u-tall subject at origin
import bpy
bpy.context.scene.eevee.volumetric_start = 0.1
bpy.context.scene.eevee.volumetric_end = 450.0   # must reach the domain or it vanishes
dom = bvfx_volume(
    name="nebula",
    center=(0, 130, 210),        # behind (+Y) and above the subject
    size=(560, 120, 330),        # wide, thin front-to-back, tall
    optical_depth=0.4,           # visibility, size-independent (density auto-derived)
    color=(0.05, 0.30, 0.10),    # match the ref's tint
    emission_strength=0.6,       # faint self-glow so structure reads
    noise_scale=2.4, noise_detail=9.0,
    contrast=(0.45, 0.70),       # narrow = punchy clumps; widen for softer wisps
    stretch=(0.3, 1.0, 1.0),     # horizontal streaked wisps
)
```
