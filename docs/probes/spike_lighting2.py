"""SPIKE #38, part 2: sweep ALBEDO x KEY ENERGY.

Part 1 answered its first question cleanly and botched its second.

Confirmed: layer 1 contains no light object at all, and the hero's material is a pure
EMISSION shader. Adding a key and fill changed the hero's pixels by +0.0 sigma -- exactly
zero, as an emission shader must. The surface is physically incapable of being keyed.

Botched: variant B swapped in a Principled BSDF but kept base colour 0.055 -- the
near-black albedo that is itself the other half of the complaint. A sun on a 0.055 surface
returns almost nothing, so the test asked "can we light a black body" rather than "does
this mesh's faceting read when lit". The body pixels moved -0.00 sigma, and I nearly wrote
that up as "lighting does not help".

So: sweep. Albedo across the range from the current near-black to a normal building
value, key energy across a plausible range, and measure sigma over the BODY pixels only
-- the whole-tower sigma is dominated by bright-window-vs-dark-body contrast (~48) and
would hide any amount of facade modulation underneath it.

The mesh carries 260,332 polygons. The question is whether any of that survives to screen.

    .venv/bin/python docs/probes/spike_lighting2.py
"""
import sys; sys.path.insert(0, "src")
import json
from pathlib import Path

from bambi_vfx.blender.session import BlenderSession
from bambi_vfx.brief import load_shot
from bambi_vfx.agents.builder import _RESET, _preamble

shot = load_shot("shots/barrel_roll")
OUT = Path("/tmp/claude-1000/-home-sahan-Desktop-bambi-vfx/"
           "9428c845-d22d-4014-93d3-84155fc1c6eb/scratchpad/light2")
OUT.mkdir(parents=True, exist_ok=True)

FRAME = 45
# (albedo, key energy). 0.055 is what the shot ships today; 0.18 is mid-grey; 0.35 is a
# pale facade. Energies bracket "dim night key" to "strong moon/practical bounce".
GRID = [(0.055, 3.0), (0.055, 12.0),
        (0.12, 5.0), (0.12, 12.0),
        (0.18, 3.0), (0.18, 8.0), (0.18, 20.0),
        (0.35, 8.0)]

s = BlenderSession(blender="blender", blend_file=None,
                   assets_dir=shot.folder / "assets", cwd=shot.folder).start()


def run(code):
    r = s.run(code)
    out = (r.get("stdout") or "").strip()
    if out:
        print(out)
    return r


