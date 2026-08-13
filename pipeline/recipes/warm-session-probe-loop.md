---
name: warm-session-probe-loop
tags: [workflow, measurement, probe, ablation, render, driver_namespace, blender5, gotcha]
blender: "5.2+"
when: "you need to measure a render many times inside one warm session while tuning — and want to know which node actually drives a metric before you spend turns tuning it"
verified: true
---
Tuning a gate against a reference means rendering-and-measuring dozens of times. Two things
make that cheap: define the measurement helper ONCE and persist it across separate `run_bpy`
calls, and ABLATE before you tune so you only tune sockets that actually move the metric.

## Persist the helper in `bpy.app.driver_namespace`

Each `run_bpy` call is its own `exec` scope — a `def` in call N is gone by call N+1, and
re-pasting a 25-line probe into every call burns turns and drifts between copies.
`bpy.app.driver_namespace` is a plain dict that lives on the Blender session, so it survives
between calls (it is Blender's driver-expression namespace; stashing helpers there is free).

GOTCHAS:
- **A scene reset drops the namespace.** `bpy.ops.wm.read_homefile(use_empty=True)` (or any
  full wipe/reload) clears it, and the next lookup raises `KeyError: 'measure_frame'` — the
  traceback points at the worker's dispatch line, NOT at your code, so it reads like a harness
  bug. It isn't. Re-register after every wipe, or look up defensively:
  `mf = bpy.app.driver_namespace.get('measure_frame') or _register()`.
- **Save and restore EVERY render setting the probe touches** — `resolution_percentage`,
  `taa_render_samples`, `filepath`, and `file_format`. A probe that leaves the scene at 25%
  silently poisons the next real render, and that is very hard to spot in a metric.
- **`file_format` is stills-only in 5.x** — `'FFMPEG'` is NOT in the enum (see `blender-5-api`).
  Probes write `'PNG'`; video muxing is the harness's job, not a gate's.
- Load the probe image with `check_existing=False` and `bpy.data.images.remove(img)` when done,
  or repeated probes accumulate datablocks named `_m.png.001`, `.002`, … and you start
  measuring a stale one.
- `list(img.pixels)` is the slow part. Keep the probe at ~25% resolution and low samples —
  band statistics are stable there even when the full-res look is not.

```python
import os

def _register():
    sc, cwd = bpy.context.scene, os.getcwd()

    def measure_frame(fr):
        tmp = os.path.join(cwd, 'renders', '_m')
        # save EVERY setting we are about to stomp
        op, osamp = sc.render.resolution_percentage, sc.eevee.taa_render_samples
        opath, ofmt = sc.render.filepath, sc.render.image_settings.file_format
        sc.render.resolution_percentage = 25
        sc.eevee.taa_render_samples = 16
        sc.render.filepath = tmp
        sc.render.image_settings.file_format = 'PNG'      # never 'FFMPEG' in 5.x
        sc.frame_set(fr)
        bpy.ops.render.render(write_still=True)
        # restore before doing anything else
        sc.render.resolution_percentage, sc.eevee.taa_render_samples = op, osamp
        sc.render.filepath, sc.render.image_settings.file_format = opath, ofmt

        img = bpy.data.images.load(tmp + '.png', check_existing=False)
        w, h = img.size
        px = list(img.pixels)
        try:
            return band_stats(px, w, h)       # your metric: mu/sigma per band, etc.
        finally:
            bpy.data.images.remove(img)       # or datablocks pile up across probes

    bpy.app.driver_namespace['measure_frame'] = measure_frame
    return measure_frame

_register()

# ...any LATER run_bpy call, no redefinition needed:
mf = bpy.app.driver_namespace.get('measure_frame') or _register()
RESULT = {'f48': mf(48)}
```

## Ablate before you tune

Before spending turns tuning a socket toward a metric, prove it drives that metric: set it to
a LITERAL EXTREME (zero / off), measure, and compare against the on value. If the numbers
barely move, the metric is being carried by something else and every turn spent tuning that
socket is wasted — or worse, you crush a value far from the reference chasing a number the
socket cannot reach.

Record confirmed no-ops in the script docstring and pin them off explicitly, so the next gate
does not "fix" them back and re-lose the same turns.

```python
SOCK = glare.inputs[7]                 # the thing you suspect drives the metric
_was = SOCK.default_value
SOCK.default_value = 0.0
off = mf(48)
SOCK.default_value = _was
on = mf(48)
# off ~= on  ->  this socket does NOT drive the metric at this frame. Stop tuning it.
```

Real results from this pattern, each of which would otherwise have cost tuning turns:
- Compositor Glare strength keyed to literal zero moved the halation readout 32.0 → 31.0.
  The metric was tracking the city's crisp bright dots, not the compositor. Brightness had to
  come from SMOOTH ground haze instead.
- Depth of field was byte-identical at f/2.8, f/0.9, f/0.45 and f/0.22 with a focus object —
  a silent no-op in this EEVEE build. Pinned off (`cam.dof.use_dof = False`) rather than tuned.
- A far city tier measured identically at emission 3.6 / 6.0 / 9.0 — fully occluded by nearer
  geometry at the final camera height, so it was set for palette consistency only.
- A world background fully enclosed by a sky dome changed nothing when muted and zeroed;
  neutralised anyway so it cannot reintroduce a colour bias if the dome is ever opened up.
