"""Repeated bbox failures fence mutation until feasibility is measured (HIR-0183)."""

from __future__ import annotations

from vfx_harness.blender.tools.bbox_feasibility_gate import (
    BBOX_FEASIBILITY_STREAK,
    bbox_feasibility_block,
    record_bbox_failures,
    record_bbox_feasibility,
)


def _failed(row_id: str, metric: str = "bbox_height") -> dict:
    return {"id": row_id, "metric": metric, "selector_roles": ["exterior.mass"], "pass": False}


def test_streak_fences_mutation_then_feasibility_verdict_decides() -> None:
    state: dict = {}
    for _ in range(BBOX_FEASIBILITY_STREAK - 1):
        record_bbox_failures(state, [_failed("corner-f113"), _failed("count", metric="object_count")])
        assert bbox_feasibility_block(state) is None
    record_bbox_failures(state, [_failed("corner-f113")])
    block = bbox_feasibility_block(state)
    assert block is not None and "corner-f113" in block
    assert "check_scene(kind='bbox_feasibility', roles=['exterior.mass'])" in block

    record_bbox_feasibility(state, row_ids=["corner-f113", "far-f1"], feasible=False, binding=["corner-f113"])
    block = bbox_feasibility_block(state)
    assert block is not None and "cannot_express_in_scope" in block and "corner-f113" in block

    record_bbox_feasibility(state, row_ids=["corner-f113"], feasible=True, binding=[])
    assert bbox_feasibility_block(state) is None

    # A row that passes drops out of the streak; a non-bbox row never counts.
    record_bbox_failures(state, [_failed("count", metric="object_count")])
    assert state["bbox_failure_streaks"] == {}
    assert bbox_feasibility_block(state) is None
