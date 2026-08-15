"""SPIKE #38, part 3 — the actual question, now that the instrument works.

Parts 1 and 2 were mostly me debugging my own test. What they established:

1. Layer 1 contains NO light object, and the hero's material is a pure EMISSION shader.
   Adding lights changed the render by +0.0 sigma. Emission cannot be lit.
2. Swapping in a Principled BSDF and sweeping albedo x sun energy changed NOTHING --
   eight variants, bit-identical whole-image means. Not a finding; a broken instrument.
3. The cause: a SUN. Under the shot's world Volume Scatter, a white 0.8 body lit by a
   sun at energy 25 renders at mean 6.74/255. Effectively black. A sun is infinitely
   distant, so its shadow ray through an unbounded homogeneous volume accumulates
   unbounded optical depth. No volumetric knob changes it (shadows off, custom end,
   256 samples -- all bit-identical).
4. A LOCAL light has a finite path and works fine at the same atmosphere: the same white
   body under an area light reads mean 154 / sigma 60.

So the shot has an atmosphere that silently deletes any sun. Every builder that reached
for conventional key lighting would have seen a black tower and concluded that lighting
does not work here -- which is exactly the reasoning that produced five attempts of
emissive geometry.

THIS script asks the question #38 was actually opened for, with the real material:

    does the hero's existing 260,332-polygon facade MODULATE under a local key,
    or is the detail too fine to survive to screen at this camera distance?

Measured over the FACADE MASK -- the tower's non-window pixels, fixed from the shipping
render so a brighter facade cannot reclassify itself into the window population. Whole-
tower sigma is useless here: it is dominated by bright-window-vs-dark-body contrast
(~48) and would hide any amount of facade detail underneath it.

Baseline to beat: the shipping render's facade sigma of 18.37.

    .venv/bin/python docs/probes/spike_lighting3.py
"""
import sys; sys.path.insert(0, ".")
import json
from pathlib import Path
from pipeline.blender.session import BlenderSession
from pipeline.brief import load_shot
from pipeline.build_agent import _RESET, _preamble

shot = load_shot("shots/barrel_roll")
OUT = Path("/tmp/claude-1000/-home-sahan-Desktop-bambi-vfx/"
           "9428c845-d22d-4014-93d3-84155fc1c6eb/scratchpad/light8")
OUT.mkdir(parents=True, exist_ok=True)
SHIP = Path("/tmp/claude-1000/-home-sahan-Desktop-bambi-vfx/"
            "9428c845-d22d-4014-93d3-84155fc1c6eb/scratchpad/light/A0_baseline_f45.png")
FRAME = 45

# (albedo, key energy). Local AREA light. 1e6 was the readable exposure in part 2;
# bracket it, and vary albedo from the shipping near-black to a normal facade value.
GRID = [(0.055, 6e5), (0.055, 2e6),
        (0.12, 6e5), (0.12, 2e6),
        (0.18, 3e5), (0.18, 6e5), (0.18, 2e6),
        (0.30, 6e5)]

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

    # Real material preserved: layer 1's emissive windows stay exactly as built, and a
    # Principled body is mixed in underneath using the SAME mask that already separates
    # window from facade. Only the facade becomes a surface that can catch light.
    run('''
import bpy, math
sc = bpy.context.scene
hero = bpy.data.objects['hero_tower']
nt = hero.data.materials[0].node_tree
out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL')
emi = next(n for n in nt.nodes if n.type == 'EMISSION')
strn = next(l.from_node for l in nt.links
            if l.to_node == emi and l.to_socket.name == 'Strength')

bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled'); bsdf.name = 'spike_bsdf'
bsdf.inputs['Roughness'].default_value = 0.45
mix = nt.nodes.new('ShaderNodeMixShader')
norm = nt.nodes.new('ShaderNodeMath'); norm.operation = 'DIVIDE'; norm.use_clamp = True
nt.links.new(strn.outputs[0], norm.inputs[0]); norm.inputs[1].default_value = 3.0
nt.links.new(norm.outputs[0], mix.inputs['Fac'])
nt.links.new(bsdf.outputs['BSDF'], mix.inputs[1])
nt.links.new(emi.outputs['Emission'], mix.inputs[2])
nt.links.new(mix.outputs['Shader'], out.inputs['Surface'])

# LOCAL area key, raking across the facade from camera-left and slightly above the
# tower's mid-height. Tower is 40x43x100 with origin at z=50.
ld = bpy.data.lights.new('spike_key_d', type='AREA')
ld.size = 90.0
ld.color = (1.0, 0.93, 0.82)
key = bpy.data.objects.new('spike_key', ld)
sc.collection.objects.link(key)
key.location = (120.0, -90.0, 160.0)
key.rotation_euler = (math.radians(75.0), 0.0, math.radians(50.0))
print('RESULT lit body + local area key installed')
''')

    for albedo, energy in GRID:
        p = OUT / f"a{albedo}_e{energy:.0e}.png"
        run(f'''
import bpy
sc = bpy.context.scene
nt = bpy.data.objects['hero_tower'].data.materials[0].node_tree
nt.nodes['spike_bsdf'].inputs['Base Color'].default_value = (
    {albedo}, {albedo * 1.03}, {albedo * 1.18}, 1.0)
bpy.data.lights['spike_key_d'].energy = {energy}
sc.frame_set({FRAME}); bpy.context.view_layer.update()
sc.render.filepath = r"{p}"
sc.render.image_settings.file_format = 'PNG'
sc.render.resolution_percentage = 50
bpy.ops.render.render(write_still=True)
print("RESULT wrote {p.name}")
''')
finally:
    s.close()

# ---- measure the FACADE, with a mask fixed from the shipping render ------------------
from PIL import Image   # noqa: E402

BOX = (0.40, 0.20, 0.60, 0.90)


def box(path):
    im = Image.open(path).convert("L")
    w, h = im.size
    return list(im.crop((int(BOX[0] * w), int(BOX[1] * h),
                         int(BOX[2] * w), int(BOX[3] * h))).get_flattened_data())


def stats(v):
    n = len(v)
    m = sum(v) / n
    return m, (sum((x - m) ** 2 for x in v) / n) ** 0.5


ship = box(SHIP)
mask = [i for i, v in enumerate(ship) if v < 80]
sm, ss = stats([ship[i] for i in mask])

print(f"\nfacade mask: {len(mask)} px ({len(mask)/len(ship):.0%} of the hero box)")
print(f"SHIPPING (emission only, no light):  mean {sm:6.2f}   sigma {ss:6.2f}\n")
print(f"{'albedo':>7} {'key':>9} {'mean':>8} {'sigma':>8} {'vs ship':>9}")
rows = {}
best = None
for albedo, energy in GRID:
    p = OUT / f"a{albedo}_e{energy:.0e}.png"
    if not p.exists():
        print(f"{albedo:>7} {energy:>9.0e}   MISSING")
        continue
    v = box(p)
    m, sd = stats([v[i] for i in mask])
    rows[f"a{albedo}_e{energy:.0e}"] = (m, sd)
    print(f"{albedo:>7} {energy:>9.0e} {m:>8.2f} {sd:>8.2f} {sd - ss:>+9.2f}")
    if best is None or sd > best[1]:
        best = (f"albedo {albedo}, key {energy:.0e}", sd)

(OUT / "rows.json").write_text(json.dumps(rows, indent=2))
if best:
    print(f"\nbest facade contrast: {best[0]} at sigma {best[1]:.2f} "
          f"({best[1] / ss:.2f}x the shipping render's {ss:.2f})")
print(f"\nimages in {OUT}")
