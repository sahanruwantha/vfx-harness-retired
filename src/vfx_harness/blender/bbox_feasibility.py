"""Axis-aligned proxy-box feasibility for coupled ``bbox_*`` contract bands.

Run 20260903T081518Z-9a32ab rebuilt a building mass 46 times against a frame-1 height cap
and a frame-113 height floor that no rigid mass could satisfy under the sealed camera
path, then abstained after 154 turns. The question is a deterministic search: does any
axis-aligned box (centre, size) inside declared bounds project inside every band at its
frame? The worker supplies the projector; this module owns the search and is pure so it
can be tested with a synthetic camera.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping, Sequence

BBOX_METRIC_READINGS: dict[str, tuple[str, int | None]] = {
    "bbox_width": ("width", None),
    "bbox_height": ("height", None),
    "bbox_center_x": ("centre", 0),
    "bbox_center_y": ("centre", 1),
    "bbox_top_y": ("bbox", 1),
    "bbox_bottom_y": ("bbox", 3),
}
OFF_FRUSTUM_RESIDUAL = 1.0
DEFAULT_EVALUATIONS = 2500
FEASIBILITY_TOLERANCE = 1e-3


def row_bounds(row: Mapping) -> tuple[float | None, float | None]:
    """The accepted [lo, hi] window of one contract row (HIR-0125 operator shapes)."""
    op = str(row.get("op") or "")
    if op == "band":
        return float(row["lo"]), float(row["hi"])
    if op == "min":
        return float(row["lo"]), None
    if op == "max":
        return None, float(row["hi"])
    if op == "eq":
        value = float(row["value"])
        tol = float(row.get("tol") or 0.0)
        return value - tol, value + tol
    raise ValueError(f"bbox feasibility cannot bound op {op!r} on row {row.get('id')!r}")


def reading_value(record: Mapping | None, kind: str) -> float | None:
    if record is None:
        return None
    key, index = BBOX_METRIC_READINGS[kind]
    value = record.get(key)
    if index is not None:
        value = None if value is None else value[index]
    return None if value is None else float(value)


def residual(value: float | None, lo: float | None, hi: float | None) -> float:
    if value is None:
        return OFF_FRUSTUM_RESIDUAL
    out = 0.0
    if lo is not None and value < lo:
        out = lo - value
    if hi is not None and value > hi:
        out = max(out, value - hi)
    return out


def solve_box_feasibility(
    rows: Sequence[Mapping],
    project: Callable[[Sequence[float], Sequence[float], int], Mapping | None],
    bounds: Mapping[str, Sequence[float]],
    *,
    seed: int = 0,
    evaluations: int = DEFAULT_EVALUATIONS,
    tolerance: float = FEASIBILITY_TOLERANCE,
    starts: Sequence[Sequence[float]] = (),
) -> dict:
    """Search centre/size within ``bounds`` for a box whose projections satisfy every row.

    Deterministic: ``starts`` (centre and size triples the caller derives from the camera
    frustums, HIR-0184) are refined first, then random restarts drawn from ``seed``, each by
    shrinking coordinate descent minimising the largest residual over rows. A seeded start
    steps in proportion to its own size, so a small subject far inside a wide search space
    is refined rather than lost. The result names the best box, every row's best value and
    residual, and the binding rows.
    """

    typed = [dict(row) for row in rows]
    if not typed:
        raise ValueError("bbox feasibility needs at least one bbox_* row")
    for row in typed:
        if row.get("kind") not in BBOX_METRIC_READINGS:
            raise ValueError(f"bbox feasibility cannot decide kind {row.get('kind')!r} on row {row.get('id')!r}")
        row["_lo"], row["_hi"] = row_bounds(row)
        row["_frame"] = int(row["frame"])
    lo = [float(v) for v in (*bounds["centre_lo"], *bounds["size_lo"])]
    hi = [float(v) for v in (*bounds["centre_hi"], *bounds["size_hi"])]
    if len(lo) != 6 or len(hi) != 6 or any(h < l for l, h in zip(lo, hi, strict=True)):
        raise ValueError("bbox feasibility bounds need centre_lo/hi and size_lo/hi with lo <= hi")
    if any(size <= 0.0 for size in lo[3:]):
        raise ValueError("bbox feasibility size bounds must be positive")

    rng = random.Random(int(seed))
    used = 0

    def evaluate(params: Sequence[float]) -> tuple[float, list[dict]]:
        nonlocal used
        used += 1
        centre, size = params[:3], params[3:]
        readings: list[dict] = []
        worst = 0.0
        for row in typed:
            value = reading_value(project(centre, size, row["_frame"]), str(row["kind"]))
            gap = residual(value, row["_lo"], row["_hi"])
            worst = max(worst, gap)
            readings.append(
                {
                    "id": str(row.get("id")),
                    "kind": str(row["kind"]),
                    "frame": row["_frame"],
                    "lo": row["_lo"],
                    "hi": row["_hi"],
                    "value": None if value is None else round(value, 4),
                    "residual": round(gap, 4),
                }
            )
        return worst, readings

    span = [h - l for l, h in zip(lo, hi, strict=True)]
    best_params = [(l + h) / 2 for l, h in zip(lo, hi, strict=True)]
    best_worst, best_readings = evaluate(best_params)
    seeded = [
        [min(hi[axis], max(lo[axis], float(value))) for axis, value in enumerate(start)]
        for start in starts
        if len(start) == 6
    ]
    # Every seeded start gets a fair share of the budget: a wide frustum search space
    # otherwise spends every evaluation refining the first seeds and never reaches the
    # depth where the subject actually fits.
    per_start = max(60, evaluations // (len(seeded) + 2)) if seeded else evaluations
    while used < evaluations and best_worst > tolerance:
        if seeded:
            start = seeded.pop(0)
            reach = max(start[3:])
            step = [reach, reach, reach, *(0.5 * size for size in start[3:])]
            deadline = min(evaluations, used + per_start)
        else:
            start = [l + rng.random() * s for l, s in zip(lo, span, strict=True)]
            step = [0.25 * s for s in span]
            deadline = evaluations
        worst, readings = evaluate(start)
        while used < deadline and max(step) > 1e-4 and worst > tolerance:
            improved = False
            for axis in range(6):
                if step[axis] <= 0.0:
                    continue
                for direction in (-1.0, 1.0):
                    candidate = list(start)
                    candidate[axis] = min(hi[axis], max(lo[axis], start[axis] + direction * step[axis]))
                    if candidate[axis] == start[axis]:
                        continue
                    trial_worst, trial_readings = evaluate(candidate)
                    if trial_worst < worst:
                        start, worst, readings, improved = candidate, trial_worst, trial_readings, True
                        break
                    if used >= deadline:
                        break
                if used >= deadline:
                    break
            if not improved:
                step = [s * 0.5 for s in step]
        if worst < best_worst:
            best_params, best_worst, best_readings = start, worst, readings

    binding = sorted(
        (row for row in best_readings if row["residual"] > tolerance),
        key=lambda row: -row["residual"],
    )
    return {
        "feasible": best_worst <= tolerance,
        "objective": round(best_worst, 4),
        "box": {
            "centre": [round(v, 4) for v in best_params[:3]],
            "size": [round(v, 4) for v in best_params[3:]],
        },
        "rows": best_readings,
        "binding": [row["id"] for row in binding],
        "evaluations": used,
        "seed": int(seed),
    }


def proxy_bounds_from_frustums(scene, frames) -> dict:
    """Search space for a subject nothing carries yet: the union of the camera frustums.

    A camera layer proves downstream framing before any geometry exists (HIR-0184), so the
    proxy may sit anywhere the camera can see at any bound frame, out to the camera's own
    clip_end; the box may be as small as 5 cm or as large as that volume.
    """
    import bpy  # noqa: PLC0415 — embedded-runtime dependency, only inside the worker
    from mathutils import Vector  # noqa: PLC0415 — embedded-runtime dependency

    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for frame in frames:
        scene.frame_set(int(frame))
        dg = bpy.context.evaluated_depsgraph_get()
        ev = scene.camera.evaluated_get(dg)
        corners = ev.data.view_frame(scene=scene)
        depth = max(-corners[0].z, 1e-6)
        far = float(ev.data.clip_end)
        points = [ev.matrix_world @ Vector((0.0, 0.0, 0.0))]
        points.extend(ev.matrix_world @ (corner * (far / depth)) for corner in corners)
        for point in points:
            for axis in range(3):
                lo[axis] = min(lo[axis], point[axis])
                hi[axis] = max(hi[axis], point[axis])
    extent = [max(h - l, 1.0) for l, h in zip(lo, hi, strict=True)]
    return {
        "centre_lo": list(lo),
        "centre_hi": list(hi),
        "size_lo": [0.05, 0.05, 0.05],
        "size_hi": list(extent),
        "source": "camera_frustum",
    }


def frustum_seed_boxes(scene, frames, *, depths: int = 6, fractions=(0.1, 0.4)) -> list[list[float]]:
    """Seed boxes on each bound frame's optical axis: log-spaced depths, sizes as frustum fractions."""
    import bpy  # noqa: PLC0415 — embedded-runtime dependency, only inside the worker
    from mathutils import Vector  # noqa: PLC0415 — embedded-runtime dependency

    per_frame: list[list[list[float]]] = []
    for frame in frames:
        seeds: list[list[float]] = []
        per_frame.append(seeds)
        scene.frame_set(int(frame))
        dg = bpy.context.evaluated_depsgraph_get()
        ev = scene.camera.evaluated_get(dg)
        corners = ev.data.view_frame(scene=scene)
        depth_ref = max(-corners[0].z, 1e-6)
        half_width = max(max(abs(c.x) for c in corners), max(abs(c.y) for c in corners)) / depth_ref
        near = max(float(ev.data.clip_start), 1e-3) * 2.0
        far = max(float(ev.data.clip_end), near * 2.0)
        origin = ev.matrix_world.translation.copy()
        forward = (ev.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))).normalized()
        for index in range(depths):
            distance = near * math.exp(math.log(far / near) * index / max(depths - 1, 1))
            centre = origin + forward * distance
            for fraction in fractions:
                size = max(2.0 * half_width * distance * fraction, 0.05)
                seeds.append([centre.x, centre.y, centre.z, size, size, size])
    # Interleave frames so the first evaluations already cover every bound camera pose.
    interleaved: list[list[float]] = []
    for index in range(max((len(rows) for rows in per_frame), default=0)):
        interleaved.extend(rows[index] for rows in per_frame if index < len(rows))
    return interleaved