try:
    s.run(_RESET); s.run(_preamble(shot))
    run((shot.folder / "build/01_layout.py").read_text(encoding="utf-8"))

    # Convert the hero to a lit body once; the sweep then just retunes values.
    run('''
import bpy, math
sc = bpy.context.scene
hero = bpy.data.objects['hero_tower']
nt = hero.data.materials[0].node_tree
out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL')
emi = next(n for n in nt.nodes if n.type == 'EMISSION')
strn = next(l.from_node for l in nt.links
            if l.to_node == emi and l.to_socket.name == 'Strength')

bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
bsdf.name = 'spike_bsdf'
bsdf.inputs['Roughness'].default_value = 0.45
mix = nt.nodes.new('ShaderNodeMixShader')
norm = nt.nodes.new('ShaderNodeMath')
norm.operation = 'DIVIDE'; norm.use_clamp = True
nt.links.new(strn.outputs[0], norm.inputs[0]); norm.inputs[1].default_value = 3.0
nt.links.new(norm.outputs[0], mix.inputs['Fac'])
nt.links.new(bsdf.outputs['BSDF'], mix.inputs[1])
nt.links.new(emi.outputs['Emission'], mix.inputs[2])
nt.links.new(mix.outputs['Shader'], out.inputs['Surface'])

kd = bpy.data.lights.new('spike_key_data', type='SUN')
kd.color = (1.0, 0.92, 0.80); kd.angle = math.radians(2.0)
key = bpy.data.objects.new('spike_key', kd)
sc.collection.objects.link(key)
key.rotation_euler = (math.radians(62.0), 0.0, math.radians(35.0))

fd = bpy.data.lights.new('spike_fill_data', type='SUN')
fd.color = (0.55, 0.70, 0.95)
fill = bpy.data.objects.new('spike_fill', fd)
sc.collection.objects.link(fill)
fill.rotation_euler = (math.radians(70.0), 0.0, math.radians(-120.0))

# Shadows must actually be on, or "faceting" cannot appear however bright the key is.
# Stated explicitly rather than assumed -- an unlit-looking render with shadows disabled
# would look exactly like a mesh with no detail.
print("RESULT eevee shadows:", getattr(sc.eevee, 'use_shadows', 'n/a'))
try:
    sc.eevee.use_shadows = True
except Exception as e:
    print("RESULT could not force shadows:", e)
print("RESULT key.use_shadow:", kd.use_shadow if hasattr(kd, 'use_shadow') else 'n/a')
print("RESULT engine:", sc.render.engine)
''')

    for albedo, energy in GRID:
        p = OUT / f"a{albedo}_e{energy}_f{FRAME}.png"
        run(f'''
import bpy
sc = bpy.context.scene
nt = bpy.data.objects['hero_tower'].data.materials[0].node_tree
b = nt.nodes['spike_bsdf']
b.inputs['Base Color'].default_value = ({albedo}, {albedo * 1.03}, {albedo * 1.18}, 1.0)
bpy.data.lights['spike_key_data'].energy = {energy}
bpy.data.lights['spike_fill_data'].energy = {energy * 0.15}
sc.frame_set({FRAME})
bpy.context.view_layer.update()
sc.render.filepath = r"{p}"
sc.render.image_settings.file_format = 'PNG'
sc.render.resolution_percentage = 50
bpy.ops.render.render(write_still=True)
print("RESULT wrote {p.name}")
''')
finally:
    s.close()

# ---- measure the BODY, not the whole tower -----------------------------------------
from PIL import Image

BOX = (0.40, 0.20, 0.60, 0.90)
BASE = Path("/tmp/claude-1000/-home-sahan-Desktop-bambi-vfx/"
            "9428c845-d22d-4014-93d3-84155fc1c6eb/scratchpad/light/A0_baseline_f45.png")


def body(path):
    """Pixels of the hero box, plus the subset that is facade rather than window.

    Window pixels are identified from the SHIPPING render and reused as a fixed mask, so
    every variant is measured over the same population. Re-deriving the mask per variant
    would let a brighter facade reclassify itself as window and flatter the result.
    """
    im = Image.open(path).convert("L")
    w, h = im.size
    return list(im.crop((int(BOX[0] * w), int(BOX[1] * h),
                         int(BOX[2] * w), int(BOX[3] * h))).get_flattened_data())


base = body(BASE)
mask = [i for i, v in enumerate(base) if v < 80]      # facade, not window


def stats(vals):
    n = len(vals)
    m = sum(vals) / n
    return m, (sum((x - m) ** 2 for x in vals) / n) ** 0.5


bm, bs = stats([base[i] for i in mask])
print(f"\nfacade mask: {len(mask)} px ({len(mask)/len(base):.0%} of the hero box)")
print(f"SHIPPING (emission, no light): mean {bm:.1f}  sigma {bs:.2f}\n")
print(f"{'albedo':>7} {'key':>6} {'mean':>8} {'sigma':>8} {'vs ship':>9}")
rows = {}
for albedo, energy in GRID:
    p = OUT / f"a{albedo}_e{energy}_f{FRAME}.png"
    if not p.exists():
        print(f"{albedo:>7} {energy:>6}   MISSING")
        continue
    v = body(p)
    m, sd = stats([v[i] for i in mask])
    rows[f"a{albedo}_e{energy}"] = (m, sd)
    print(f"{albedo:>7} {energy:>6} {m:>8.1f} {sd:>8.2f} {sd - bs:>+9.2f}")

(OUT / "rows.json").write_text(json.dumps(rows, indent=2))
print(f"\nimages in {OUT}")
print("\nsigma over the facade mask IS the question: it is the contrast the 260k-poly "
      "mesh puts on screen. The shipping render sits at "
      f"{bs:.2f}.")
