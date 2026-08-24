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
                                   "asserts": "image",
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
            "owned_requirements": ["R-final-lock"],
        },
    })
    _write(root / "layers.json", data)
    requirements = json.loads((root / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "deferred_owner", "ids": [], "owner_layer": "2",
        "due": {"kind": "before_layer", "layer": "2"},
    }
    _write(root / "requirements.json", requirements)
    _write(root / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })


def _declaring(layer: dict) -> dict:
    """Materialized stages must declare look_capabilities; [] owns no appearance."""
    row = dict(layer)
    row["stages"] = [
        {**stage, "look_capabilities": stage.get("look_capabilities", [])}
        for stage in row.get("stages") or []
    ]
    return row


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
                           "asserts": "image",
                           "evidence": [{"kind": "scene_contract", "id": "polish-lock"}],
                       }]},
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": [],
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
        "requirement_bindings": [{
            "requirement_id": "R-final-lock", "contract_ids": ["polish-lock"],
        }],
        "acceptance": [],
    })
    return path


def _passed_layer_one_outcome(root: Path) -> None:
    _write(root / "plans" / "outcomes" / "01.json", {
        "schema": 2, "layer": "1", "status": "passed",
        "interfaces": [{"id": "final-lock", "pass": True}],
    })


def test_deferred_root_needs_no_fictional_upstream_outcome(tmp_path: Path) -> None:
    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    document["schema"] = 5
    layer = document["layers"][0]
    layer["execution"] = "jit_deferred"
    layer["stages"] = []
    layer["jit"] = {
        "depends_on_layers": [],
        "required_outcomes": [],
        "reserved_roles": ["product.*"],
        "owned_requirements": ["R-final-lock"],
    }
    _write(tmp_path / "layers.json", document)

    parsed = load_layers_from_path(tmp_path / "layers.json")

    assert parsed["1"].execution == "jit_deferred"
    assert parsed["1"].jit.depends_on_layers == ()
    assert parsed["1"].jit.required_outcomes == ()


def test_deferred_root_materializes_without_fabricated_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    ready_layer = document["layers"][0]
    deferred_layer = {**ready_layer, "execution": "jit_deferred", "stages": []}
    deferred_layer["jit"] = {
        "depends_on_layers": [],
        "required_outcomes": [],
        "reserved_roles": ["comp"],
        "owned_requirements": ["R-final-lock"],
    }
    document["schema"] = 5
    document["layers"] = [deferred_layer]
    _write(tmp_path / "layers.json", document)
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "deferred_owner", "ids": [], "owner_layer": "1",
        "due": {"kind": "before_layer", "layer": "1"},
    }
    _write(tmp_path / "requirements.json", requirements)
    _write(tmp_path / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })
    layout = run_artifacts.create(tmp_path, "root-materialization")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = tmp_path / "root-jit.json"
    _write(payload, {
        "schema": "vfx-harness.jit-layer-materialization/v1",
        "bundle_hash": bundle.content_hash,
        "layer": _declaring({**ready_layer, "execution": "ready"}),
        "scene_contracts": [{
            "id": "final-lock", "kind": "frame_delta", "owner_layer": "1",
            "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
            "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
        }],
        "image_contracts": [],
        "requirement_bindings": [{
            "requirement_id": "R-final-lock", "contract_ids": ["final-lock"],
        }],
        "acceptance": [],
    })

    pointer = publish_materialization(tmp_path, payload)

    assert pointer.is_file()
    selected = json.loads(pointer.read_text(encoding="utf-8"))
    assert selected["materialized_layers"] == ["1"]


def test_schema_five_global_publication_rejects_ready_preproduction(tmp_path: Path) -> None:
    from vfx_harness.evaluation.plan_gate import _check_contracts

    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    document["schema"] = 5
    _write(tmp_path / "layers.json", document)

    findings, _ = _check_contracts(tmp_path)

    assert any(
        finding.check == "global-preproduction" and "ready layers: 1" in finding.what
        for finding in findings
    )


