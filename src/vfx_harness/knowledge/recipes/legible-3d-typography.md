---
name: legible-3d-typography
tags: [typography, text, font, emission, billboard, camera, reveal, keyframes, hud, label, signage]
blender: "5.2+"
when: "a shot needs readable on-screen type — a label, a HUD/inventory list, a building sign — and it must hold up at delivery resolution"
verified: false
---
Blender FONT objects are sized in metres, but the critic judges type in PIXELS. Drive
`curve.size` from a target CAP HEIGHT, then check the pixel height that cap subtends at the
delivery resolution. Everything else (billboarding, reveal, exit) hangs off that.

    px = cap_m / (2 * dist * tan(fov/2)) * resolution_y

Three moves make type read instead of mush:

1. **Cap-height sizing.** `curve.size` is em size, not cap height. For Bfont Regular the cap
   is `0.682 * size`, so `curve.size = cap / 0.682`. Aim for ≥55 px of cap at 1080 for body
   type; below ~35 px anti-aliasing eats the strokes and the axis scores low.
2. **Billboard by empty, not by constraint on the text.** Parent the text to an empty that
   carries a `COPY_ROTATION` to the camera. The empty owns the orientation; the text keeps
   its local offset, so a multi-line block stays a rigid block and you can still scale/nudge
   individual lines.
3. **Emission above the Glare threshold.** Type at emission ~2-5 sits under a Bloom threshold
   of ~1.0 and reads as flat matte paint. Push to ~8-10 so glyphs carry a tight halo.

GOTCHAS:
- **PIN THE DELIVERY RESOLUTION at the bottom of the script.** If anything upstream leaves the
  scene at 960x540, a 54 px cap becomes 27 px and the typography axis fails for a reason that
  has nothing to do with the type. Set `resolution_x/y` and `resolution_percentage = 100` last.
- **A faded-to-0 emissive still renders as black geometry.** Ramping Strength to 0 does not
  remove the object — against a bright background it becomes a black hole in the shape of your
  text. Always follow the fade with a keyed `hide_render`/`hide_viewport` a frame or two later.
- **`TextCurve.body` is NOT animatable.** There is no keyframe for text content. Typewriter
  reveals are built as a STACK of prefix objects (`body[:j+1]`), each visible for exactly its
  own step via keyed `hide_render`; the last prefix of a line stays on. See
  `stepped-visibility-swap` for the boolean-keying rules.
- **Fades read as pops on eased curves.** An emission ramp with default BEZIER holds near-full
  for most of its span then drops. Force LINEAR on the fcurves — and in 5.x fcurves live in
  per-slot channelbags, not `action.fcurves` (see `blender-5-api`).
- **Bfont is wide.** Condense to a reference width with a non-uniform object `scale.x` rather
  than fighting the font; `scale = (target_w / measured_w, 1, 1)` is stable and cheap.
- Extruded signage type must sit PROUD of its backing panel (~0.3-0.5% of the panel width) or
  it z-fights.

```python
import bpy, math
sc = bpy.context.scene
cam = bpy.data.objects['camera']

BFONT_CAP = 0.682                    # cap height of Bfont Regular at size 1.0

def new_text(name, body, cap, align_x='LEFT', align_y='TOP_BASELINE', extrude=0.0):
    cu = bpy.data.curves.new(name, type='FONT')
    cu.body, cu.size = body, cap / BFONT_CAP        # size from CAP HEIGHT
    cu.align_x, cu.align_y, cu.extrude = align_x, align_y, extrude
    ob = bpy.data.objects.new(name, cu)
    sc.collection.objects.link(ob)
    return ob

def cap_px(cap_m, dist_m, focal_mm, res_y, sensor_mm=36.0):
    """Sanity-check legibility BEFORE rendering. Want >=55 px for body type at 1080."""
    fov = 2 * math.atan(sensor_mm / (2 * focal_mm))
    return cap_m / (2 * dist_m * math.tan(fov / 2)) * res_y

def billboard_root(name, location):
    """Empty that faces camera; parent type to it so a block stays rigid."""
    e = bpy.data.objects.new(name, None)
    e.empty_display_size = 0.1
    sc.collection.objects.link(e)
    e.location = location
    e.constraints.new('COPY_ROTATION').target = cam
    return e

def key_vis(ob, spans):                             # [(frame, hidden_bool), ...]
    for f, h in spans:
        ob.hide_render = ob.hide_viewport = h
        ob.keyframe_insert('hide_render', frame=f)
        ob.keyframe_insert('hide_viewport', frame=f)

def key_strength(mat, spans, linear=True):
    """Key an emission material's Strength; LINEAR or the fade holds then pops.
    5.x fcurves live in per-slot channelbags, NOT action.fcurves — see `blender-5-api`."""
    em = next((n for n in mat.node_tree.nodes if n.type == 'EMISSION'), None)
    assert em is not None, 'no EMISSION node in %s' % mat.name
    for f, v in spans:
        em.inputs['Strength'].default_value = v
        em.inputs['Strength'].keyframe_insert('default_value', frame=f)
    if not linear:
        return
    ad = mat.node_tree.animation_data
    for layer in ad.action.layers:                  # iterate — layers[0] IndexErrors
        for strip in layer.strips:
            for cb in strip.channelbags:
                for fc in cb.fcurves:
                    for kp in fc.keyframe_points:
                        kp.interpolation = 'LINEAR'
                    fc.update()

# ---- typewriter reveal: prefix stack, STEP frames per character ----------------
CAP, PITCH, STEP, START, OFF = 0.176, 0.285, 2, 140, 199
root = billboard_root('inv_root', (1.29, 0.09, 1.37))
mat = bvfx_emission('mat_type', (0.949, 0.969, 1.0), 9.0)   # >= Glare threshold

g = 0
for li, line in enumerate(['Code', 'Configs', 'Backups']):
    for j in range(len(line)):
        f_on = START + g * STEP
        ob = new_text('inv_%d_%02d' % (li, j), line[:j + 1], CAP)
        ob.parent, ob.location = root, (0.0, -li * PITCH, 0.0)
        ob.data.materials.append(mat)
        last = (j == len(line) - 1)
        key_vis(ob, [(1, True), (f_on, False),
                     (OFF if last else f_on + STEP, True)])   # last prefix persists
        g += 1

# ---- exit: LINEAR emission fade, THEN hide (0-strength still renders black) ----
key_strength(mat, [(186, 9.0), (198, 0.0)], linear=True)

# ---- pin delivery resolution LAST so nothing upstream wins ---------------------
sc.render.resolution_x, sc.render.resolution_y = 1920, 1080
sc.render.resolution_percentage = 100
```
