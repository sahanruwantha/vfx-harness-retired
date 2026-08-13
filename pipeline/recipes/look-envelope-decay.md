---
name: look-envelope-decay
tags: [look, animation, keyframes, decay, envelope, grade, emission, volume, world, workflow, blender5]
blender: "5.2+"
when: "a shot's look must rise to a peak and then settle/decay over a span, and MANY sockets (emission, volume density, world strength) have to move together without re-balancing"
verified: true
---
Do NOT hand-key each emitter's decay separately. Tune the whole look ONCE at the scored
milestone frame, then drive every look socket through a single shared multiplier table:

    value(f) = V_AT_SCORED_FRAME * DECAY(f)

The balance tuned against the critic's reference at the scored frame is then the SAME balance
at every later frame, only dimmer. Retiming or reshaping the settle means editing the `DECAY`
tuple and nothing else — no re-balancing, no per-material archaeology. This is what lets a
gate end *settled but not frozen*: the last span carries LOOK keys only, so levels keep
drifting down while nothing moves.

The envelope does not have to be monotonic — a fast rise to a peak slightly BEFORE the scored
frame, then a slow monotone ease down through it, reads as a world resolving and then calming.
Keep the scored frame's multiplier at exactly `1.00` so the tuned values are literal.

GOTCHAS:
- Anchor the table at `1.00` on the scored frame. If the scored frame's multiplier drifts off
  1.0, every tuned value silently shifts and the fingerprint the critic passed is gone.
- Make the decay leg **strictly monotone** in mean. A settle that wobbles reads as a mistake;
  a settle that only ever dims reads as intent. Sample the frames and check.
- "Settled" ≠ "frozen": if sigma also collapses the frame looks dead. Aim for level down,
  sigma within a few percent.
- Sockets keyed by an f-string/`data_path` must resolve on the tree that OWNS the animation —
  key node-socket curves on the `node_tree` (material/world/compositor group), not on the
  material or world ID itself.
- Force `LINEAR` interpolation on every curve the envelope touches. Bezier handles overshoot a
  fast rise into values above the peak, and the overshoot differs on a re-run, so the delta
  stops being idempotent. (See `milestone-delta-keying` for the robust `linearise()`.)
- Anything faded to 0 by the envelope is still RENDERED geometry — pair it with a `hide_render`
  key (`CONSTANT` interpolation) to truly vanish.

```python
D = bpy.data

def ksock(nt, path, frame, value):
    """Key a node-socket default_value on a node tree by data_path."""
    node = nt.nodes[path.split('"')[1]]
    idx = int(path.split("inputs[")[1].split("]")[0])
    node.inputs[idx].default_value = value
    nt.keyframe_insert(path, frame=frame)

# one envelope: (frame, multiplier). 1.00 sits on the SCORED frame.
DECAY = ((24, 1.02), (27, 1.16), (30, 1.00), (36, 0.95),
         (40, 0.89), (44, 0.83), (48, 0.78))

EM  = 'nodes["Emission"].inputs[1].default_value'
LOOK = (  # (material, socket data_path, value at the scored frame)
    ("city_far_mat",      EM,                                            5.0),
    ("street_mat",        EM,                                            2.2),
    ("cloud_dome_mat",    'nodes["dome_gain"].inputs[1].default_value',  0.80),
    ("sky_haze_vol",      'nodes["Math.013"].inputs[1].default_value',   0.062),
    ("tower_windows",     'nodes["Math.011"].inputs[1].default_value',   6.0),
)
for mat, path, v_scored in LOOK:
    nt = D.materials[mat].node_tree
    for f, d in DECAY:
        ksock(nt, path, f, v_scored * d)

# the world rides the same envelope
wnt = D.worlds["sky_world"].node_tree
for f, d in DECAY:
    ksock(wnt, 'nodes["Background"].inputs[1].default_value', f, 3.6 * d)
```

**Volume density is an OCCLUDER, not just a glow.** A haze slab lying across the horizon eats
whatever is behind it, so its entry in `LOOK` trades against the far elements' emission. When a
mid/far band reads too dark, lower the haze's density in the table and raise the far emission —
not the other way round.

**Assert the settle.** A gate that retimes curves can leave a transform drifting into the hold
and fail review even with a perfect look. Scan every object's action for transform keys inside
the settle span and fail the build if any exist:

```python
_moving = []
for ob in D.objects:
    ad = ob.animation_data
    if not ad or not ad.action:
        continue
    for layer in ad.action.layers:                     # iterate — never layers[0]
        for strip in layer.strips:
            for cb in strip.channelbags:
                for fc in cb.fcurves:
                    if fc.data_path in ("location", "rotation_euler", "scale",
                                        "rotation_quaternion"):
                        for kp in fc.keyframe_points:
                            if SETTLE_START <= kp.co[0] <= SETTLE_END:
                                _moving.append((ob.name, fc.data_path, kp.co[0]))
assert not _moving, "transform keys inside the settle: %r" % _moving
```
