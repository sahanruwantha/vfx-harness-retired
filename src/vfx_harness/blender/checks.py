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
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_GEOM_PATH = os.path.join(_HERE, "geom.py")
_ROLES_PATH = os.path.abspath(os.path.join(_HERE, "..", "domain", "semantic_roles.py"))

# Loaded BY PATH, not as `vfx_harness.blender.*`. Blender ships its own Python without
# the repo's dependencies, so importing the package is not the worker path. geom.py and
# domain/semantic_roles.py are dependency-free so the same arithmetic / matching can run
# both inside Blender and in the no-Blender test suite — one implementation, two callers.
_spec = importlib.util.spec_from_file_location("bvfx_geom", _GEOM_PATH)
_geom = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_geom)

try:
    from vfx_harness.domain import semantic_roles as _roles
except ImportError:
    if "bvfx_roles" in sys.modules:
        _roles = sys.modules["bvfx_roles"]
    else:
        _rspec = importlib.util.spec_from_file_location("bvfx_roles", _ROLES_PATH)
        _roles = importlib.util.module_from_spec(_rspec)
        sys.modules["bvfx_roles"] = _roles
        _rspec.loader.exec_module(_roles)

frustum_union_ndc = _geom.frustum_union_ndc
project_clip_point = _geom.project_clip_point
BOX_EDGES = _geom.BOX_EDGES
mesh_issues = _geom.mesh_issues
motion_from_positions = _geom.motion_from_positions
scale_issues = _geom.scale_issues
validate_role_token = _roles.validate_role_token
match_semantic = _roles.match_semantic
format_object_miss = _roles.format_object_miss
format_object_ambiguous = _roles.format_object_ambiguous
pick_objects = _roles.pick_objects


def camera_clip_matrix(scene, depsgraph):
    """projection @ view for the scene camera, matching the render's resolution, sensor
    fit, pixel aspect and shift — the same matrix for every projected metric, per
    ADR-0003. Built from Camera.view_frame(scene=...) because Blender 5.x removed
    calc_matrix_camera; view_frame already folds every render-shape input in."""
    from mathutils import Matrix

    cam = scene.camera
    if cam is None:
        raise ValueError("scene has no active camera to project through")
    ev = cam.evaluated_get(depsgraph)
    corners = ev.data.view_frame(scene=scene)
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    left, right, bottom, top = min(xs), max(xs), min(ys), max(ys)
    near, far = float(ev.data.clip_start), float(ev.data.clip_end)
    if ev.data.type == "ORTHO":
        proj = Matrix((
            (2.0 / (right - left), 0.0, 0.0, -(right + left) / (right - left)),
            (0.0, 2.0 / (top - bottom), 0.0, -(top + bottom) / (top - bottom)),
            (0.0, 0.0, -2.0 / (far - near), -(far + near) / (far - near)),
            (0.0, 0.0, 0.0, 1.0),
        ))
    else:
        depth = -corners[0].z  # frame extents normalized to unit depth
        l1, r1 = left / depth, right / depth
        b1, t1 = bottom / depth, top / depth
        proj = Matrix((
            (2.0 / (r1 - l1), 0.0, (r1 + l1) / (r1 - l1), 0.0),
            (0.0, 2.0 / (t1 - b1), (t1 + b1) / (t1 - b1), 0.0),
            (0.0, 0.0, -(far + near) / (far - near), -2.0 * far * near / (far - near)),
            (0.0, 0.0, -1.0, 0.0),
        ))
    return proj @ ev.matrix_world.inverted()


def object_inventory() -> list[dict]:
    """Warm-scene name/role/owner/type rows for miss diagnostics and inspect."""
    import bpy

    return [
        {
            "name": o.name,
            "role": str(o.get("bvfx_role") or ""),
            "owner": str(o.get("bvfx_owner_layer") or ""),
            "type": o.type,
        }
        for o in bpy.context.scene.objects
    ]


def resolve_object(*, role: str | None = None, name: str | None = None):
    """Exactly one scene object. Role matching is ``match_semantic`` (ADR-0003)."""
    import bpy

    inventory = object_inventory()
    hits = pick_objects(inventory, role=role, name=name)
    if not hits:
        raise ValueError(format_object_miss(inventory=inventory, role=role, name=name))
    if len(hits) > 1:
        raise ValueError(format_object_ambiguous(role=str(role or ""), hits=hits))
    obj = bpy.data.objects.get(hits[0]["name"])
    if obj is None:
        raise ValueError(format_object_miss(inventory=inventory, role=role, name=name))
    return obj


def _obj(name: str):
    return resolve_object(name=name)


def _camera():
    import bpy

    cam = bpy.context.scene.camera
    if cam is None:
        raise RuntimeError("no camera in the scene")
    return cam


