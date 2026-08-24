---
name: harness-lesson-rig-aim-ownership
tags: [camera, aim, track-to, look-at, constraint, pitch, rig, repair, canonical, rotation]
blender: "5.2+"
when: "a canonical repair or build touches HOW a rig-parented camera aims — moving/removing a Track-To, baking look-at rotations, or the render camera points away from the set while its keyframe contracts still pass"
verified: false
---
Distilled from HIR-0015 (run 20260824T103842Z-afec73): two canonical repairs "fixed" camera
aim on a rig-parented camera and published an artifact whose camera faced away from the set
at every frame — while its keyframe-schedule contract still passed, because the schedule
lives on the rig and the breakage lived on the child.

The rig contract (see `camera-roll-rig` for the full recipe):

- The RIG empty owns **location + pitch**. Level is `rotation_euler[0] = radians(90)` — a
  camera looks down its own −Z, so 90° is horizontal, not 0.
- The camera child owns **roll only** (`rotation_euler[2]`, local Z). Its X/Y rotation must
  stay 0 and UNKEYED.

The two failure shapes this lesson exists to prevent:

1. **Baking a world-space look-at into the CHILD's local rotation.** A look-at toward a
   mostly-horizontal target is itself ≈ a 90° X rotation. Composed with the rig's keyed
   90°, the camera's world pitch lands near 180° — it renders the sky behind the set. If
   `probe_candidate` shows camera `world_rotation_deg` near ±180 on X, this is what
   happened. Near ±90 is level and correct.
2. **A TRACK_TO whose up axis is parallel to the travel/aim direction** (e.g. `UP_Y` while
   flying down +Y). The constraint solution is degenerate: it can resolve differently
   between the warm session and a fresh rebuild of the same script. Do not repair it by
   moving the constraint between rig and camera — remove it and key the RIG's pitch
   numerically from the same samples:

```python
import math
# aim the RIG (never the camera child) at a target from known spine samples
for frame, cam_pos in samples:                       # world-space positions
    dx, dy, dz = (target[i] - cam_pos[i] for i in range(3))
    horizontal = (dx * dx + dy * dy) ** 0.5
    pitch_up = math.degrees(math.atan2(dz, horizontal))
    rig.rotation_euler = (math.radians(90.0 + pitch_up), 0.0, math.atan2(-dx, dy))
    rig.keyframe_insert('rotation_euler', frame=frame)
```

Verify with `probe_candidate` after the edit: camera world rotation X near 90°, the
subject's evidence rows in range, and the solid render showing the set — not empty sky.
The `rig_contract` scene check fails closed on X/Y keys or tracking constraints on a
rig-parented camera; a repair that reintroduces them is rejected before it costs a round.
