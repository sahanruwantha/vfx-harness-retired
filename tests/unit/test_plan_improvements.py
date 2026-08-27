from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from vfx_harness.agents import plan_tools
from vfx_harness.agents.plan_guardrails import validate_planner_artifact
from vfx_harness.agents.prompts import PLANNER_SYSTEM
from vfx_harness.evaluation.grounding import audit
from vfx_harness.evaluation.plan_gate import (
    Finding,
    GateResult,
    _check_done,
    _check_evidence,
    _check_evidence_coherence,
    _planned_outputs,
    write_report,
)
from vfx_harness.evidence.metrics import METRIC_SET, canonical_fingerprint, look_vector
from vfx_harness.evidence.scene_checks import _blender_probe, functional_evidence, validate_row
from vfx_harness.observability import run_artifacts


def test_global_plan_defers_all_world_model_work_to_jit() -> None:
    """All-deferred publication, empty evidence documents, and derived ownership are now
    properties of the mechanical expansion (proved gate-clean by construction in
    test_plan_authoring); the prompt's remaining job is the judgment contract."""
    flat = PLANNER_SYSTEM.replace("\n", " ")
    assert "every layer `jit_deferred` with derived `owned_requirements`" in flat
    assert (
        "kinds, moments, thresholds, and techniques are chosen at the owning layer's"
        in flat
    )
    assert "Ownership is coverage, not design" in flat
    assert "no reference measurement, image-check\ncalibration, recipe search" in PLANNER_SYSTEM


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


def test_plan_check_cannot_bind_process_local_adversary(tmp_path: Path) -> None:
    refs = tmp_path / "refs"
    refs.mkdir()
    Image.new("RGB", (16, 16), (20, 20, 20)).save(refs / "good.png")
    outside = tmp_path.parent / "process-local-adversary.png"
    Image.new("RGB", (16, 16), (240, 240, 240)).save(outside)
    _write(tmp_path / "checks.json", {
        "schema": 2,
        "checks": [{
            "id": "exposure",
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "1",
            "lifecycle": "layer",
            "axis": "look",
            "frame": 1,
            "ref": "refs/good.png",
            "metric": "frame_mean",
            "op": "band",
            "lo": 0,
            "hi": 255,
            "stage": "pre_grade",
            "rejects": [str(outside)],
            "proof": {"ref": 20, "adversary": [240]},
        }],
    })

    findings, _ = _check_done(tmp_path)

    assert any(
        finding.blocking and "absolute adversary path" in finding.what
        for finding in findings
    )


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
    control_bound = {
        **base,
        "kind": "onset_order",
        "roles": ["iris_blade.*"],
        "compare_control_roles": ["unlock_group_4"],
    }
    assert validate_row(control_bound) is None
    probe = _blender_probe([control_bound], 10)
    assert "bvfx_control" in probe
    assert "compare_control_roles" in probe
    assert validate_row({**base, "kind": "frame_delta"}) is None
    assert "increasing" in validate_row({**base, "kind": "frame_delta", "frames": [20, 10]})
    compile(_blender_probe([{**base, "kind": "radial_distance_trend"}], 10), "<probe>", "exec")


def test_exact_keyframe_schedule_is_typed_and_executable() -> None:
    row = {
        "id": "camera-spine",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "persistent",
        "axis": "camera_framing",
        "kind": "keyframe_schedule",
        "roles": ["cam_rig"],
        "samples": [
            {"frame": 1, "values": {"location": [0, -6, 0]}},
            {"frame": 240, "values": {"location": [0, 225, 0]}},
        ],
        "op": "max",
        "hi": 0.001,
    }

    assert validate_row(row) is None
    probe = _blender_probe([row], 1)
    compile(probe, "<camera-spine-schedule>", "exec")
    assert "actual_frames!=expected_frames" in probe
    assert "action_slot" in probe
    assert "_schedule_frames" in probe
    control_bound = {**row, "roles": [], "control_roles": ["camera_spine"]}
    assert validate_row(control_bound) is None
    assert "row.get('control_roles')" in _blender_probe([control_bound], 1)
    assert "sample frames must be unique" in validate_row({
        **row,
        "samples": [row["samples"][0], row["samples"][0]],
    })


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


def test_plan_spike_uses_the_smoke_tested_blender_binary(tmp_path: Path, monkeypatch) -> None:
    invoked = {}
    monkeypatch.setattr(plan_tools, "resolve_blender", lambda requested: "/resolved/blender")

    def fake_sh(args, *, timeout):
        invoked.update(args=args, timeout=timeout)
        return 0, "spike ok", ""

    monkeypatch.setattr(plan_tools, "_sh", fake_sh)
    result = plan_tools._spike(
        "blender",
        "print('ok')",
        None,
        60,
        tmp_path / "spike.py",
        tmp_path / "spike.png",
    )

    assert invoked["args"][0] == "/resolved/blender"
    assert invoked["timeout"] == 60
    assert result["rc"] == 0