@pytest.mark.parametrize(
    ("title", "reserved_role", "axis"),
    [
        ("Product foundation", "product.*", "product_shape"),
        ("Camera foundation", "camera.*", "camera_framing"),
    ],
)
def test_unit_first_global_bundle_is_clean_for_heterogeneous_roots(
    tmp_path: Path, title: str, reserved_role: str, axis: str
) -> None:
    from vfx_harness.evaluation.plan_gate import run

    brief = (
        "---\n"
        "id: heterogeneous-root\n"
        "frames: 1\n"
        "fps: 24\n"
        "---\n"
        "The delivered image must preserve the approved visual target.\n"
    )
    (tmp_path / "brief.md").write_text(brief, encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "target.png").write_bytes(b"fixture")
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "global.md").write_text(
        f"# Unit-first publication\n\n1. {title}: owns `{axis}`.\n", encoding="utf-8"
    )
    _write(tmp_path / "layers.json", {
        "schema": 5,
        "layers": [{
            "id": "1", "script": "build/01_foundation.py", "title": title,
            "primary_judge": 1,
            "judge": [{"frame": 1, "ref": "refs/target.png"}],
            "owns": [axis], "reads": "approved target", "evidence_domains": ["image"],
            "execution": "jit_deferred", "stages": [],
            "jit": {
                "depends_on_layers": [], "required_outcomes": [],
                "reserved_roles": [reserved_role], "owned_requirements": ["R1"],
            },
        }],
    })
    _write(tmp_path / "acceptance.json", [])
    _write(tmp_path / "critic_axes.json", [{"key": axis, "desc": "approved visual target"}])
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    digest = hashlib.sha256((tmp_path / "brief.md").read_bytes()).hexdigest()
    _write(tmp_path / "requirements.json", {
        "schema": "vfx-harness.requirements/v1",
        "requirements": [{
            "id": "R1", "statement": "The delivered image must preserve the approved visual target.",
            "citation": {
                "source": "brief.md", "sha256": digest, "line_start": 6, "line_end": 6,
            },
            "resolution": {
                "kind": "deferred_owner", "ids": [], "owner_layer": "1",
                "due": {"kind": "before_layer", "layer": "1"},
            },
        }],
    })
    _write(tmp_path / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })
    _write(tmp_path / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1", "assumptions": [],
    })

    result = run(tmp_path)

    assert result.blocking == []