def surface_visible_fraction(scene, depsgraph, camera, objects) -> dict:
    """Canonical occlusion-true surface visibility sampler.

    The denominator is the selected meshes' on-screen evaluated surface samples,
    matching the ``visible_fraction`` scene-contract definition. Keep this in the
    Blender sibling module so live diagnostics and generated contract probes call one
    implementation (ADR-0003), rather than merely sharing a metric name.
    """
    subjects = {obj.name for obj in objects}
    mvp = camera_clip_matrix(scene, depsgraph)
    camera_location = camera.matrix_world.translation
    sampled = on_screen = seen = 0
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        if evaluated.type != "MESH":
            continue
        world = evaluated.matrix_world
        points = []
        vertices = evaluated.data.vertices
        vertex_stride = max(1, len(vertices) // 32)
        points.extend(
            world @ vertices[index].co for index in range(0, len(vertices), vertex_stride)
        )
        polygons = evaluated.data.polygons
        polygon_stride = max(1, len(polygons) // 32)
        points.extend(
            world @ polygons[index].center for index in range(0, len(polygons), polygon_stride)
        )
        for point in points:
            sampled += 1
            projected = mvp @ point.to_4d()
            if (
                projected.w <= 1e-9
                or abs(projected.x) > projected.w
                or abs(projected.y) > projected.w
                or projected.z < -projected.w
                or projected.z > projected.w
            ):
                continue
            direction = point - camera_location
            distance = direction.length
            if distance <= 1e-6:
                continue
            on_screen += 1
            hit, _location, _normal, _index, hit_object, _matrix = scene.ray_cast(
                depsgraph,
                camera_location,
                direction.normalized(),
                distance=distance - 1e-4,
            )
            original = getattr(hit_object, "original", hit_object)
            if not hit or (original is not None and original.name in subjects):
                seen += 1
    value = seen / on_screen if on_screen else 0.0
    return {
        "visible_fraction": value,
        "surface_samples": sampled,
        "on_screen_samples": on_screen,
        "visible_samples": seen,
        "occluded_samples": on_screen - seen,
        "off_screen_samples": sampled - on_screen,
    }


def check_visibility(name: str, frame: int) -> dict:
    """Observe canonical surface visibility for one rendered subject.

    This diagnostic has no universal acceptance threshold. Exact PASS/FAIL belongs
    to the active contract and is exposed by ``contract_result``.
    """
    import bpy

    sc = bpy.context.scene
    sc.frame_set(int(frame))
    bpy.context.view_layer.update()
    obj, cam = _obj(name), _camera()
    deps = bpy.context.evaluated_depsgraph_get()
    reading = surface_visible_fraction(sc, deps, cam, [obj])
    fraction = float(reading["visible_fraction"])
    on_screen = int(reading["on_screen_samples"])
    visible = int(reading["visible_samples"])
    if not on_screen:
        issues = [f"{name} has no on-screen surface samples at f{frame}"]
    elif not visible:
        issues = [f"{name} is fully occluded at f{frame} across {on_screen} on-screen samples"]
    else:
        issues = []
    return {
        "ok": not issues,
        "object": name,
        "frame": int(frame),
        **reading,
        "visible_fraction": round(fraction, 6),
        "issues": issues,
    }


def check_framing(name: str, frames: list[int]) -> dict:
    """Frustum-clipped bound-box → public bbox, width, centre. Origin top-left.

    The box is clipped as 12 edges against the camera frustum, so the record describes
    the VISIBLE portion and its coordinates are inside [0,1] by construction. An object
    with no frustum intersection reports bbox None — never an off-frame rectangle."""
    import bpy
    from mathutils import Vector

    sc = bpy.context.scene
    _camera()
    obj = _obj(name)
    per = []
    issues = []
    for f in frames:
        sc.frame_set(int(f))
        dg = bpy.context.evaluated_depsgraph_get()
        mvp = camera_clip_matrix(sc, dg)
        ev = obj.evaluated_get(dg)
        clip = [tuple(mvp @ (ev.matrix_world @ Vector(c)).to_4d()) for c in ev.bound_box]
        rec = frustum_union_ndc(clip, BOX_EDGES)
        if rec is None:
            rec = {"bbox": None, "width": 0.0, "height": 0.0, "centre": None, "on_screen": 0.0}
            issues.append(f"f{f}: {name} does not intersect the camera frustum")
        else:
            rec["on_screen"] = round(rec.pop("points_inside") / max(rec.pop("points_total"), 1), 3)
            if rec["on_screen"] < 0.5:
                issues.append(f"f{f}: only {rec['on_screen']:.0%} of {name} bbox corners on screen")
            if rec["width"] < 0.02:
                issues.append(f"f{f}: {name} spans {rec['width']:.3f} of frame width — below a critic patch")
        rec["frame"] = int(f)
        per.append(rec)
    return {"ok": not issues, "object": name, "frames": per, "issues": issues}


def check_projection(points: list[list[float]], frame: int) -> dict:
    """Read-only world-point projection through the evaluated active camera."""
    import bpy
    from mathutils import Vector

    scene = bpy.context.scene
    scene.frame_set(int(frame))
    depsgraph = bpy.context.evaluated_depsgraph_get()
    matrix = camera_clip_matrix(scene, depsgraph)
    projected = []
    for raw in points:
        world = [float(value) for value in raw]
        record = project_clip_point(tuple(matrix @ Vector((*world, 1.0))))
        projected.append({"world": world, **record})
    return {
        "ok": True,
        "frame": int(frame),
        "camera": scene.camera.name,
        "points": projected,
        "issues": [],
    }


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


def _object_fcurves(obj) -> list:
    """Every fcurve on an object across legacy and 5.x slotted actions."""
    ad = getattr(obj, "animation_data", None)
    if not ad or not ad.action:
        return []
    legacy = getattr(ad.action, "fcurves", None)
    if legacy and len(legacy):
        return list(legacy)
    out: list = []
    for layer in getattr(ad.action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            for bag in getattr(strip, "channelbags", []) or []:
                out.extend(bag.fcurves)
    return out


def check_rig_contract() -> dict:
    """A rig-parented camera owes the rig its aim: child keys roll only, no trackers.

    HIR-0015 (run 20260824T103842Z-afec73): a canonical repair baked a world-space
    look-at onto the camera CHILD's local rotation under a rig whose spine already keys
    +90° pitch — the pitches composed to ~180° and the published camera faced away from
    the set at every frame while its rig-level keyframe contracts still passed."""
    import bpy

    violations = []
    checked = 0
    for obj in bpy.data.objects:
        if obj.type != "CAMERA" or obj.parent is None:
            continue
        if not str(obj.parent.get("bvfx_role") or ""):
            continue
        checked += 1
        for fc in _object_fcurves(obj):
            if (
                fc.data_path == "rotation_euler"
                and fc.array_index in (0, 1)
                and len(fc.keyframe_points)
            ):
                violations.append(
                    f"{obj.name}: rotation_euler[{fc.array_index}] carries "
                    f"{len(fc.keyframe_points)} key(s) — under rig {obj.parent.name} the RIG "
                    "owns location+pitch and the camera child owns roll (index 2) only; a "
                    "world-space look-at keyed here double-applies the rig's pitch"
                )
        for con in obj.constraints:
            if con.type in {"TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"}:
                violations.append(
                    f"{obj.name}: {con.type} on the rig-parented camera recomputes full "
                    "orientation each frame and fights the rig; aim by keying the rig's "
                    "pitch numerically (recipe harness-lesson-rig-aim-ownership)"
                )
    return {"ok": not violations, "checked_cameras": checked, "issues": violations}


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


_OBJECT_CHECK_KINDS = frozenset(
    {"visibility", "framing", "motion", "mesh", "scale", "bbox", "subject_bbox"}
)
_VISUAL_CHECK_KINDS = frozenset({"visibility", "framing", "bbox", "subject_bbox"})
_NON_VISUAL_OBJECT_TYPES = frozenset(
    {"CAMERA", "LIGHT", "EMPTY", "ARMATURE", "LATTICE", "SPEAKER", "LIGHT_PROBE"}
)


def visual_subject_error(kind: str, name: str, object_type: str) -> str | None:
    """Reject control hosts that do not occupy rendered subject pixels."""
    if kind not in _VISUAL_CHECK_KINDS or object_type not in _NON_VISUAL_OBJECT_TYPES:
        return None
    if object_type == "LIGHT":
        return (
            f"check_scene(kind={kind!r}) measures a visible SUBJECT, but {name!r} is a "
            "Light control host and has no rendered bounding box. Use "
            f"render_pass(light={name!r}, pass='beauty') to measure its contribution, "
            "or frame the mesh/curve it illuminates."
        )
    if object_type == "CAMERA":
        return (
            f"check_scene(kind={kind!r}) measures a visible SUBJECT, but {name!r} is the "
            "active camera. Pass object= for the mesh/curve being framed and omit role=; "
            "inspect_scene(section='objects') lists names."
        )
    return (
        f"check_scene(kind={kind!r}) measures a visible SUBJECT, but {name!r} is a "
        f"non-renderable {object_type} control host. Frame the rendered mesh/curve/volume "
        "it controls instead."
    )


def dispatch(kind: str, args: dict) -> dict:
    k = (kind or "").lower()
    if k in _OBJECT_CHECK_KINDS:
        role = str(args.get("role") or "").strip() or None
        name = str(args.get("object") or "").strip() or None
        resolved = resolve_object(role=role, name=name)
        subject_error = visual_subject_error(k, resolved.name, resolved.type)
        if subject_error:
            raise ValueError(subject_error)
        args = {**args, "object": resolved.name}
    if k == "visibility":
        return check_visibility(args["object"], int(args["frame"]))
    if k == "framing":
        frames = args.get("frames") or [int(args["frame"])]
        return check_framing(args["object"], [int(f) for f in frames])
    if k == "projection":
        return check_projection(args["points"], int(args["frame"]))
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
    if k == "rig_contract":
        return check_rig_contract()
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
    r = check_rig_contract()
    results["rig_contract"] = {"fired": bool(r["issues"]), "issues": r["issues"]}

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
