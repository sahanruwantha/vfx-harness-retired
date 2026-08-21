---
name: point-cloud-displace-dissolve
tags: [dissolve, disintegration, particles, instancing, displace, point-cloud, keyed, no-sim]
blender: "5.2+"
when: "a vertex-instanced dot cloud (scatter tower, debris, hologram) must disperse/dissolve over time WITHOUT a sim — keyed values only"
verified: true
---
A loose-vert point cloud with VERTS-instanced dots can be dissolved deterministically
with one keyed Displace modifier — instances follow the displaced verts. Two silent
traps make the naive setup a NO-OP (verified by spike, Blender 5.2 headless):

- `direction` defaults to `'NORMAL'` — loose verts have NO normals (no faces), so
  displacement is exactly zero at any strength. Use `'RGB_TO_XYZ'` (or an axis).
- `mid_level` defaults to 0.5 — displacement = (tex − mid_level) × strength, so a
  mid-grey texture cancels toward zero. Set `mid_level = 0.0` for outward scatter.

```python
tex = bpy.data.textures.new("dissolve_noise", type="CLOUDS")
tex.noise_scale = 0.4                     # feature size of the scatter directions
mod = holder.modifiers.new("dissolve", "DISPLACE")   # holder = the point-cloud mesh obj
mod.texture = tex
mod.direction = "RGB_TO_XYZ"              # REQUIRED: NORMAL is a no-op on loose verts
mod.mid_level = 0.0                       # REQUIRED: else mid-grey cancels to ~zero
mod.strength = 0.0
mod.keyframe_insert("strength", frame=f_start)       # intact
mod.strength = 30.0
mod.keyframe_insert("strength", frame=f_end)         # dispersed
```

Pair with keys on the instanced template's scale (1 → ~0.4) and its emission
strength (→ 0) on the same frame range for the full disintegration read.

GOTCHAS:
- Keyed modifier strength evaluates correctly, but under 5.x slotted actions
  `action.fcurves` reads EMPTY — verify keys with the `list_keyframes` tool /
  channelbags, never by len(action.fcurves).
- Inverting the ramp (strength high → 0) runs the effect backwards: chaos settling
  into place (energy-mass "landing" on a crown, debris assembling).
- Per-tier/per-cloud CLOUDS textures with different seeds (`tex.noise_basis` offset
  or separate textures) avoid visibly identical scatter directions.
