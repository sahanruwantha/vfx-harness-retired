"""SPIKE #39: a lookdev turntable for sr2_tower, judged against its own isolation plate.

The plate `assets/sr2_tower/isolated/view_0.png` is the artifact nobody has used. It shows
the tower large, isolated and evenly lit on white: near-black body, two bright OUTER
window strips, two inner columns flanking a DARK RECESSED CORE, ribbed piers, a deep
stepped podium, and a dark sign panel with GLOWING letters. Every property layer 2 failed
on, in the form an asset department would want it.

This renders the asset the same way -- front-on, filling frame, neutral light, white
backdrop, no sky, no city, no grade -- so the two are directly comparable. Matching the
ANGLE matters: a front-on plate and a corner-on shot frame are different geometry, and
comparing their facade profiles is meaningless. That is the whole point of a turntable.

MEASURED TARGETS, taken from the plate rather than invented:

    backdrop  254        body mean  36.8        window mean  246.6      win/body  6.69

The body target is the one that matters. "Near-black body carrying bright windows" has
been prose for five attempts; it is 36.8.

Uses LOCAL lights only -- a sun renders black under a world volume (#44). This rig has no
volume, but the treatment approved here has to survive the shot's atmosphere.

    .venv/bin/python docs/probes/spike_lookdev.py
"""
import sys; sys.path.insert(0, "src")
import json
from pathlib import Path

from PIL import Image

from bambi_vfx.blender.session import BlenderSession
from bambi_vfx.brief import load_shot
from bambi_vfx.agents.builder import _RESET, _preamble
from bambi_vfx.facade import compare_profiles, facade_profile, render_profile

shot = load_shot("shots/barrel_roll")
OUT = Path("/tmp/claude-1000/-home-sahan-Desktop-bambi-vfx/"
           "9428c845-d22d-4014-93d3-84155fc1c6eb/scratchpad/lookdev")
OUT.mkdir(parents=True, exist_ok=True)
PLATE = "shots/barrel_roll/assets/sr2_tower/isolated/view_0.png"

TARGET_BODY = 36.8
ANGLES = [0, 35, 90, 180]
# Key energies to search for the one that puts the body at the plate's value.
KEYS = [2e5, 6e5, 1.5e6, 4e6]

s = BlenderSession(blender="blender", blend_file=None,
                   assets_dir=shot.folder / "assets", cwd=shot.folder).start()


def run(code):
    r = s.run(code)
    out = (r.get("stdout") or "").strip()
    if out:
        print(out)
    return r


def body_stats(path):
    """Body and window levels over the shaft, using the plate's own thresholds so the
    populations are defined identically on both sides."""
    im = Image.open(path).convert("L")
    w, h = im.size
    px = im.load()
    # shaft band of the SILHOUETTE (backdrop is bright, asset is not)
    ys = [y for y in range(0, h, 2) if any(px[x, y] < 200 for x in range(0, w, 3))]
    if not ys:
        return None
    y0, y1 = min(ys), max(ys)
    H = y1 - y0
    v = [px[x, y]
         for y in range(int(y0 + 0.16 * H), int(y0 + 0.70 * H), 2)
         for x in range(0, w, 2) if px[x, y] < 200]
    if not v:
        return None
    body = [x for x in v if x < 90]
    win = [x for x in Image.open(path).convert("L").get_flattened_data() if x > 150]
    return {
        "body": round(sum(body) / len(body), 1) if body else None,
        "n_body": len(body),
        "win": round(sum(win) / len(win), 1) if win else None,
    }


