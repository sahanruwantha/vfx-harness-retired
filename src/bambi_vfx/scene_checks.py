"""Authoritative facts measured from the live Blender scene.

Image checks answer what the pixels contain.  They are deliberately poor instruments for
facts Blender already knows exactly: projected object size, object counts, mesh density,
and shading/normal integrity.  ``scene_checks.json`` lets a plan state those facts as
machine-readable contracts and evaluates them on the exact frame being judged.

These checks do not replace photographic judgment.  An object can exist and still fail to
read; a rib-count PASS therefore blocks "there are only two rib objects", but not "the
third rib disappears into the wall".
"""

from __future__ import annotations

import json
from pathlib import Path

SUPPORTED_KINDS = {
    "bbox_width",
    "bbox_height",
    "bbox_center_x",
    "bbox_center_y",
    "bbox_top_y",
    "bbox_bottom_y",
    "object_count",
    "mesh_vertex_count",
    "smooth_fraction",
    "radial_inward_fraction",
}
SUPPORTED_OPS = {"band", "eq", "min", "max"}


def _target(row: dict) -> str:
    op = row.get("op", "band")
    if op == "band":
        return f"{row.get('lo')}..{row.get('hi')}"
    if op == "eq":
        return f"= {row.get('value')}"
    if op == "min":
        return f">= {row.get('lo')}"
    if op == "max":
        return f"<= {row.get('hi')}"
    return str(op)


def _holds(row: dict, value: float | int | None) -> bool:
    if value is None:
        return False
    op = row.get("op", "band")
    if op == "band":
        return float(row["lo"]) <= float(value) <= float(row["hi"])
    if op == "eq":
        return abs(float(value) - float(row["value"])) <= float(row.get("tol", 0.0))
    if op == "min":
        return float(value) >= float(row["lo"])
    if op == "max":
        return float(value) <= float(row["hi"])
    return False


def validate_row(row: dict) -> str | None:
    """Return a concise schema error, or ``None`` for a runnable contract."""
    if not row.get("id"):
        return "missing id"
    if str(row.get("kind", "")) not in SUPPORTED_KINDS:
        return f"unsupported kind {row.get('kind')!r}"
    roles = row.get("roles")
    if "objects" in row:
        return "name-based 'objects' selectors are removed; migrate to semantic 'roles'"
    selectors = roles
    if isinstance(selectors, str):
        selectors = [selectors]
    if not isinstance(selectors, list) or not selectors:
        return "roles must be a non-empty string or list"
    op = str(row.get("op", "band"))
    if op not in SUPPORTED_OPS:
        return f"unsupported op {op!r}"
    try:
        if op == "band":
            if row.get("lo") is None or row.get("hi") is None:
                return "band requires lo and hi"
            if float(row["lo"]) > float(row["hi"]):
                return "band lo exceeds hi"
        elif op == "eq":
            if row.get("value") is None:
                return "eq requires value"
            float(row["value"])
            float(row.get("tol", 0.0))
        elif op == "min":
            if row.get("lo") is None:
                return "min requires lo"
            float(row["lo"])
        elif op == "max":
            if row.get("hi") is None:
                return "max requires hi"
            float(row["hi"])
    except (TypeError, ValueError):
        return f"{op} threshold must be numeric"
    return None


