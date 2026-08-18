"""SPIKE: where does a key light have to BE to light a 100-unit tower's shaft?

Layer 5 has now failed three times — 2.0 at every frame of every round, $14.85 — and
every judge said the same thing:

    "key only reaches podium height; above the setback the hero shaft is flat near-black"
    "exposure ramp is INVERTED: the podia are the brightest built pixels while the
     shaft is black"

Three build attempts could not fix it, so this is no longer a specification problem to
solve by rewriting the ticket again. It is a question about the scene, and the cheapest
way to answer a question about the scene is to measure it — no critic, no build agent,
the way #38 answered "does the mesh have detail" for nothing.

THE QUESTION: what key HEIGHT and ENERGY puts light on the SHAFT rather than the podium?

The last ticket specified the key's angle off-axis (35-45°) and its elevation (25-35°)
but never its height relative to the subject, and on a 100-unit tower a correctly-angled
light placed low still only reaches the base. That is the hypothesis under test.

MEASURED PER CELL, over three bands of the hero column at f45:
  upper shaft  — the region every critique says is black
  mid shaft
  podium       — the region every critique says is blown
Reported as podium/shaft RATIO (>1 means the inversion the critic keeps naming) and as
upper-shaft sigma (does the facade modulate at all, i.e. do piers and setbacks separate).

Targets, from the plate: facade body mean ~37, shaft sigma 30-60, podium NOT brighter
than the shaft.

    .venv/bin/python docs/probes/spike_lighting4.py
"""
import sys; sys.path.insert(0, "src")
import json
from pathlib import Path

from PIL import Image

from bambi_vfx.blender.session import BlenderSession
from bambi_vfx.brief import load_shot
from bambi_vfx.agents.builder import _RESET, _preamble

shot = load_shot("shots/barrel_roll")
OUT = Path("/tmp/claude-1000/-home-sahan-Desktop-bambi-vfx/"
           "9428c845-d22d-4014-93d3-84155fc1c6eb/scratchpad/light9")
OUT.mkdir(parents=True, exist_ok=True)
FRAME = 45

# Hero column at f45, split into the three bands the critique keeps distinguishing.
# x from the measured bbox (0.388..0.612); y split so podium is the lower flare.
BANDS = {"upper": (0.44, 0.20, 0.56, 0.42),
         "mid":   (0.44, 0.42, 0.56, 0.62),
         "podium": (0.42, 0.62, 0.58, 0.80)}

# (key height as a fraction of tower height, energy). The tower is 100u tall with its
# base at z=0. Attempt 3 placed its key around podium height; sweep well past that.
GRID = [(0.65, 3e4), (0.65, 6e4), (0.65, 1.2e5), (0.65, 2e5),
        (1.00, 3e4), (1.00, 6e4), (1.00, 1.2e5), (1.00, 2e5)]

s = BlenderSession(blender="blender", blend_file=None,
                   assets_dir=shot.folder / "assets", cwd=shot.folder).start()


def run(code):
    r = s.run(code)
    out = (r.get("stdout") or "").strip()
    if out:
        print(out)
    return r


def bands(path):
    im = Image.open(path).convert("L")
    w, h = im.size
    px = im.load()
    out = {}
    for name, (x0, y0, x1, y1) in BANDS.items():
        vals = [px[x, y]
                for y in range(int(y0 * h), int(y1 * h), 2)
                for x in range(int(x0 * w), int(x1 * w), 2)]
        m = sum(vals) / len(vals)
        sd = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
        out[name] = (round(m, 1), round(sd, 1), max(vals))
    return out


try:
    s.run(_RESET); s.run(_preamble(shot))
    # The assembled scene as it actually stands: layers 1-4 all passed and are chained.
    for script in ("01_layout.py", "02_towers.py", "03_city.py", "04_sky.py"):
        run((shot.folder / "build" / script).read_text(encoding="utf-8"))
    print("RESULT layers 1-4 built")

    run('''
import bpy
sc = bpy.context.scene
# Layer 1's placeholder sun is scaffolding; the lighting stage owns the key.
k = bpy.data.objects.get('layout_key')
if k:
    k.data.energy = 0.0
    print('RESULT layout_key dimmed to 0')
hero = bpy.data.objects['hero_tower']
print('RESULT hero dims:', [round(v,1) for v in hero.dimensions])
''')

    for frac, energy in GRID:
        p = OUT / f"h{frac}_e{energy:.0e}.png"
        run(f'''
import bpy, math
sc = bpy.context.scene
for o in [o for o in bpy.data.objects if o.name.startswith('spk_')]:
    bpy.data.objects.remove(o, do_unlink=True)
H = 100.0
ld = bpy.data.lights.new('spk_key_d', type='AREA')
ld.energy = {energy}; ld.size = 60.0
ld.color = (0.80, 0.90, 1.0)          # cool/teal, per the canopy spill note
ld.use_shadow = True
key = bpy.data.objects.new('spk_key', ld); sc.collection.objects.link(key)
# 40 deg off the camera axis, camera-left, at the swept HEIGHT up the tower.
key.location = (H*0.85, -H*0.85, H*{frac})
key.rotation_euler = (math.radians(72.0), 0.0, math.radians(45.0))
sc.frame_set({FRAME}); bpy.context.view_layer.update()
sc.render.filepath = r"{p}"
sc.render.image_settings.file_format = 'PNG'
sc.render.resolution_percentage = 50
bpy.ops.render.render(write_still=True)
print("RESULT wrote {p.name}")
''')
finally:
    s.close()

base = None
rows = {}
print(f"\n{'key h':>6} {'energy':>8} | {'upper μ/σ':>12} {'mid μ/σ':>12} "
      f"{'podium μ':>9} {'podium/shaft':>13}")
for frac, energy in GRID:
    p = OUT / f"h{frac}_e{energy:.0e}.png"
    if not p.exists():
        print(f"{frac:>6} {energy:>8.0e}   MISSING")
        continue
    b = bands(p)
    up, mid, pod = b["upper"], b["mid"], b["podium"]
    shaft = (up[0] + mid[0]) / 2
    ratio = pod[0] / max(shaft, 0.1)
    rows[f"h{frac}_e{energy:.0e}"] = b
    flag = ""
    if ratio > 1.15:
        flag = "  <- INVERTED (podium brighter)"
    elif 30 <= shaft <= 45 and up[1] >= 25:
        flag = "  <- in band, shaft modulating"
    print(f"{frac:>6} {energy:>8.0e} | {up[0]:>5}/{up[1]:<6} {mid[0]:>5}/{mid[1]:<6} "
          f"{pod[0]:>9} {ratio:>13.2f}{flag}")

(OUT / "rows.json").write_text(json.dumps(rows, indent=2))
print(f"\ntargets: shaft mean 30-45, upper sigma >=25 (piers/setbacks separating), "
      f"podium/shaft <= 1.0\nimages in {OUT}")
