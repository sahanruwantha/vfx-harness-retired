"""Contradictory curve_derivative_max bounds are refused before any builder spend (HIR-0175)."""

from __future__ import annotations

from vfx_harness.evidence.scene_checks import derivative_bound_contradictions, validate_row_set


def _row(id: str, op: str, frames: list[int], *, lo=None, hi=None, roles=("camera.rig",), prop="location") -> dict:
    row = {
        "id": id,
        "kind": "curve_derivative_max",
        "roles": list(roles),
        "property": prop,
        "op": op,
        "frames": frames,
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "axis": "speed_ramp",
    }
    if lo is not None:
        row["lo"] = lo
    if hi is not None:
        row["hi"] = hi
    return row


def test_floor_inside_a_lower_cap_window_is_a_contradiction() -> None:
    rows = [
        _row("cam-path-smooth", "max", [1, 213], hi=0.06),
        _row("speed-ramp-late", "min", [113, 175], lo=0.12),
    ]
    findings = derivative_bound_contradictions(rows)
    assert [(f["floor_id"], f["cap_id"]) for f in findings] == [("speed-ramp-late", "cam-path-smooth")]
    message = findings[0]["message"]
    assert "lo 0.12 over frames 113..175" in message
    assert "hi 0.06 over frames 1..213" in message
    assert "no curve satisfies both" in message
    assert message in validate_row_set(rows)


def test_partial_overlap_different_roles_or_satisfiable_bounds_pass() -> None:
    assert derivative_bound_contradictions([
        _row("cap", "max", [1, 120], hi=0.06),
        _row("floor", "min", [113, 175], lo=0.12),
    ]) == []
    assert derivative_bound_contradictions([
        _row("cap", "max", [1, 213], hi=0.06),
        _row("floor", "min", [113, 175], lo=0.12, roles=("camera.rig.camera",)),
    ]) == []
    assert derivative_bound_contradictions([
        _row("cap", "max", [1, 213], hi=0.06),
        _row("floor", "min", [113, 175], lo=0.12, prop="rotation_euler"),
    ]) == []
    assert derivative_bound_contradictions([
        _row("cap", "max", [1, 213], hi=0.2),
        _row("floor", "min", [113, 175], lo=0.12),
    ]) == []


def test_band_rows_participate_on_both_sides() -> None:
    rows = [
        _row("cap-band", "band", [1, 213], lo=0.0, hi=0.05),
        _row("floor-band", "band", [50, 60], lo=0.1, hi=0.3),
    ]
    findings = derivative_bound_contradictions(rows)
    assert [(f["floor_id"], f["cap_id"]) for f in findings] == [("floor-band", "cap-band")]
