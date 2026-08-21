from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from vfx_harness.agents.plan_guardrails import validate_planner_artifact
from vfx_harness.evaluation.grounding import audit
from vfx_harness.evaluation.plan_gate import (
    Finding,
    GateResult,
    _check_evidence_coherence,
    _planned_outputs,
    write_report,
)
from vfx_harness.evidence.metrics import METRIC_SET, canonical_fingerprint, look_vector
from vfx_harness.evidence.scene_checks import _blender_probe, functional_evidence, validate_row
from vfx_harness.observability import run_artifacts


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _layer_doc(*, dependency: str | None = None, temporal_id: str | None = None) -> dict:
    evidence = [{"kind": "scene_contract", "id": temporal_id}] if temporal_id else []
    return {
        "schema": 4,
        "layers": [
            {
                "id": "1",
                "script": "build/01_camera.py",
                "title": "Camera",
                "primary_judge": 1,
                "judge": [{"frame": 1, "ref": "refs/a.png"}, {"frame": 2, "ref": "refs/b.png"}],
                "owns": ["camera_framing"],
                "reads": "camera move",
                "stages": [
                    {
                        "id": "move",
                        "title": "Move",
                        "plan": "plans/01_camera/01_move.md",
                        "depends_on": [dependency] if dependency else [],
                        "mutates": {
                            "mode": "scoped",
                            "roles": ["hero"],
                            "controls": ["camera_dolly"],
                            "script_spans": ["build/units/01_camera/01_move.py"],
                        },
                        "protects": {
                            "selector": "all_active_upstream_interfaces",
                            "resolve_to_explicit_ids_at": "freeze",
                        },
                        "evaluation": {
                            "primary_judge": 1,
                            "judge": [
                                {"frame": 1, "ref": "refs/a.png"},
                                {"frame": 2, "ref": "refs/b.png"},
                            ],
                            "temporal_evidence": "motion",
                            "claims": [
                                {
                                    "id": "move-claim",
                                    "proposition": "hero moves",
                                    "axis": "camera_framing",
                                    "property": "motion",
                                    "subject_roles": ["hero"],
                                    "subject_controls": ["camera_dolly"],
                                    "moments": [1, 2],
                                    "kind": "atomic",
                                    "required": True,
                                    "authority": "executable_required",
                                    "repair_owner": "move",
                                    "evidence": evidence,
                                }
                            ],
                        },
                        "completion": "all_required_claims_and_protected_contracts_pass",
                    }
                ],
            }
        ],
    }


def test_gate_result_serializes_reusable_authority() -> None:
    result = GateResult("shot", [Finding("hierarchy", True, "layers.json", "bad edge")], {"layers": 1})

    record = result.to_dict(outcome="budget")

    assert record["schema"] == "vfx-harness.plan-gate/v1"
    assert record["outcome"] == "budget"
    assert record["blocking_count"] == 1
    assert record["findings"][0]["severity"] == "blocking"


