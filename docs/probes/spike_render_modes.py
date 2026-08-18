"""SPIKE: do the Phase 2 render modes isolate what their captions claim?

The lighting layer's axis says "score MODELLING BY LIGHT only — emissive sources must
not raise or lower this score". That asks a vision model to mentally subtract emission
from a beauty render. In Blender it is a render setting, so the question is whether the
setting works here, on this machine, on this Blender.

The scene is built to make the answer unambiguous: ONE lit diffuse body and ONE emissive
body, side by side, under one area key.

  beauty          both bodies visible
  diffuse_direct  the lit body only — the emitter must go DARK
  emit            the emitter only — the lit body must go DARK
  clay            both bodies same mid-grey; form only
  silhouette      both white on black
  crop + res_pct  a region at higher pixel density than the full frame

A mode that does not separate them is worse than no mode: it would ship a caption
promising an isolation it did not perform.

    .venv/bin/python docs/probes/spike_render_modes.py
"""
import sys

sys.path.insert(0, "src")

from pathlib import Path

from PIL import Image

from bambi_vfx.blender.session import BlenderSession

OUT = Path("renders/probe_render_modes")
OUT.mkdir(parents=True, exist_ok=True)

SCENE = '''
import bpy, math
from mathutils import Vector
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
_av = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}
sc.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _av else 'BLENDER_EEVEE'
sc.render.resolution_x, sc.render.resolution_y = 960, 480
sc.frame_start, sc.frame_end = 1, 2

cam_d = bpy.data.cameras.new("Cam")
cam = bpy.data.objects.new("Camera", cam_d)
sc.collection.objects.link(cam); sc.camera = cam
cam.location = (0.0, -9.0, 1.0)
cam.rotation_euler = (math.radians(90), 0, 0)

# LEFT: a diffuse body. It can only be seen if something LIGHTS it.
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(-2.2, 0.0, 1.0))
lit = bpy.context.active_object; lit.name = "LitBody"
m = bpy.data.materials.new("lit_mat"); m.use_nodes = True
nt = m.node_tree; nt.nodes.clear()
out = nt.nodes.new("ShaderNodeOutputMaterial")
bsdf = nt.nodes.new("ShaderNodeBsdfDiffuse")
bsdf.inputs["Color"].default_value = (0.8, 0.8, 0.8, 1)
nt.links.new(bsdf.outputs[0], out.inputs["Surface"])
lit.data.materials.append(m)

# RIGHT: an emitter. It is visible with NO light at all and cannot be lit.
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(2.2, 0.0, 1.0))
em_obj = bpy.context.active_object; em_obj.name = "Emitter"
me = bpy.data.materials.new("em_mat"); me.use_nodes = True
nt = me.node_tree; nt.nodes.clear()
out = nt.nodes.new("ShaderNodeOutputMaterial")
em = nt.nodes.new("ShaderNodeEmission")
em.inputs["Color"].default_value = (1.0, 0.6, 0.2, 1)
em.inputs["Strength"].default_value = 12.0
nt.links.new(em.outputs[0], out.inputs["Surface"])
em_obj.data.materials.append(me)

# TWO local area lights (not suns — see docs/LIGHTING_FINDING.md), one per side.
# Two is the minimum that can catch a light isolation that silently does nothing: with
# one light, "isolated" and "beauty" are the same image and the mode passes by accident.
kl = bpy.data.lights.new("Key", type="AREA"); kl.energy = 900.0; kl.size = 4.0
key = bpy.data.objects.new("Key", kl)
sc.collection.objects.link(key)
key.location = (-4.0, -5.0, 5.0)
key.rotation_euler = (math.radians(50), 0, math.radians(-35))

# A real fill: much weaker than the key. Equal-energy lights mirrored about the camera
# axis make a symmetric frame, and then "Key alone" and "Fill alone" measure the SAME
# brightness — the probe would pass a broken isolation and fail a working one.
fl = bpy.data.lights.new("Fill", type="AREA"); fl.energy = 150.0; fl.size = 4.0
fill = bpy.data.objects.new("Fill", fl)
sc.collection.objects.link(fill)
fill.location = (4.0, -5.0, 5.0)
fill.rotation_euler = (math.radians(50), 0, math.radians(35))
RESULT = "scene built"
'''

# The two halves of the frame, so "did the emitter go dark" is a number.
LEFT = (0.08, 0.25, 0.42, 0.85)     # the lit body
RIGHT = (0.58, 0.25, 0.92, 0.85)    # the emitter


def halves(path: str) -> tuple[float, float]:
    im = Image.open(path).convert("L")
    W, H = im.size
    out = []
    for (x0, y0, x1, y1) in (LEFT, RIGHT):
        box = (int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))
        px = list(im.crop(box).getdata())
        out.append(sum(px) / max(1, len(px)))
    return out[0], out[1]


