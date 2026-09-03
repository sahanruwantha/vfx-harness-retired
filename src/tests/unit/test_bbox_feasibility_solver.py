"""The proxy-box feasibility solver decides coupled bbox bands deterministically (HIR-0183)."""

from __future__ import annotations

from vfx_harness.blender.bbox_feasibility import residual, row_bounds, solve_box_feasibility

# A pinhole camera on the -y axis looking +y; the camera moves closer between frames.
_CAMERA_Y = {1: -12.0, 113: -4.0}
_TAN_HALF_FOV = 0.5


def _project(centre, size, frame):
    depth = centre[1] - _CAMERA_Y[int(frame)]
    if depth <= 0.05:
        return None
    half_h = depth * _TAN_HALF_FOV
    half_w = half_h * (16 / 9)
    x0 = 0.5 + (centre[0] - size[0] / 2) / (2 * half_w)
    x1 = 0.5 + (centre[0] + size[0] / 2) / (2 * half_w)
    y0 = 0.5 - (centre[2] + size[2] / 2) / (2 * half_h)
    y1 = 0.5 - (centre[2] - size[2] / 2) / (2 * half_h)
    x0, x1 = max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))
    y0, y1 = max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    return {"bbox": [x0, y0, x1, y1], "width": x1 - x0, "height": y1 - y0, "centre": [(x0 + x1) / 2, (y0 + y1) / 2]}


_BOUNDS = {
    "centre_lo": [-6.0, 0.0, 0.0],
    "centre_hi": [6.0, 12.0, 6.0],
    "size_lo": [0.2, 0.2, 0.2],
    "size_hi": [8.0, 8.0, 8.0],
}


def _row(id, kind, frame, op, **bound):
    return {"id": id, "kind": kind, "frame": frame, "op": op, **bound}


def test_row_bounds_and_residuals_follow_the_operator_shapes() -> None:
    assert row_bounds({"op": "band", "lo": 0.1, "hi": 0.4}) == (0.1, 0.4)
    assert row_bounds({"op": "min", "lo": 0.55}) == (0.55, None)
    assert row_bounds({"op": "max", "hi": 0.35}) == (None, 0.35)
    assert row_bounds({"op": "eq", "value": 0.5, "tol": 0.02}) == (0.48, 0.52)
    assert residual(0.3, 0.1, 0.4) == 0.0
    assert residual(0.05, 0.1, 0.4) == 0.05
    assert residual(None, 0.1, 0.4) == 1.0


def test_consistent_bands_yield_a_satisfying_box_deterministically() -> None:
    rows = [
        _row("far-height", "bbox_height", 1, "band", lo=0.05, hi=0.35),
        _row("near-height", "bbox_height", 113, "min", lo=0.55),
        _row("far-width", "bbox_width", 1, "band", lo=0.05, hi=0.45),
    ]
    first = solve_box_feasibility(rows, _project, _BOUNDS, seed=3)
    second = solve_box_feasibility(rows, _project, _BOUNDS, seed=3)
    assert first["feasible"] is True and first["binding"] == []
    assert first == second
    assert all(row["residual"] == 0.0 for row in first["rows"])
    assert _project(first["box"]["centre"], first["box"]["size"], 113)["height"] >= 0.55


def test_contradictory_bands_are_proved_infeasible_with_the_binding_rows() -> None:
    rows = [
        _row("far-tall", "bbox_height", 1, "min", lo=0.9),
        _row("near-short", "bbox_height", 113, "max", hi=0.1),
    ]
    result = solve_box_feasibility(rows, _project, _BOUNDS, seed=0, evaluations=1500)
    assert result["feasible"] is False
    assert result["objective"] > 0.05
    assert set(result["binding"]) & {"far-tall", "near-short"}
    assert result["evaluations"] <= 1500
