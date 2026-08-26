---
name: blackout-beat
tags: [lighting, keyframes, compositor, glare, bloom, volume, emission, blackout, eevee, blender5]
blender: "5.2+"
when: "the shot must pass through a near-black frame (blackout, blink, impact, power-cut, invisible edit) inside a CONTINUOUS camera move — and the measured black% refuses to climb"
verified: false
---
A blackout is not "set the lights to zero". Zeroing every emission socket typically still
leaves 10–15% of the frame off black, because three things keep emitting or occluding after
you think you turned them off. Drive one shared LEVEL ENVELOPE across every emissive socket,
then fix the three leaks below.

The envelope is a single list of `(frame, multiplier)` applied to every socket's base value,
so the whole world dims as one unit and there is exactly one curve to retime. Key the floor
value at the darkest frame, one re-light key after it, and full strength at the frame the
next layer owns — everything before the first envelope key is untouched.

GOTCHAS:
- **Bloom is what breaks the blackout.** A normal-size Glare on a handful of clipped cores
  smears them over a huge fraction of the frame. Key the compositor Glare `Size` DOWN and
  `Strength` UP across the dark frames (tight, hard halo instead of a wide soft one), and
  restore both afterwards so later layers keep their look. This alone moved black% from ~86 to
  ~94 on barrel_roll. Glare settings are input SOCKETS in 5.x (see `blender-5-api`).
- **Dimming to 1% is not enough on the deepest frame — hang a CURTAIN.** Under fast motion the
  dying world smears into faint but plainly readable filament arcs (measured 3–8/255 across one
  quadrant), and *that arc* is what a critic scores as "a residual silhouette". Key
  `hide_render=True` on EVERY object except the hero elements for the darkest frame ALONE, then
  back off. It is free: rendering that frame with the world hidden and only the hero elements
  left measured mean 0 / black 100%, i.e. the dimmed world contributed nothing but the arc.
  Prefer this over chasing the last points of black% by dropping the level further — that
  trade drops the frame mean out of the reference band and the arcs survive anyway.
- **An emissive faded to 0 is still black geometry**, and **a volume faded to 0 still
  ABSORBS**. A dead volume domain over a bright background reads as a legible dark silhouette.
  Give volumes a `hide_render` key ONE FRAME EARLIER than the emissives they wrap.
- Do not take everything to literal zero. Leaving a spark (~1–5% of base) on fragment/particle
  emitters reads as *dying embers*; a hard cut to zero reads as *the renderer turned off*.
- **Motion blur inflates the frame mean.** The same emission that measures 54 on a static
  frame can measure 88 once the camera rolls fast enough to smear every hot dot. Key the
  envelope to MEASURED means per frame, not to "hold full strength" — holding literal values
  overshot the storm by ~50%.
- The last 2% of level is not linear in perceived black%: dropping a floor from 0.02 to 0.009
  cost 0.7 of mean but bought 8 points of black%, because it pushed a smeared arc below the
  display floor. Bisect the floor value; don't assume 0.02 ≈ 0.
- Re-running the delta must be idempotent: Bezier handles overshoot a one-frame crash into
  NEGATIVE values. Force `LINEAR` on every fcurve this layer touches (and `CONSTANT` on
  `hide_render`) after keying.

```python
import bpy

# (frame, level multiplier): hold -> plateau -> crash -> floor -> re-light -> restore
LVL = ((14, 1.00), (16, 0.45), (18, 0.40), (19, 0.15),
       (20, 0.009), (21, 0.24), (22, 0.55), (24, 1.00))

def ks(sock, seq):
    for f, v in seq:
        sock.default_value = v
        sock.keyframe_insert('default_value', frame=f)

def envelope(sock, base, lvl=LVL, spark=None):
    """Ride `base` down the shared envelope; `spark` overrides the floor frame."""
    seq = [(f, base * m) for f, m in lvl]
    if spark is not None:
        seq = [(f, spark if m == min(x[1] for x in lvl) else v) for (f, v), (_, m) in zip(seq, lvl)]
    ks(sock, seq)

def blackout_glare(glare_node, dark=(19, 21), normal_size=0.55, normal_strength=0.30,
                   tight_size=0.08, hot_strength=0.62, pre=1, post=1):
    a, b = dark
    ks(glare_node.inputs['Size'],
       ((a - pre, normal_size), (a, tight_size), (b, tight_size), (b + post, normal_size)))
    ks(glare_node.inputs['Strength'],
       ((a - pre, normal_strength), (a, hot_strength), (b, hot_strength), (b + post, normal_strength)))

def kill_at(ob, frame, lead=0):
    """Hard-hide `ob` at `frame`; pass lead=1 for VOLUME domains (they absorb at 0 emission)."""
    ob.hide_viewport = ob.hide_render = False
    ob.keyframe_insert('hide_render', frame=1)
    ob.keyframe_insert('hide_render', frame=frame - lead - 1)
    ob.hide_render = True
    ob.keyframe_insert('hide_render', frame=frame - lead)

def curtain(frame, keep=(), scene=None):
    """Hide EVERY object but `keep` on `frame` alone — kills smeared residual arcs that
    dimming cannot reach. Keys CONSTANT-friendly steps: visible, hidden, visible."""
    scene = scene or bpy.context.scene
    for ob in scene.objects:
        if ob.name in keep or ob.type in {'CAMERA', 'LIGHT'}:
            continue
        for f, v in ((1, False), (frame - 1, False), (frame, True), (frame + 1, False)):
            ob.hide_render = v
            ob.keyframe_insert('hide_render', frame=f)


def linearise(ad):
    """Re-runnable deltas: Bezier overshoot on a 1-frame crash goes negative."""
    if not ad or not ad.action:
        return
    for slot in ad.action.slots:
        cb = ad.action.layers[0].strips[0].channelbag(slot)
        if not cb:
            continue
        for fc in cb.fcurves:
            const = 'hide_render' in fc.data_path
            for kp in fc.keyframe_points:
                kp.interpolation = 'CONSTANT' if const else 'LINEAR'
            fc.update()

# --- apply -----------------------------------------------------------------------------
D = bpy.data
envelope(D.materials['street_mat'].node_tree.nodes['Emission'].inputs['Strength'], 14.0)
envelope(D.materials['dot_mat'].node_tree.nodes['em'].inputs['Strength'], 2.35, spark=0.16)
blackout_glare(D.node_groups['bvfx_compositor'].nodes['Glare'], dark=(19, 21))
kill_at(D.objects['plume_cloud'], 20)             # emissive mesh
kill_at(D.objects['plume_glow'], 20, lead=1)      # volume domain: dies a frame earlier
curtain(20, keep={'streak_0', 'streak_1', 'streak_2'})   # f20 renders the streaks ALONE

for coll in (D.materials, D.worlds):
    for db in coll:
        if db.node_tree:
            linearise(db.node_tree.animation_data)
for ng in D.node_groups:
    linearise(ng.animation_data)
```
