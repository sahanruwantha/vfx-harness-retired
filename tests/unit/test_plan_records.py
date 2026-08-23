from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from vfx_harness.evaluation.plan_gate import _check_meta_records
from vfx_harness.evidence.checks import acceptance_evidence
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.jit_materialization import (
    publish_materialization,
    validate_materialization,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.plan_authority import publish_current
from vfx_harness.orchestration.plan_due import (
    PlanDueError,
    require_due_clear,
    resolve_acceptance_completion,
    resolve_unit_completion,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _candidate(root: Path) -> None:
    (root / "brief.md").write_text("Final image must hold unchanged from frame 239 to 240.\n", encoding="utf-8")
    (root / "refs").mkdir()
    (root / "plans").mkdir()
    (root / "plans" / "global.md").write_text("# executable fixture plan\n", encoding="utf-8")
    _write(root / "layers.json", {
        "schema": 4,
        "layers": [{
            "id": "1", "script": "build/01_finish.py", "title": "Finish",
            "primary_judge": 240,
            "judge": [{"frame": 239, "ref": "refs/a.png"}, {"frame": 240, "ref": "refs/a.png"}],
            "owns": ["final_lock"], "reads": "locked ending",
            "evidence_domains": ["scene", "temporal"],
            "stages": [{
                "id": "lock", "title": "Lock", "plan": "plans/01_finish/lock.md",
                "depends_on": [],
                "mutates": {"mode": "scoped", "roles": ["comp"], "controls": ["hold"],
                            "control_roles": {"hold": ["comp"]},
                            "script_spans": ["build/units/01_finish/lock.py"]},
                "protects": {"selector": "all_active_upstream_interfaces",
                             "resolve_to_explicit_ids_at": "freeze"},
                "evaluation": {"primary_judge": 240,
                               "judge": [{"frame": 239, "ref": "refs/a.png"},
                                         {"frame": 240, "ref": "refs/a.png"}],
                               "temporal_evidence": "keyframes",
                               "claims": [{
                                   "id": "lock-claim", "proposition": "ending locks",
                                   "axis": "final_lock", "property": "frame_delta",
                                   "subject_roles": ["comp"], "subject_controls": ["hold"],
                                   "moments": [239, 240], "kind": "atomic", "required": True,
                                   "authority": "executable_required", "repair_owner": "lock",
                                   "evidence": [{"kind": "scene_contract", "id": "final-lock"}],
                               }]},
                "completion": "all_required_claims_and_protected_contracts_pass",
            }],
        }],
    })
    _write(root / "acceptance.json", [])
    _write(root / "critic_axes.json", [{"key": "final_lock", "desc": "ending is still"}])
    _write(root / "checks.json", {"schema": 2, "checks": []})
    _write(root / "scene_checks.json", {"schema": 2, "contracts": [{
        "id": "final-lock", "kind": "frame_delta", "owner_layer": "1",
        "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
        "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
    }]})
    digest = hashlib.sha256((root / "brief.md").read_bytes()).hexdigest()
    _write(root / "requirements.json", {
        "schema": "vfx-harness.requirements/v1",
        "requirements": [{
            "id": "R-final-lock", "statement": "frames 239 and 240 are unchanged",
            "citation": {"source": "brief.md", "sha256": digest, "line_start": 1, "line_end": 1},
            "resolution": {"kind": "obligation", "ids": ["O-final-lock"]},
        }],
    })
    _write(root / "obligations.json", {
        "schema": "vfx-harness.obligations/v1",
        "obligations": [{
            "id": "O-final-lock", "statement": "prove the 239 to 240 rendered lock",
            "requirement_ids": ["R-final-lock"], "owner": "1.lock",
            "due": {"kind": "before_acceptance"},
            "evidence": [{"kind": "scene_contract", "id": "final-lock"}],
        }],
    })
    _write(root / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1", "assumptions": [],
    })


def _add_deferred_layer(root: Path) -> None:
    data = json.loads((root / "layers.json").read_text(encoding="utf-8"))
    data["layers"][0]["execution"] = "ready"
    data["layers"].append({
        "id": "2", "script": "build/02_polish.py", "title": "Polish",
        "primary_judge": 240,
        "judge": [{"frame": 239, "ref": "refs/a.png"}, {"frame": 240, "ref": "refs/a.png"}],
        "owns": ["final_lock"], "reads": "polished ending",
        "evidence_domains": ["scene", "temporal"],
        "execution": "jit_deferred", "stages": [],
        "jit": {
            "depends_on_layers": ["1"],
            "required_outcomes": [{"kind": "scene_contract", "id": "final-lock"}],
            "reserved_roles": ["polish.*"],
            "promises": [{
                "id": "JIT-polish-lock", "contract_kind": "frame_delta",
                "moments": [239, 240], "requirement_ids": ["R-final-lock"],
            }],
        },
    })
    _write(root / "layers.json", data)


def _jit_payload(root: Path, bundle_hash: str) -> Path:
    layers = json.loads((root / "layers.json").read_text(encoding="utf-8"))["layers"]
    layer = dict(layers[1])
    layer["execution"] = "ready"
    layer.pop("jit")
    layer["stages"] = [{
        "id": "polish", "title": "Polish lock", "plan": "plans/02_polish/polish.md",
        "depends_on": [],
        "mutates": {"mode": "scoped", "roles": ["polish.comp"], "controls": ["hold"],
                    "control_roles": {"hold": ["polish.comp"]},
                    "script_spans": ["build/units/02_polish/polish.py"]},
        "protects": {"selector": "all_active_upstream_interfaces",
                     "resolve_to_explicit_ids_at": "freeze"},
        "evaluation": {"primary_judge": 240, "judge": layer["judge"],
                       "temporal_evidence": "keyframes", "claims": [{
                           "id": "polish-lock", "proposition": "polish preserves the lock",
                           "axis": "final_lock", "property": "frame_delta",
                           "subject_roles": ["polish.comp"], "subject_controls": ["hold"],
                           "moments": [239, 240], "kind": "atomic", "required": True,
                           "authority": "executable_required", "repair_owner": "polish",
                           "evidence": [{"kind": "scene_contract", "id": "polish-lock"}],
                       }]},
        "completion": "all_required_claims_and_protected_contracts_pass",
    }]
    path = root / "jit.json"
    _write(path, {
        "schema": "vfx-harness.jit-layer-materialization/v1",
        "bundle_hash": bundle_hash,
        "layer": layer,
        "scene_contracts": [{
            "id": "polish-lock", "kind": "frame_delta", "owner_layer": "2",
            "fault_owner": "2", "activates_at": "2", "lifecycle": "layer",
            "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
        }],
        "image_contracts": [],
        "promise_bindings": [{
            "promise_id": "JIT-polish-lock", "kind": "scene_contract",
            "contract_id": "polish-lock",
        }],
    })
    return path


def _passed_layer_one_outcome(root: Path) -> None:
    _write(root / "plans" / "outcomes" / "01.json", {
        "schema": 2, "layer": "1", "status": "passed",
        "interfaces": [{"id": "final-lock", "pass": True}],
    })


def test_deferred_layer_has_no_fake_units_and_materializes_through_bound_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    parsed = load_layers_from_path(tmp_path / "layers.json")
    assert parsed["2"].execution == "jit_deferred"
    assert parsed["2"].stages == ()
    assert not (tmp_path / "state" / "units" / "2.json").exists()

    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    _passed_layer_one_outcome(tmp_path)
    publish_materialization(tmp_path, payload)

    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    materialized = load_layers_from_path(selected_artifact_path(tmp_path, "layers.json"))
    assert materialized["2"].execution == "ready"
    assert [unit.id for unit in materialized["2"].stages] == ["polish"]
    assert not (tmp_path / "state" / "units" / "2.json").exists()


def test_jit_materialization_fails_closed_on_unbound_promise(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["promise_bindings"] = []
    _write(payload, data)

    with pytest.raises(ValueError, match="missing JIT-polish-lock"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_jit_materialization_waits_for_declared_upstream_outcome(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    with pytest.raises(ValueError, match="dependency 1 has a sealed outcome"):
        publish_materialization(tmp_path, payload)


def test_final_lock_is_typed_and_blocks_acceptance_until_matching_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    findings, stats = _check_meta_records(tmp_path)
    assert findings == []
    assert stats == {"requirements": 1, "open_obligations": 1, "open_assumptions": 0}
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")

    with pytest.raises(PlanDueError, match="O-final-lock"):
        require_due_clear(tmp_path, acceptance=True)

    resolutions = tmp_path / "state" / "plan-resolutions.jsonl"
    resolutions.parent.mkdir()
    resolutions.write_text(json.dumps({
        "schema": "vfx-harness.plan-resolutions/v1",
        "bundle_hash": bundle.content_hash,
        "kind": "obligation", "id": "O-final-lock", "status": "satisfied",
        "evidence": [{"kind": "scene_contract", "id": "wrong-contract"}],
    }) + "\n", encoding="utf-8")
    with pytest.raises(PlanDueError, match="O-final-lock"):
        require_due_clear(tmp_path, acceptance=True)

    with resolutions.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": bundle.content_hash,
            "kind": "obligation", "id": "O-final-lock", "status": "satisfied",
            "evidence": [{"kind": "scene_contract", "id": "final-lock"}],
        }) + "\n")
    require_due_clear(tmp_path, acceptance=True)


def test_requirement_resolution_cannot_name_an_unknown_obligation(tmp_path: Path) -> None:
    _candidate(tmp_path)
    data = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    data["requirements"][0]["resolution"]["ids"] = ["missing"]
    _write(tmp_path / "requirements.json", data)

    findings, _ = _check_meta_records(tmp_path)

    assert any(f.blocking and "unknown obligation" in f.what for f in findings)


def test_uncited_brief_clause_blocks_register_completeness(tmp_path: Path) -> None:
    _candidate(tmp_path)
    (tmp_path / "brief.md").write_text(
        "Final image must hold unchanged from frame 239 to 240.\n\n"
        "| Beat | Required action |\n"
        "|---|---|\n"
        "| Final | End with a clean two-frame lock. |\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256((tmp_path / "brief.md").read_bytes()).hexdigest()
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["citation"]["sha256"] = digest
    _write(tmp_path / "requirements.json", requirements)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.check == "requirement-completeness"
        and "lines 5" in finding.what
        for finding in findings
    )


def test_terminal_hold_cannot_resolve_to_a_single_frame_proxy(tmp_path: Path) -> None:
    _candidate(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "contract",
        "ids": ["final-brightness"],
    }
    _write(tmp_path / "requirements.json", requirements)
    checks = {
        "schema": 2,
        "checks": [{
            "id": "final-brightness",
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "1",
            "lifecycle": "layer",
            "axis": "final_lock",
            "frame": 240,
            "ref": "refs/a.png",
            "metric": "frame_mean",
            "op": "band",
            "lo": 1,
            "hi": 255,
            "stage": "pre_grade",
            "rejects": ["artifacts/bad.png"],
            "proof": {"ref": 100, "adversary": [0]},
        }],
    }
    _write(tmp_path / "checks.json", checks)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.check == "temporal-requirement"
        and "239→240" in finding.what
        for finding in findings
    )


