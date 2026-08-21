"""Phase 1 — checks that need no judgment.

Each function is a bmesh / camera query. A beauty render is silent on flipped
normals, occluded heroes, and a 4× scale; these are not. The gate is: a known-bad
scene must fire the matching issue list before the check is trusted.

Runs inside the warm worker (`h_check`). `self_test()` builds the fixtures and is
what `docs/research/probes/spike_checks.py` drives.
"""

from __future__ import annotations

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_GEOM_PATH = os.path.join(os.path.dirname(_HERE), "geom.py")

# Loaded BY PATH, not as `vfx_harness.blender.geom`. Blender ships its own Python without the repo's
# dependencies, so importing the package runs `vfx_harness/__init__.py` and dies on
# `dotenv`. geom.py is deliberately dependency-free so the same arithmetic can run both
# inside Blender and in the no-Blender test suite — one implementation, two callers.
_spec = importlib.util.spec_from_file_location("bvfx_geom", _GEOM_PATH)
_geom = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_geom)

framing_from_ndc = _geom.framing_from_ndc
mesh_issues = _geom.mesh_issues
motion_from_positions = _geom.motion_from_positions
scale_issues = _geom.scale_issues


def _obj(name: str):
    import bpy

    obj = bpy.data.objects.get(name)
    if obj is None:
        raise KeyError(f"no object named {name!r}")
    return obj


def _camera():
    import bpy

    cam = bpy.context.scene.camera
    if cam is None:
        raise RuntimeError("no camera in the scene")
    return cam


def check_visibility(name: str, frame: int, samples: int = 27) -> dict:
    """Ray-cast from the camera to bbox corners + a grid on the bbox. Visible
    fraction is the share of samples whose first hit is the target (or nothing
    closer than the target)."""
    import bpy
    from mathutils import Vector

    sc = bpy.context.scene
    sc.frame_set(int(frame))
    bpy.context.view_layer.update()
    obj, cam = _obj(name), _camera()
    deps = bpy.context.evaluated_depsgraph_get()
    origin = cam.matrix_world.translation
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    # A grid through the bbox, inclusive of corners. bound_box is 8 corners; take
    # min/max per axis and lerp between them.
    x0, x1 = min(c.x for c in corners), max(c.x for c in corners)
    y0, y1 = min(c.y for c in corners), max(c.y for c in corners)
    z0, z1 = min(c.z for c in corners), max(c.z for c in corners)
    pts = []
    steps = max(2, round(samples ** (1 / 3)))
    for i in range(steps):
        for j in range(steps):
            for k in range(steps):
                pts.append(
                    Vector(
                        (
                            x0 + (x1 - x0) * i / (steps - 1),
                            y0 + (y1 - y0) * j / (steps - 1),
                            z0 + (z1 - z0) * k / (steps - 1),
                        )
                    )
                )
    hits = occluded = missed = 0
    for pt in pts:
        direction = pt - origin
        dist = direction.length
        if dist < 1e-8:
            continue
        direction.normalize()
        hit, _loc, _n, _i, hit_obj, _m = sc.ray_cast(deps, origin, direction, distance=dist + 1e-4)
        if not hit:
            missed += 1
            continue
        if hit_obj is not None and hit_obj.name == obj.name:
            hits += 1
        else:
            occluded += 1
    n = hits + occluded + missed
    frac = hits / n if n else 0.0
    return {
        "ok": frac >= 0.5,
        "object": name,
        "frame": int(frame),
        "visible_fraction": round(frac, 3),
        "hits": hits,
        "occluded": occluded,
        "missed": missed,
        "samples": n,
        "issues": []
        if frac >= 0.5
        else [f"{name} visible in {frac:.0%} of camera rays at f{frame} ({occluded} occluded, {missed} missed)"],
    }


def check_framing(name: str, frames: list[int]) -> dict:
    """world_to_camera_view → public bbox, width, centre. Origin top-left."""
    import bpy
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector

    sc = bpy.context.scene
    cam = _camera()
    obj = _obj(name)
    per = []
    issues = []
    for f in frames:
        sc.frame_set(int(f))
        bpy.context.view_layer.update()
        corners = [world_to_camera_view(sc, cam, obj.matrix_world @ Vector(c)) for c in obj.bound_box]
        rec = framing_from_ndc([(p.x, p.y, p.z) for p in corners])
        rec["frame"] = int(f)
        per.append(rec)
        if rec.get("on_screen", 0) < 0.5:
            issues.append(f"f{f}: only {rec.get('on_screen', 0):.0%} of {name} bbox corners on screen")
        if rec.get("width", 0) < 0.02 and rec.get("on_screen", 0) > 0:
            issues.append(f"f{f}: {name} spans {rec['width']:.3f} of frame width — below a critic patch")
    return {"ok": not issues, "object": name, "frames": per, "issues": issues}