try:
    s.run(_RESET)
    s.run(_preamble(shot))

    run('''
import bpy, math
from mathutils import Vector

sc = bpy.context.scene
sc.render.engine = 'BLENDER_EEVEE'
sc.render.resolution_x, sc.render.resolution_y = 720, 1080

names = bvfx_import_asset('sr2_tower')
hero = bpy.data.objects[names[0]]
hero.name = 'lookdev_asset'
hero.rotation_euler = (0.0, 0.0, 0.0)
bpy.context.view_layer.update()

# FRAME FROM THE ACTUAL WORLD BBOX. The first version of this spike set location to
# (0,0,0) and aimed at z = height/2, assuming the asset's base sat at z=0. It does not --
# the import leaves the centre near the origin -- so the camera framed the top third and
# every measurement below was of empty backdrop.
bb = [hero.matrix_world @ Vector(c) for c in hero.bound_box]
zmin, zmax = min(v.z for v in bb), max(v.z for v in bb)
xmin, xmax = min(v.x for v in bb), max(v.x for v in bb)
cz = 0.5 * (zmin + zmax)
H = zmax - zmin
print('RESULT asset world bbox z', round(zmin,2), round(zmax,2), ' height', round(H,2))

# Same emissive-window treatment layer 1 applies, so lookdev approves what the shot uses.
bvfx_emissive_windows(hero, window_color=(0.95, 0.96, 0.98), strength=6.0,
                      aspect=0.55, mortar=0.35, density=8)

# Mix a Principled body under the window emission using the mask that already separates
# them, so the facade becomes a surface that can catch light (see #38).
nt = hero.data.materials[0].node_tree
out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL')
emi = next(n for n in nt.nodes if n.type == 'EMISSION')
strn = next(l.from_node for l in nt.links
            if l.to_node == emi and l.to_socket.name == 'Strength')
strn.inputs[1].default_value = 3.0
strn.inputs[2].default_value = 0.0
bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled'); bsdf.name = 'ld_bsdf'
bsdf.inputs['Base Color'].default_value = (0.16, 0.165, 0.185, 1.0)
bsdf.inputs['Roughness'].default_value = 0.45
mix = nt.nodes.new('ShaderNodeMixShader')
norm = nt.nodes.new('ShaderNodeMath'); norm.operation='DIVIDE'; norm.use_clamp=True
nt.links.new(strn.outputs[0], norm.inputs[0]); norm.inputs[1].default_value = 3.0
nt.links.new(norm.outputs[0], mix.inputs['Fac'])
nt.links.new(bsdf.outputs['BSDF'], mix.inputs[1])
nt.links.new(emi.outputs['Emission'], mix.inputs[2])
nt.links.new(mix.outputs['Shader'], out.inputs['Surface'])

# WHITE BACKDROP as a lit plane well behind the asset, not a bright world. A world
# background also lights the subject, which is how the first attempt ended up with a
# backdrop DARKER than the blown-out tower.
w = sc.world or bpy.data.worlds.new('W'); sc.world = w
w.use_nodes = True
wnt = w.node_tree; wnt.nodes.clear()
wo = wnt.nodes.new('ShaderNodeOutputWorld')
wbg = wnt.nodes.new('ShaderNodeBackground')
wbg.inputs['Color'].default_value = (0.05, 0.05, 0.06, 1.0)
wbg.inputs['Strength'].default_value = 0.15
wnt.links.new(wbg.outputs[0], wo.inputs['Surface'])

bpy.ops.mesh.primitive_plane_add(size=H * 6.0, location=(0, H * 2.2, cz))
bd = bpy.context.active_object; bd.name = 'ld_backdrop'
bd.rotation_euler = (math.radians(90), 0, 0)
bm = bpy.data.materials.new('ld_backdrop_mat'); bm.use_nodes = True
bnt = bm.node_tree; bnt.nodes.clear()
bo = bnt.nodes.new('ShaderNodeOutputMaterial')
be = bnt.nodes.new('ShaderNodeEmission')
be.inputs['Color'].default_value = (1, 1, 1, 1)
be.inputs['Strength'].default_value = 1.35     # -> ~254 after the view transform
bnt.links.new(be.outputs[0], bo.inputs['Surface'])
bd.data.materials.append(bm)

cd = bpy.data.cameras.new('ld_cam'); cd.lens = 85.0
cam = bpy.data.objects.new('ld_cam', cd); sc.collection.objects.link(cam)
sc.camera = cam
# Portrait sensor fit: vertical FOV governs. Solve distance for the height plus margin.
sensor = cd.sensor_width
fov_v = 2 * math.atan(sensor / (2 * cd.lens))
dist = (H * 1.12) / (2 * math.tan(fov_v / 2))
cam.location = (0.0, -dist, cz)
cam.rotation_euler = (math.radians(90.0), 0.0, 0.0)
print('RESULT camera dist', round(dist,1), ' aim z', round(cz,2))

def area(name, loc, rot, energy, size, col):
    ld = bpy.data.lights.new(name, type='AREA')
    ld.energy = energy; ld.size = size; ld.color = col
    ob = bpy.data.objects.new(name, ld); sc.collection.objects.link(ob)
    ob.location = loc; ob.rotation_euler = rot
    return ob

area('ld_key',  ( H*0.9, -H*0.9, cz + H*0.35), (math.radians(70), 0, math.radians(45)),
     1e6, H*0.7, (1.0, 0.96, 0.90))
area('ld_fill', (-H*1.0, -H*0.8, cz + H*0.05), (math.radians(80), 0, math.radians(-50)),
     2.5e5, H*0.8, (0.80, 0.86, 1.0))
print('RESULT rig built')
''')

    # ---- exposure search: find the key that puts the body at the plate's 36.8 ---------
    print(f"\nsearching key energy for body ~= {TARGET_BODY} (plate)")
    best = None
    for e in KEYS:
        p = OUT / f"tune_{e:.0e}.png"
        run(f'''
import bpy
sc = bpy.context.scene
bpy.data.lights['ld_key'].energy = {e}
bpy.data.lights['ld_fill'].energy = {e * 0.25}
bpy.context.view_layer.update()
sc.render.filepath = r"{p}"
sc.render.image_settings.file_format = 'PNG'
sc.render.resolution_percentage = 60
bpy.ops.render.render(write_still=True)
print("RESULT wrote {p.name}")
''')
        st = body_stats(p)
        if st and st["body"]:
            d = abs(st["body"] - TARGET_BODY)
            print(f"  key {e:>8.0e}   body {st['body']:>6}  win {st['win']:>6}   "
                  f"|delta| {d:.1f}")
            if best is None or d < best[1]:
                best = (e, d)
        else:
            print(f"  key {e:>8.0e}   no silhouette found")

    if best is None:
        raise SystemExit("exposure search found nothing to measure")
    print(f"\nchosen key {best[0]:.0e}")

    for a in ANGLES:
        p = OUT / f"view_{a:03d}.png"
        run(f'''
import bpy, math
sc = bpy.context.scene
bpy.data.lights['ld_key'].energy = {best[0]}
bpy.data.lights['ld_fill'].energy = {best[0] * 0.25}
bpy.data.objects['lookdev_asset'].rotation_euler = (0.0, 0.0, math.radians({a}))
bpy.context.view_layer.update()
sc.render.filepath = r"{p}"
sc.render.image_settings.file_format = 'PNG'
sc.render.resolution_percentage = 100
bpy.ops.render.render(write_still=True)
print("RESULT wrote {p.name}")
''')
finally:
    s.close()