def test_plan_spike_deposits_citable_evidence_in_global_workspace(tmp_path: Path) -> None:
    (tmp_path / ".plan-workspace.json").write_text("{}\n", encoding="utf-8")
    lab = tmp_path / "outside-lab" / "draft"
    lab.mkdir(parents=True)
    script = lab / "spike_01.py"
    script.write_text("print('measured=0.5625')\n", encoding="utf-8")
    script.with_suffix(".out").write_text("measured=0.5625\n", encoding="utf-8")
    render = lab / "spike_01.png"
    Image.new("RGB", (8, 8), "red").save(render)

    relative = plan_tools._persist_spike_evidence(
        tmp_path,
        phase="draft",
        number=1,
        script_path=script,
        render_path=render,
        result={"rc": 0, "wall": 1.25},
    )

    assert relative == Path("plans/evidence/spikes/draft-spike-01.md")
    evidence = (tmp_path / relative).read_text(encoding="utf-8")
    assert "measured=0.5625" in evidence
    assert "script_sha256" in evidence
    assert (tmp_path / "plans/evidence/spikes/draft-spike-01.png").is_file()


def test_jit_spike_does_not_write_global_plan_evidence_without_workspace_marker(
    tmp_path: Path,
) -> None:
    script = tmp_path / "spike.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    script.with_suffix(".out").write_text("ok\n", encoding="utf-8")

    assert plan_tools._persist_spike_evidence(
        tmp_path,
        phase="layer-01",
        number=1,
        script_path=script,
        render_path=tmp_path / "missing.png",
        result={"rc": 0, "wall": 0.1},
    ) is None
    assert not (tmp_path / "plans/evidence").exists()


def _typed_spike_fixture(root: Path) -> tuple[str, dict]:
    evidence = root / "plans/evidence/spikes"
    evidence.mkdir(parents=True)
    script = b"print('probe')\n"
    output = b"Blender 5.0.0\nprobe\n"
    (evidence / "probe.py").write_bytes(script)
    (evidence / "probe.out").write_bytes(output)
    contract = {"id": "bbox-f36", "kind": "bbox_width", "frame": 36, "op": "band", "lo": 0.9, "hi": 1.3}
    _write(root / "scene_checks.json", {"schema": 2, "contracts": [contract]})
    record = {
        "schema": "vfx-harness.plan-spike/v1",
        "script": {"path": "probe.py", "sha256": hashlib.sha256(script).hexdigest()},
        "output": {"path": "probe.out", "sha256": hashlib.sha256(output).hexdigest()},
        "blender": {"executable": "/snap/bin/blender", "version": "Blender 5.0.0"},
        "contracts": [contract],
        "results": [{"id": "bbox-f36", "value": 1.0, "pass": True, "error": ""}],
        "passed": True,
    }
    _write(evidence / "probe.json", record)
    plan = (
        "**G10·T1 · Camera**  [known ✓spiked]\n"
        "- mechanism proof: `plans/evidence/spikes/probe.json`\n"
    )
    return plan, record


def test_unspiked_composition_ticket_is_not_a_plan_publication_blocker(tmp_path: Path) -> None:
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})

    findings, stats = _check_evidence(
        tmp_path,
        "**G10·T1 · Camera**  [known]\n- bbox is evaluated by the producing unit\n",
    )

    assert findings == []
    assert stats["spiked"] == 0


def test_spiked_claim_requires_exact_immutable_runtime_bytes(tmp_path: Path) -> None:
    plan, _record = _typed_spike_fixture(tmp_path)

    findings, stats = _check_evidence(tmp_path, plan)

    assert findings == []
    assert stats["spiked"] == 1

    (tmp_path / "plans/evidence/spikes/probe.out").write_text("changed\n", encoding="utf-8")
    findings, _ = _check_evidence(tmp_path, plan)
    assert any(finding.blocking and "stale output bytes" in finding.what for finding in findings)


def test_spiked_claim_cannot_cite_a_stale_contract_threshold(tmp_path: Path) -> None:
    plan, _record = _typed_spike_fixture(tmp_path)
    scene = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    scene["contracts"][0]["hi"] = 1.2
    _write(tmp_path / "scene_checks.json", scene)

    findings, _ = _check_evidence(tmp_path, plan)

    assert any(finding.blocking and "stale or narrower" in finding.what for finding in findings)


def test_gate_requires_temporal_and_composition_evidence(tmp_path: Path) -> None:
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
    assert any(f.check == "composition-coverage" and f.blocking for f in findings)
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


