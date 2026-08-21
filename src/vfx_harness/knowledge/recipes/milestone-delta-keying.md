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
  then `fc.update()`. `set_keys()` below is the reusable form.
- **Match the fcurve EXACTLY on `data_path` AND `array_index`.** A substring test like
  `'Emission' in fc.data_path` also catches the colour curve (`inputs[0]`) and silently writes
  a strength value into a colour channel — the render goes wrong somewhere you aren't looking.
  Colour is `inputs[0]` with `array_index` 0/1/2 = R/G/B; strength is `inputs[1]`, index 0.
- **Assert you hit something.** A renamed node or a re-numbered `Math.011` turns a retro-edit
  into a silent no-op, and you spend a critic round wondering why the value didn't take.
- Move `handle_left`/`handle_right` with `co[1]`. Editing only `co` leaves the bezier handles
  at the old value, so the curve bulges away from both of its own keys between them.
- A scalar assignment to a property that is already keyed is DEAD CODE — the fcurve overwrites
  it on the next `frame_set`. Re-key it (or leave the upstream curve alone), never assign.
  Plain unkeyed siblings (`render.motion_blur_position`) still take a normal assignment.

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

Retro-editing existing keys (5.x slotted actions) — works for any ID with `animation_data`
(material/world node_tree, scene, object):

```python
def _channelbag_fcurves(ad):
    """Yield every fcurve of a 5.x slotted action.

    ITERATE layers/strips — `layers[0].strips[0]` raises IndexError on an ID whose
    action has no layer/strip yet (nothing keyed on it). See `blender-5-api`.
    """
    if not ad or not ad.action:
        return
    for layer in ad.action.layers:
        for strip in layer.strips:
            for cb in strip.channelbags:
                yield from cb.fcurves


def linearise(ad):
    """Re-runnable deltas: bezier handles overshoot a fast ramp into nonsense, and the
    overshoot differs per re-run, so a delta layer stops being idempotent. Booleans must
    be CONSTANT or they render half-hidden on the in-between frames."""
    for fc in _channelbag_fcurves(ad):
        const = "hide_render" in fc.data_path
        for kp in fc.keyframe_points:
            kp.interpolation = "CONSTANT" if const else "LINEAR"
        fc.update()

def set_keys(ad, data_path, array_index, frame_vals):
    """Retarget EXISTING keys on one exact (data_path, array_index) curve.

    frame_vals: {frame: new_value}. Frames not present are left alone; no keys are created.
    """
    hit = False
    for fc in _channelbag_fcurves(ad):
        if fc.data_path != data_path or fc.array_index != array_index:
            continue
        hit = True
        for kp in fc.keyframe_points:
            v = frame_vals.get(round(kp.co[0], 1))
            if v is not None:
                kp.co[1] = kp.handle_left[1] = kp.handle_right[1] = v   # handles too
        fc.update()
    assert hit, "no fcurve %s[%d]" % (data_path, array_index)

EM_STR = 'nodes["Emission"].inputs[1].default_value'          # strength
EM_COL = 'nodes["Emission"].inputs[0].default_value'          # colour, index 0/1/2 = R/G/B
ad = bpy.data.materials['path_gold'].node_tree.animation_data
set_keys(ad, EM_STR, 0, {340.0: 22.0, 372.0: 44.0, 462.0: 32.0})
set_keys(ad, EM_COL, 1, {340.0: 0.58, 374.0: 0.74, 462.0: 0.56})
```

**Settle assertion.** A delta layer that retimes curves can leave the last beat drifting, which
fails review even with perfect framing. Assert it instead of trusting it — sample the pose and
every value you keyed across the hold and require byte-equality:

Sample only sockets you know are numeric — a MENU socket (Glare `Type`/`Quality`) returns a
`str` and `round()` raises `TypeError: type str doesn't define __round__ method`. Use the
`_num` guard below if the sampled set is built generically (see `blender-5-api`).

```python
def _num(v):
    """round() a socket value that may be a float, a colour/vector, or a menu string."""
    if isinstance(v, str):
        return v                                   # menu socket — compare as-is
    if hasattr(v, '__len__'):                      # NB: str has __len__ too — checked first
        return tuple(round(x, 6) for x in v)
    return round(v, 6)

def _sample(f):
    sc.frame_set(f)
    cam = bpy.data.objects['camera']
    return (tuple(round(v, 6) for v in cam.matrix_world.translation),
            tuple(round(v, 6) for v in cam.matrix_world.to_euler()),
            round(cam.data.lens, 4),
            _num(nt.nodes['Emission'].inputs[1].default_value))   # + each keyed socket

_base = _sample(HOLD_START)
for _f in range(HOLD_START + 1, LAST_FRAME + 1):
    assert _sample(_f) == _base, "settle drift at f%d" % _f
```
