"""Judge-free geometry: motion, framing, mesh-issue classification.

These are the numbers a beauty render cannot show. The Blender-side queries live in
`vfx_harness/blender/checks.py`; this module is the part that can be tested without bpy.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


def _sub(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _len(v: Sequence[float]) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _scale(v: Sequence[float], s: float) -> tuple[float, float, float]:
    return (v[0] * s, v[1] * s, v[2] * s)


def motion_from_positions(frames: Sequence[int], positions: Sequence[Sequence[float]]) -> dict:
    """Velocity / accel / jerk from a sampled world-space path.

    `frames` and `positions` are parallel. Speed is distance per frame (u/f), matching
    the layout comment `travel: max speed 4.66 u/f (f24), max |accel| 0.39 u/f^2`.
    """
    if len(frames) != len(positions):
        raise ValueError("frames and positions must be the same length")
    if len(frames) < 2:
        return {
            "ok": False,
            "reason": "need at least 2 samples",
            "n": len(frames),
            "max_speed": 0.0,
            "max_accel": 0.0,
            "max_jerk": 0.0,
            "unbroken": True,
            "peak_speed_frame": None,
            "peak_speed_span": None,
            "speeds": [],
            "accels": [],
        }

    vels: list[tuple[float, float, float]] = []
    speeds: list[float] = []
    speed_at: list[int] = []
    for i in range(len(frames) - 1):
        dt = frames[i + 1] - frames[i]
        if dt <= 0:
            raise ValueError(f"frames must increase (got {frames[i]} → {frames[i + 1]})")
        v = _scale(_sub(positions[i + 1], positions[i]), 1.0 / dt)
        vels.append(v)
        speeds.append(_len(v))
        speed_at.append(frames[i + 1])

    accels: list[float] = []
    accel_vecs: list[tuple[float, float, float]] = []
    for i in range(len(vels) - 1):
        dt = frames[i + 2] - frames[i + 1]
        a = _scale(_sub(vels[i + 1], vels[i]), 1.0 / dt)
        accel_vecs.append(a)
        accels.append(_len(a))

    jerks: list[float] = []
    for i in range(len(accel_vecs) - 1):
        dt = frames[i + 3] - frames[i + 2]
        jerks.append(_len(_scale(_sub(accel_vecs[i + 1], accel_vecs[i]), 1.0 / dt)))

    # Unbroken: leading/trailing holds are legitimate choreography.  Only the active
    # interval must remain contiguous and never reverse.  The previous pairwise rule
    # called HOLD → MOVE a broken restart whenever the hold had more than one sample.
    unbroken = True
    active = [index for index, speed in enumerate(speeds) if speed > 1e-6]
    active_span = None
    leading_hold = trailing_hold = 0
    if active:
        start, end = active[0], active[-1]
        active_span = [int(frames[start]), int(frames[end + 1])]
        leading_hold = start
        trailing_hold = len(speeds) - end - 1
        if any(speed <= 1e-6 for speed in speeds[start : end + 1]):
            unbroken = False
        for i in range(start, end):
            dot = vels[i][0] * vels[i + 1][0] + vels[i][1] * vels[i + 1][1] + vels[i][2] * vels[i + 1][2]
            if dot < -1e-9:
                unbroken = False
                break

    peak_i = max(range(len(speeds)), key=lambda i: speeds[i])
    return {
        "ok": True,
        "n": len(frames),
        "max_speed": round(max(speeds), 4),
        "max_accel": round(max(accels), 4) if accels else 0.0,
        "max_jerk": round(max(jerks), 4) if jerks else 0.0,
        "unbroken": unbroken,
        "peak_speed_frame": speed_at[peak_i],
        "peak_speed_span": [frames[peak_i], speed_at[peak_i]],
        "speeds": [round(s, 4) for s in speeds],
        "accels": [round(a, 4) for a in accels],
        "active_frame_span": active_span,
        "leading_hold_segments": leading_hold,
        "trailing_hold_segments": trailing_hold,
    }


# Blender's Object.bound_box corner order; pairs are the 12 box edges. Shared by every
# caller that projects a bounding box, so a box crossing the near plane is clipped as
# geometry (edges), not sampled as 8 loose corners.
BOX_EDGES: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
)

# GL-convention clip-space half-space tests, each linear in (x, y, z, w): a point is
# visible iff all six are >= 0 (which also implies w > 0 away from the apex).
_CLIP_PLANES: tuple[tuple[float, float, float, float], ...] = (
    (1.0, 0.0, 0.0, 1.0),   # x >= -w
    (-1.0, 0.0, 0.0, 1.0),  # x <= +w
    (0.0, 1.0, 0.0, 1.0),   # y >= -w
    (0.0, -1.0, 0.0, 1.0),  # y <= +w
    (0.0, 0.0, 1.0, 1.0),   # z >= -w (near)
    (0.0, 0.0, -1.0, 1.0),  # z <= +w (far)
)


def _plane_eval(plane: Sequence[float], p: Sequence[float]) -> float:
    return plane[0] * p[0] + plane[1] * p[1] + plane[2] * p[2] + plane[3] * p[3]


def _screen_xy(p: Sequence[float]) -> tuple[float, float]:
    # clip -> NDC -> [0,1] with origin TOP-LEFT (public vfx_harness convention).
    x = (p[0] / p[3] + 1.0) / 2.0
    y = 1.0 - (p[1] / p[3] + 1.0) / 2.0
    return x, y


def project_clip_point(clip_point: Sequence[float]) -> dict:
    """Project one homogeneous clip point without pretending it is rendered geometry.

    Unlike ``frustum_union_ndc``, this diagnostic intentionally preserves off-frame
    coordinates.  It answers where a proposed world point lands through the active
    camera so an agent does not need to create marker meshes merely to query projection.
    """
    point = tuple(float(value) for value in clip_point)
    if len(point) != 4:
        raise ValueError("clip_point must contain exactly four coordinates")
    in_front = point[3] > 1e-9
    if not in_front:
        return {
            "screen": None,
            "in_front": False,
            "in_frustum": False,
            "clip_w": round(point[3], 6),
        }
    x, y = _screen_xy(point)
    return {
        "screen": [round(x, 6), round(y, 6)],
        "in_front": True,
        "in_frustum": all(_plane_eval(plane, point) >= 0.0 for plane in _CLIP_PLANES),
        "clip_w": round(point[3], 6),
    }


def frustum_union_ndc(
    clip_points: Sequence[Sequence[float]],
    edges: Iterable[Sequence[int]] = (),
) -> dict | None:
    """Union bbox of the VISIBLE portion of geometry, in top-left normalized coordinates.

    `clip_points` are homogeneous clip-space 4-vectors (projection @ view @ world point);
    `edges` are index pairs into them. Segments are clipped against the frustum before the
    perspective divide, so a wall grazing the lens contributes exactly its visible sliver
    and every returned coordinate is inside [0, 1] by construction. The previous
    implementation projected raw vertices and could report a "normalized" height of 1132
    for a camera facing away from its subject (run 20260824T103842Z-afec73).

    Returns None when nothing intersects the frustum — absence of a reading, never a
    zero-sized box, so callers must fail closed rather than compare it to a target.
    """
    pts = [tuple(float(c) for c in p) for p in clip_points]
    xs: list[float] = []
    ys: list[float] = []
    inside = 0
    for p in pts:
        if all(_plane_eval(plane, p) >= 0.0 for plane in _CLIP_PLANES) and p[3] > 1e-9:
            inside += 1
            x, y = _screen_xy(p)
            xs.append(x)
            ys.append(y)
    for a, b in edges:
        p, q = pts[a], pts[b]
        t0, t1 = 0.0, 1.0
        for plane in _CLIP_PLANES:
            fp, fq = _plane_eval(plane, p), _plane_eval(plane, q)
            if fp < 0.0 and fq < 0.0:
                t0, t1 = 1.0, 0.0
                break
            if fp < 0.0:
                t0 = max(t0, fp / (fp - fq))
            elif fq < 0.0:
                t1 = min(t1, fp / (fp - fq))
        if t0 > t1:
            continue
        for t in (t0, t1):
            c = tuple(p[i] + (q[i] - p[i]) * t for i in range(4))
            if c[3] > 1e-9:
                x, y = _screen_xy(c)
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    # numerical guard only — clipped coordinates are already inside the frame
    x0, x1 = min(1.0, max(0.0, min(xs))), min(1.0, max(0.0, max(xs)))
    y0, y1 = min(1.0, max(0.0, min(ys))), min(1.0, max(0.0, max(ys)))
    return {
        "bbox": [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)],
        "width": round(x1 - x0, 4),
        "height": round(y1 - y0, 4),
        "centre": [round((x0 + x1) / 2, 4), round((y0 + y1) / 2, 4)],
        "points_inside": inside,
        "points_total": len(pts),
    }


def mesh_issues(counts: dict) -> list[str]:
    """Turn raw bmesh counts into the issue list a known-bad fixture must fire."""
    issues = []
    if counts.get("nonmanifold_edges", 0):
        issues.append(f"{counts['nonmanifold_edges']} non-manifold edge(s)")
    if counts.get("islands", 1) > 1:
        issues.append(f"{counts['islands']} disconnected island(s)")
    if counts.get("degenerate_faces", 0):
        issues.append(f"{counts['degenerate_faces']} degenerate face(s)")
    if counts.get("loose_verts", 0):
        issues.append(f"{counts['loose_verts']} loose vert(s)")
    if counts.get("poles", 0):
        issues.append(f"{counts['poles']} pole(s) with >8 edges")
    if counts.get("ngons", 0):
        issues.append(f"{counts['ngons']} n-gon(s)")
    return issues


def scale_issues(scale: Sequence[float], apply_eps: float = 1e-4) -> list[str]:
    issues = []
    if any(abs(s - 1.0) > apply_eps for s in scale[:3]):
        issues.append(f"scale {tuple(round(s, 4) for s in scale[:3])} is not (1,1,1) — apply scale before judging size")
    if any(s <= 0 for s in scale[:3]):
        issues.append(f"non-positive scale {tuple(scale[:3])}")
    return issues