def test_gate_report_and_terminal_metadata_are_published(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    result = GateResult("shot", [])
    with run_artifacts.invocation(tmp_path, "plan") as layout:
        report = write_report(tmp_path, result, outcome="clean")
        layout.terminal_metadata.update(
            {"outcome": "clean", "blocking_count": 0, "plan_gate_report": "reports/plan_gate.json"}
        )

    status = json.loads(layout.status.read_text(encoding="utf-8"))
    assert report == layout.reports / "plan_gate.json"
    assert status["state"] == "passed"
    assert status["outcome"] == "clean"
    assert status["blocking_count"] == 0


def test_declared_unit_plans_are_forward_outputs(tmp_path: Path) -> None:
    _write(tmp_path / "layers.json", _layer_doc())

    assert "plans/01_camera/01_move.md" in _planned_outputs(tmp_path)


def test_write_time_layers_validation_reports_every_unknown_dependency(tmp_path: Path) -> None:
    doc = _layer_doc(dependency="other-layer-unit")
    second = json.loads(json.dumps(doc["layers"][0]))
    second["id"] = "2"
    second["script"] = "build/02_camera.py"
    second["stages"][0]["id"] = "move-two"
    second["stages"][0]["depends_on"] = ["also-unknown"]
    doc["layers"].append(second)
    _write(tmp_path / "layers.json", doc)

    errors = validate_planner_artifact(tmp_path, "layers.json")

    assert len(errors) == 2
    assert "other-layer-unit" in errors[0]
    assert "also-unknown" in errors[1]


def test_structured_fingerprint_uses_canonical_registry(tmp_path: Path) -> None:
    refs = tmp_path / "refs"
    refs.mkdir()
    image = Image.new("RGB", (64, 32), (30, 40, 50))
    ref = refs / "a.png"
    image.save(ref)
    fingerprint = canonical_fingerprint(str(ref))
    _write(
        tmp_path / "acceptance.json",
        [{"id": "M1", "frame": 1, "ref": "refs/a.png", "reads": "still", "fingerprint": fingerprint}],
    )

    result = audit(tmp_path)

    assert fingerprint["metric_set"] == METRIC_SET
    assert result["n_error"] == 0
    assert result["n_mismatch"] == 0
    assert result["n_ok"] == result["n_claims"]
    assert fingerprint["values"]["exposure_mean"] == look_vector(str(ref))["exposure_mean"]


def test_structured_fingerprint_rejects_unknown_metric_id(tmp_path: Path) -> None:
    refs = tmp_path / "refs"
    refs.mkdir()
    ref = refs / "a.png"
    Image.new("RGB", (32, 16), (10, 20, 30)).save(ref)
    _write(
        tmp_path / "acceptance.json",
        [
            {
                "id": "M1",
                "frame": 1,
                "ref": "refs/a.png",
                "reads": "still",
                "fingerprint": {"metric_set": METRIC_SET, "values": {"invented_glow": 12.7}},
            }
        ],
    )

    result = audit(tmp_path)

    assert result["n_error"] == 1
    assert "unknown" in result["moments"][0]["schema_errors"][0]


def test_write_time_acceptance_validation_returns_metric_mismatch(tmp_path: Path) -> None:
    refs = tmp_path / "refs"
    refs.mkdir()
    Image.new("RGB", (32, 16), (10, 10, 10)).save(refs / "a.png")
    _write(
        tmp_path / "acceptance.json",
        [
            {
                "id": "M1",
                "frame": 1,
                "ref": "refs/a.png",
                "reads": "dark",
                "fingerprint": {"metric_set": METRIC_SET, "values": {"exposure_mean": 200}},
            }
        ],
    )

    errors = validate_planner_artifact(tmp_path, "acceptance.json")

    assert any("exposure_mean is MISMATCH" in error for error in errors)


def test_temporal_contract_kinds_are_typed() -> None:
    base = {
        "id": "motion",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "axis": "motion",
        "roles": ["debris.*"],
        "frames": [10, 20],
        "op": "max",
        "hi": 0,
    }

    assert validate_row({**base, "kind": "radial_distance_trend"}) is None
    assert validate_row({**base, "kind": "transform_return_delta", "component": "rotation"}) is None
    assert validate_row({**base, "kind": "onset_order", "compare_roles": ["fragments.*"]}) is None
    assert validate_row({**base, "kind": "frame_delta"}) is None
    assert "increasing" in validate_row({**base, "kind": "frame_delta", "frames": [20, 10]})
    compile(_blender_probe([{**base, "kind": "radial_distance_trend"}], 10), "<probe>", "exec")


def test_frame_delta_compares_two_rendered_frames(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (16, 16), (20, 20, 20)).save(first)
    Image.new("RGB", (16, 16), (20, 20, 20)).save(second)

    class Session:
        def render_full(self, *, frame, mode, scale):
            return {"image_path": str(first if frame == 1 else second)}

    row = {
        "id": "lock",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "axis": "motion",
        "kind": "frame_delta",
        "frames": [1, 2],
        "op": "max",
        "hi": 0.1,
    }

    evidence = functional_evidence(tmp_path, "1", session=Session(), rows=[row])

    assert evidence[0]["metric"] == "frame_delta"
    assert evidence[0]["value"] == 0
    assert evidence[0]["pass"] is True


def test_gate_requires_temporal_evidence_and_warns_on_coverage(tmp_path: Path) -> None:
    _write(tmp_path / "layers.json", _layer_doc())
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    _write(
        tmp_path / "checks.json",
        {
            "schema": 2,
            "checks": [
                {"id": f"c{i}", "owner_layer": str(1 + i % 2), "fault_owner": "6"}
                for i in range(4)
            ],
        },
    )

    findings, stats = _check_evidence_coherence(tmp_path)

    assert stats["motion_units"] == 1
    assert any(f.check == "temporal-coverage" and f.blocking for f in findings)
    assert any(f.check == "composition-coverage" and not f.blocking for f in findings)
    assert sum(f.check == "ownership" for f in findings) == 2


def test_temporal_binding_closes_motion_coverage(tmp_path: Path) -> None:
    _write(tmp_path / "layers.json", _layer_doc(temporal_id="trend"))
    _write(
        tmp_path / "scene_checks.json",
        {
            "schema": 2,
            "contracts": [
                {
                    "id": "trend",
                    "kind": "radial_distance_trend",
                    "owner_layer": "1",
                    "fault_owner": "1",
                    "activates_at": "1",
                    "lifecycle": "layer",
                    "axis": "camera_framing",
                    "roles": ["hero"],
                    "frames": [1, 2],
                    "op": "max",
                    "hi": 0,
                }
            ],
        },
    )
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)

    assert not any(f.check == "temporal-coverage" for f in findings)
