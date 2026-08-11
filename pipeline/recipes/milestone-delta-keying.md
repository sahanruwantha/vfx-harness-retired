---
name: milestone-delta-keying
tags: [animation, keyframes, milestones, workflow, hide_render, action, blender5]
blender: "5.2+"
when: "a later milestone script must change the look WITHOUT altering frames the critic already scored"
verified: true
---
Milestone builds run cumulatively (m1..mN in one scene). To evolve the look in mN
without shifting the pixels of frames scored in earlier milestones: for EVERY socket or
property you touch, first key it at the LAST SCORED FRAME with its pre-existing
*evaluated* value, then key the new target at the new milestone's frame. Interpolation
then only happens inside the new frame range; everything at or before the boundary is
byte-identical.

GOTCHAS:
- Read the evaluated value BEFORE assigning (`v = sock.default_value` — or evaluate the
  fcurve at the boundary frame) — assigning first destroys the value you needed to pin.
- Applies to object props too: `hide_render`, location per-axis
  (`keyframe_insert('location', index=2, frame=B)`), scale — same pin-then-ramp pattern.
- An emissive mesh faded to strength 0 still RENDERS as black geometry against the sky.
  Fade-outs need a `hide_render` key pair (False at frame B, True at B+1) to truly vanish.
- If you must retro-edit an existing key on an already-scored frame (e.g. compensating
  emission after shrinking an instanced template so total flux is unchanged), edit the
  keyframe point directly via the 5.x slotted-action channelbag (see `blender-5-api`),
  then `fc.update()`.

```python
def pin_then_ramp(sock, boundary_frame, target_frame, target_value):
    """Freeze current look at boundary, ramp to new look after it."""
    v = sock.default_value                      # read BEFORE writing
    sock.default_value = v
    sock.keyframe_insert('default_value', frame=boundary_frame)
    sock.default_value = target_value
    sock.keyframe_insert('default_value', frame=target_frame)

# example: dim a haze volume from f32 (last scored) to f48 (new milestone)
nt = bpy.data.materials['horizon_haze_vol'].node_tree
pin_then_ramp(nt.nodes['bvfx_dim_horizon_haze_vol_em'].inputs[1], 32, 48, 0.09)

# example: truly remove a faded-out emissive mesh after its fade completes at f28
ob = bpy.data.objects['seam_lights']
ob.hide_render = False; ob.keyframe_insert('hide_render', frame=28)
ob.hide_render = True;  ob.keyframe_insert('hide_render', frame=29)
```
