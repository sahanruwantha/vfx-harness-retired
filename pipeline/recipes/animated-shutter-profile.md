---
name: animated-shutter-profile
tags: [camera, motion-blur, shutter, keyframes, action, pacing, eevee]
blender: "5.2+"
when: "one shutter value can't serve the whole shot — calm holds smear, or fast action strobes"
verified: true
---
Motion blur shutter is a normal animatable property, so it can carry a *pacing profile*
instead of one compromise value: narrow (0.4–0.6) on slow/held beats so detail stays crisp,
wide (1.4–1.8) through whip pans, spins and impacts so fast motion reads as a smear rather
than a strobe, then closing again for the settled tail. Keying it costs nothing at render
time and removes the usual "blurry when still / juddery when fast" tradeoff.

GOTCHAS:
- The property lives on `scene.render`, NOT `scene.eevee` (`scene.eevee.use_motion_blur`
  raises AttributeError in 5.x). See `blender-5-api`.
- `keyframe_insert` reads the property's CURRENT value, so you must set it before each
  insert. Call `sc.frame_set(f)` first too — the scene must be evaluated at the frame you
  key, or the stored value can come from the wrong evaluation state.
- Always `frame_set` back to the start frame when done; leaving the scene parked on the last
  keyed frame silently poisons any framing/measurement code that runs after you.
- Shutter is evaluated PER FRAME. Verify by stepping frames and reading the evaluated value,
  not by reading `sc.render.motion_blur_shutter` once (that's just wherever the scene sits).
- Keep the ramp on the same frames as the action beats. A shutter change on a frame with no
  motion change is visible as a texture pop.
- >2.0 is rarely useful; it starts eating the silhouette entirely.

```python
import bpy
sc = bpy.context.scene

sc.render.use_motion_blur = True
sc.render.motion_blur_position = 'CENTER'      # 'START'/'END' shift the smear off the pose

# (frame, shutter) — narrow on holds, wide through the fast beat, closing on the tail
SHUTTER = ((1, 0.5), (16, 1.5), (20, 1.8), (38, 0.6))

for _f, _s in SHUTTER:
    sc.frame_set(_f)                            # evaluate at the frame you're keying
    sc.render.motion_blur_shutter = _s          # keyframe_insert reads the CURRENT value
    sc.render.keyframe_insert('motion_blur_shutter', frame=_f)

sc.frame_set(sc.frame_start)                    # never leave the scene parked mid-shot

# verify: step and read the EVALUATED value
for _f in (1, 20, 38):
    sc.frame_set(_f)
    print(_f, round(sc.render.motion_blur_shutter, 3))
sc.frame_set(sc.frame_start)
```
