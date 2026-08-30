"""Builder instruments for unsatisfiable pairs, optical-signal abstention, and bpy teaching."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
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


def test_builder_drain_fails_closed_when_response_stream_goes_idle(monkeypatch) -> None:
    from vfx_harness.agents import builder

    class FakeSettings:
        model_event_idle_seconds = 0.01

        @classmethod
        def from_environment(cls, **_kwargs):
            return cls()

    class FakeClient:
        async def receive_response(self):
            await anyio.sleep_forever()
            yield  # pragma: no cover - makes this an async generator

    events = []
    monkeypatch.setattr(builder, "Settings", FakeSettings)
    monkeypatch.setattr(builder.transcript, "event", lambda name, **fields: events.append((name, fields)))

    with pytest.raises(builder.BuildTruncated) as caught:
        anyio.run(builder._drain_once, FakeClient(), False)

    assert caught.value.terminal_cause == "model_session_idle_timeout"
    assert "no SDK event for 0.01s" in str(caught.value)
    assert events == [
        (
            "model_event_idle_timeout",
            {
                "idle_seconds": 0.01,
                "messages_seen": 0,
                "last_message_type": "response_start",
            },
        )
    ]


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


def test_visibility_report_is_canonical_observation_not_a_second_threshold() -> None:
    from vfx_harness.blender.tools import _check_args_error, _check_report

    report = _check_report(
        "visibility",
        {
            "ok": True,
            "visible_fraction": 0.428571,
            "visible_samples": 6,
            "occluded_samples": 8,
            "on_screen_samples": 14,
            "off_screen_samples": 0,
            "issues": [],
        },
    )

    assert "OBSERVED" in report
    assert "canonical visible_fraction 0.428571" in report
    assert "contract_result" in report
    assert not report.startswith("check visibility: PASS")
    assert "omit samples" in (
        _check_args_error("visibility", {"object": "hero", "frame": 1, "samples": 27}) or ""
    )


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


def _staged_unit_row(
    uid: str,
    *,
    layer: str,
    roles: list[str],
    provides: list[str],
    axis: str,
    depends_on: list[str] | None = None,
) -> dict:
    return {
        "id": uid,
        "title": uid,
        "plan": f"plans/{layer.zfill(2)}_{uid}/{uid}.md",
        "depends_on": depends_on or [],
        "mutates": {
            "mode": "scoped",
            "roles": roles,
            "controls": [],
            "script_spans": [f"build/units/{layer.zfill(2)}/{uid}.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "look_capabilities": [],
        "provides": provides,
        "evaluation": {
            "primary_judge": 1,
            "judge": [{"frame": 1, "ref": "refs/a.png"}],
            "temporal_evidence": "none",
            "claims": [
                {
                    "id": f"{uid}-claim",
                    "proposition": f"{uid} holds",
                    "axis": axis,
                    "property": "object_count",
                    "subject_roles": roles,
                    "subject_controls": [],
                    "moments": [1],
                    "kind": "atomic",
                    "required": True,
                    "authority": "executable_required",
                    "repair_owner": uid,
                    "evidence": [{"kind": "scene_contract", "id": f"{uid}-count"}],
                }
            ],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
    }


def test_fault_owner_options_include_earlier_layer_camera(tmp_path: Path) -> None:
    import json
    from types import SimpleNamespace

    from vfx_harness.agents.builder import _fault_owner_options_for_unit
    from vfx_harness.blender.tools import record_cannot_express
    from vfx_harness.orchestration.ledger import load_layers

    (tmp_path / "layers.json").write_text(
        json.dumps(
            {
                "schema": 4,
                "layers": [
                    {
                        "id": "1",
                        "script": "build/01_camera.py",
                        "title": "Camera",
                        "primary_judge": 1,
                        "judge": [{"frame": 1, "ref": "refs/a.png"}],
                        "owns": ["camera_framing"],
                        "reads": "camera",
                        "stages": [
                            _staged_unit_row(
                                "camera_path",
                                layer="1",
                                roles=["camera"],
                                provides=["camera"],
                                axis="camera_framing",
                            )
                        ],
                    },
                    {
                        "id": "2",
                        "script": "build/02_form.py",
                        "title": "Form",
                        "primary_judge": 1,
                        "judge": [{"frame": 1, "ref": "refs/a.png"}],
                        "owns": ["form"],
                        "reads": "form",
                        "stages": [
                            _staged_unit_row(
                                "lighting",
                                layer="2",
                                roles=["hero.light"],
                                provides=[],
                                axis="form",
                            ),
                            _staged_unit_row(
                                "facade",
                                layer="2",
                                roles=["atrium.shell"],
                                provides=["geometry"],
                                axis="form",
                                depends_on=["lighting"],
                            ),
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    shot = SimpleNamespace(folder=tmp_path)
    layers = load_layers(shot)
    options = _fault_owner_options_for_unit(shot, layers["2"], layers["2"].stages[1])
    by_id = {row["id"]: row for row in options}
    assert "lighting" in by_id
    assert by_id["lighting"]["layer"] == "2"
    assert "camera_path" in by_id
    assert by_id["camera_path"]["layer"] == "1"
    routed = {"fault_owner_options": options}
    accepted = record_cannot_express(
        routed,
        {
            "contract_ids": ["subject-bbox-1"],
            "reason": "screen composition is owned by the earlier camera",
            "fault_owner_units": ["camera_path"],
        },
    )
    assert not accepted.get("is_error")
    assert routed["cannot_express"]["fault_owner_units"] == ["camera_path"]


def test_geometry_protects_deferred_subject_bbox_and_camera_omits_inactive_debt(
    tmp_path: Path,
) -> None:
    import json
    from types import SimpleNamespace

    from tests.unit.test_vis_repair_authority import _detail_unit
    from vfx_harness.agents.builder import (
        _executable_unit_verdict,
        _geometry_protected_vis_ids,
        _scene_ids_active_on_layer,
    )

    rows = [
        {
            "id": "cam-count",
            "kind": "object_count",
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "1",
            "lifecycle": "layer",
            "axis": "camera_framing",
            "roles": ["camera"],
            "op": "eq",
            "value": 1,
        },
        {
            "id": "subject-bbox",
            "kind": "bbox_height",
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "2",
            "lifecycle": "persistent",
            "axis": "camera_framing",
            "roles": ["world.detail"],
            "frame": 40,
            "op": "band",
            "lo": 0.35,
            "hi": 0.55,
        },
        {
            "id": "other-bbox",
            "kind": "bbox_width",
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "2",
            "lifecycle": "persistent",
            "axis": "camera_framing",
            "roles": ["atrium.shell"],
            "frame": 40,
            "op": "band",
            "lo": 0.35,
            "hi": 0.55,
        },
    ]
    (tmp_path / "scene_checks.json").write_text(
        json.dumps({"schema": 2, "contracts": rows}),
        encoding="utf-8",
    )
    shot = SimpleNamespace(folder=tmp_path)
    layer = SimpleNamespace(id="2")
    unit = _detail_unit(provides=["geometry"])
    protected = _geometry_protected_vis_ids(shot, layer, unit, 40)
    assert "subject-bbox" in protected
    assert "other-bbox" not in protected

    due_camera = _scene_ids_active_on_layer(
        shot, "1", {"cam-count", "subject-bbox", "missing-id"}, [40]
    )
    assert due_camera == {"cam-count", "missing-id"}
    due_form = _scene_ids_active_on_layer(shot, "2", {"cam-count", "subject-bbox"}, [40])
    assert due_form == {"subject-bbox"}

    axes = [("form", "declared form")]
    evidence = [{"id": "contract.detail", "pass": True, "authoritative": True}]
    sealed = _executable_unit_verdict(
        unit,
        40,
        axes,
        evidence,
        extra_required_ids={"subject-bbox"},
        inactive_ids={"subject-bbox"},
    )
    assert sealed is not None and sealed["pass"] is True
    unpaid = _executable_unit_verdict(
        unit, 40, axes, evidence, extra_required_ids={"subject-bbox"}
    )
    assert unpaid is not None and unpaid["pass"] is False
    assert "subject-bbox" in unpaid["missing_evidence"]
