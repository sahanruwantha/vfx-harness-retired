"""Builder instruments for unsatisfiable pairs, optical-signal abstention, and bpy teaching."""

from __future__ import annotations

from pathlib import Path

import anyio
from PIL import Image

from vfx_harness.agents.builder import (
    BuildTruncated,
    _budget_terminal_cause,
    _image_optical_signal,
    _live_round_budget,
)
from vfx_harness.application.preflight import empty_success, model_phase_failure
from vfx_harness.blender.geom import motion_from_positions
from vfx_harness.blender.tools import (
    _run_bpy_instrument_hint,
    _run_bpy_write_family_error,
)
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


def test_run_bpy_refuses_family_specific_calls_outside_compiled_cluster() -> None:
    card = {
        "write_clusters": [{
            "role_namespace": "iris.blades",
            "host_class": "geometry",
            "instrument_family": "mesh",
        }],
        "mutates": {"dresses": []},
    }
    mixed = """
mesh = bpy.data.meshes.new('blade')
obj.keyframe_insert(data_path='rotation_euler', frame=1)
bvfx_role(obj, 'iris.blades', owner_layer='2')
"""

    message = _run_bpy_write_family_error(mixed, card)

    assert "BLOCKED" in message
    assert "mesh:bpy.data.meshes.new@L2" in message
    assert "keyframe:obj.keyframe_insert@L3" in message
    assert "semantic role tags cannot make mixed mutation legal" in message
    assert _run_bpy_write_family_error(
        "mesh = bpy.data.meshes.new('blade')\nbvfx_role(obj, 'iris.blades')", card
    ) == ""


def test_run_bpy_allows_typed_dressing_but_not_undeclared_mesh_work() -> None:
    card = {
        "write_clusters": [{
            "role_namespace": "iris.rig",
            "host_class": "control_host",
            "instrument_family": "keyframe",
        }],
        "mutates": {"dresses": ["iris.blades"]},
    }

    assert _run_bpy_write_family_error(
        "obj.keyframe_insert(data_path='rotation_euler')\n"
        "mat = bpy.data.materials.new('metal')",
        card,
    ) == ""
    assert "mesh" in _run_bpy_write_family_error("bmesh.new()", card)


def test_run_bpy_treats_camera_host_keyframes_as_camera_work() -> None:
    card = {
        "write_clusters": [{
            "role_namespace": "camera.rig",
            "host_class": "camera",
            "instrument_family": "camera",
        }],
        "mutates": {"dresses": []},
    }
    payload = (
        "cam, target = bvfx_camera_rig(role='camera.rig')\n"
        "cam.keyframe_insert(data_path='location', frame=1)"
    )

    assert _run_bpy_write_family_error(payload, card) == ""
    assert "light" in _run_bpy_write_family_error("bpy.data.lights.new('bad','AREA')", card)


def test_run_bpy_refuses_mutation_when_selected_unit_is_not_atomic() -> None:
    card = {
        "write_clusters": [
            {
                "role_namespace": "iris.blades",
                "host_class": "geometry",
                "instrument_family": "mesh",
            },
            {
                "role_namespace": "iris.blades",
                "host_class": "control_host",
                "instrument_family": "keyframe",
            },
        ],
        "mutates": {"dresses": []},
    }

    message = _run_bpy_write_family_error("bvfx_role(obj, 'iris.blades')", card)

    assert "does not have exactly one derived write-cluster" in message
    assert "Rematerialize or split" in message


def test_typed_cannot_express_ends_live_critique_budget() -> None:
    assert _live_round_budget(2, {}) == 2
    assert _live_round_budget(
        2,
        {"cannot_express": {"contract_ids": ["blacks-f072"], "reason": "measured floor"}},
    ) == 0


def test_empty_success_uses_incremental_cost_for_later_model_phases() -> None:
    prior = 2.5419
    stalled = {"subtype": "success", "turns": 1, "cost": prior}
    assert "spent $0.00 in this phase" in empty_success(
        stalled, 0, prior_cost=prior
    )
    assert empty_success(
        {**stalled, "cost": prior + 0.01}, 0, prior_cost=prior
    ) is None
    assert empty_success(stalled, 1, prior_cost=prior) is None


def test_provider_error_outranks_success_subtype_even_after_productive_work() -> None:
    info = {
        "subtype": "success",
        "turns": 59,
        "cost": 2.5418767,
        "is_error": True,
        "api_error_status": 429,
    }
    why = model_phase_failure(info, 57)
    assert "HTTP 429" in why
    assert "despite SDK subtype 'success'" in why


def test_builder_truncation_keeps_typed_terminal_cause() -> None:
    failure = BuildTruncated("provider failed", terminal_cause="model_session_failure")
    assert failure.terminal_cause == "model_session_failure"
    assert _budget_terminal_cause("error_max_turns") == "max_turns_exhausted"
    assert _budget_terminal_cause("error_max_budget_usd") == "model_budget_exhausted"


def test_builder_drain_preserves_provider_error_fields(monkeypatch) -> None:
    from vfx_harness.agents import builder

    class FakeResultMessage:
        def __init__(self) -> None:
            self.subtype = "success"
            self.num_turns = 59
            self.total_cost_usd = 2.5418767
            self.session_id = "session"
            self.usage = {}
            self.is_error = True
            self.api_error_status = 429

    class FakeClient:
        async def receive_response(self):
            yield FakeResultMessage()

    monkeypatch.setattr(builder, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(builder.transcript, "message", lambda _message: None)
    monkeypatch.setattr(builder.costlog, "record", lambda _message: None)
    info = anyio.run(builder._drain_once, FakeClient(), False)
    assert info["subtype"] == "success"
    assert info["is_error"] is True
    assert info["api_error_status"] == 429


def test_provider_error_skips_optional_context_usage_telemetry(monkeypatch) -> None:
    from vfx_harness.agents import builder

    class FakeResultMessage:
        def __init__(self) -> None:
            self.subtype = "success"
            self.num_turns = 1
            self.total_cost_usd = 0.0
            self.session_id = "session"
            self.usage = {}
            self.is_error = True
            self.api_error_status = 429

    class FakeClient:
        context_requested = False

        async def receive_response(self):
            yield FakeResultMessage()

        async def get_context_usage(self):
            self.context_requested = True
            raise AssertionError("terminal provider errors must bypass optional telemetry")

    client = FakeClient()
    monkeypatch.setattr(builder, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(builder.transcript, "message", lambda _message: None)
    monkeypatch.setattr(builder.costlog, "record", lambda _message: None)
    info = anyio.run(builder._drain, client, False)
    assert info["api_error_status"] == 429
    assert client.context_requested is False


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

    routed = {
        "fault_owner_options": [{"id": "lighting_bloom"}],
    }
    accepted = record_cannot_express(
        routed,
        {
            "contract_ids": ["form-look-f40"],
            "reason": "isolated pass proves the sealed bloom plate is saturated",
            "fault_owner_units": ["lighting_bloom"],
        },
    )
    assert not accepted.get("is_error")
    assert routed["cannot_express"]["fault_owner_units"] == ["lighting_bloom"]
    rejected = record_cannot_express(
        routed,
        {
            "contract_ids": ["form-look-f40"],
            "reason": "measured floor",
            "fault_owner_units": ["invented_unit"],
        },
    )
    assert rejected.get("is_error")
    assert "legal upstream options: lighting_bloom" in rejected["content"][0]["text"]


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
