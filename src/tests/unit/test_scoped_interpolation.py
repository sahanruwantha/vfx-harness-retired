"""Interpolation can be scoped to the curves a unit meant, and the guard says so (HIR-0203).

Room run 20260904T143607Z-565c1e died here. The builder measured a thin margin on a rotation
contract, wrote a rotation edit, and was blocked because `bvfx_interp(cam, mode='LINEAR')`
walks the whole host closure (HIR-0074) and so would also re-interpolate a passing protected
`data.lens` schedule. There was no call form that expressed "motion but not optics", and the
guard's remediation text told a camera unit with no lights to run a light-coverage pass.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKER = Path(__file__).resolve().parents[2] / "vfx_harness" / "blender" / "worker.py"
MUTATE = Path(__file__).resolve().parents[2] / "vfx_harness" / "blender" / "tools" / "mutate.py"


class _Key:
    def __init__(self) -> None:
        self.interpolation = "BEZIER"
        self.handle_left_type = self.handle_right_type = "AUTO"


class _Curve:
    def __init__(self, data_path: str) -> None:
        self.data_path = data_path
        self.keyframe_points = [_Key(), _Key()]
        self.updated = False

    def update(self) -> None:
        self.updated = True


def _interp():
    """Load the helper without Blender: it only touches the curves the fcurve walk yields."""
    source = WORKER.read_text(encoding="utf-8")
    start = source.index("def _bvfx_interp(")
    end = source.index("def _bvfx_camera_rig(")
    namespace: dict = {}
    exec(compile(source[start:end], str(WORKER), "exec"), namespace)
    return namespace["_bvfx_interp"]


def test_interpolation_scopes_to_the_curves_the_unit_meant(monkeypatch) -> None:
    interp = _interp()
    curves = [
        _Curve("rotation_euler"),
        _Curve("location"),
        _Curve("data.lens"),
        _Curve("hide_render"),
    ]
    host = object()
    namespace = interp.__globals__
    namespace["bpy"] = type("bpy", (), {"data": type("d", (), {"objects": {}})})
    namespace["_bvfx_fcurves"] = lambda item: curves if item is host else []

    touched = interp(host, "LINEAR", data_paths=["rotation_euler"])

    assert touched == 1
    assert curves[0].keyframe_points[0].interpolation == "LINEAR"
    assert curves[2].keyframe_points[0].interpolation == "BEZIER", "the protected lens is untouched"
    assert not curves[2].updated

    for curve in curves:
        curve.keyframe_points = [_Key(), _Key()]
        curve.updated = False
    touched = interp(host, "LINEAR", exclude_paths=["data.lens"])

    assert touched == 3, "everything except the excluded schedule"
    assert curves[2].keyframe_points[0].interpolation == "BEZIER"
    assert curves[3].keyframe_points[0].interpolation == "CONSTANT", "visibility still hard-cuts"

    for curve in curves:
        curve.keyframe_points = [_Key(), _Key()]
    assert interp(host, "LINEAR") == 4, "the unscoped walk is unchanged"


def test_the_schedule_guard_names_a_legal_form_and_no_volumetric_advice() -> None:
    source = MUTATE.read_text(encoding="utf-8")
    block = source[source.index("BLOCKED: required exact schedule(s) already pass") :][:1400]

    assert "exclude_paths=" in block and "data_paths=" in block, (
        "the guard must name the call form that expresses the edit it just refused"
    )
    assert "cannot_express_in_scope" in block
    assert "light_coverage" not in block, (
        "a camera unit with no lights was told to run a light-coverage pass"
    )
    assert "density branch" not in block and "beauty remains black" not in block
    assert re.search(r"re-key that schedule legally", block)
