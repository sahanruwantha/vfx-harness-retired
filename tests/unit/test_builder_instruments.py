"""Builder instruments for unsatisfiable pairs, optical-signal abstention, and bpy teaching."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from vfx_harness.agents.builder import _image_optical_signal
from vfx_harness.blender.geom import motion_from_positions
from vfx_harness.blender.tools import _run_bpy_instrument_hint
from vfx_harness.evidence.compare_panels import focus_signal


def test_frommesh_error_names_path_clearance_instrument() -> None:
    script = "from mathutils.bvhtree import BVHTree\nbvh = BVHTree.FromMesh(mesh)"
    error = "AttributeError: type object 'BVHTree' has no attribute 'FromMesh'"
    hinted = _run_bpy_instrument_hint(script, error)
    assert "path_clearance_min" in hinted
    assert "check_scene(kind='motion'" in hinted
    assert "FromBMesh" in hinted


def test_unrelated_run_bpy_error_is_not_rewritten() -> None:
    error = "NameError: name 'foo' is not defined"
    assert _run_bpy_instrument_hint("print(1)", error) == error


def test_black_plate_has_no_optical_signal(tmp_path: Path) -> None:
    path = tmp_path / "black.png"
    Image.new("RGB", (64, 36), (0, 0, 0)).save(path)
    signal = _image_optical_signal(path)
    assert signal is not None
    assert signal["has_signal"] is False


def test_unsatisfiable_pair_findings_require_a_failing_member(monkeypatch) -> None:
    from vfx_harness.agents.builder import _unsatisfiable_pair_findings
    from vfx_harness.evidence.scene_checks import schedule_smoothness_contradictions

    schedule = {
        "id": "cam-spine-schedule",
        "kind": "keyframe_schedule",
        "roles": ["cam_rig"],
        "samples": [
            {"frame": 1, "values": {"location": [0.0, -30.0, 0.0]}},
            {"frame": 24, "values": {"location": [2.0, 140.0, 5.0]}},
        ],
    }
    smooth = {
        "id": "cam-location-smoothness",
        "kind": "curve_derivative_max",
        "roles": ["cam_rig"],
        "property": "location",
        "hi": 6.0,
    }
    rows = [schedule, smooth]
    assert schedule_smoothness_contradictions(rows)
    monkeypatch.setattr("vfx_harness.evidence.scene_checks.load_rows", lambda _folder: rows)
    shot = type("Shot", (), {"folder": Path("/unused")})()
    assert _unsatisfiable_pair_findings(shot, set()) == []
    hits = _unsatisfiable_pair_findings(shot, {"cam-location-smoothness"})
    assert len(hits) == 1
    assert hits[0]["schedule_id"] == "cam-spine-schedule"


def test_structured_plate_has_optical_signal(tmp_path: Path) -> None:
    path = tmp_path / "signal.png"
    image = Image.new("RGB", (64, 36), (0, 0, 0))
    for x in range(64):
        image.putpixel((x, 18), (255, 255, 255))
    image.save(path)
    assert focus_signal(Image.open(path))["has_signal"] is True


def test_motion_instrument_names_the_peak_speed_span() -> None:
    rec = motion_from_positions(
        [1, 24, 40],
        [(0.0, -30.0, 0.0), (2.0, 140.0, 5.0), (-2.0, 172.0, 7.0)],
    )
    assert rec["peak_speed_frame"] == 24
    assert rec["peak_speed_span"] == [1, 24]

    from vfx_harness.blender.tools import _check_report

    with_span = _check_report(
        "motion",
        {"ok": True, "max_speed": 7.39, "max_accel": 0.0, "max_jerk": 0.0,
         "unbroken": True, "peak_speed_frame": 24, "peak_speed_span": [1, 24],
         "issues": []},
    )
    assert "f1→f24" in with_span
    without_span = _check_report(
        "motion",
        {"ok": True, "max_speed": 4.66, "max_accel": 0.39, "max_jerk": 0.0,
         "unbroken": True, "peak_speed_frame": 24, "issues": []},
    )
    assert "f24" in without_span
    assert "f1→f24" not in without_span


def test_record_cannot_express_requires_ids_and_reason() -> None:
    from vfx_harness.blender.tools import record_cannot_express

    state: dict = {}
    missing = record_cannot_express(state, {"contract_ids": [], "reason": "x"})
    assert missing.get("is_error")
    assert "cannot_express" not in state
    unbound = record_cannot_express(None, {"contract_ids": ["a"], "reason": "measured floor"})
    assert unbound.get("is_error")
    recorded = record_cannot_express(
        state, {"contract_ids": ["haze-shaft-gradient-link"], "reason": "density did not move pixels"}
    )
    assert not recorded.get("is_error")
    assert state["cannot_express"]["contract_ids"] == ["haze-shaft-gradient-link"]
    assert state["cannot_express"]["classification"] == "unsatisfiable_in_scope"

    debts = {
        "image_debts": [
            {
                "id": "form-look-f40",
                "frame": 40,
                "property": "render_region_stat",
                "axis": "form",
            }
        ]
    }
    unpaid = record_cannot_express(
        debts,
        {
            "contract_ids": ["check:form-look-f40"],
            "reason": "no honest adversary on this plate",
        },
    )
    assert not unpaid.get("is_error")
    assert debts["cannot_express"]["contract_ids"] == ["form-look-f40"]
    assert debts["cannot_express"]["classification"] == "unpaid_image_debt"


def test_repair_candidate_server_binds_cannot_express(tmp_path) -> None:
    """HIR-0043: repair ToolSearch for a blender tool missed abstention."""
    from types import SimpleNamespace

    from vfx_harness.agents.builder import _build_probe_candidate_server

    state: dict = {}
    _server, names = _build_probe_candidate_server(
        SimpleNamespace(folder=tmp_path),
        "build/units/02/atmosphere.py",
        {
            "blender": "echo",
            "scratch_dir": str(tmp_path / "scratch"),
            "prior_paths": [],
            "judges": [],
            "layer_id": "2",
            "roles": [],
            "comparison_state": state,
        },
    )
    assert "mcp__candidate__cannot_express_in_scope" in names
    assert all("propose_checks" not in name for name in names)
    _server, finalize_names = _build_probe_candidate_server(
        SimpleNamespace(folder=tmp_path),
        "build/units/02/atmosphere.py",
        {
            "blender": "echo",
            "scratch_dir": str(tmp_path / "scratch"),
            "prior_paths": [],
            "judges": [],
            "layer_id": "2",
            "roles": [],
        },
    )
    assert "mcp__candidate__cannot_express_in_scope" not in finalize_names
    assert all("propose_checks" not in name for name in finalize_names)
