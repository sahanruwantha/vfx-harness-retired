"""Deferred-subject rows name who shares their union and how much room is left (HIR-0197).

Room run 20260904T105849Z-0c9a45: ``exterior_facade`` froze at bbox_height 0.349 of a
0.35 ceiling over ``exterior.*``; ``exterior_ground``, the dependency-complete payer, could
only grow that union and abstained. Neither unit had been told the other shared the band.
"""

from __future__ import annotations

from types import SimpleNamespace

from vfx_harness.blender.tools import reports
from vfx_harness.evidence.scene_checks import (
    deferred_subject_sharing_for_unit,
    deferred_subject_union_producers,
    deferred_subject_union_slack,
)


def _unit(uid: str, *, roles: tuple[str, ...], depends_on: tuple[str, ...] = (), provides=("geometry",)):
    return SimpleNamespace(
        id=uid,
        provides=tuple(provides),
        depends_on=tuple(depends_on),
        mutates=SimpleNamespace(roles=tuple(roles), dresses=()),
    )


def _row(cid: str, *, kind: str = "bbox_height", roles=("exterior.*",), op: str = "band", **bounds):
    return {
        "id": cid,
        "kind": kind,
        "roles": list(roles),
        "op": op,
        "frame": 1,
        "owner_layer": "1",
        "activates_at": "2",
        "lifecycle": "persistent",
        "fault_owner": "1",
        **bounds,
    }


ROWS = [_row("bbox-ext-f1", lo=0.15, hi=0.35), _row("bbox-sky", roles=("sky.*",), lo=0.1, hi=0.9)]
UNITS = (
    # authored order: ground before facade, but facade depends on massing only, so the
    # topological order with the authored tie-break is massing, ground, facade, atmosphere.
    _unit("exterior_massing", roles=("exterior.mass",)),
    _unit("exterior_ground", roles=("exterior.ground",), depends_on=("exterior_massing", "exterior_facade")),
    _unit("exterior_facade", roles=("exterior.facade",), depends_on=("exterior_massing",)),
    _unit(
        "exterior_atmosphere",
        roles=("exterior.atmosphere",),
        depends_on=("exterior_massing",),
        provides=("volume",),
    ),
    _unit("sky_dome", roles=("sky.dome",)),
)


def test_union_producers_follow_dependency_order_with_authored_tie_break() -> None:
    producers = deferred_subject_union_producers(ROWS, UNITS, "2")

    assert producers["bbox-ext-f1"] == ("exterior_massing", "exterior_facade", "exterior_ground")
    assert producers["bbox-sky"] == ("sky_dome",)
    # A volume unit overlapping the namespace is not a geometry producer of the union.
    assert "exterior_atmosphere" not in producers["bbox-ext-f1"]


def test_sharing_names_pending_producers_outside_the_dependency_closure() -> None:
    facade = deferred_subject_sharing_for_unit(ROWS, UNITS, UNITS[2], "2")
    assert facade == {
        "bbox-ext-f1": {
            "producers": ["exterior_massing", "exterior_facade", "exterior_ground"],
            "pending": ["exterior_ground"],
        }
    }
    payer = deferred_subject_sharing_for_unit(ROWS, UNITS, UNITS[1], "2")
    assert payer["bbox-ext-f1"]["pending"] == [], "the dependency-complete payer has nobody after it"
    assert deferred_subject_sharing_for_unit(ROWS, UNITS, UNITS[4], "2") == {
        "bbox-sky": {"producers": ["sky_dome"], "pending": []}
    }
    assert deferred_subject_sharing_for_unit(ROWS, UNITS, UNITS[3], "2") == {}


def test_union_slack_is_measured_on_the_irreversible_side_only() -> None:
    rows = [
        _row("h-band", lo=0.15, hi=0.35),
        _row("w-max", kind="bbox_width", op="max", hi=0.5),
        _row("b-eq", kind="bbox_bottom_y", op="eq", value=0.8, tol=0.05),
        _row("t-band", kind="bbox_top_y", lo=0.2, hi=0.4),
        _row("t-min", kind="bbox_top_y", op="min", lo=0.25),
        _row("h-min", op="min", lo=0.1),
        _row("count", kind="object_count", op="min", lo=1),
    ]
    evidence = [
        {"id": "h-band", "value": 0.349},
        {"id": "w-max", "value": 0.6},
        {"id": "b-eq", "value": 0.82},
        {"id": "t-band", "value": 0.3},
        {"id": "t-min", "value": 0.2},
        {"id": "h-min", "value": 0.05},
        {"id": "count", "value": 0},
        {"id": "unknown", "value": 1.0},
    ]

    slack = deferred_subject_union_slack(rows, evidence)

    assert slack["h-band"] == {"kind": "bbox_height", "side": "grows", "bound": 0.35, "slack": 0.35 - 0.349}
    assert slack["w-max"]["slack"] < 0, "already past the ceiling reads as negative room"
    assert abs(slack["b-eq"]["slack"] - 0.03) < 1e-9
    assert slack["t-band"] == {"kind": "bbox_top_y", "side": "falls", "bound": 0.2, "slack": 0.3 - 0.2}
    assert slack["t-min"]["slack"] < 0
    assert "h-min" not in slack, "a floor on a growing union is repairable, not irreversible"
    assert "count" not in slack and "unknown" not in slack


def test_forecast_read_back_states_slack_and_pending_producers() -> None:
    evidence = [
        {"id": "bbox-ext-f1", "metric": "bbox_height", "value": 0.349, "target": [0.15, 0.35], "pass": True},
    ]
    sharing = {
        "bbox-ext-f1": {
            "producers": ["exterior_massing", "exterior_facade", "exterior_ground"],
            "pending": ["exterior_ground"],
        }
    }

    note = reports._deferred_subject_forecast_note(evidence, ROWS, sharing)

    assert "DIAGNOSTIC ONLY" in note
    assert "union can only grow: +0.0010 left before bbox_height exceeds 0.35" in note
    assert "exterior_ground still add geometry to this union" in note

    # The payer, with nobody pending, sees the slack without a sharing clause; a blocker
    # keeps its REQUIRED BEFORE FREEZE section unchanged.
    alone = reports._deferred_subject_forecast_note(
        evidence, ROWS, {"bbox-ext-f1": {"producers": ["a"], "pending": []}}
    )
    assert "still add geometry" not in alone and "+0.0010 left" in alone
    over = [{"id": "bbox-ext-f1", "metric": "bbox_height", "value": 0.3936, "target": [0.15, 0.35], "pass": False}]
    blocked = reports._deferred_subject_forecast_note(over, ROWS, sharing)
    assert "REQUIRED BEFORE FREEZE" in blocked and "still add geometry" not in blocked
