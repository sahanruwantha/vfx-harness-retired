"""Runs INSIDE Blender: normalize a raw image→3D GLB into a clean, unit-known asset.

    blender --background --factory-startup --python _normalize_bpy.py -- \
        --in raw.glb --out model.glb --meta meta.json --height 100 --up Z

Deterministic: same input GLB + args → same output GLB + geometry meta. Recenters
the mesh base to the origin, uniform-scales to a target height, tidies normals and
doubles, and re-exports. Writes geometric metadata as JSON for the host to augment.
"""

import json
import math
import sys

import bpy
import bmesh
from mathutils import Matrix


def _argv():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def _opt(name, default=""):
    a = _argv()
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


def _mesh_objects():
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def _world_bbox(objs):
    mins = [math.inf] * 3
    maxs = [-math.inf] * 3
    for o in objs:
        mw = o.matrix_world
        for v in o.data.vertices:
            w = mw @ v.co
            for i in range(3):
                mins[i] = min(mins[i], w[i])
                maxs[i] = max(maxs[i], w[i])
    return mins, maxs


def _tidy(objs):
    for o in objs:
        try:
            bm = bmesh.new()
            bm.from_mesh(o.data)
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            bm.to_mesh(o.data)
            bm.free()
            o.data.update()
        except Exception as e:
            # Must not die on one bad mesh — but it must SAY so. A silently skipped mesh
            # ships an asset normalized everywhere except one part, and the only symptom
            # downstream is geometry that looks subtly wrong in a finished render.
            print(f"! normalize SKIPPED {getattr(o, 'name', '?')}: "
                  f"{type(e).__name__}: {e}", flush=True)


def main():
    raw = _opt("--in")
    out = _opt("--out")
    meta_path = _opt("--meta")
    target_h = float(_opt("--height", "100"))
    # up axis is informational: import_scene.gltf already converts glTF +Y-up to
    # Blender +Z-up, so meshes arrive Z-up. `--up` is recorded, not re-applied.
    up = _opt("--up", "Z")

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=raw)

    objs = _mesh_objects()
    if not objs:
        raise SystemExit("no mesh objects in imported GLB")

    mins, maxs = _world_bbox(objs)
    dims = [maxs[i] - mins[i] for i in range(3)]
    height = dims[2] or 1.0
    scale = target_h / height
    pivot = ((mins[0] + maxs[0]) / 2.0, (mins[1] + maxs[1]) / 2.0, mins[2])  # base-centre

    # world point p -> scale * (p - pivot): base to origin, uniform scale to target.
    M = Matrix.Scale(scale, 4) @ Matrix.Translation((-pivot[0], -pivot[1], -pivot[2]))
    for o in objs:
        o.matrix_world = M @ o.matrix_world

    _tidy(objs)

    mins2, maxs2 = _world_bbox(objs)
    dims2 = [round(maxs2[i] - mins2[i], 4) for i in range(3)]
    tris = sum(max(0, len(p.vertices) - 2) for o in objs for p in o.data.polygons)

    bpy.ops.export_scene.gltf(filepath=out, export_format="GLB", use_selection=False)

    meta = {
        "up": up,
        "target_height": target_h,
        "scale_applied": round(scale, 6),
        "objects": len(objs),
        "tris": tris,
        "bbox_dims": dims2,          # x,y,z after normalize (z ≈ target_height)
        "base_z": round(mins2[2], 4),  # ≈ 0
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)


if __name__ == "__main__":
    main()
