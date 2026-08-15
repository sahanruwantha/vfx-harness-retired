"""SPIKE (#38): does the hero mesh's existing faceting read once something LIGHTS it?

Five attempts and $78.76 went into layer 2 adding piers, banding, setbacks and a podium
to the hero tower. Hiding those added objects showed the mesh ALREADY HAS all of them.
The render shows a plain box anyway. This asks why, before we build two new stages on
the assumption that the answer is "nothing keys it".

Reading layer 1 first sharpened the question. There is no light object in the scene at
all — only emissive materials and a volumetric world at strength 0.6. And the hero's
material is a pure EMISSION shader, which cannot receive light at any intensity. So
"nothing keys it" is not quite right; the stronger statement is that the surface is
physically incapable of being keyed.

That makes for a two-part test, because the parts can fail independently:

  A  lights only, material untouched   -> should change NOTHING. Confirms the emission
                                          shader is the blocker, not the light rig.
  B  diffuse body + the same lights    -> the real question. Does the geometry that is
                                          already there start to read?

If B works, layer 2's five attempts were solving a lighting problem with geometry, and
#39/#40 are the right fix. If B does not, the detail is too fine at this camera distance
and the honest conclusion is the opposite one — model it, or amend the axis.

    .venv/bin/python docs/probes/spike_lighting.py
"""
import sys; sys.path.insert(0, ".")
import json
from pathlib import Path
from pipeline.blender.session import BlenderSession
from pipeline.brief import load_shot
from pipeline.build_agent import _RESET, _preamble

shot = load_shot("shots/barrel_roll")
OUT = Path("/tmp/claude-1000/-home-sahan-Desktop-bambi-vfx/"
           "9428c845-d22d-4014-93d3-84155fc1c6eb/scratchpad/light")
OUT.mkdir(parents=True, exist_ok=True)

# f45 is where layer 2 was judged hardest and where the "flat box" reads worst.
# f100 is the wider look; included so a win at f45 is not a one-frame accident.
FRAMES = [45, 100]

s = BlenderSession(blender="blender", blend_file=None,
                   assets_dir=shot.folder / "assets", cwd=shot.folder).start()


def run(code):
    """The worker captures stdout rather than letting it through, so a spike that only
    calls s.run() prints nothing and looks like it did nothing. Echo it."""
    r = s.run(code)
    out = (r.get("stdout") or "").strip()
    if out:
        print(out)
    return r


def render(tag):
    """Render every frame under the current scene state and report the hero's stats."""
    for f in FRAMES:
        p = OUT / f"{tag}_f{f}.png"
        run(f'''
import bpy
sc = bpy.context.scene
sc.frame_set({f})
bpy.context.view_layer.update()
sc.render.filepath = r"{p}"
sc.render.image_settings.file_format = 'PNG'
sc.render.resolution_percentage = 50
bpy.ops.render.render(write_still=True)
print("RESULT wrote {p.name}")
''')
    return tag