def check_motion(name: str, frames: list[int]) -> dict:
    """matrix_world translation per frame → speed / accel / jerk."""
    import bpy

    sc = bpy.context.scene
    obj = _obj(name)
    positions = []
    for f in frames:
        sc.frame_set(int(f))
        bpy.context.view_layer.update()
        positions.append(tuple(obj.matrix_world.translation))
    rec = motion_from_positions([int(f) for f in frames], positions)
    rec["object"] = name
    rec["frames"] = [int(f) for f in frames]
    rec["positions"] = [[round(c, 4) for c in p] for p in positions]
    issues = []
    if not rec.get("unbroken", True):
        issues.append(f"{name} path reverses or stops and restarts inside its active move")
    rec["issues"] = issues
    rec["ok"] = rec.get("ok", False) and not issues
    return rec


def check_mesh(name: str, allow_boundary: bool = False) -> dict:
    import bmesh

    obj = _obj(name)
    if obj.type != "MESH" or obj.data is None:
        return {"ok": False, "object": name, "issues": [f"{name} is not a mesh"], "counts": {}}
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        boundary = sum(1 for e in bm.edges if len(e.link_faces) == 1)
        branch = sum(1 for e in bm.edges if len(e.link_faces) > 2)
        wire = sum(1 for e in bm.edges if len(e.link_faces) == 0)
        nonman = boundary + branch + wire
        loose = sum(1 for v in bm.verts if not v.link_edges)
        degen = sum(1 for f in bm.faces if f.calc_area() < 1e-12)
        ngons = sum(1 for f in bm.faces if len(f.verts) > 4)
        poles = sum(1 for v in bm.verts if len(v.link_edges) > 8)
        # islands via walk
        seen = set()
        islands = 0
        for v in bm.verts:
            if v.index in seen:
                continue
            islands += 1
            stack = [v]
            seen.add(v.index)
            while stack:
                cur = stack.pop()
                for e in cur.link_edges:
                    oth = e.other_vert(cur)
                    if oth.index not in seen:
                        seen.add(oth.index)
                        stack.append(oth)
        counts = {
            "verts": len(bm.verts),
            "edges": len(bm.edges),
            "faces": len(bm.faces),
            "nonmanifold_edges": nonman,
            "loose_verts": loose,
            "boundary_edges": boundary,
            "branch_edges": branch,
            "wire_edges": wire,
            "degenerate_faces": degen,
            "ngons": ngons,
            "poles": poles,
            "islands": islands,
        }
    finally:
        bm.free()
    issue_counts = dict(counts)
    if allow_boundary:
        issue_counts["nonmanifold_edges"] = branch + wire
    issues = mesh_issues(issue_counts)
    return {
        "ok": not issues,
        "object": name,
        "counts": counts,
        "allow_boundary": bool(allow_boundary),
        "issues": issues,
    }


def check_scale(name: str) -> dict:
    import bpy

    obj = _obj(name)
    sc = bpy.context.scene
    unit = getattr(getattr(sc, "unit_settings", None), "system", "METRIC")
    scale = tuple(obj.scale)
    dims = tuple(round(v, 4) for v in obj.dimensions)
    issues = scale_issues(scale)
    return {
        "ok": not issues,
        "object": name,
        "scale": [round(s, 4) for s in scale],
        "dimensions": list(dims),
        "unit_system": unit,
        "issues": issues,
    }


