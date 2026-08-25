from __future__ import annotations

from types import SimpleNamespace

from vfx_harness.agents.builder import (
    _scope_added_object_errors,
    _unit_completion_evidence_ids,
    _unit_evidence_ids,
)
from vfx_harness.blender.tools import _bound_static_frames


def test_unit_completion_requires_bindings_from_every_required_moment() -> None:
    unit = SimpleNamespace(
        evaluation=SimpleNamespace(
            claims=(
                SimpleNamespace(
                    required=True,
                    moments=(1,),
                    evidence=(SimpleNamespace(id="frame-1"),),
                ),
                SimpleNamespace(
                    required=True,
                    moments=(36,),
                    evidence=(SimpleNamespace(id="frame-36"),),
                ),
                SimpleNamespace(
                    required=False,
                    moments=(72,),
                    evidence=(SimpleNamespace(id="optional"),),
                ),
            )
        )
    )

    assert _unit_evidence_ids(unit, 1) == {"frame-1"}
    assert _unit_completion_evidence_ids(unit) == {"frame-1", "frame-36"}


def test_active_unit_static_evidence_is_produced_at_every_bound_frame() -> None:
    rows = [
        {"id": "frame-1", "frame": 1, "kind": "bbox_width"},
        {"id": "frame-36", "frame": 36, "kind": "bbox_width"},
        {"id": "functional", "frames": [1, 36], "kind": "frame_delta"},
        {"id": "unrelated", "frame": 72, "kind": "bbox_width"},
    ]

    assert _bound_static_frames(rows, {"frame-1", "frame-36", "functional"}, 1) == [1, 36]


def test_scoped_artifact_rejects_persisted_untagged_and_undeclared_objects() -> None:
    before = {"upstream": "chamber"}
    after = {
        **before,
        "housing": "iris_housing",
        "TEMP_camera": "",
        "foreign": "camera",
    }

    assert _scope_added_object_errors(before, after, ("iris_housing",)) == [
        "new object 'TEMP_camera' has no bvfx_role",
        "new object 'foreign' has undeclared role 'camera'; allowed ['iris_housing']",
    ]


def test_scoped_artifact_accepts_only_dot_delimited_role_namespace_descendants() -> None:
    before: dict[str, str] = {}
    after = {
        "camera": "camera.main",
        "blade": "iris_blade.segment.01",
        "sibling": "camera_rig.main",
    }

    assert _scope_added_object_errors(before, after, ("camera", "iris_blade")) == [
        "new object 'sibling' has undeclared role 'camera_rig.main'; "
        "allowed ['camera', 'iris_blade']"
    ]


def test_frame_scoped_bindings_are_due_at_their_own_frame() -> None:
    """Run 20260825 (detail_instancing): one claim judging [72, 150] bound vis-f72 AND
    vis-f150; each canonical frame faulted the OTHER frame's row as 'not produced'
    although both were green at their own frame. A binding scoped to a sibling judged
    frame is due there; one scoped to a frame no claim moment covers stays loudly due."""
    from types import SimpleNamespace

    from vfx_harness.agents.builder import _executable_unit_verdict

    claim = SimpleNamespace(
        required=True, authority="executable_required", moments=(72, 150),
        evidence=[SimpleNamespace(kind="scene_contract", id="vis-f72"),
                  SimpleNamespace(kind="scene_contract", id="vis-f150")],
    )
    unit = SimpleNamespace(evaluation=SimpleNamespace(claims=[claim]))
    frames = {"vis-f72": 72, "vis-f150": 150}
    evidence_f72 = [{"id": "vis-f72", "pass": True, "value": 0.9}]

    verdict = _executable_unit_verdict(unit, 72, [("a", "axis")], evidence_f72, contract_frames=frames)
    assert verdict is not None and verdict["pass"], verdict

    # a binding scoped to a frame outside every claim moment must stay missing
    stray = {"vis-f72": 72, "vis-f150": 240}
    verdict = _executable_unit_verdict(unit, 72, [("a", "axis")], evidence_f72, contract_frames=stray)
    assert verdict is not None and not verdict["pass"]
    assert verdict["contract_gap"]