def main() -> int:
    s = BlenderSession(blender="blender").start()
    bad = []
    try:
        s.run(SCENE)
        rows = []

        def shot(tag, **kw):
            r = s.render_full(frame=1, mode="eevee", scale=0.5, **kw)
            dest = OUT / f"{tag}.png"
            with Image.open(r["image_path"]) as im:
                im.save(dest)
                px = [im.width, im.height]
            lit, emit = halves(r["image_path"])
            rows.append((tag, lit, emit, r.get("caption", ""), dest,
                         r.get("resolution"), r.get("pixels", px)))
            return lit, emit

        b_lit, b_emit = shot("beauty")
        d_lit, d_emit = shot("diffuse_direct", **{"pass": "diffuse_direct"})
        e_lit, e_emit = shot("emit", **{"pass": "emit"})
        c_lit, c_emit = shot("clay", shade="clay")
        s_lit, s_emit = shot("silhouette", shade="silhouette")
        shot("crop_zoom", crop=[0.55, 0.2, 0.95, 0.9], res_pct=400)
        # Key is on the LEFT, Fill on the RIGHT: isolating one must darken the far side
        # of the lit body relative to the other. Emission is unaffected by lights, so
        # the emitter is expected to hold — that is the control inside the test.
        k_lit, _k_emit = shot("light_key_only", light="Key")
        f_lit, _f_emit = shot("light_fill_only", light="Fill")

        print(f"\n── Phase 2 render modes · {OUT} ──")
        print(f"   {'mode':<16} {'LEFT lit body':>14} {'RIGHT emitter':>14}   "
              f"res%   pixels")
        for tag, lit, emit, cap, _dest, res, px in rows:
            pct = (res or [None, None, "?"])[2]
            print(f"   {tag:<16} {lit:>14.1f} {emit:>14.1f}   {pct!s:>4}   {px}")
            if cap:
                print(f"       caption: {cap[:110]}")

        print("\n   assertions:")

        def claim(name, cond, detail):
            print(f"     {'✓' if cond else '✗'} {name} — {detail}")
            if not cond:
                bad.append(name)

        claim("beauty shows both bodies", b_lit > 12 and b_emit > 12,
              f"lit {b_lit:.1f}, emitter {b_emit:.1f}")
        claim("diffuse_direct REMOVES emission", d_emit < b_emit * 0.35,
              f"emitter {b_emit:.1f} → {d_emit:.1f}")
        claim("diffuse_direct KEEPS the lit body", d_lit > 8,
              f"lit body {b_lit:.1f} → {d_lit:.1f}")
        claim("emit keeps only the emitter", e_emit > 12 and e_lit < max(4.0, e_emit * 0.3),
              f"lit {e_lit:.1f}, emitter {e_emit:.1f}")
        claim("clay flattens the emitter to the same material",
              abs(c_lit - c_emit) < max(c_lit, c_emit) * 0.6 or c_emit < b_emit * 0.5,
              f"lit {c_lit:.1f} vs emitter {c_emit:.1f}")
        claim("silhouette lifts both to white", s_lit > 100 and s_emit > 100,
              f"lit {s_lit:.1f}, emitter {s_emit:.1f}")
        zoom = next(r for r in rows if r[0] == "crop_zoom")
        full = next(r for r in rows if r[0] == "beauty")
        claim("crop + res_pct raises pixel density", (zoom[5] or [0, 0, 0])[2] > 100,
              f"resolution_percentage {(zoom[5] or [None, None, None])[2]}")
        # The crop is 0.40 x 0.70 of frame at 400%: ~1.6x the linear pixels of the
        # full frame at 50%, for a fraction of the area. That is optical zoom, not upscale.
        zw = (zoom[6] or [0, 0])[0]
        fw = (full[6] or [1, 1])[0]
        claim("the cropped region is delivered at MORE pixels than the same region "
              "inside the full frame", zw > fw * 0.40,
              f"crop {zoom[6]} vs full frame {full[6]}")
        # Isolating a light must CHANGE the frame and must differ from isolating the
        # other one. Blender's `lightgroup` is Cycles-only and produced neither: on
        # EEVEE it returned the untouched beauty frame under a caption claiming the
        # other lights were excluded.
        claim("isolating a light actually changes the render",
              abs(k_lit - b_lit) > 1.0,
              f"lit body: both lights {b_lit:.1f} → Key alone {k_lit:.1f}")
        claim("isolating DIFFERENT lights gives DIFFERENT renders",
              abs(k_lit - f_lit) > 1.0,
              f"Key alone {k_lit:.1f} vs Fill alone {f_lit:.1f}")
        cap_k = next(r for r in rows if r[0] == "light_key_only")[3]
        claim("the light caption states what was hidden, not what was asked for",
              "hidden" in cap_k and "Fill" in cap_k, cap_k[-130:])
    finally:
        s.close()
    if bad:
        print(f"\n{len(bad)} mode(s) do NOT do what their caption claims: {bad}")
        print("Do not ship a caption for those — the isolation is not happening.")
        return 3
    print("\nEvery mode isolates what its caption promises.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
