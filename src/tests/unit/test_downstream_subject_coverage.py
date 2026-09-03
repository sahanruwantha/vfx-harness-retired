"""A camera layer frames every later subject at shared judge frames and proves it (HIR-0184)."""

from __future__ import annotations

from vfx_harness.domain.work_units.capabilities import compile_deferred_subject_activation
from vfx_harness.domain.work_units.subject_framing import (
    successor_judge_rows,
    uncovered_downstream_subjects,
)


def _sparse(layer_id: str, *, judges: list[int], reserved: list[str], depends: list[str], provides=None) -> dict:
    return {
        "id": layer_id,
        "title": f"layer {layer_id}",
        "judge": [{"frame": frame, "ref": f"refs/f{frame}.png"} for frame in judges],
        "execution": "jit_deferred",
        "jit": {
            "depends_on_layers": depends,
            "reserved_roles": reserved,
            "provides": provides or {},
            "required_outcomes": [],
            "owned_requirements": [],
        },
    }


def _deferred(id: str, frame: int, roles: list[str], *, owner: str = "1", activates_at: str = "2") -> dict:
    return {
        "id": id,
        "kind": "bbox_height",
        "roles": roles,
        "frame": frame,
        "op": "band",
        "lo": 0.3,
        "hi": 0.6,
        "owner_layer": owner,
        "fault_owner": owner,
        "activates_at": activates_at,
        "lifecycle": "persistent",
        "axis": "framing",
    }


LAYERS = [
    _sparse("1", judges=[1, 38, 113], reserved=["camera.*"], depends=[], provides={"camera": ["camera.*"]}),
    _sparse("2", judges=[1, 113, 150], reserved=["exterior.*"], depends=["1"]),
    _sparse("3", judges=[175], reserved=["hero.*"], depends=["1", "2"]),
]


def test_successors_carry_judges_and_reserved_roles() -> None:
    rows = successor_judge_rows(LAYERS, "1")
    assert [(row["id"], row["reserved_roles"]) for row in rows] == [("2", ("exterior.*",)), ("3", ("hero.*",))]
    assert [judge["frame"] for judge in rows[0]["judges"]] == [1, 113, 150]
    card = compile_deferred_subject_activation(LAYERS, "1")
    assert card["framing_obligations"] == [
        {"frame": 1, "ref": "refs/f1.png", "layer_id": "2", "reserved_roles": ["exterior.*"]},
        {"frame": 113, "ref": "refs/f113.png", "layer_id": "2", "reserved_roles": ["exterior.*"]},
    ]


def test_shared_judge_frames_without_a_camera_row_are_gaps() -> None:
    successors = successor_judge_rows(LAYERS, "1")
    rows = [_deferred("ext-f1", 1, ["exterior.mass"])]
    gaps = uncovered_downstream_subjects("1", [1, 38, 113], successors, rows)
    assert [(gap.frame, gap.layer_id, gap.reserved_roles, gap.ref) for gap in gaps] == [
        (113, "2", ("exterior.*",), "refs/f113.png")
    ]
    # The canonical matcher covers a reserved namespace with a dotted member; a row of a
    # different namespace, a different frame, or a layer-scoped row does not.
    covered = [*rows, _deferred("ext-f113", 113, ["exterior.mass.base"])]
    assert uncovered_downstream_subjects("1", [1, 38, 113], successors, covered) == ()
    wrong = [*rows, _deferred("hero-f113", 113, ["hero.frame"])]
    assert len(uncovered_downstream_subjects("1", [1, 38, 113], successors, wrong)) == 1
    same_layer = [*rows, _deferred("ext-f113", 113, ["exterior.mass"], activates_at="1")]
    assert len(uncovered_downstream_subjects("1", [1, 38, 113], successors, same_layer)) == 1

