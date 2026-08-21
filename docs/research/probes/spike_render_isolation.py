"""SPIKE: does the DEFAULT render path still produce the same frame after Phase 2?

    .venv/bin/python docs/research/probes/spike_render_isolation.py

This one found a real defect and is kept as the guard for it. The silhouette shade
captured the previous world AFTER installing a placeholder, so a scene with NO world got
one handed back, and every plain render afterwards came out 47.8/255 brighter — a
diagnostic mode silently re-lighting the canonical render the critic scores. Exit 3 means
a mode is leaking again.


Phase 2 added knobs to h_render. The critic's canonical render goes through the same
function, so if the plain call moved, every score in the ledger is now measured against
a different instrument than it was before.

The CONTROL matters as much as the test. h_render writes to a fixed path per mode, so
two plain renders overwrite each other; and if Blender is not byte-deterministic on its
own, a byte difference after a diagnostic render proves nothing. So: render plain twice
with nothing in between (control), then plain-diagnostics-plain (test), copying each
result aside and comparing PIXELS, not just bytes.
"""
import hashlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "src")
from vfx_harness.blender.session import BlenderSession
from vfx_harness.blender.tools import subtract_png

OUT = Path("renders/probe_render_isolation")
OUT.mkdir(parents=True, exist_ok=True)

SCENE = '''
import bpy, math
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
_av = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}
sc.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _av else 'BLENDER_EEVEE'
sc.render.resolution_x, sc.render.resolution_y = 640, 320
cam_d = bpy.data.cameras.new("Cam"); cam = bpy.data.objects.new("Camera", cam_d)
sc.collection.objects.link(cam); sc.camera = cam
cam.location = (0, -7, 1); cam.rotation_euler = (math.radians(90), 0, 0)
bpy.ops.mesh.primitive_monkey_add(location=(0, 0, 1))
m = bpy.data.materials.new("m"); m.use_nodes = True
bpy.context.active_object.data.materials.append(m)
kl = bpy.data.lights.new("K", type="AREA"); kl.energy = 600
k = bpy.data.objects.new("K", kl); sc.collection.objects.link(k)
k.location = (-3, -4, 4); k.rotation_euler = (math.radians(50), 0, math.radians(-35))
RESULT = "ok"
'''


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]


s = BlenderSession(blender="blender").start()
rc = 0
try:
    s.run(SCENE)

    def plain(tag):
        p = s.render(frame=1, mode="eevee", scale=0.5)
        dest = OUT / f"{tag}.png"
        shutil.copy(p, dest)
        return dest

    # CONTROL: two plain renders, nothing in between.
    c1, c2 = plain("control_1"), plain("control_2")
    ctl = subtract_png(str(c1), str(c2), str(OUT / "d_control.png"))

    # TEST: every diagnostic mode, then a plain render again.
    s.render_full(frame=1, mode="eevee", scale=0.5, **{"pass": "diffuse_direct"})
    s.render_full(frame=1, mode="eevee", scale=0.5, **{"pass": "emit"})
    s.render_full(frame=1, mode="eevee", scale=0.5, shade="clay")
    s.render_full(frame=1, mode="eevee", scale=0.5, shade="silhouette")
    s.render_full(frame=1, mode="eevee", scale=0.5, crop=[0.3, 0.3, 0.7, 0.7], res_pct=200)
    t3 = plain("after_diagnostics")
    tst = subtract_png(str(c2), str(t3), str(OUT / "d_test.png"))

    print("\n  control  : plain vs plain, nothing in between")
    print(f"             sha {sha(c1)} vs {sha(c2)} · "
          f"mean pixel delta {ctl['mean_delta']} · max {ctl['max_delta']}")
    print("  test     : plain vs plain, ALL FIVE diagnostic modes in between")
    print(f"             sha {sha(c2)} vs {sha(t3)} · "
          f"mean pixel delta {tst['mean_delta']} · max {tst['max_delta']}")

    same_pixels = tst["mean_delta"] <= max(ctl["mean_delta"], 0.01)
    if same_pixels:
        print("\n✓ The default render path is unchanged: after every diagnostic mode, a "
              "plain render matches the control to within the control's own noise. The "
              "modes restore what they touch, so no score in the ledger changed meaning.")
    else:
        print(f"\n✗ THE DEFAULT RENDER PATH MOVED — {tst['mean_delta']}/255 mean pixel "
              f"delta against a control of {ctl['mean_delta']}. A diagnostic mode leaked "
              f"a setting.")
        rc = 3
    if sha(c1) != sha(c2):
        print("  (note: Blender is not BYTE-deterministic here even with no diagnostics "
              "in between, which is why this compares pixels rather than hashes.)")

    r = s.check("mesh", object="Suzanne")
    print(f"\n  check_scene(mesh, Suzanne): ok={r['ok']} "
          f"verts={r['counts']['verts']} issues={r['issues']}")
    d = s.diff(str(c1), str(t3))
    print(f"  diff of two matching renders: mean_delta={d['mean_delta']} "
          f"did_work={d['did_work']} (must be False)")
    if d["did_work"]:
        rc = 3
finally:
    s.close()
sys.exit(rc)