def test_jit_materialization_rejects_candidate_sensitive_image_contracts(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    document["image_contracts"] = [{"id": "premature-image-check", "owner_layer": "2"}]
    _write(payload, document)

    with pytest.raises(ValueError, match="candidate-sensitive image checks"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


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
    requirements = json.loads(
        selected_artifact_path(tmp_path, "requirements.json").read_text(encoding="utf-8")
    )
    assert requirements["requirements"][0]["resolution"] == {
        "kind": "contract", "ids": ["polish-lock"],
    }
    assert not (tmp_path / "state" / "units" / "2.json").exists()


def test_jit_materialization_fails_closed_on_unbound_requirement(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["requirement_bindings"] = []
    _write(payload, data)

    with pytest.raises(ValueError, match=r"missing R-final-lock"):
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


def test_typed_promises_are_rejected_by_the_loader(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][1]["jit"]["promises"] = [{
        "id": "L2.JIT-P1", "contract_kind": "frame_delta", "moments": [239, 240],
        "requirement_ids": ["R-final-lock"],
    }]
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError, match="superseded"):
        load_layers_from_path(tmp_path / "layers.json")


def _structured_camera_decision(root: Path) -> dict:
    state = root / "state"
    state.mkdir(exist_ok=True)
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
    return expected


def _all_deferred_schema5(root: Path, reserved: list[str]) -> None:
    data = json.loads((root / "layers.json").read_text(encoding="utf-8"))
    data["schema"] = 5
    for row in data["layers"]:
        row["execution"] = "jit_deferred"
        row["stages"] = []
        row.setdefault("jit", {
            "depends_on_layers": [], "required_outcomes": [],
            "reserved_roles": ["placeholder.*"], "owned_requirements": ["R-final-lock"],
        })
    data["layers"][0]["jit"]["reserved_roles"] = reserved
    _write(root / "layers.json", data)


def test_unit_first_decision_adoption_defers_to_owning_layer(tmp_path: Path) -> None:
    """A schema-5 bundle publishes no contracts, so demanding the exact adopted copy at
    global time deadlocks against the global-preproduction rule (run
    20260823T095834Z-7040c2). The global obligation narrows to ownership; the verbatim
    copy is enforced by validate_materialization at the owning layer."""
    _candidate(tmp_path)
    _structured_camera_decision(tmp_path)
    _all_deferred_schema5(tmp_path, reserved=["cam_rig", "camera.*"])

    findings, _ = _check_meta_records(tmp_path)
    assert not any(finding.check == "decision-adoption" for finding in findings)


def test_unit_first_unowned_decision_roles_still_block(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _structured_camera_decision(tmp_path)
    _all_deferred_schema5(tmp_path, reserved=["iris.*"])

    findings, _ = _check_meta_records(tmp_path)
    assert any(
        finding.blocking
        and finding.check == "decision-adoption"
        and "A-camera" in finding.where
        and "no deferred layer reserves" in finding.what
        for finding in findings
    )


def test_materialization_must_adopt_owned_structured_decision(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    expected = {
        "kind": "keyframe_schedule",
        "roles": ["polish.comp"],
        "samples": [
            {"frame": 239, "values": {"location": [0, 0, 0]}},
            {"frame": 240, "values": {"location": [0, 0, 0]}},
        ],
        "op": "max",
        "hi": 0.001,
    }
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "plan-resolutions.jsonl").write_text(json.dumps({
        "schema": "vfx-harness.plan-resolutions/v1",
        "bundle_hash": bundle.content_hash,
        "kind": "assumption",
        "id": "A-polish",
        "status": "satisfied",
        "evidence": [{"kind": "human_decision", "id": "user-approved-polish"}],
        "decision": "approved polish hold",
        "values": {"contract": expected},
    }) + "\n", encoding="utf-8")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    with pytest.raises(ValueError, match="must adopt structured decision A-polish"):
        validate_materialization(
            bundle.root, payload,
            expected_bundle_hash=bundle.content_hash,
            resolutions_path=state / "plan-resolutions.jsonl",
        )

    data = json.loads(payload.read_text(encoding="utf-8"))
    data["scene_contracts"].append({
        "id": "polish-spine", "decision_id": "A-polish", "owner_layer": "2",
        "fault_owner": "2", "activates_at": "2", "lifecycle": "persistent",
        "axis": "final_lock", **expected,
    })
    data["layer"]["stages"][0]["evaluation"]["claims"].append({
        "id": "polish-spine-claim",
        "proposition": "polish keys the approved hold exactly",
        "axis": "final_lock", "property": "keyframe_schedule",
        "subject_roles": ["polish.comp"], "subject_controls": [],
        "moments": [240], "kind": "atomic", "required": True,
        "authority": "executable_required", "repair_owner": "polish",
        "asserts": "temporal",
        "evidence": [{"kind": "scene_contract", "id": "polish-spine"}],
    })
    _write(payload, data)

    materialized = validate_materialization(
        bundle.root, payload,
        expected_bundle_hash=bundle.content_hash,
        resolutions_path=state / "plan-resolutions.jsonl",
    )
    assert any(
        row.get("decision_id") == "A-polish" for row in materialized.scene_contracts
    )


def test_deferred_dependent_may_leave_required_outcomes_empty(tmp_path: Path) -> None:
    """All-deferred global documents have no sealed evidence to name: evidence_owners is
    empty because nothing has stages, so demanding non-empty required_outcomes forced
    fabricated ids (run 20260823T093656Z-35a68c). The edge is global; the binding waits
    for materialization."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["schema"] = 5
    data["layers"][0]["execution"] = "jit_deferred"
    data["layers"][0]["stages"] = []
    data["layers"][0]["jit"] = {
        "depends_on_layers": [], "required_outcomes": [],
        "reserved_roles": ["product.*"], "owned_requirements": ["R-root"],
    }
    data["layers"][1]["jit"]["required_outcomes"] = []
    _write(tmp_path / "layers.json", data)

    parsed = load_layers_from_path(tmp_path / "layers.json")

    assert parsed["2"].jit.required_outcomes == ()


def test_outcome_against_deferred_dependency_says_leave_empty(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["schema"] = 5
    data["layers"][0]["execution"] = "jit_deferred"
    data["layers"][0]["stages"] = []
    data["layers"][0]["jit"] = {
        "depends_on_layers": [], "required_outcomes": [],
        "reserved_roles": ["product.*"], "owned_requirements": ["R-root"],
    }
    # layer 2 keeps its scene_contract:final-lock reference, but layer 1 is now deferred
    # and owns no sealed evidence — the error must teach the legal shape.
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError, match="leave required_outcomes empty"):
        load_layers_from_path(tmp_path / "layers.json")


def test_ready_dependency_still_requires_named_outcomes(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][1]["jit"]["required_outcomes"] = []
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError, match="must name the sealed evidence"):
        load_layers_from_path(tmp_path / "layers.json")


def test_unit_first_ready_dependency_leaves_deferred_consumer_unbound(tmp_path: Path) -> None:
    """A dependency turning ready is the normal state after every upstream
    materialization in a unit-first document; the still-deferred consumer's row is
    immutable global authority with deliberately empty outcomes. Run 20260823T133128Z
    burned a full session against the legacy rule firing during the ROOT layer's
    overlay validation — an error with no legal fix."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["schema"] = 5
    data["layers"][1]["jit"]["required_outcomes"] = []
    _write(tmp_path / "layers.json", data)

    parsed = load_layers_from_path(tmp_path / "layers.json")

    assert parsed["1"].execution == "ready"
    assert parsed["2"].jit.required_outcomes == ()


def test_root_materialization_validates_with_deferred_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The combined validation view must keep the base document's schema so unit-first
    loader semantics apply — the exact shape of materializing layer 1 while layer 2
    stays deferred with empty outcomes."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    ready_layer = document["layers"][0]
    root = {**ready_layer, "execution": "jit_deferred", "stages": []}
    root["jit"] = {
        "depends_on_layers": [], "required_outcomes": [],
        "reserved_roles": ["comp"], "owned_requirements": ["R-final-lock"],
    }
    dependent = {
        **ready_layer, "id": "2", "script": "build/02_later.py", "title": "Later",
        "execution": "jit_deferred", "stages": [],
        "jit": {
            "depends_on_layers": ["1"], "required_outcomes": [],
            "reserved_roles": ["later.*"], "owned_requirements": ["R-later"],
        },
    }
    document["schema"] = 5
    document["layers"] = [root, dependent]
    _write(tmp_path / "layers.json", document)
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "deferred_owner", "ids": [], "owner_layer": "1",
        "due": {"kind": "before_layer", "layer": "1"},
    }
    _write(tmp_path / "requirements.json", requirements)
    _write(tmp_path / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })
    layout = run_artifacts.create(tmp_path, "root-with-dependents")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = tmp_path / "root-jit.json"
    _write(payload, {
        "schema": "vfx-harness.jit-layer-materialization/v1",
        "bundle_hash": bundle.content_hash,
        "layer": _declaring({**ready_layer, "execution": "ready"}),
        "scene_contracts": [{
            "id": "final-lock", "kind": "frame_delta", "owner_layer": "1",
            "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
            "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
        }],
        "image_contracts": [],
        "requirement_bindings": [{
            "requirement_id": "R-final-lock", "contract_ids": ["final-lock"],
        }],
        "acceptance": [],
    })

    materialized = validate_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized.layer.id == "1"


def test_concretely_resolved_owned_requirement_fails_closed_at_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run 20260823T135432Z deadlocked on a bundle whose owned_requirements carried
    decision-resolved rows: closure demanded a binding the publish check forbade. Owned
    means owed — such a bundle is inconsistent authority and the remedy is
    republication, never a consumption-side accommodation that would generalize one
    shot's accident into the contract."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"].append({
        "id": "R-format",
        "statement": "Build target: 240 frames at 24fps.",
        "citation": requirements["requirements"][0]["citation"],
        "resolution": {"kind": "decision", "ids": [],
                       "decision": "authored front matter",
                       "decision_strength": "hard_constraint"},
    })
    _write(tmp_path / "requirements.json", requirements)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["owned_requirements"] = ["R-final-lock", "R-format"]
    _write(tmp_path / "layers.json", layers)
    layout = run_artifacts.create(tmp_path, "closed-rows")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    with pytest.raises(ValueError, match="inconsistent authority; republish"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_gate_rejects_owned_requirements_that_carry_no_debt(tmp_path: Path) -> None:
    """The publication gate owns this consistency: bundle a5692e9f shipped the deadlock
    because only the deferred_owner→owned direction was verified."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"].append({
        "id": "R-format",
        "statement": "Build target: 240 frames at 24fps.",
        "citation": requirements["requirements"][0]["citation"],
        "resolution": {"kind": "decision", "ids": [],
                       "decision": "authored front matter",
                       "decision_strength": "hard_constraint"},
    })
    _write(tmp_path / "requirements.json", requirements)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["owned_requirements"] = ["R-final-lock", "R-format"]
    _write(tmp_path / "layers.json", layers)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.check == "requirement-closure"
        and "carries no debt" in finding.what
        and finding.where == "R-format"
        for finding in findings
    )


def test_owned_requirement_deferred_to_another_layer_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "deferred_owner", "ids": [], "owner_layer": "1",
        "due": {"kind": "before_layer", "layer": "1"},
    }
    _write(tmp_path / "requirements.json", requirements)
    layout = run_artifacts.create(tmp_path, "foreign-owner")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    with pytest.raises(ValueError, match="deferred to another layer"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_required_outcome_kind_error_enumerates_valid_kinds(tmp_path: Path) -> None:
    """The planner is workspace-confined, so the validation message is its only route to
    the enum. Run 20260823T085630Z-1c18c2 spent draft turns guessing spellings for a
    constraint whose accepted values it had no way to look up."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][1]["jit"]["required_outcomes"] = [
        {"kind": "property", "id": "camera_spine_locked"}
    ]
    _write(tmp_path / "layers.json", data)

    with pytest.raises(
        ValueError, match="'scene_contract', 'image_contract', or 'semantic_diff'"
    ):
        load_layers_from_path(tmp_path / "layers.json")


