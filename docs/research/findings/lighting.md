# The tower was never missing detail. It was missing a light.

Measured 2026-08-15 by `docs/research/probes/spike_lighting{,2,3}.py`. This is the finding that
explains five failed attempts at layer 2 and $78.76 of building geometry that already
existed.

## The result

Facade contrast — sigma over the hero tower's non-window pixels at f45, with the mask
fixed from the shipping render so a brighter facade cannot reclassify itself as window:

| variant | facade sigma |
|---|---|
| shipping (emission only, no light) | **18.37** |
| lit body + local area key, best of 8 | **57.88** (3.15x) |

All eight albedo x energy cells improved. Even at the shipping near-black albedo of 0.055
with a modest key, 18.37 -> 27.76.

![4-up](probes/L38_lighting_reveals_mesh.png)

Left is what ships: a flat black slab with two dotted window columns — the "featureless
flat-grey box" the critic marked down four frames running. The lit variants show a deep
central recess, ribbed vertical piers, the crown box, setbacks and the podium.

All of that is in the mesh. 260,332 polygons of it. Layer 2 spent five attempts building
"two bright OUTER window strips against a darker recessed core" on top of a mesh that
already had exactly that.

## Why nobody could see it

Two faults compounding, and the second is a trap rather than an omission.

**1. The hero's material is a pure EMISSION shader.** Adding a key and fill changed the
render by exactly `+0.0` sigma. An emission surface cannot be lit at any intensity. This
much was expected.

**2. A sun renders black in this scene.** Under the shot's world Volume Scatter
(`bvfx_volumetric_world`, density 6e-5), a white 0.8-albedo body under a sun at energy 25
renders at mean **6.74/255**. The same body under a **local** area light at the same
atmosphere reads mean **154**, sigma 60. A sun is infinitely distant, so its shadow ray
through an unbounded homogeneous volume accumulates unbounded optical depth.

It is a cliff, not a gradient:

| world volume density | hero mean (white body, sun 25) |
|---|---|
| 0.0 | 216.02 |
| 6e-6 | 216.02 — *bit-identical to no volume* |
| 6e-5 (**shipping**) | 6.74 |
| 6e-4 | 6.05 |

And no volumetric setting rescues it. `use_volumetric_shadows=False`,
`volumetric_end=400`, `volumetric_samples=256` all render **bit-identically**. (Separately:
`volumetric_end` does nothing at all unless `use_volume_custom_range` is set.)

So a builder reaching for a sun — the obvious first move for a key light — sees a black
tower and concludes, correctly from the evidence in front of it, that lighting does not
work in this scene. The only thing that puts pixels on screen is emission. That is exactly
the path every layer-2 attempt took: drive albedo to 0.02–0.05, add emissive strips,
repeat.

The pipeline had disabled its own instrument and then spent $78.76 working around the
readings.

## What this does and does not license

**Does:** the geometry reads under light, so the fix is a lighting/lookdev treatment
rather than more facade construction. Tickets #39 (lookdev), #40 (lighting), #44 (guard
the sun trap).

**Does not:** say this particular lighting is right for the shot. The test rig is
brighter and greyer than the night reference; it was built to answer "does the detail
survive to screen", not "what is the key". Choosing the actual balance is #40's job.

## A note on how this was found

Part 2 of the spike swept albedo against sun energy and produced eight bit-identical
renders. I had kept base colour 0.055 — the near-black albedo that is itself half the
complaint — so the test asked "can we light a black body" rather than "does the faceting
read". Then the sun made every variant identical regardless.

I was one write-up away from recording "lighting does not help" as a measured result. What
caught it was the numbers being *too* clean: eight cells agreeing to two decimals across a
6x albedo change is not a finding, it is a broken instrument. The probe scripts are kept
in full, including the botched sweep, because that failure mode is more instructive than
the result.