def test_explicit_motion_law_cannot_be_downgraded_to_a_prose_decision(tmp_path: Path) -> None:
    _candidate(tmp_path)
    (tmp_path / "brief.md").write_text(
        "Final image must hold unchanged from frame 239 to 240.\n\n"
        "Light travels through conduits in expanding waves rather than all at once.\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256((tmp_path / "brief.md").read_bytes()).hexdigest()
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["citation"]["sha256"] = digest
    requirements["requirements"].append({
        "id": "R-pulse",
        "statement": "light travels through conduits in expanding waves",
        "citation": {"source": "brief.md", "sha256": digest, "line_start": 3, "line_end": 3},
        "resolution": {
            "kind": "decision",
            "ids": [],
            "decision": "animate the conduit light in the ignition unit",
        },
    })
    _write(tmp_path / "requirements.json", requirements)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.check == "temporal-requirement"
        and "explicit motion law" in finding.what
        for finding in findings
    )


def test_structured_human_decision_must_be_adopted_exactly_by_required_contract(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    expected = {
        "kind": "keyframe_schedule",
        "roles": ["cam_rig"],
        "samples": [
            {"frame": 1, "values": {"location": [0, -6, 0]}},
            {"frame": 240, "values": {"location": [0, 225, 0]}},
        ],
        "op": "max",
        "hi": 0.001,
    }
    (state / "plan-resolutions.jsonl").write_text(json.dumps({
        "schema": "vfx-harness.plan-resolutions/v1",
        "bundle_hash": "prior-bundle",
        "kind": "assumption",
        "id": "A-camera",
        "status": "satisfied",
        "evidence": [{"kind": "human_decision", "id": "user-approved-camera"}],
        "decision": "approved exact camera spine",
        "values": {"contract": expected},
    }) + "\n", encoding="utf-8")

    findings, _ = _check_meta_records(tmp_path)
    assert any(
        finding.blocking
        and finding.check == "decision-adoption"
        and "A-camera" in finding.where
        for finding in findings
    )

    scene = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    scene["contracts"].append({
        "id": "camera-spine",
        "decision_id": "A-camera",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "persistent",
        "axis": "final_lock",
        **expected,
    })
    _write(tmp_path / "scene_checks.json", scene)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][0]["stages"][0]["mutates"]["roles"].append("cam_rig")
    layers["layers"][0]["stages"][0]["evaluation"]["claims"].append({
        "id": "camera-spine-claim",
        "proposition": "camera follows the approved exact schedule",
        "axis": "final_lock",
        "property": "keyframe_schedule",
        "subject_roles": ["cam_rig"],
        "subject_controls": [],
        "moments": [1, 240],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "lock",
        "evidence": [{"kind": "scene_contract", "id": "camera-spine"}],
    })
    _write(tmp_path / "layers.json", layers)

    findings, _ = _check_meta_records(tmp_path)
    assert not any(finding.check == "decision-adoption" for finding in findings)


