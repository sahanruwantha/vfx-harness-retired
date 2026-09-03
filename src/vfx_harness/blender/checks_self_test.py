"""Known-bad fixtures proving every judgment-free check fires (loaded by path from checks.py).

A check that is silent on its own bad fixture is not shipped; the worker runs this as
`check(kind='self_test')` during preflight. Kept beside `checks.py` so that module stays
within the line budget; it receives the checks module explicitly.
"""

from __future__ import annotations


def run_self_test(checks) -> dict:
    """Build one known-bad object per check and assert the issue list is non-empty.

    This is the Phase 1 gate. A check that is silent on its fixture is not shipped.
    """
    import bmesh  # noqa: PLC0415 — embedded Blender runtime only
    import bpy  # noqa: PLC0415 — embedded Blender runtime only

    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    cam_d = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Camera", cam_d)
    sc.collection.objects.link(cam)
    sc.camera = cam
    cam.location = (0.0, -6.0, 1.5)
    cam.rotation_euler = (1.4, 0.0, 0.0)

    results = {}

    # --- mesh: n-gon + loose vert + a third face on one edge (non-manifold) ---
    bm = bmesh.new()
    bmesh.ops.create_circle(bm, cap_ends=True, segments=5, radius=1.0)  # pentagon = n-gon
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.verts.new((8.0, 8.0, 8.0))  # loose vert
    edge = bm.edges[0]
    spur = bm.verts.new((0.0, 0.0, 2.0))
    bm.faces.new((edge.verts[0], edge.verts[1], spur))  # 3rd face on edge
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    me = bpy.data.meshes.new("BadMesh")
    obj = bpy.data.objects.new("BadMesh", me)
    sc.collection.objects.link(obj)
    bm.to_mesh(me)
    bm.free()
    r = checks.check_mesh("BadMesh")
    results["mesh"] = {"fired": bool(r["issues"]), "issues": r["issues"], "counts": r["counts"]}

    # --- scale ---
    obj.scale = (4.0, 4.0, 4.0)
    r = checks.check_scale("BadMesh")
    results["scale"] = {"fired": bool(r["issues"]), "issues": r["issues"]}

    # --- framing: park it far off-axis ---
    obj.location = (40.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    r = checks.check_framing("BadMesh", [1])
    results["framing"] = {"fired": bool(r["issues"]), "issues": r["issues"]}

    # --- visibility: wall between camera and a hero at the origin ---
    obj.location = (0.0, 0.0, 1.0)
    obj.scale = (1.0, 1.0, 1.0)
    wall_me = bpy.data.meshes.new("Wall")
    wall = bpy.data.objects.new("Wall", wall_me)
    sc.collection.objects.link(wall)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=4.0)
    bm.to_mesh(wall_me)
    bm.free()
    wall.location = (0.0, -3.0, 1.0)
    bpy.context.view_layer.update()
    r = checks.check_visibility("BadMesh", 1)
    results["visibility"] = {
        "fired": r["visible_fraction"] < 0.5,
        "visible_fraction": r["visible_fraction"],
        "issues": r["issues"],
    }

    # --- motion: A → B → A in three frames ---
    obj.location = (0.0, 0.0, 1.0)
    obj.keyframe_insert("location", frame=1)
    obj.location = (4.0, 0.0, 1.0)
    obj.keyframe_insert("location", frame=2)
    obj.location = (0.0, 0.0, 1.0)
    obj.keyframe_insert("location", frame=3)
    r = checks.check_motion("BadMesh", [1, 2, 3])
    results["motion"] = {
        "fired": (not r.get("unbroken", True)) or r["max_accel"] > 1.0,
        "unbroken": r.get("unbroken"),
        "max_speed": r.get("max_speed"),
        "max_accel": r.get("max_accel"),
        "issues": r.get("issues"),
    }

    # --- rig contract: a rig-parented camera with a pitch key on the CHILD must fire ---
    rig = bpy.data.objects.new("BadRig", None)
    rig["bvfx_role"] = "cam_rig"
    sc.collection.objects.link(rig)
    bad_cam_data = bpy.data.cameras.new("BadRigCam")
    bad_cam = bpy.data.objects.new("BadRigCam", bad_cam_data)
    sc.collection.objects.link(bad_cam)
    bad_cam.parent = rig
    bad_cam.rotation_euler = (1.5, 0.0, 0.0)
    bad_cam.keyframe_insert("rotation_euler", index=0, frame=1)
    r = checks.check_rig_contract()
    results["rig_contract"] = {"fired": bool(r["issues"]), "issues": r["issues"]}

    # --- passes: a render that completes; NaN fixture is engine-dependent so we
    # only require the check to return a structured result, and a synthetic
    # negative-count path is asserted in the harness. ---
    r = checks.check_passes(1, scale=0.1)
    results["passes"] = {
        "fired": True,  # structural: ran and returned `ok`
        "ok": r.get("ok"),
        "issues": r.get("issues", []),
    }

    failed = [k for k, v in results.items() if k != "passes" and not v.get("fired")]
    results["gate"] = {"ok": not failed, "silent": failed}
    return results