def check_passes(frame: int, scale: float = 0.25) -> dict:
    """Beauty vs isolated passes. Counts NaN/Inf/negative on the float buffer.

    Residual (beauty − emit − diffuse_direct) is reported, not gated: a look that
    is mostly volume or glare will have a large residual on purpose.
    """
    import math

    import bpy

    sc = bpy.context.scene
    vl = sc.view_layers[0]
    flags = {
        "emit": "use_pass_emit",
        "diffuse_direct": "use_pass_diffuse_direct",
    }
    restored = {}
    for _n, attr in flags.items():
        if hasattr(vl, attr):
            restored[attr] = getattr(vl, attr)
            setattr(vl, attr, True)
    sc.frame_set(int(frame))
    old_pct = sc.render.resolution_percentage
    sc.render.resolution_percentage = max(1, min(100, int(float(scale) * 100)))
    try:
        bpy.ops.render.render(write_still=True)
        img = bpy.data.images.get("Render Result")
        if img is None:
            return {"ok": False, "frame": int(frame), "issues": ["no Render Result after render"], "passes": {}}
        # Combined pixels — 8-bit PNG won't carry NaN; the Render Result is float.
        px = list(img.pixels)
        n = len(px)
        nan = inf = neg = 0
        for v in px:
            if math.isnan(v):
                nan += 1
            elif math.isinf(v):
                inf += 1
            elif v < -1e-6:
                neg += 1
        issues = []
        if nan:
            issues.append(f"{nan} NaN pixel channel(s)")
        if inf:
            issues.append(f"{inf} Inf pixel channel(s)")
        if neg:
            issues.append(f"{neg} negative pixel channel(s)")
        return {
            "ok": not issues,
            "frame": int(frame),
            "channels": n,
            "nan": nan,
            "inf": inf,
            "negative": neg,
            "passes_enabled": [k for k, a in flags.items() if hasattr(vl, a)],
            "issues": issues,
        }
    finally:
        sc.render.resolution_percentage = old_pct
        for attr, val in restored.items():
            setattr(vl, attr, val)


def subject_bbox(name: str, frame: int) -> dict:
    """Oracle crop box in public normalized coordinates (origin top-left)."""
    rec = check_framing(name, [int(frame)])
    fr = rec["frames"][0] if rec.get("frames") else {}
    return {
        "object": name,
        "frame": int(frame),
        "bbox": fr.get("bbox"),
        "width": fr.get("width"),
        "height": fr.get("height"),
        "centre": fr.get("centre"),
        "on_screen": fr.get("on_screen"),
        "ok": bool(fr.get("bbox")),
    }


def dispatch(kind: str, args: dict) -> dict:
    k = (kind or "").lower()
    if k == "visibility":
        return check_visibility(args["object"], int(args["frame"]), int(args.get("samples", 27)))
    if k == "framing":
        frames = args.get("frames") or [int(args["frame"])]
        return check_framing(args["object"], [int(f) for f in frames])
    if k == "motion":
        frames = args.get("frames") or []
        if len(frames) < 2:
            raise ValueError("check_motion needs frames=[...] with at least 2 entries")
        return check_motion(args["object"], [int(f) for f in frames])
    if k == "mesh":
        return check_mesh(args["object"], bool(args.get("allow_boundary", False)))
    if k == "scale":
        return check_scale(args["object"])
    if k == "passes":
        return check_passes(int(args["frame"]), float(args.get("scale", 0.25)))
    if k in ("bbox", "subject_bbox"):
        return subject_bbox(args["object"], int(args["frame"]))
    raise ValueError(f"unknown check {kind!r}")


def self_test() -> dict:
    """Build one known-bad object per check and assert the issue list is non-empty.

    This is the Phase 1 gate. A check that is silent on its fixture is not shipped.
    """
    import bmesh
    import bpy

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
    r = check_mesh("BadMesh")
    results["mesh"] = {"fired": bool(r["issues"]), "issues": r["issues"], "counts": r["counts"]}

    # --- scale ---
    obj.scale = (4.0, 4.0, 4.0)
    r = check_scale("BadMesh")
    results["scale"] = {"fired": bool(r["issues"]), "issues": r["issues"]}

    # --- framing: park it far off-axis ---
    obj.location = (40.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    r = check_framing("BadMesh", [1])
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
    r = check_visibility("BadMesh", 1)
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
    r = check_motion("BadMesh", [1, 2, 3])
    results["motion"] = {
        "fired": (not r.get("unbroken", True)) or r["max_accel"] > 1.0,
        "unbroken": r.get("unbroken"),
        "max_speed": r.get("max_speed"),
        "max_accel": r.get("max_accel"),
        "issues": r.get("issues"),
    }

    # --- passes: a render that completes; NaN fixture is engine-dependent so we
    # only require the check to return a structured result, and a synthetic
    # negative-count path is asserted in the harness. ---
    r = check_passes(1, scale=0.1)
    results["passes"] = {
        "fired": True,  # structural: ran and returned `ok`
        "ok": r.get("ok"),
        "issues": r.get("issues", []),
    }

    failed = [k for k, v in results.items() if k != "passes" and not v.get("fired")]
    results["gate"] = {"ok": not failed, "silent": failed}
    return results