def test_required_unit_image_evidence_must_be_runnable_when_unit_completes(tmp_path: Path) -> None:
    doc = _layer_doc(temporal_id="trend")
    claim = doc["layers"][0]["stages"][0]["evaluation"]["claims"][0]
    claim["evidence"].append({"kind": "image_contract", "id": "final-look"})
    final = json.loads(json.dumps(doc["layers"][0]))
    final["id"] = "2"
    final["title"] = "Grade"
    final["script"] = "build/02_grade.py"
    final["stages"][0]["id"] = "grade"
    final["stages"][0]["plan"] = "plans/02_grade/01_grade.md"
    final["stages"][0]["evaluation"]["claims"] = []
    final["stages"][0]["evaluation"]["temporal_evidence"] = "none"
    doc["layers"].append(final)
    _write(tmp_path / "layers.json", doc)
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
    _write(
        tmp_path / "checks.json",
        {
            "schema": 2,
            "checks": [
                {
                    "id": "final-look",
                    "owner_layer": "1",
                    "fault_owner": "1",
                    "activates_at": "1",
                    "stage": "post_grade",
                }
            ],
        },
    )

    findings, _ = _check_evidence_coherence(tmp_path)

    assert any(f.check == "unit-evidence-due" and f.blocking for f in findings)


def test_control_selector_must_close_through_unit_mutation_authority(tmp_path: Path) -> None:
    doc = _layer_doc(temporal_id="order")
    _write(tmp_path / "layers.json", doc)
    _write(
        tmp_path / "scene_checks.json",
        {
            "schema": 2,
            "contracts": [
                {
                    "id": "order",
                    "kind": "onset_order",
                    "owner_layer": "1",
                    "fault_owner": "1",
                    "activates_at": "1",
                    "lifecycle": "layer",
                    "axis": "camera_framing",
                    "roles": ["hero"],
                    "compare_control_roles": ["late_group"],
                    "frames": [1, 2],
                    "op": "eq",
                    "value": 1,
                }
            ],
        },
    )
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)

    assert any(f.check == "control-selector-closure" and f.blocking for f in findings)


def test_control_ids_in_compare_roles_fail_role_selector_closure(tmp_path: Path) -> None:
    doc = _layer_doc(temporal_id="order")
    _write(tmp_path / "layers.json", doc)
    _write(
        tmp_path / "scene_checks.json",
        {
            "schema": 2,
            "contracts": [
                {
                    "id": "order",
                    "kind": "onset_order",
                    "owner_layer": "1",
                    "fault_owner": "1",
                    "activates_at": "1",
                    "lifecycle": "layer",
                    "axis": "camera_framing",
                    "roles": ["hero.*"],
                    "compare_roles": ["unlock_group_4"],
                    "frames": [1, 2],
                    "op": "eq",
                    "value": 1,
                }
            ],
        },
    )
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)

    assert any(f.check == "role-selector-closure" and f.blocking for f in findings)


def test_composition_blockout_cannot_be_measured_before_its_dependent_camera(tmp_path: Path) -> None:
    doc = _layer_doc(temporal_id="trend")
    move = doc["layers"][0]["stages"][0]
    proxy = json.loads(json.dumps(move))
    proxy["id"] = "blockout"
    proxy["title"] = "Composition blockout"
    proxy["plan"] = "plans/01_camera/00_blockout.md"
    proxy["depends_on"] = []
    proxy["evaluation"]["temporal_evidence"] = "none"
    proxy["evaluation"]["claims"][0]["id"] = "blockout-claim"
    proxy["evaluation"]["claims"][0]["repair_owner"] = "blockout"
    proxy["evaluation"]["claims"][0]["evidence"] = [
        {"kind": "scene_contract", "id": "bbox-1"},
        {"kind": "scene_contract", "id": "bbox-2"},
    ]
    move["depends_on"] = ["blockout"]
    move["evaluation"]["composition_context"] = {
        "frames": [1, 2], "source_unit": "blockout",
    }
    doc["layers"][0]["stages"] = [proxy, move]
    _write(tmp_path / "layers.json", doc)
    _write(tmp_path / "scene_checks.json", {
        "schema": 2,
        "contracts": [
            {"id": "trend", "kind": "radial_distance_trend"},
            {"id": "bbox-1", "kind": "bbox_width", "frame": 1, "activates_at": "1"},
            {"id": "bbox-2", "kind": "bbox_width", "frame": 2, "activates_at": "1"},
        ],
    })
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)

    assert not any(f.check == "composition-coverage" for f in findings)
    assert any(f.check == "composition-bootstrap" for f in findings)