def test_exact_reassembly_uses_the_prefracture_frame_as_its_baseline(tmp_path: Path) -> None:
    _candidate(tmp_path)
    (tmp_path / "brief.md").write_text(
        "---\n"
        "id: fixture\n"
        "title: Fixture\n"
        "type: motion\n"
        "frames: 240\n"
        "fps: 24\n"
        "resolution: [1920, 1080]\n"
        "engine: BLENDER_EEVEE_NEXT\n"
        "palette: [black]\n"
        "---\n\n"
        "Final image must hold unchanged from frame 239 to 240.\n\n"
        "| Beat | Frames | Required action |\n"
        "|---|---:|---|\n"
        "| B5 — Fracture | 169–204 | The core fractures the architecture. |\n"
        "| B6 — Reassembly | 205–232 | Pieces return to their exact structural positions. |\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256((tmp_path / "brief.md").read_bytes()).hexdigest()
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["citation"]["sha256"] = digest
    requirements["requirements"][0]["citation"]["line_start"] = 12
    requirements["requirements"][0]["citation"]["line_end"] = 12
    requirements["requirements"].extend([
        {
            "id": "R-fracture",
            "statement": "fracture spans 169 through 204",
            "citation": {"source": "brief.md", "sha256": digest, "line_start": 16, "line_end": 16},
            "resolution": {"kind": "decision", "ids": [], "decision": "fracture timing"},
        },
        {
            "id": "R-return",
            "statement": "pieces return exactly",
            "citation": {"source": "brief.md", "sha256": digest, "line_start": 17, "line_end": 17},
            "resolution": {"kind": "contract", "ids": ["return-delta"]},
        },
    ])
    _write(tmp_path / "requirements.json", requirements)
    scene = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    scene["contracts"].append({
        "id": "return-delta",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "axis": "final_lock",
        "kind": "transform_return_delta",
        "roles": ["comp"],
        "frames": [224, 240],
        "component": "location",
        "op": "max",
        "hi": 0.01,
    })
    _write(tmp_path / "scene_checks.json", scene)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][0]["stages"][0]["evaluation"]["claims"].append({
        "id": "return-claim",
        "proposition": "pieces return exactly",
        "axis": "final_lock",
        "property": "transform_return_delta",
        "subject_roles": ["comp"],
        "subject_controls": [],
        "moments": [224, 240],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "lock",
        "evidence": [{"kind": "scene_contract", "id": "return-delta"}],
    })
    _write(tmp_path / "layers.json", layers)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.check == "temporal-requirement"
        and "baseline 168" in finding.what
        for finding in findings
    )


