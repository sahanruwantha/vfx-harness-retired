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

    # An infeasible single-box verdict is a measurement, not a proof that no subject can:
    # run fa5dbb's building_mass read infeasible twice and passed with a multi-part mass.
    # The verdict clears the streak and reopens mutation (HIR-0184).
    record_bbox_feasibility(state, row_ids=["corner-f113", "far-f1"], feasible=False, binding=["corner-f113"])
    assert bbox_feasibility_block(state) is None
    assert "corner-f113" not in state["bbox_failure_streaks"]
    assert state["bbox_feasibility"]["feasible"] is False

    record_bbox_feasibility(state, row_ids=["corner-f113"], feasible=True, binding=[])
    assert bbox_feasibility_block(state) is None

    # A row that passes drops out of the streak; a non-bbox row never counts.
    record_bbox_failures(state, [_failed("count", metric="object_count")])
    assert state["bbox_failure_streaks"] == {}
    assert bbox_feasibility_block(state) is None


def test_a_feasible_verdict_is_not_downgraded_by_narrower_supplied_bounds() -> None:
    """Run fa5dbb read INFEASIBLE inside bounds it had narrowed to a tower after a feasible
    verdict under harness bounds, and later passed with a multi-part mass (HIR-0183,
    HIR-0184): the measurement clears the streak, a feasible record survives a narrower
    supplied-bounds infeasibility, and no infeasible verdict forces the abstention."""
    state: dict = {}
    for _ in range(BBOX_FEASIBILITY_STREAK):
        record_bbox_failures(state, [_failed("corner-f113")])
    record_bbox_feasibility(state, row_ids=["corner-f113", "far-f1"], feasible=True, binding=[], bounds_source="camera")
    assert bbox_feasibility_block(state) is None
    record_bbox_feasibility(
        state, row_ids=["corner-f113"], feasible=False, binding=["corner-f113"], bounds_source="supplied"
    )
    assert state["bbox_feasibility"]["feasible"] is True
    assert state["bbox_feasibility"]["bounds_source"] == "camera"
    assert bbox_feasibility_block(state) is None

    # An infeasible verdict, under supplied or harness bounds, is advisory: it clears the
    # streak and is recorded, but never becomes a refusal that forces cannot_express_in_scope.
    for bounds_source in ("supplied", "hosts"):
        fresh: dict = {}
        for _ in range(BBOX_FEASIBILITY_STREAK):
            record_bbox_failures(fresh, [_failed("corner-f113")])
        assert bbox_feasibility_block(fresh) is not None
        record_bbox_feasibility(
            fresh, row_ids=["corner-f113"], feasible=False, binding=["corner-f113"], bounds_source=bounds_source
        )
        assert fresh["bbox_failure_streaks"] == {}
        assert fresh["bbox_feasibility"] == {
            "row_ids": ["corner-f113"],
            "feasible": False,
            "binding": ["corner-f113"],
            "bounds_source": bounds_source,
        }
        assert bbox_feasibility_block(fresh) is None