def test_atomic_camera_and_blockout_closes_empty_scene_composition_bootstrap(tmp_path: Path) -> None:
    doc = _layer_doc(temporal_id="trend")
    move = doc["layers"][0]["stages"][0]
    move["mutates"]["roles"] = ["camera", "hero"]
    move["evaluation"]["composition_context"] = {
        "frames": [1, 2],
        "contract_ids": ["bbox-1", "bbox-2"],
    }
    _write(tmp_path / "layers.json", doc)
    _write(
        tmp_path / "scene_checks.json",
        {
            "schema": 2,
            "contracts": [
                {"id": "trend", "kind": "radial_distance_trend"},
                {"id": "bbox-1", "kind": "bbox_width", "frame": 1, "activates_at": "1"},
                {"id": "bbox-2", "kind": "bbox_width", "frame": 2, "activates_at": "1"},
            ],
        },
    )
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)

    assert not any(
        finding.check in {"composition-coverage", "composition-bootstrap"}
        for finding in findings
    )


def test_visible_fraction_may_observe_roles_it_does_not_mutate(tmp_path: Path) -> None:
    """HIR-0019 / HIR-0051: a camera unit may observe plan-declared geometry vis.

    Observation vis is camera-or-mutator, not any unit. A name the plan never
    declares anywhere is still a violation."""
    doc = _layer_doc(temporal_id="vis")
    scout = json.loads(json.dumps(doc["layers"][0]["stages"][0]))
    scout["id"] = "blockout"
    scout["plan"] = "plans/01_camera/00_blockout.md"
    scout["mutates"] = {**scout["mutates"], "roles": ["proxy.interior"], "controls": []}
    scout["evaluation"]["claims"] = []
    doc["layers"][0]["stages"].insert(0, scout)
    doc["layers"][0]["stages"][1]["provides"] = ["camera"]
    _write(tmp_path / "layers.json", doc)

    def vis_row(row_id: str, roles: list[str]) -> dict:
        return {
            "id": row_id, "kind": "visible_fraction", "owner_layer": "1",
            "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
            "axis": "camera_framing", "roles": roles, "frame": 1,
            "op": "min", "lo": 0.25,
        }

    _write(tmp_path / "scene_checks.json", {
        "schema": 2,
        "contracts": [vis_row("vis", ["proxy.interior"])],
    })
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})
    findings, _ = _check_evidence_coherence(tmp_path)
    assert not any(f.check == "role-selector-closure" for f in findings)
    assert not any(f.check == "vis-repair-owner" for f in findings)

    _write(tmp_path / "scene_checks.json", {
        "schema": 2,
        "contracts": [vis_row("vis", ["never.declared.anywhere"])],
    })
    findings, _ = _check_evidence_coherence(tmp_path)
    assert any(
        f.check == "role-selector-closure" and "never.declared.anywhere" in f.what
        for f in findings
    )


def test_dressing_authority_is_granted_by_the_owner(tmp_path: Path) -> None:
    """ADR-0007: run af3084's composed frames were layer 1's naked proxies — the
    gunmetal existed on layer 2's own subjects because no unit could legally assign
    materials to another layer's geometry. A unit declares mutates.dresses; the gate
    blocks any selector no OTHER layer lists under dressable."""
    doc = _layer_doc(temporal_id=None)
    unit = doc["layers"][0]["stages"][0]
    unit["mutates"] = {**unit["mutates"], "dresses": ["proxy.masses"]}
    unit["evaluation"]["claims"][0]["subject_roles"] = ["hero", "proxy.masses"]
    _write(tmp_path / "layers.json", doc)
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)
    assert any(
        f.check == "dressing-closure" and f.blocking and "proxy.masses" in f.what
        for f in findings
    ), [str(f) for f in findings]

    owner = json.loads(json.dumps(doc["layers"][0]))
    owner["id"] = "0"
    owner["script"] = "build/00_blockout.py"
    owner["owns"] = []
    owner["stages"] = []
    owner["dressable"] = ["proxy.masses"]
    doc["layers"].insert(0, owner)
    _write(tmp_path / "layers.json", doc)
    findings, _ = _check_evidence_coherence(tmp_path)
    assert not any(f.check == "dressing-closure" for f in findings), [str(f) for f in findings]


def test_another_layers_stuck_state_does_not_block_this_layers_plan() -> None:
    """Run bwng97m5n: layer 1's amendment generated a clean unit plan and died on
    'layer 2 has no ready unit' — a state-progress finding the layer-2 transaction
    owns. Layer-scoped findings block their own layer's generation and the global
    verdict, never a sibling's plan."""
    from vfx_harness.evaluation.plan_gate import Finding, GateResult

    result = GateResult(
        "shot",
        findings=[
            Finding("hierarchy", True, "layer 2 work-unit DAG", "no work unit is ready",
                    "replan", layer="2"),
        ],
    )
    assert not result.clean
    assert result.clean_for("1")
    assert not result.clean_for("2")

    plan_wide = GateResult(
        "shot",
        findings=[Finding("contracts", True, "scene_checks.json", "bad row", "fix")],
    )
    assert not plan_wide.clean_for("1")
