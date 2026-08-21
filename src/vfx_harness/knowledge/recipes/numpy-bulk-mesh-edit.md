---
name: numpy-bulk-mesh-edit
tags: [mesh, numpy, performance, city, greeble, instancing, scatter, layout, occlusion, foreach]
blender: "5.2+"
when: "thousands of scattered boxes/greeble live in ONE mesh and a REGION of them must be reshaped — flattening a city so it stops occluding the hero, grading heights by distance, sinking a plaza"
verified: true
---
Never loop over `mesh.vertices` to reshape a scatter mesh — 6k boxes is 50k verts and a
Python loop takes minutes per build. Pull the whole `co` array once with `foreach_get`,
`reshape(-1, K, 3)` so each row is one primitive (K = verts per primitive, 8 for a box),
classify rows with `np.select` on their centroids, scale, and push back with `foreach_set`.
Whole thing is milliseconds and stays deterministic across rebuilds.

The common use is OCCLUSION CONTROL: when the camera settles low inside a scatter field,
the near blocks eat the hero. Flatten by camera-relative region (near field and the hero's
plaza go lowest) so the foreground becomes a lit carpet instead of black silhouettes —
much cheaper and more controllable than deleting geometry or re-scattering.

GOTCHAS:
- This only works if the primitives are CONSECUTIVE vertex runs of equal size (true for
  meshes built by repeated primitive-add + join, or from_pydata). Verify
  `len(verts) % K == 0` first, and eyeball one row before trusting the reshape.
- Scale about each primitive's OWN base (`z.min(axis=1)`), not the origin, or blocks sink
  through the ground plane / float above it.
- `np.select` takes the FIRST matching condition — order narrow regions before broad ones,
  and always give a `default` (1.0 = untouched).
- `foreach_set` wants a flat array of the right dtype; call `mesh.update()` after.
- Do this BEFORE anything reads the mesh's bounds (parenting, boolean, camera framing).

```python
import numpy as np

K = 8                                    # verts per primitive (box)
obj = bpy.data.objects[scatter_name]
co = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
obj.data.vertices.foreach_get('co', co)
g = co.reshape(-1, K, 3)                 # (n_primitives, K, xyz)

cx, cy = g[:, :, 0].mean(axis=1), g[:, :, 1].mean(axis=1)
base   = g[:, :, 2].min(axis=1)          # each primitive's own footing
rad    = np.sqrt(cx ** 2 + cy ** 2)

# height factor per primitive, narrow regions first, default = leave alone
f = np.select(
    [(rad < HERO_R) & (cy < NEAR_Y),     # plaza that also overlaps the near field
     (rad < HERO_R),                     # hero plaza — keep the base clear
     (cy < BEHIND_Y),                    # at/behind camera
     (cy < NEAR_Y)],                     # near band
    [0.18, 0.40, 0.50, 0.45],
    default=1.0).astype(np.float32)

g[:, :, 2] = base[:, None] + (g[:, :, 2] - base[:, None]) * f[:, None]
obj.data.vertices.foreach_set('co', g.reshape(-1))
obj.data.update()
```

The same shape works for any per-primitive attribute: swap the `np.select` for a radial
falloff (`f = np.clip(rad / R, 0.2, 1.0)`), or drive X/Y to nudge primitives off a road.
