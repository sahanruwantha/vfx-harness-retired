---
name: stepped-visibility-swap
tags: [animation, keyframes, visibility, proxy, hero, swap, layout, instancing]
blender: "5.2+"
when: "one object must replace another on an exact frame — proxy→hero, LOD, damage state, reveal"
verified: true
---
To hand off between two objects mid-shot (proxy stand-in → hero asset, clean → destroyed,
LOD near → far), key `hide_render` AND `hide_viewport` on the frame before and the frame of
the cut. Booleans are keyed with CONSTANT interpolation automatically, so the change is a
true hard cut: there is never an in-between frame with both objects on screen or one
half-faded. Hide the cut inside a blackout, a whip, an impact flash or a shutter-wide beat
and it is invisible.

This is what lets a layout gate ship cheap proxies that a later gate deletes and replaces —
the timing is already locked and verified before the hero asset exists.

GOTCHAS:
- Key BOTH `hide_render` and `hide_viewport`. Keying only `hide_render` looks correct in the
  viewport and correct in renders, but any tool that measures the depsgraph/viewport state
  (framing checks, bbox queries) reads the wrong object and lies to you.
- Set the value, THEN `keyframe_insert` — insert reads the current value.
- You need the "before" key too. A single key at the cut frame holds that value backwards
  across the entire shot; the pair at (cut-1, cut) is what makes it step.
- `hide_viewport` (the monitor icon) is NOT `hide_set()` (the eye icon). `hide_set()` is
  view-layer-local, isn't keyable the same way, and doesn't survive a headless rebuild.
- Objects in a collection you plan to delete later: link them to a dedicated collection
  (e.g. `proxy`) at creation so the teardown is one `bpy.data.collections.remove`, not a
  name-matching sweep.

```python
import bpy

def stepped_swap(objects_visible_before, objects_visible_after, cut_frame):
    """Hard-cut visibility handoff at `cut_frame` (first frame the 'after' set is live)."""
    pairs = [(o, False) for o in objects_visible_before] + \
            [(o, True) for o in objects_visible_after]
    for ob, hidden_before in pairs:
        ob.hide_render = ob.hide_viewport = hidden_before      # state up to cut-1
        ob.keyframe_insert('hide_render', frame=cut_frame - 1)
        ob.keyframe_insert('hide_viewport', frame=cut_frame - 1)
        ob.hide_render = ob.hide_viewport = not hidden_before  # state from cut onward
        ob.keyframe_insert('hide_render', frame=cut_frame)
        ob.keyframe_insert('hide_viewport', frame=cut_frame)

# proxy A carries the first half, hero/proxy B takes over on the cut
stepped_swap([proxy_a_body, proxy_a_plume], [proxy_b], cut_frame=20)

# verify: exactly one set renders on each side of the cut
for f in (19, 20):
    bpy.context.scene.frame_set(f)
    print(f, [o.name for o in bpy.context.scene.objects if not o.hide_render])
```