try:
    s.run(_RESET); s.run(_preamble(shot))
    run((shot.folder / "build/01_layout.py").read_text(encoding="utf-8"))
    print("RESULT layer-1 scene built")

    # ---- what is actually in the scene, stated rather than assumed -------------------
    run('''
import bpy
lights = [o.name for o in bpy.data.objects if o.type == 'LIGHT']
hero = bpy.data.objects['hero_tower']
mat = hero.data.materials[0]
kinds = sorted({n.type for n in mat.node_tree.nodes})
surf = [l.from_node.type for l in mat.node_tree.links
        if l.to_node.type == 'OUTPUT_MATERIAL' and l.to_socket.name == 'Surface']
print("RESULT light objects in scene:", lights or "NONE")
print("RESULT hero surface shader:", surf)
print("RESULT hero verts:", len(hero.data.vertices), "polys:", len(hero.data.polygons))
''')

    # ---- 0. baseline ----------------------------------------------------------------
    render("A0_baseline")

    # ---- A. lights only, material untouched -----------------------------------------
    # Raking key from camera-left and slightly above, so piers and setbacks cast across
    # the facade rather than washing it flat. Fill is dim and cool to keep the shadow
    # side from going to pure black.
    run('''
import bpy, math
sc = bpy.context.scene

for name in ('spike_key', 'spike_fill'):
    old = bpy.data.objects.get(name)
    if old:
        bpy.data.objects.remove(old, do_unlink=True)

kd = bpy.data.lights.new('spike_key_data', type='SUN')
kd.energy = 3.0
kd.color = (1.0, 0.92, 0.80)
kd.angle = math.radians(3.0)          # tight-ish -> crisp pier shadows
key = bpy.data.objects.new('spike_key', kd)
sc.collection.objects.link(key)
# Rake ACROSS the facade: low elevation, swung well off the camera axis.
key.rotation_euler = (math.radians(62.0), 0.0, math.radians(35.0))

fd = bpy.data.lights.new('spike_fill_data', type='SUN')
fd.energy = 0.45
fd.color = (0.55, 0.70, 0.95)
fill = bpy.data.objects.new('spike_fill', fd)
sc.collection.objects.link(fill)
fill.rotation_euler = (math.radians(70.0), 0.0, math.radians(-120.0))
print("RESULT key + fill added")
''')
    render("A1_lights_only")

    # ---- B. a body that can receive light, plus the same lights ---------------------
    # Keep the window emission exactly as layer 1 built it; add a diffuse body underneath
    # it. Implemented by mixing the existing Emission into a Principled BSDF via the same
    # mask that already separates window from body, so the windows stay lit and only the
    # body between them becomes a surface that can catch light.
    run('''
import bpy
hero = bpy.data.objects['hero_tower']
nt = hero.data.materials[0].node_tree
out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL')
emi = next(n for n in nt.nodes if n.type == 'EMISSION')

# The strength input is driven by a Math node whose two operands are (window, body)
# emission levels -- layer 1 sets 3.0 and 0.008 on it. That same node is the window mask.
strn = next(l.from_node for l in nt.links
            if l.to_node == emi and l.to_socket.name == 'Strength')

bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
bsdf.inputs['Base Color'].default_value = (0.055, 0.058, 0.067, 1.0)
bsdf.inputs['Roughness'].default_value = 0.42
if 'Metallic' in bsdf.inputs:
    bsdf.inputs['Metallic'].default_value = 0.15

mix = nt.nodes.new('ShaderNodeMixShader')
# Normalise the strength signal to 0..1 so it can drive the mix: window -> emission,
# body -> BSDF. Divide by the window level (3.0) and clamp.
norm = nt.nodes.new('ShaderNodeMath')
norm.operation = 'DIVIDE'
norm.use_clamp = True
nt.links.new(strn.outputs[0], norm.inputs[0])
norm.inputs[1].default_value = 3.0

nt.links.new(norm.outputs[0], mix.inputs['Fac'])
nt.links.new(bsdf.outputs['BSDF'], mix.inputs[1])
nt.links.new(emi.outputs['Emission'], mix.inputs[2])
nt.links.new(mix.outputs['Shader'], out.inputs['Surface'])
print("RESULT hero body is now a Principled BSDF; windows still emissive")
''')
    render("B_lit_body")

finally:
    s.close()

# ---- measure, do not eyeball -------------------------------------------------------
# The claim under test is "the faceting reads", which means detail CONTRAST across the
# tower's own pixels. sigma over the hero's subject region is the statistic for that --
# the frame band is mostly city and would drown it (the #37 lesson).
from PIL import Image                          # noqa: E402

# Hero occupies roughly the centre column. Deliberately generous vertically so setbacks
# and podium are inside the box, and narrow horizontally so city does not leak in.
BOX = (0.40, 0.18, 0.60, 0.92)     # x0, y0, x1, y1 normalised

print("\n=== hero subject region: does detail contrast appear? ===")
print(f"{'variant':<20} {'frame':>6} {'mean':>8} {'sigma':>8} {'max':>7}")
rows = {}
for tag in ("A0_baseline", "A1_lights_only", "B_lit_body"):
    for f in FRAMES:
        p = OUT / f"{tag}_f{f}.png"
        if not p.exists():
            print(f"{tag:<20} {f:>6}   MISSING")
            continue
        im = Image.open(p).convert("L")
        w, h = im.size
        crop = im.crop((int(BOX[0] * w), int(BOX[1] * h),
                        int(BOX[2] * w), int(BOX[3] * h)))
        px = list(crop.getdata())
        n = len(px)
        mean = sum(px) / n
        sd = (sum((v - mean) ** 2 for v in px) / n) ** 0.5
        rows[(tag, f)] = (mean, sd, max(px))
        print(f"{tag:<20} {f:>6} {mean:>8.1f} {sd:>8.1f} {max(px):>7}")

print("\n=== the two questions ===")
for f in FRAMES:
    b = rows.get(("A0_baseline", f))
    a = rows.get(("A1_lights_only", f))
    c = rows.get(("B_lit_body", f))
    if not (b and a and c):
        continue
    print(f"\nf{f}:")
    print(f"  A  lights alone changed sigma {b[1]:.1f} -> {a[1]:.1f} "
          f"({a[1] - b[1]:+.1f})   [expected ~0: emission ignores light]")
    print(f"  B  lit body     changed sigma {b[1]:.1f} -> {c[1]:.1f} "
          f"({c[1] - b[1]:+.1f})   [the actual test]")

(OUT / "rows.json").write_text(json.dumps({f"{k[0]}_f{k[1]}": v for k, v in rows.items()},
                                          indent=2))
print(f"\nimages + rows.json in {OUT}")