def _blender_probe(rows: list[dict], frame: int) -> str:
    """Build one Blender-side probe for all contracts to keep IPC overhead constant."""
    payload = json.dumps(rows)
    # This code executes inside the persistent Blender worker.  Keep it self-contained:
    # each run_bpy namespace is fresh, and Blender's Python does not share this module.
    return f'''\
import bpy
import fnmatch
import json
from bpy_extras.object_utils import world_to_camera_view

_rows = json.loads({json.dumps(payload)})
_scene = bpy.context.scene
_scene.frame_set({int(frame)})
_depsgraph = bpy.context.evaluated_depsgraph_get()
_camera = _scene.camera
_out = []

def _selected(row):
    patterns = row['roles']
    if isinstance(patterns, str):
        patterns = [patterns]
    return sorted([obj for obj in _scene.objects
                   if any(fnmatch.fnmatchcase(str(obj.get('bvfx_role', '')), pat)
                          for pat in patterns)], key=lambda obj: obj.name)

def _projected(objects):
    coords = []
    for obj in objects:
        evaluated = obj.evaluated_get(_depsgraph)
        mesh = None
        if evaluated.type == 'MESH':
            try:
                mesh = evaluated.to_mesh()
                points = [evaluated.matrix_world @ vert.co for vert in mesh.vertices]
            finally:
                if mesh is not None:
                    evaluated.to_mesh_clear()
        else:
            points = [evaluated.matrix_world @ __import__('mathutils').Vector(corner)
                      for corner in evaluated.bound_box]
        for point in points:
            ndc = world_to_camera_view(_scene, _camera, point)
            if ndc.z > 0:
                coords.append((float(ndc.x), float(1.0 - ndc.y)))
    return coords

for row in _rows:
    objects = _selected(row)
    kind = row['kind']
    value = None
    error = ''
    try:
        if kind == 'object_count':
            value = len(objects)
        elif not objects:
            error = 'selector matched no objects'
        elif kind.startswith('bbox_'):
            points = _projected(objects)
            if not points:
                error = 'no selected geometry is in front of the camera'
            else:
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                value = {{
                    'bbox_width': x1 - x0,
                    'bbox_height': y1 - y0,
                    'bbox_center_x': (x0 + x1) / 2.0,
                    'bbox_center_y': (y0 + y1) / 2.0,
                    'bbox_top_y': y0,
                    'bbox_bottom_y': y1,
                }}[kind]
        elif kind == 'mesh_vertex_count':
            value = sum(len(obj.evaluated_get(_depsgraph).data.vertices)
                        for obj in objects if obj.type == 'MESH')
        elif kind == 'smooth_fraction':
            polygons = [poly for obj in objects if obj.type == 'MESH'
                        for poly in obj.data.polygons]
            value = (sum(1 for poly in polygons if poly.use_smooth) / len(polygons)
                     if polygons else None)
            if value is None:
                error = 'selector matched no mesh polygons'
        elif kind == 'radial_inward_fraction':
            tested = []
            for obj in objects:
                if obj.type != 'MESH':
                    continue
                for poly in obj.data.polygons:
                    centre = obj.matrix_world @ poly.center
                    normal = (obj.matrix_world.to_3x3() @ poly.normal).normalized()
                    radius = (centre.x * centre.x + centre.y * centre.y) ** 0.5
                    if radius < 1e-9 or abs(normal.z) > 0.9:
                        continue
                    tested.append((normal.x * centre.x + normal.y * centre.y) / radius <= 0.0)
            value = (sum(tested) / len(tested)) if tested else None
            if value is None:
                error = 'selector matched no radial mesh polygons'
    except Exception as exc:
        value = None
        error = str(exc)[:160]
    _out.append({{'id': row['id'], 'value': value,
                 'objects': [obj.name for obj in objects],
                 'roles': [str(obj.get('bvfx_role', '')) for obj in objects],
                 'error': error}})
RESULT = _out
'''


def _evidence(rows: list[dict], raw: list[dict]) -> list[dict]:
    """Combine Blender readings with contracts.  Split out for deterministic tests."""
    readings = {str(item.get("id")): item for item in raw if isinstance(item, dict)}
    out = []
    for row in rows:
        error = validate_row(row)
        reading = readings.get(str(row.get("id")), {})
        if not error:
            error = str(reading.get("error") or "")
        value = reading.get("value")
        if isinstance(value, float):
            value = round(value, 4)
        origin = str(row.get("origin") or "planner")
        out.append({
            "id": str(row.get("id") or "<missing>"),
            "axis": str(row.get("axis") or ""),
            "metric": str(row.get("kind") or "scene_contract"),
            "value": value,
            "target": _target(row),
            "pass": not error and _holds(row, reading.get("value")),
            "origin": origin,
            "source": "scene_contract",
            "authoritative": origin != "builder",
            "objects": list(reading.get("objects") or []),
            "roles": list(reading.get("roles") or []),
            **({"error": error} if error else {}),
        })
    return out


def layer_evidence(shot_folder: str | Path, layer_id: str, *, frame: int,
                   session) -> list[dict]:
    """Evaluate this layer's scene contracts in the exact live canonical scene."""
    path = Path(shot_folder) / "scene_checks.json"
    if not path.is_file():
        return []
    try:
        all_rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(all_rows, list):
        return []

    def matches(row):
        if not isinstance(row, dict) or str(row.get("layer", "")) != str(layer_id):
            return False
        if row.get("frame") is None:
            return True
        try:
            return int(row["frame"]) == int(frame)
        except (TypeError, ValueError):
            return True  # include it so invalid evidence fails visibly rather than vanishing

    rows = [row for row in all_rows if matches(row)]
    if not rows:
        return []
    invalid = [row for row in rows if validate_row(row)]
    runnable = [row for row in rows if not validate_row(row)]
    raw = []
    if runnable:
        result = session.run(_blender_probe(runnable, frame), journal=False)
        raw = result.get("result") or []
        if not isinstance(raw, list):
            raw = []
    return _evidence(invalid, []) + _evidence(runnable, raw)
