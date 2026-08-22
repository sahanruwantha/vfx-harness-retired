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