def test_entry_gate_cannot_depend_on_contract_produced_by_same_unit(tmp_path: Path) -> None:
    _candidate(tmp_path)
    data = json.loads((tmp_path / "obligations.json").read_text(encoding="utf-8"))
    data["obligations"][0]["due"] = {
        "kind": "before_unit",
        "layer": "1",
        "unit": "lock",
    }
    _write(tmp_path / "obligations.json", data)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.where == "O-final-lock"
        and "evidence produced by the gated layer" in finding.what
        for finding in findings
    )


def test_unit_completion_obligation_is_discharged_only_by_declared_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    data = json.loads((tmp_path / "obligations.json").read_text(encoding="utf-8"))
    data["obligations"][0]["due"] = {
        "kind": "unit_completion",
        "layer": "1",
        "unit": "lock",
    }
    _write(tmp_path / "obligations.json", data)
    findings, _ = _check_meta_records(tmp_path)
    assert findings == []
    layout = run_artifacts.create(tmp_path, "plan-run")
    publish_current(tmp_path, layout, outcome="clean_with_deferred")

    require_due_clear(tmp_path, layer="1", unit="lock")
    with pytest.raises(PlanDueError, match=r"completion.*O-final-lock"):
        require_due_clear(tmp_path, layer="1", unit="lock", completion=True)

    assert resolve_unit_completion(
        tmp_path,
        layer="1",
        unit="lock",
        passed_evidence={("scene_contract", "wrong-contract")},
    ) == ()
    assert resolve_unit_completion(
        tmp_path,
        layer="1",
        unit="lock",
        passed_evidence={("scene_contract", "final-lock")},
    ) == ("O-final-lock",)
    require_due_clear(tmp_path, layer="1", unit="lock", completion=True)


