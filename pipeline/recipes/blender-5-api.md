---
name: blender-5-api
tags: [api, blender5, engine, compositor, action, keyframes, eevee, gotcha]
blender: "5.2"
when: "bpy AttributeError/enum errors on engine, compositor (scene.node_tree), or action.fcurves"
verified: true
---
Blender 5.x moved several APIs the model reaches for by habit. The fixes:

- ENGINE enum: EEVEE is `'BLENDER_EEVEE'` — `'BLENDER_EEVEE_NEXT'` is NOT valid in 5.2
  (that was 4.2–4.x). The harness already sets it; don't touch `sc.render.engine`.
- COMPOSITOR: `scene.node_tree` is GONE. It's a node group on `scene.compositing_node_group`
  (build: CompositorNodeTree with a Group Output socket; Glare settings are input SOCKETS,
  Type='Bloom'). Use `bvfx_glare_bloom(...)` to set and `inspect_nodes('compositor')` to read.
- BLOOM: EEVEE-Next has no bloom toggle — it's the compositor Glare (see above).
- SLOTTED ACTIONS (4.4+): `action.fcurves` is gone. F-curves live in per-slot channelbags.
  Use the `list_keyframes` tool (handles both), or in bpy:
  ```python
  def action_fcurves(obj, action):
      fcs = getattr(action, "fcurves", None)
      if fcs and len(fcs): return list(fcs)   # legacy
      out = []; slot = obj.animation_data.action_slot
      for layer in action.layers:
          for strip in layer.strips:
              cb = strip.channelbag(slot) if slot else None
              out += list(cb.fcurves) if cb else [f for b in strip.channelbags for f in b.fcurves]
      return out
  ```
- VOLUME emission: no `ShaderNodeVolumeEmission` — use `ShaderNodeVolumePrincipled`
  (Emission Strength/Color inputs). Use `bvfx_volume(...)` and you never touch this.
- MOTION BLUR: it's on `scene.render`, NOT `scene.eevee` (legacy). Set
  `scene.render.use_motion_blur = True`, `scene.render.motion_blur_shutter = <0.5-2.0>`,
  `scene.render.motion_blur_position` ('CENTER'/'START'/'END'). `scene.eevee.use_motion_blur`
  raises AttributeError in 5.x. (The harness preamble already enables render motion blur.)
