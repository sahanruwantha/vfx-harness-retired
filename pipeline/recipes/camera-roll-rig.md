---
name: camera-roll-rig
tags: [camera, rig, parenting, roll, barrel-roll, keyframes, animation, layout, eevee]
blender: "5.2+"
when: "the camera must ROLL (barrel roll, bank, whip, horizon tilt) while it also travels or holds on a subject — and rolling the camera directly swings the subject out of frame"
verified: true
---
Split the camera into TWO objects. An EMPTY (`cam_rig`) owns location and pitch; the camera
is its child and owns roll on its **local Z**. Because the parent already aimed the view
axis, the camera's local Z *is* the view axis, so `rotation_euler[2]` rolls the frame about
its own centre and the subject stays exactly where it was composed.

On a bare unparented camera, `rotation_euler[2]` is world yaw — it swings the subject out of
frame instead of rolling around it. That is the failure this rig exists to prevent, and it
is not obvious from the viewport at pitch 0: it only diverges once you pitch up or down.

The two levels also want DIFFERENT interpolation, which is the second reason to split them:

- **rig (travel) → BEZIER.** One smooth continuous move. Any reframe or velocity step in the
  travel is visible as a camera bump.
- **camera (roll) → LINEAR.** A roll "ladder" is a list of (frame, degrees) whose *segment
  rates* are the actual look. Bezier eases every corner and drags the peak rate off the
  frames you keyed it on — key a 75°/frame peak at f195 with BEZIER and the measured peak
  lands somewhere else entirely.

This composes with `camera-parented-spill-lights` (parent lights to the RIG so they do not
roll) and `screen-space-camera-elements` (parent to the CAMERA so they do roll with frame).

GOTCHAS:
- **Set `cam.matrix_parent_inverse.identity()` BEFORE `cam.location`.** Otherwise Blender
  bakes the camera's pre-parent world transform into the inverse and your "rig-local" offsets
  are silently wrong.
- **No `TRACK_TO` / `DAMPED_TRACK` constraint anywhere.** A tracking constraint recomputes the
  full orientation each frame including the roll axis, so it fights and flattens the roll.
  Aim by keying the rig's pitch numerically instead.
- Pitch lives on the rig as `rotation_euler[0]`, and a camera looks down its own −Z, so
  **level is `radians(90)`**, not 0. Pitching UP is `90 + p`; a sign flip here is the classic
  "horizon on the wrong side of frame" bug and it still *looks* plausible in isolation.
- Key roll with `keyframe_insert('rotation_euler', index=2)` — keying all three axes on the
  camera adds redundant curves that can drift and fight the rig.
- Blender 5.x actions are **slotted**: `action.fcurves` is not the list. Walk
  `action.layers → strips → channelbags → fcurves` (`_fcurves` below) or you will silently
  set interpolation on nothing. See `blender-5-api`.
- Call `fc.update()` after changing interpolation, or the curve keeps evaluating with the old
  handles until something else touches it.
- Verify the roll is a TRUE roll: put a marker on the subject, sample it with
  `bpy_extras.object_utils.world_to_camera_view` at roll 0/90/180 and confirm its radius from
  (0.5, 0.5) is unchanged. Constant radius = local-Z roll; growing radius = you are yawing.

```python
import bpy, math

def _fcurves(ob):
    """Every fcurve of a 5.x slotted action — ITERATE layers/strips, never index [0]."""
    out = []
    ad = ob.animation_data
    if not ad or not ad.action:
        return out
    for layer in ad.action.layers:
        for strip in layer.strips:
            for cb in strip.channelbags:
                out.extend(cb.fcurves)
    return out

def _interp(ob, mode):
    for fc in _fcurves(ob):
        for kp in fc.keyframe_points:
            kp.interpolation = mode
            if mode == 'BEZIER':
                kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
        fc.update()

def build_roll_rig(name='cam_rig', lens=35.0, sensor=36.0,
                   clip=(0.5, 20000.0), rig_display=4.0):
    """EMPTY owns location+pitch; camera child owns roll on local Z. Returns (rig, cam)."""
    sc = bpy.context.scene
    for n in (name, 'camera'):
        o = bpy.data.objects.get(n)
        if o:
            bpy.data.objects.remove(o, do_unlink=True)

    rig = bpy.data.objects.new(name, None)
    rig.empty_display_size = rig_display
    sc.collection.objects.link(rig)

    camd = bpy.data.cameras.new('camera')
    camd.lens, camd.sensor_width, camd.sensor_fit = lens, sensor, 'AUTO'
    camd.clip_start, camd.clip_end = clip
    cam = bpy.data.objects.new('camera', camd)
    sc.collection.objects.link(cam)
    cam.parent = rig
    cam.matrix_parent_inverse.identity()      # BEFORE location -> offsets stay rig-local
    cam.location = (0.0, 0.0, 0.0)
    cam.rotation_euler = (0.0, 0.0, 0.0)
    sc.camera = cam
    return rig, cam

def key_spine(rig, spine):
    """spine: [(frame, distance, altitude, pitch_up_deg), ...] — subject on +Y at origin."""
    rig.animation_data_clear()
    for f, d, z, p in spine:
        rig.location = (0.0, -d, z)
        rig.rotation_euler = (math.radians(90.0 + p), 0.0, 0.0)   # 90 == level
        rig.keyframe_insert('location', frame=f)
        rig.keyframe_insert('rotation_euler', frame=f)
    _interp(rig, 'BEZIER')                    # one smooth travel, no velocity step

def key_roll(cam, ladder):
    """ladder: [(frame, roll_deg), ...] — LINEAR so segment rates land where keyed."""
    cam.animation_data_clear()
    for f, r in ladder:
        cam.rotation_euler[2] = math.radians(r)
        cam.keyframe_insert('rotation_euler', index=2, frame=f)
    _interp(cam, 'LINEAR')

# --- apply -------------------------------------------------------------------------------
rig, cam = build_roll_rig(lens=35.0)
key_spine(rig, [(  1,  91.0, 22.0, 37.50),
                (100, 229.0, 23.4,  7.62),
                (440, 150.7, 58.9,  4.41)])

# hold flat, wind up, snap through the beat, settle at a full 360 (== level)
LADDER = [(1, 0.0), (150, 0.0), (191, 50.0), (195, 175.0),
          (205, 330.0), (232, 360.0), (440, 360.0)]
key_roll(cam, LADDER)

# verify segment rates — the roll ladder's real look. Confirm the peak lands on the
# frames you intended it to; with LINEAR keys these numbers are exactly what renders.
for (f0, r0), (f1, r1) in zip(LADDER, LADDER[1:]):
    print(f'f{f0}-{f1}: {(r1 - r0) / (f1 - f0):.2f} deg/frame')
```