def test_owned_requirements_must_be_unique(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][1]["jit"]["owned_requirements"] = ["R-final-lock", "R-final-lock"]
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError, match="unique non-empty"):
        load_layers_from_path(tmp_path / "layers.json")


def test_deferred_layer_cannot_hide_future_units_in_its_stub(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][1]["stages"] = [data["layers"][0]["stages"][0]]
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError, match="must be empty for jit_deferred"):
        load_layers_from_path(tmp_path / "layers.json")


def test_deferred_layer_is_exempt_from_ready_coverage_rules(tmp_path: Path) -> None:
    from vfx_harness.evaluation.plan_gate import _check_coverage

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)

    findings, _ = _check_coverage(tmp_path)

    assert not [f for f in findings if f.where == "layer 2"], (
        "a jit_deferred layer deliberately has no executable checks before materialization"
    )


def test_full_global_gate_emits_no_executable_findings_for_sparse_deferred_layer(
    tmp_path: Path,
) -> None:
    from vfx_harness.evaluation.plan_gate import run

    _candidate(tmp_path)
    Image.new("RGB", (32, 32), "black").save(tmp_path / "refs" / "a.png")
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "deferred_owner", "ids": [], "owner_layer": "2",
        "due": {"kind": "before_layer", "layer": "2"},
    }
    _write(tmp_path / "requirements.json", requirements)
    _write(tmp_path / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })

    result = run(tmp_path)

    deferred_findings = [
        finding for finding in result.findings
        if "layer 2" in finding.where.lower()
        or "L2.JIT" in finding.where
        or "L2.JIT" in finding.what
    ]
    assert deferred_findings == []


