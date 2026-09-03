"""Axis-aligned proxy-box feasibility for coupled ``bbox_*`` contract bands.

Run 20260903T081518Z-9a32ab rebuilt a building mass 46 times against a frame-1 height cap
and a frame-113 height floor that no rigid mass could satisfy under the sealed camera
path, then abstained after 154 turns. The question is a deterministic search: does any
axis-aligned box (centre, size) inside declared bounds project inside every band at its
frame? The worker supplies the projector; this module owns the search and is pure so it
can be tested with a synthetic camera.
"""

from __future__ import annotations

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
) -> dict:
    """Search centre/size within ``bounds`` for a box whose projections satisfy every row.

    Deterministic: random restarts drawn from ``seed`` refined by shrinking coordinate
    descent, minimising the largest residual over rows. The result names the best box,
    every row's best value and residual, and the binding rows.
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
    while used < evaluations and best_worst > tolerance:
        start = [l + rng.random() * s for l, s in zip(lo, span, strict=True)]
        worst, readings = evaluate(start)
        step = [0.25 * s for s in span]
        while used < evaluations and max(step) > 1e-4 and worst > tolerance:
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
                    if used >= evaluations:
                        break
                if used >= evaluations:
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
