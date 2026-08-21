# The asset was right the whole time. The pipeline deleted it in layer 1.

Measured 2026-08-15 by `docs/research/probes/spike_lookdev.py` and
`vfx_harness/application/facade.py`. This
started as "add a lookdev stage", became "the asset has no facade", and ended somewhere
worse and much more fixable.

## The finding

`sr2_tower/model.glb` ships **three 2048×2048 textures** — base colour, normal, ORM — and
a UV map, on a full Principled material. Rendered with its native material, the asset
reproduces its design plate almost exactly.

![native vs procedural](probes/L45_native_vs_procedural.png)

*Left: the design plate. Middle: the mesh with its own textures. Right: what the pipeline
actually shipped.*

Dense window cells in strips, the dark recessed core, ribbed piers, the stepped podium
with its entrance, and the **"Silk Road 2.0" sign with glowing letters** — all of it, in
the box, from the start.

`bvfx_emissive_windows()` clears the object's material slots and replaces every bit of it
with a procedural grid. **Layer 1 calls it on the hero.** So from the first stage onward
the real facade was gone.

Measured front-on against the plate:

| | outer/core ratio | profile L1 |
|---|---|---|
| design plate (target) | 3.37 | — |
| **native texture** | **4.41** | **0.184** |
| `bvfx_emissive_windows` (shipped) | 1.91 | 0.242 |

The plate's polarity is bright outer strips against a dark core. The native texture has
it. The procedural grid **inverts** it — which is the exact defect the critic reported
across five layer-2 attempts, and which layer 2 could never fix, because the thing it was
being asked to reproduce had been deleted one layer upstream.

Layer 2 also spent rounds building the crown sign by hand. The sign is painted into the
base-colour map.

## Why nothing caught it

Ticket #30 added `compare_to_plate` at normalise time and it passes: 1.73x base flare
against the plate's 1.85x. **A silhouette statistic cannot see a facade.** It measures the
outline, and the outline was never wrong. Filed as #46.

That check is the pipeline's one working example of "generate, then gate on a
measurement", and `docs/operations/when-to-generate.md` holds it up as the pattern to generalise. It gates
on the wrong quantity. Same defect as #37 — a band statistic standing in for a
subject-shaped requirement — in a different place:

> A gate must measure the property the artifact is *for*. An asset is for its look, not
> its outline.

## The fix

`bvfx_emissive_from_texture(obj, threshold, soft, strength, tint, body_glow)` — keep the
asset's material, and make the bright cells of its own base-colour map emit. A smooth
`MapRange` ramp on texture luminance drives an Emission that is **added** to the existing
shader, so the body still takes light and self-shadows while the windows glow.

![night](probes/L45_night_from_texture.png)

*Reference f045 · the asset at night via emissive-from-texture · the same at 90° · what we
were shipping.*

`bvfx_emissive_windows` now warns loudly before discarding textures, and remains correct
for the untextured/procedural meshes it was written for.

## A caveat on the numbers

The night render scores facade L1 **0.35** against the plate — worse than the daylight
0.184, and worse than the procedural. That is not a regression; it is the metric being
misapplied. The plate is evenly lit, and at night the body correctly goes black, leaving
two narrow spikes where the plate has broad structure.

Which is the same shape of error as comparing a front-on plate to a corner-on shot frame.
Both say the profile comparison is only meaningful **between matched lighting and matched
angle** — so lookdev judges the asset under neutral light against the plate, and the night
key is lighting's call, not lookdev's. The department split earns its keep here.

## Three bugs found building the rig

Each returned a plausible number and raised nothing.

**1. `rotation_euler` is a silent no-op on imported assets.** glTF import leaves objects in
`QUATERNION` mode, where Blender ignores `rotation_euler` entirely. The turntable produced
four "angles" identical to the pixel. `bvfx_aim()` had the same hole. Fixed in
`_bvfx_import_asset` and `_bvfx_aim`.

**2. Segmentation on a magic constant.** `facade_profile` treated background as ≥240; the
turntable backdrop resolved to 211, so the whole frame counted as subject and every angle
returned an identical profile *of the backdrop*. Now segments against the measured corner
value, and warns when the shaft fills the frame.

**3. Framing from an assumed origin.** The rig set the asset to `(0,0,0)` and aimed at
`z = height/2`, assuming the base sat at z=0. It does not, so the camera framed the top
third and measured empty backdrop for two runs.

## The pattern

Layer 2 was asked to make the tower read like the reference while:

- the facade it was told to reproduce had been deleted by layer 1,
- the helper it was given inverts that facade's polarity by construction,
- a sun renders black in this scene, so emission was the only tool available
  ([`lighting.md`](lighting.md)), and
- the axis bundled massing, windows and typography into one scalar (#41).

Five attempts. $78.76. Every one of those is a pipeline defect, and not one of them was
visible in a render — which is why the fixes here are loud failure modes rather than
corrected constants.