def test_deferred_owner_and_requirement_link_directly_without_obligation(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    findings, _ = _check_meta_records(tmp_path)

    assert not [
        finding for finding in findings
        if finding.check in {"requirement-closure", "temporal-requirement"}
    ]


def test_deferred_owner_link_must_be_symmetric(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["owned_requirements"] = ["R-other"]
    _write(tmp_path / "layers.json", layers)

    findings, _ = _check_meta_records(tmp_path)

    assert any("ownership register names layer 2" in finding.what for finding in findings)


def test_deferred_layer_cannot_publish_concrete_contract_before_materialization(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    checks = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    checks["contracts"].append({
        "id": "early-polish-lock", "kind": "frame_delta", "owner_layer": "2",
        "fault_owner": "2", "activates_at": "2", "lifecycle": "layer",
        "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
    })
    _write(tmp_path / "scene_checks.json", checks)

    findings, _ = _check_meta_records(tmp_path)

    assert any(finding.check == "deferred-overplanning" for finding in findings)


def test_global_acceptance_rejects_unmaterialized_layer_fingerprint(tmp_path: Path) -> None:
    from vfx_harness.evaluation.plan_gate import _check_contracts

    _candidate(tmp_path)
    Image.new("RGB", (32, 32), "black").save(tmp_path / "refs" / "a.png")
    _add_deferred_layer(tmp_path)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["judge"] = [{"frame": 300, "ref": "refs/a.png"}]
    layers["layers"][1]["primary_judge"] = 300
    _write(tmp_path / "layers.json", layers)
    _write(tmp_path / "acceptance.json", [{
        "id": "M-later", "frame": 300, "ref": "refs/a.png", "reads": "later finish",
        "fingerprint": "exposure_mean=0",
    }])

    findings, _ = _check_contracts(tmp_path)

    assert any(
        finding.check == "deferred-overplanning" and "fingerprints frame 300" in finding.what
        for finding in findings
    )


def test_materialization_can_close_owned_requirement_with_typed_decision(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["requirement_bindings"] = [{
        "requirement_id": "R-final-lock",
        "decision": {
            "statement": "The authored lock is retained as an approved constraint.",
            "decision_strength": "approved_start",
        },
    }]
    _write(payload, data)

    materialized = validate_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized.requirement_decisions["R-final-lock"]["decision_strength"] == "approved_start"


def test_direct_required_bbox_claims_are_projected_composition_context(
    tmp_path: Path,
) -> None:
    from vfx_harness.evaluation.plan_gate import _check_evidence_coherence

    _candidate(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layer = data["layers"][0]
    layer["evidence_domains"] = ["scene", "temporal", "projected_composition"]
    unit = layer["stages"][0]
    unit["mutates"]["roles"] = ["comp", "camera"]
    unit["evaluation"]["claims"].append({
        "id": "framing-claim", "proposition": "subject stays framed",
        "axis": "final_lock", "property": "bbox_height",
        "subject_roles": ["camera"], "subject_controls": ["hold"],
        "moments": [239, 240], "kind": "atomic", "required": True,
        "authority": "executable_required", "repair_owner": "lock",
        "evidence": [
            {"kind": "scene_contract", "id": "subject-bbox-f239"},
            {"kind": "scene_contract", "id": "subject-bbox-f240"},
        ],
    })
    _write(tmp_path / "layers.json", data)
    checks = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    for frame in (239, 240):
        checks["contracts"].append({
            "id": f"subject-bbox-f{frame}", "kind": "bbox_height", "owner_layer": "1",
            "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
            "axis": "final_lock", "roles": ["comp"], "frame": frame,
            "op": "band", "lo": 0.4, "hi": 0.9,
        })
    _write(tmp_path / "scene_checks.json", checks)

    findings, _ = _check_evidence_coherence(tmp_path)
    assert not [f for f in findings if f.check == "composition-coverage"], (
        "a required claim bound straight to bbox contracts at the judge frames is "
        "executable projected context; composition_context is one valid spelling, not the only one"
    )

    stripped = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    stripped["layers"][0]["stages"][0]["evaluation"]["claims"] = [
        claim
        for claim in stripped["layers"][0]["stages"][0]["evaluation"]["claims"]
        if claim["id"] != "framing-claim"
    ]
    _write(tmp_path / "layers.json", stripped)
    findings, _ = _check_evidence_coherence(tmp_path)
    assert [f for f in findings if f.check == "composition-coverage"]
