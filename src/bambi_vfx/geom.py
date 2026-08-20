"""Judge-free geometry: motion, framing, mesh-issue classification.

These are the numbers a beauty render cannot show. The Blender-side queries live in
`bambi_vfx/blender/checks.py`; this module is the part that can be tested without bpy.
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


def motion_from_positions(frames: Sequence[int],
                          positions: Sequence[Sequence[float]]) -> dict:
    """Velocity / accel / jerk from a sampled world-space path.

    `frames` and `positions` are parallel. Speed is distance per frame (u/f), matching
    the layout comment `travel: max speed 4.66 u/f (f24), max |accel| 0.39 u/f^2`.
    """
    if len(frames) != len(positions):
        raise ValueError("frames and positions must be the same length")
    if len(frames) < 2:
        return {"ok": False, "reason": "need at least 2 samples",
                "n": len(frames), "max_speed": 0.0, "max_accel": 0.0,
                "max_jerk": 0.0, "unbroken": True, "peak_speed_frame": None,
                "speeds": [], "accels": []}

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

    # Unbroken: the path never reverses (dot of consecutive velocity vectors >= 0)
    # and never stops mid-move then restarts.
    unbroken = True
    for i in range(len(vels) - 1):
        dot = (vels[i][0] * vels[i + 1][0]
               + vels[i][1] * vels[i + 1][1]
               + vels[i][2] * vels[i + 1][2])
        if dot < -1e-9:
            unbroken = False
            break
        if speeds[i] < 1e-9 and speeds[i + 1] > 1e-6 and i not in (0, len(speeds) - 1):
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
        "speeds": [round(s, 4) for s in speeds],
        "accels": [round(a, 4) for a in accels],
    }


def framing_from_ndc(corners: Iterable[Sequence[float]]) -> dict:
    """Frame bbox from Blender camera-space corners, returned in TOP-LEFT coordinates.

    Input is Blender's world_to_camera_view convention (bottom-left). Public bambi_vfx
    rectangles are always [x0,y0,x1,y1] with origin top-left, x right and y down.
    """
    pts = [tuple(c) for c in corners]
    if not pts:
        return {"ok": False, "reason": "no corners", "on_screen": 0.0,
                "width": 0.0, "height": 0.0, "centre": None, "bbox": None}

    in_front = [p for p in pts if p[2] > 0]
    on = [p for p in in_front if 0.0 <= p[0] <= 1.0 and 0.0 <= p[1] <= 1.0]
    use = in_front or pts
    xs, ys = [p[0] for p in use], [p[1] for p in use]
    x0, x1 = min(xs), max(xs)
    bottom_y0, bottom_y1 = min(ys), max(ys)
    y0, y1 = 1.0 - bottom_y1, 1.0 - bottom_y0
    width, height = x1 - x0, y1 - y0
    return {
        "ok": True,
        "bbox": [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)],
        "width": round(width, 4),
        "height": round(height, 4),
        "centre": [round((x0 + x1) / 2, 4), round((y0 + y1) / 2, 4)],
        "on_screen": round(len(on) / len(pts), 3),
        "n_corners": len(pts),
        "n_in_front": len(in_front),
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
        issues.append(f"scale {tuple(round(s, 4) for s in scale[:3])} is not (1,1,1) "
                      f"— apply scale before judging size")
    if any(s <= 0 for s in scale[:3]):
        issues.append(f"non-positive scale {tuple(scale[:3])}")
    return issues