def test_assumption_cannot_use_machine_completion_gate(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _write(tmp_path / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1",
        "assumptions": [{
            "id": "A-calibration",
            "statement": "use a provisional calibration",
            "requirement_ids": [],
            "owner": "PLAN",
            "due": {"kind": "unit_completion", "layer": "1", "unit": "lock"},
            "impact": {"layers": ["1"], "axes": ["final_lock"], "global_decision": False},
        }],
    })

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.where == "A-calibration"
        and "cannot be machine-resolved" in finding.what
        for finding in findings
    )


def test_provisional_start_requires_a_named_runtime_falsification_path(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _write(tmp_path / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1",
        "assumptions": [{
            "id": "A-camera",
            "statement": "start from the approved camera spine",
            "requirement_ids": [],
            "owner": "PLAN",
            "decision_strength": "approved_start",
            "due": {"kind": "before_layer", "layer": "1"},
            "impact": {"layers": ["1"], "axes": ["final_lock"], "global_decision": False},
        }],
    })

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and "requires falsification.owner and contract_ids" in finding.what
        for finding in findings
    )


def test_legacy_assumption_defaults_to_hard_constraint_not_tunable_start(tmp_path: Path) -> None:
    from vfx_harness.domain.plan_records import load_assumptions

    _candidate(tmp_path)
    _write(tmp_path / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1",
        "assumptions": [{
            "id": "A-aspect",
            "statement": "use 16:9",
            "requirement_ids": [],
            "owner": "PLAN",
            "due": {"kind": "before_layer", "layer": "1"},
            "impact": {"layers": ["1"], "axes": [], "global_decision": True},
        }],
    })

    assert load_assumptions(tmp_path)[0].decision_strength == "hard_constraint"


def test_acceptance_obligation_blocks_verdict_not_acceptance_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    publish_current(tmp_path, layout, outcome="clean_with_deferred")

    # The finished-chain renderer must be allowed to run and produce this evidence.
    require_due_clear(
        tmp_path,
        acceptance=True,
        record_kinds=frozenset({"assumption"}),
    )
    with pytest.raises(PlanDueError, match="O-final-lock"):
        require_due_clear(tmp_path, acceptance=True)

    assert resolve_acceptance_completion(
        tmp_path,
        passed_evidence={("scene_contract", "wrong-contract")},
    ) == ()
    assert resolve_acceptance_completion(
        tmp_path,
        passed_evidence={("scene_contract", "final-lock")},
    ) == ("O-final-lock",)
    require_due_clear(tmp_path, acceptance=True)


def test_unit_completion_requires_a_required_claim_in_the_due_unit(tmp_path: Path) -> None:
    _candidate(tmp_path)
    obligations = json.loads((tmp_path / "obligations.json").read_text(encoding="utf-8"))
    obligations["obligations"][0]["due"] = {
        "kind": "unit_completion",
        "layer": "1",
        "unit": "lock",
    }
    _write(tmp_path / "obligations.json", obligations)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][0]["stages"][0]["evaluation"]["claims"][0]["required"] = False
    layers["layers"][0]["stages"][0]["evaluation"]["claims"][0]["authority"] = "advisory"
    _write(tmp_path / "layers.json", layers)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.where == "O-final-lock"
        and "not bound to required claims" in finding.what
        for finding in findings
    )


def test_acceptance_evidence_evaluates_post_grade_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render = tmp_path / "finished.png"
    Image.new("RGB", (16, 16), (100, 100, 100)).save(render)
    checks = tmp_path / "checks.json"
    _write(checks, {
        "schema": 2,
        "checks": [{
            "id": "finished-exposure",
            "metric": "frame_mean",
            "op": "band",
            "lo": 90,
            "hi": 110,
            "frame": 240,
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "1",
            "lifecycle": "acceptance",
            "axis": "exposure",
            "stage": "post_grade",
            "rejects": ["artifacts/bad.png"],
            "proof": {"adversary": [20.0]},
        }],
    })
    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_authority.selected_artifact_path",
        lambda _root, name: checks if name == "checks.json" else tmp_path / name,
    )

    rows = acceptance_evidence(tmp_path, frame=240, render=render)

    assert len(rows) == 1
    assert rows[0]["id"] == "finished-exposure"
    assert rows[0]["pass"] is True
    assert rows[0]["authoritative"] is True
    assert rows[0]["source"] == "image_contract"
