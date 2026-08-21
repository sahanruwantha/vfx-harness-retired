from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from bambi_vfx.claim_evidence import validate_claim_closure
from bambi_vfx.layer_plans import read_layer_plan
from bambi_vfx.ledger import load_layers
from bambi_vfx.provenance import check as provenance_check
from bambi_vfx.provenance import stamp as provenance_stamp
from bambi_vfx.unit_state import (
    apply_replan,
    block_dependents,
    freeze_checkpoint,
    initialize,
    load,
    transition,
)
from bambi_vfx.work_units import Claim, ProtectionSpec, WorkUnit, read_document


def _claim(uid: str, *, cid: str | None = None, frame: int = 40) -> dict:
    return {
        "id": cid or f"claim.{uid}",
        "proposition": f"{uid} has the declared state",
        "axis": "form",
        "property": f"state.{uid}",
        "subject_roles": [],
        "subject_controls": [f"control.{uid}"],
        "moments": [frame],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": uid,
        "evidence": [{"kind": "scene_contract", "id": f"contract.{uid}"}],
    }


def _unit(
    uid: str,
    *,
    depends_on: list[str] | None = None,
    frame: int = 40,
    proposition_suffix: str = "",
    script_span: str = "build/04_lighting.py",
) -> WorkUnit:
    row = {
        "id": uid,
        "title": uid,
        "plan": f"plans/04_lighting/{uid}.md",
        "depends_on": depends_on or [],
        "mutates": {
            "mode": "scoped",
            "roles": [],
            "controls": [],
            "script_spans": [script_span],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": frame,
            "judge": [{"frame": frame, "ref": f"refs/f{frame:03d}.png"}],
            "temporal_evidence": "none",
            "claims": [_claim(uid, frame=frame)],
        },
        "completion": "all_required_claims_and_protected_contracts_pass" + proposition_suffix,
    }
    return WorkUnit.parse(row, f"unit.{uid}")


def _pass_unit(folder, layer_id: str, unit: WorkUnit) -> None:
    transition(folder, layer_id, unit.id, "planning", reason="test")
    transition(folder, layer_id, unit.id, "building", reason="test")
    freeze_checkpoint(
        folder,
        layer_id,
        unit,
        active_contract_ids=["upstream.z", "upstream.a"],
        candidate_hash=f"candidate-{unit.id}",
        settings_hash="settings",
        script_hash="script",
        input_hash="inputs",
    )
    transition(folder, layer_id, unit.id, "evaluating", reason="test")
    transition(folder, layer_id, unit.id, "passed", reason="test")


def test_schema4_rejects_legacy_arrays(tmp_path):
    path = tmp_path / "layers.json"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="legacy layer arrays are not supported"):
        read_document(path)