# ---- score ---------------------------------------------------------------------------
ref = facade_profile(PLATE)
print(f"\n=== PLATE (target) ===  shaft {ref['shaft_px']}px  "
      f"outer/core {ref['outer_core_ratio']}  brightest@{ref['brightest_at']}")
print(render_profile(ref["profile"]))

results = {"plate": ref}
for a in ANGLES:
    p = OUT / f"view_{a:03d}.png"
    if not p.exists():
        print(f"\nview_{a:03d}: MISSING")
        continue
    r = facade_profile(p)
    st = body_stats(p)
    results[f"view_{a:03d}"] = {**r, "levels": st}
    tag = "  <- front-on, comparable to the plate" if a == 0 else "  (off-axis)"
    print(f"\n=== turntable {a} deg ==={tag}\nshaft {r['shaft_px']}px  "
          f"outer/core {r['outer_core_ratio']}  brightest@{r['brightest_at']}  "
          f"body {st['body'] if st else '?'}  win {st['win'] if st else '?'}")
    print(render_profile(r["profile"]))
    if a == 0:
        c = compare_profiles(r, ref)
        print(f"  vs plate: L1 {c['l1']}   outer/core {c['outer_core_ratio']}  "
              f"gap {c['ratio_gap']:+}")

(OUT / "profiles.json").write_text(json.dumps(results, indent=2))
print(f"\nimages + profiles.json in {OUT}")