def test_schema4_rejects_schema3_without_adapter(tmp_path):
    path = tmp_path / "layers.json"
    path.write_text(json.dumps({"schema": 3, "layers": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema 3"):
        read_document(path)


def test_claim_rejects_collection_label_instead_of_explicit_binding():
    row = _claim("form")
    row["evidence"] = ["scene_contracts"]
    with pytest.raises(ValueError, match="must be an object"):
        Claim.parse(row, "claim")


def test_claim_closure_reports_unbound_owned_contract(tmp_path):
    (tmp_path / "scene_checks.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "contracts": [
                    {"id": "contract.form", "axis": "form", "owner_layer": "1", "frame": 40},
                    {"id": "contract.unbound", "axis": "form", "owner_layer": "1", "frame": 40},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "checks.json").write_text(
        json.dumps({"schema": 2, "checks": []}), encoding="utf-8"
    )
    layer = SimpleNamespace(id="1", owns=("form",), stages=(_unit("form"),))
    closure = validate_claim_closure(tmp_path, [layer])
    assert not closure.clean
    assert any(finding.where == "contract.unbound" for finding in closure.findings)


def test_explicit_primary_is_not_reordered(tmp_path):
    unit = _unit("complete")
    doc = {
        "schema": 4,
        "layers": [
            {
                "id": "4",
                "script": "build/04_lighting.py",
                "title": "Lighting",
                "primary_judge": 40,
                "judge": [
                    {"frame": 120, "ref": "refs/f120.png"},
                    {"frame": 1, "ref": "refs/f001.png"},
                    {"frame": 40, "ref": "refs/f040.png"},
                ],
                "owns": ["form"],
                "reads": "Lighting reads correctly.",
                "stages": [json.loads(json.dumps(unit, default=lambda value: value.__dict__))],
            }
        ],
    }
    # Dataclass serialization above uses tuples and nested dataclasses correctly but its
    # field names already match the schema except JudgePoint, which is an object here.
    stage = doc["layers"][0]["stages"][0]
    stage["evaluation"]["judge"] = [{"frame": 40, "ref": "refs/f040.png"}]
    stage["protects"] = {
        "selector": "all_active_upstream_interfaces",
        "resolve_to_explicit_ids_at": "freeze",
    }
    (tmp_path / "layers.json").write_text(json.dumps(doc), encoding="utf-8")
    layer = load_layers(SimpleNamespace(folder=tmp_path))["4"]
    assert layer.judge_frame == 40
    assert layer.judge_ref == "refs/f040.png"
    assert [frame for frame, _ref in layer.judges] == [120, 1, 40]


def test_multi_unit_layer_requires_distinct_unit_artifacts(tmp_path):
    first = _unit("form")
    second = _unit("finish", depends_on=["form"])
    stages = [json.loads(json.dumps(unit, default=lambda value: value.__dict__)) for unit in (first, second)]
    for stage in stages:
        stage["evaluation"]["judge"] = [{"frame": 40, "ref": "refs/f040.png"}]
        stage["protects"] = {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        }
    doc = {
        "schema": 4,
        "layers": [
            {
                "id": "4",
                "script": "build/04_lighting.py",
                "title": "Lighting",
                "primary_judge": 40,
                "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                "owns": ["form"],
                "reads": "Lighting reads correctly.",
                "stages": stages,
            }
        ],
    }
    (tmp_path / "layers.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="distinct script spans"):
        load_layers(SimpleNamespace(folder=tmp_path))


def test_multi_unit_layer_reserves_layer_script_for_composition(tmp_path):
    units = (
        _unit("form", script_span="build/units/04/form.py"),
        _unit("finish", depends_on=["form"], script_span="build/04_lighting.py"),
    )
    stages = [json.loads(json.dumps(unit, default=lambda value: value.__dict__)) for unit in units]
    for stage in stages:
        stage["evaluation"]["judge"] = [{"frame": 40, "ref": "refs/f040.png"}]
        stage["protects"] = {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        }
    (tmp_path / "layers.json").write_text(
        json.dumps(
            {
                "schema": 4,
                "layers": [
                    {
                        "id": "4",
                        "script": "build/04_lighting.py",
                        "title": "Lighting",
                        "primary_judge": 40,
                        "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                        "owns": ["form"],
                        "reads": "Lighting reads correctly.",
                        "stages": stages,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reserved for the composed"):
        load_layers(SimpleNamespace(folder=tmp_path))


def test_qualitative_blocking_authority_requires_qualification():
    row = _claim("form") | {"authority": "qualified_qualitative_required"}
    with pytest.raises(ValueError, match="qualification must be an object"):
        Claim.parse(row, "claim")
    row["qualification"] = {
        "suite": "form-v1",
        "judge_model": "judge-v1",
        "prompt": "abc123",
        "evidence_shape": "beauty-plus-focus-v1",
        "artifact": "qualifications/form-v1.json",
        "artifact_sha256": "0" * 64,
    }
    row["evidence"] = [{"kind": "qualification", "id": "form-v1"}]
    assert Claim.parse(row, "claim").authority == "qualified_qualitative_required"


def test_layer_loader_requires_hash_pinned_passed_qualitative_qualification(tmp_path):
    artifact = {
        "schema": 1,
        "suite": "form-v1",
        "judge_model": "judge-v1",
        "prompt": "abc123",
        "evidence_shape": "beauty-plus-focus-v1",
        "passed": True,
        "budgets": {
            "false_pass_rate": 0.05,
            "false_failure_rate": 0.05,
            "repeatability_failure_rate": 0.05,
            "scope_leakage_rate": 0.05,
            "irrelevant_change_sensitivity_rate": 0.05,
        },
        "metrics": {
            "false_pass_rate": 0.0,
            "false_failure_rate": 0.02,
            "repeatability_failure_rate": 0.01,
            "scope_leakage_rate": 0.0,
            "irrelevant_change_sensitivity_rate": 0.0,
        },
    }
    raw = (json.dumps(artifact, sort_keys=True) + "\n").encode()
    (tmp_path / "qualifications").mkdir()
    (tmp_path / "qualifications" / "form-v1.json").write_bytes(raw)
    unit = _unit("form")
    stage = json.loads(json.dumps(unit, default=lambda value: value.__dict__))
    stage["evaluation"]["judge"] = [{"frame": 40, "ref": "refs/f040.png"}]
    stage["protects"] = {
        "selector": "all_active_upstream_interfaces",
        "resolve_to_explicit_ids_at": "freeze",
    }
    stage["evaluation"]["claims"][0].update(
        authority="qualified_qualitative_required",
        evidence=[{"kind": "qualification", "id": "form-v1"}],
        qualification={
            "suite": "form-v1",
            "judge_model": "judge-v1",
            "prompt": "abc123",
            "evidence_shape": "beauty-plus-focus-v1",
            "artifact": "qualifications/form-v1.json",
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
        },
    )
    doc = {
        "schema": 4,
        "layers": [
            {
                "id": "1",
                "script": "build/01_form.py",
                "title": "Form",
                "primary_judge": 40,
                "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                "owns": ["form"],
                "reads": "Form reads.",
                "stages": [stage],
            }
        ],
    }
    (tmp_path / "layers.json").write_text(json.dumps(doc), encoding="utf-8")
    assert load_layers(SimpleNamespace(folder=tmp_path))["1"].stages[0].evaluation.claims[0].required

    artifact["passed"] = False
    bad_raw = (json.dumps(artifact, sort_keys=True) + "\n").encode()
    (tmp_path / "qualifications" / "form-v1.json").write_bytes(bad_raw)
    doc["layers"][0]["stages"][0]["evaluation"]["claims"][0]["qualification"][
        "artifact_sha256"
    ] = hashlib.sha256(bad_raw).hexdigest()
    (tmp_path / "layers.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="did not pass"):
        load_layers(SimpleNamespace(folder=tmp_path))


def test_interaction_claim_needs_bounded_coordination():
    row = _claim("form") | {
        "kind": "interaction",
        "coordination_owner": "balance",
        "participants": ["form", "exposure"],
    }
    with pytest.raises(ValueError, match="controls must bound"):
        Claim.parse(row, "claim")
    row["controls"] = ["control.key.level"]
    claim = Claim.parse(row, "claim")
    assert claim.coordination_owner == "balance"


def test_protection_selector_freezes_to_explicit_sorted_ids():
    spec = ProtectionSpec.parse(
        {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "protects",
    )
    assert spec.resolve(["contract.z", "contract.a", "contract.z"]) == ("contract.a", "contract.z")


def test_checkpoint_and_replan_preserve_only_unaffected_units(tmp_path):
    old = (_unit("rig"), _unit("form", depends_on=["rig"]), _unit("independent"))
    initialize(tmp_path, "4", old, plan_hash="plan-v1")
    for unit in old:
        _pass_unit(tmp_path, "4", unit)

    frozen = load(tmp_path, "4")["units"]["rig"]["checkpoint"]
    assert frozen["protected_contract_ids"] == ["upstream.a", "upstream.z"]

    new = (
        old[0],
        _unit("form", depends_on=["rig"], proposition_suffix="-changed"),
        _unit("finish", depends_on=["form"]),
        old[2],
    )
    record = apply_replan(
        tmp_path,
        "4",
        old,
        new,
        old_plan_hash="plan-v1",
        new_plan_hash="plan-v2",
        owner="planner",
        trigger="form unit needs a distinct finish dependency",
        evidence=["verdict:form-flat"],
    )
    state = load(tmp_path, "4")
    assert record["invalidated"] == ["finish", "form"]
    assert record["preserved"] == ["independent", "rig"]
    assert state["units"]["rig"]["status"] == "passed"
    assert state["units"]["independent"]["status"] == "passed"
    assert state["units"]["form"]["status"] == "pending"
    assert state["units"]["finish"]["status"] == "pending"
    assert state["superseded"][-1]["id"] == "form"
    assert state["replans"][-1] == record


def test_failed_dependency_blocks_transitive_units_without_failing_them(tmp_path):
    units = (_unit("a"), _unit("b", depends_on=["a"]), _unit("c", depends_on=["b"]))
    initialize(tmp_path, "1", units, plan_hash="plan")
    state = block_dependents(
        tmp_path,
        "1",
        "a",
        units,
        reason="a needs human-required adjudication",
    )
    assert state["units"]["a"]["status"] == "pending"
    assert state["units"]["b"]["status"] == "blocked"
    assert state["units"]["c"]["status"] == "blocked"


def test_work_unit_state_is_isolated_per_layer(tmp_path):
    initialize(tmp_path, "1", (_unit("layout"),), plan_hash="layout-plan")
    initialize(tmp_path, "2", (_unit("form"),), plan_hash="form-plan")
    transition(tmp_path, "1", "layout", "planning", reason="test")

    assert load(tmp_path, "1")["units"]["layout"]["status"] == "planning"
    assert load(tmp_path, "2")["units"]["form"]["status"] == "pending"


def test_stale_unit_state_cannot_authorize_a_changed_dag(tmp_path):
    initialize(tmp_path, "1", (_unit("layout"),), plan_hash="plan")
    with pytest.raises(ValueError, match="transactional replan"):
        initialize(
            tmp_path,
            "1",
            (_unit("layout", proposition_suffix="-changed"),),
            plan_hash="plan",
        )


def test_layer_plan_reader_never_flattens_a_multi_unit_dag(tmp_path):
    layer = SimpleNamespace(id="4", stages=(_unit("form"), _unit("finish", depends_on=["form"])))
    with pytest.raises(ValueError, match="cannot choose or combine"):
        read_layer_plan(tmp_path, layer)


def test_provenance_tracks_schema_declared_unit_plan_paths(tmp_path):
    unit = _unit("form")
    stage = json.loads(json.dumps(unit, default=lambda value: value.__dict__))
    stage["evaluation"]["judge"] = [{"frame": 40, "ref": "refs/f040.png"}]
    stage["protects"] = {
        "selector": "all_active_upstream_interfaces",
        "resolve_to_explicit_ids_at": "freeze",
    }
    doc = {
        "schema": 4,
        "layers": [
            {
                "id": "1",
                "script": "build/01_form.py",
                "title": "Form",
                "primary_judge": 40,
                "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                "owns": ["form"],
                "reads": "Form reads.",
                "stages": [stage],
            }
        ],
    }
    (tmp_path / "brief.md").write_text("brief\n", encoding="utf-8")
    (tmp_path / "layers.json").write_text(json.dumps(doc), encoding="utf-8")
    plan = tmp_path / unit.plan
    plan.parent.mkdir(parents=True)
    plan.write_text("bounded unit plan\n", encoding="utf-8")
    provenance_stamp(tmp_path)
    plan.write_text("changed bounded unit plan\n", encoding="utf-8")

    assert any(unit.plan in problem and "edited" in problem.lower() for problem in provenance_check(tmp_path))
