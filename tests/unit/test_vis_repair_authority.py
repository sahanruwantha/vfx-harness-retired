"""HIR-0051: vis is repaired by a ray-changing owner; unions cannot hide a subject."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from tests.unit.test_plan_improvements import _layer_doc, _write
from tests.unit.test_plan_records import _add_deferred_layer, _candidate, _jit_payload
from tests.unit.test_plan_records import _write as _write_plan
from vfx_harness.agents.builder import _executable_unit_verdict, _scope_unit_evidence
from vfx_harness.agents.planner import _TWO_SIDED_CONTRACT_BINDING
from vfx_harness.domain.work_units import (
    GEOMETRY_VIS_CYCLE_RULE,
    GEOMETRY_VIS_DEPENDENCY_RULE,
    VIS_REPAIR_OWNER_RULE,
    WorkUnit,
    geometry_vis_dependency_cycles,
    geometry_vis_dependency_gaps,
    geometry_vis_protection_ids,
    vis_roles_unrepairable_by,
)
from vfx_harness.evaluation.plan_gate import _check_evidence_coherence
from vfx_harness.evidence.scene_checks import (
    _blender_probe,
    _evidence,
    _holds,
    visible_fraction_min,
)
from vfx_harness.orchestration.unit_state import freeze_checkpoint, initialize, load, transition


def _vis_row(*, row_id: str = "vis", roles: list[str] | None = None) -> dict:
    return {
        "id": row_id,
        "kind": "visible_fraction",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "axis": "camera_framing",
        "roles": roles or ["proxy.interior"],
        "frame": 1,
        "op": "min",
        "lo": 0.25,
    }


def _detail_unit(*, provides: list[str]) -> WorkUnit:
    return WorkUnit.parse(
        {
            "id": "detail",
            "title": "Detail",
            "plan": "plans/02/detail.md",
            "depends_on": [],
            "mutates": {
                "mode": "scoped",
                "roles": ["world.detail"],
                "controls": [],
                "script_spans": ["build/units/02/detail.py"],
            },
            "protects": {
                "selector": "all_active_upstream_interfaces",
                "resolve_to_explicit_ids_at": "freeze",
            },
            "evaluation": {
                "primary_judge": 40,
                "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                "temporal_evidence": "none",
                "claims": [
                    {
                        "id": "claim.detail",
                        "proposition": "detail exists",
                        "axis": "form",
                        "property": "state.detail",
                        "subject_roles": ["world.detail"],
                        "subject_controls": [],
                        "moments": [40],
                        "kind": "atomic",
                        "required": True,
                        "authority": "executable_required",
                        "repair_owner": "detail",
                        "asserts": "scene",
                        "evidence": [{"kind": "scene_contract", "id": "contract.detail"}],
                    }
                ],
            },
            "completion": "all_required_claims_and_protected_contracts_pass",
            "provides": provides,
        },
        "unit.detail",
    )


def test_kickoff_copy_is_camera_or_mutator_not_any_unit() -> None:
    assert "provides camera" in _TWO_SIDED_CONTRACT_BINDING
    assert "volume-only" in _TWO_SIDED_CONTRACT_BINDING
    assert "may name roles this unit does not mutate" not in _TWO_SIDED_CONTRACT_BINDING


def test_volume_unit_cannot_repair_foreign_vis_roles() -> None:
    unrepaired = vis_roles_unrepairable_by(
        provides=(),
        mutation_roles=["world.atmosphere_volume"],
        vis_roles=["world.proxy_core"],
    )
    assert unrepaired == ("world.proxy_core",)
    assert vis_roles_unrepairable_by(
        provides=["camera"],
        mutation_roles=["cam_rig"],
        vis_roles=["world.proxy_core"],
    ) == ()
    assert vis_roles_unrepairable_by(
        provides=(),
        mutation_roles=["world.proxy_core"],
        vis_roles=["world.proxy_core"],
    ) == ()


def test_volume_unit_required_vis_is_refused_at_the_gate(tmp_path: Path) -> None:
    doc = _layer_doc(temporal_id="vis")
    scout = json.loads(json.dumps(doc["layers"][0]["stages"][0]))
    scout["id"] = "blockout"
    scout["plan"] = "plans/01_camera/00_blockout.md"
    scout["mutates"] = {**scout["mutates"], "roles": ["proxy.interior"], "controls": []}
    scout["evaluation"]["claims"] = []
    fog = doc["layers"][0]["stages"][0]
    fog["id"] = "fog"
    fog["plan"] = "plans/01_camera/01_fog.md"
    fog["mutates"] = {
        **fog["mutates"],
        "roles": ["world.atmosphere_volume"],
        "controls": [],
    }
    fog["evaluation"]["claims"][0]["id"] = "fog-claim"
    fog["evaluation"]["claims"][0]["repair_owner"] = "fog"
    fog["evaluation"]["claims"][0]["subject_roles"] = ["world.atmosphere_volume"]
    fog["evaluation"]["claims"][0]["subject_controls"] = []
    doc["layers"][0]["stages"] = [scout, fog]
    _write(tmp_path / "layers.json", doc)
    _write(
        tmp_path / "scene_checks.json",
        {"schema": 2, "contracts": [_vis_row()]},
    )
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)

    assert any(
        finding.check == "vis-repair-owner"
        and finding.blocking
        and "proxy.interior" in finding.what
        and VIS_REPAIR_OWNER_RULE in finding.fix
        for finding in findings
    )


def test_materialization_refuses_volume_vis_and_keeps_camera_vis(tmp_path: Path) -> None:
    from vfx_harness.observability import run_artifacts
    from vfx_harness.orchestration.jit_materialization import inspect_materialization
    from vfx_harness.orchestration.plan_authority import publish_current

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["provides"] = {"camera": ["polish.*"]}
    _write_plan(tmp_path / "layers.json", layers)
    layout = run_artifacts.create(tmp_path, "vis-owner")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    polish = document["layer"]["stages"][0]
    polish["provides"] = ["camera"]
    polish["evaluation"]["claims"][0]["evidence"].append(
        {"kind": "scene_contract", "id": "vis-f239"}
    )
    _write_plan(payload, document)
    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )
    assert materialized is not None
    assert not any("visible_fraction" in line and "volume" in line for line in findings)

    fog = json.loads(json.dumps(polish))
    fog["id"] = "fog"
    fog["plan"] = "plans/02_polish/fog.md"
    fog["provides"] = []
    fog["mutates"] = {
        **fog["mutates"],
        "roles": ["world.atmosphere_volume"],
        "controls": [],
        "control_roles": {},
        "script_spans": ["build/units/02_polish/fog.py"],
    }
    fog["evaluation"]["claims"] = [
        {
            **fog["evaluation"]["claims"][0],
            "id": "fog-vis",
            "repair_owner": "fog",
            "subject_roles": ["world.atmosphere_volume"],
            "subject_controls": [],
            "property": "visible_fraction",
            "asserts": "projected_composition",
            "evidence": [{"kind": "scene_contract", "id": "vis-f239"}],
        }
    ]
    fog["evaluation"]["composition_context"] = {
        "frames": [239, 240],
        "contract_ids": ["vis-f240"],
    }
    document["layer"]["stages"].append(fog)
    _write_plan(payload, document)
    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )
    assert materialized is None
    text = "\n".join(findings)
    assert "visible_fraction" in text
    assert "vis-f239" in text
    assert VIS_REPAIR_OWNER_RULE in text


def test_materialization_refuses_geometry_with_future_vis_producer(tmp_path: Path) -> None:
    from vfx_harness.observability import run_artifacts
    from vfx_harness.orchestration.jit_materialization import inspect_materialization
    from vfx_harness.orchestration.plan_authority import publish_current

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "geometry-vis-dependency")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    root = document["layer"]["stages"][0]
    root["provides"] = ["geometry"]
    future = json.loads(json.dumps(root))
    future["id"] = "future_detail"
    future["title"] = "Future detail"
    future["plan"] = "plans/02_polish/future_detail.md"
    future["depends_on"] = []
    future["mutates"] = {
        **future["mutates"],
        "roles": ["polish.future_detail"],
        "controls": [],
        "control_roles": {},
        "script_spans": ["build/units/02_polish/future_detail.py"],
    }
    future["evaluation"]["temporal_evidence"] = "none"
    future["evaluation"]["claims"] = [{
        **future["evaluation"]["claims"][0],
        "id": "future-vis-claim",
        "proposition": "future detail is visible",
        "property": "visible_fraction",
        "subject_roles": ["polish.future_detail"],
        "subject_controls": [],
        "moments": [240],
        "repair_owner": "future_detail",
        "asserts": "projected_composition",
        "evidence": [{"kind": "scene_contract", "id": "vis-future"}],
    }]
    document["layer"]["stages"].append(future)
    for row in document["scene_contracts"]:
        if row.get("kind") == "visible_fraction":
            row["roles"] = ["polish.comp"]
    document["scene_contracts"].append({
        "id": "vis-future",
        "kind": "visible_fraction",
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "final_lock",
        "roles": ["polish.future_detail"],
        "frame": 240,
        "op": "min",
        "lo": 0.25,
    })
    _write_plan(payload, document)

    cycle_findings, cycle_materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert cycle_materialized is None
    cycle_text = "\n".join(cycle_findings)
    assert "mutual geometry visibility cycle" in cycle_text
    assert "polish->future_detail" in cycle_text
    assert "future_detail->polish" in cycle_text
    assert GEOMETRY_VIS_CYCLE_RULE in cycle_text
    assert "geometry unit polish protects visible_fraction vis-future" not in cycle_text

    document["layer"]["stages"][1]["depends_on"] = ["polish"]
    _write_plan(payload, document)
    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized is None
    text = "\n".join(findings)
    assert "geometry unit polish protects visible_fraction vis-future" in text
    assert "future_detail" in text
    assert GEOMETRY_VIS_DEPENDENCY_RULE in text


def test_two_role_vis_fails_when_one_named_role_is_below_lo() -> None:
    fractions = {"world.proxy_rings": 1.0, "world.proxy_core": 0.09}
    named = ["world.proxy_rings", "world.proxy_core"]
    assert visible_fraction_min(fractions, named) == 0.09
    row = {
        "kind": "visible_fraction",
        "roles": named,
        "op": "min",
        "lo": 0.5,
    }
    assert _holds(row, 1.0, role_fractions=fractions) is False
    packed = _evidence(
        [row | {"id": "union", "owner_layer": "2", "fault_owner": "2",
                "activates_at": "2", "lifecycle": "layer", "axis": "a", "frame": 150}],
        [{"id": "union", "value": 1.0, "role_fractions": fractions, "error": ""}],
    )
    assert packed[0]["pass"] is False
    assert packed[0]["role_fractions"]["world.proxy_core"] == 0.09

    single = {"kind": "visible_fraction", "roles": ["world.proxy_rings"], "op": "min", "lo": 0.5}
    assert _holds(single, 1.0, role_fractions={"world.proxy_rings": 1.0}) is True
    compile(_blender_probe([row | {"id": "union", "owner_layer": "2", "fault_owner": "2",
                                   "activates_at": "2", "lifecycle": "layer",
                                   "axis": "a", "frame": 150}], 150), "<vis>", "exec")


def test_geometry_sibling_freeze_protects_layer_vis(tmp_path: Path) -> None:
    geo = _detail_unit(provides=["geometry"])
    fog_row = {
        "id": "fog",
        "title": "Fog",
        "plan": "plans/02/fog.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": ["world.atmosphere_volume"],
            "controls": [],
            "script_spans": ["build/units/02/fog.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 40,
            "judge": [{"frame": 40, "ref": "refs/f040.png"}],
            "temporal_evidence": "none",
            "claims": [
                {
                    "id": "claim.fog",
                    "proposition": "volume exists",
                    "axis": "form",
                    "property": "state.fog",
                    "subject_roles": ["world.atmosphere_volume"],
                    "subject_controls": [],
                    "moments": [40],
                    "kind": "atomic",
                    "required": True,
                    "authority": "executable_required",
                    "repair_owner": "fog",
                    "asserts": "scene",
                    "evidence": [{"kind": "scene_contract", "id": "contract.fog"}],
                }
            ],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "provides": [],
    }
    fog = WorkUnit.parse(fog_row, "unit.fog")
    assert geometry_vis_protection_ids(geo.provides, ["vis.core"]) == ("vis.core",)
    assert geometry_vis_protection_ids(fog.provides, ["vis.core"]) == ()
    assert geometry_vis_protection_ids(("camera",), ["vis.core"]) == ()

    initialize(tmp_path, "2", (geo, fog), plan_hash="plan-v1")
    for unit in (geo, fog):
        transition(tmp_path, "2", unit.id, "planning", reason="test")
        transition(tmp_path, "2", unit.id, "building", reason="test")
        freeze_checkpoint(
            tmp_path,
            "2",
            unit,
            active_contract_ids=["upstream.a"],
            candidate_hash=f"candidate-{unit.id}",
            settings_hash="settings",
            script_hash="script",
            input_hash="inputs",
            layer_active_vis_ids=["vis.core"],
        )
    state = load(tmp_path, "2")
    assert "vis.core" in state["units"]["detail"]["checkpoint"]["protected_contract_ids"]
    assert "vis.core" not in state["units"]["fog"]["checkpoint"]["protected_contract_ids"]


def test_geometry_unit_cannot_protect_visibility_created_by_future_sibling(
    tmp_path: Path,
) -> None:
    material = _detail_unit(provides=["geometry"])
    future_detail = replace(
        _detail_unit(provides=["geometry"]),
        id="future_detail",
        depends_on=(material.id,),
        mutates=replace(
            material.mutates,
            roles=("world.future_detail",),
            script_spans=("build/units/02/future_detail.py",),
        ),
    )
    row = _vis_row(row_id="vis.future", roles=["world.future_detail"])

    gaps = geometry_vis_dependency_gaps((material, future_detail), (row,), "1")

    assert len(gaps) == 1
    assert gaps[0].unit_id == material.id
    assert gaps[0].contract_id == "vis.future"
    assert gaps[0].producer_ids == (future_detail.id,)

    doc = _layer_doc(temporal_id="vis.future")
    root = doc["layers"][0]["stages"][0]
    root["provides"] = ["geometry"]
    root["mutates"]["roles"] = ["world.material_proxy"]
    detail = json.loads(json.dumps(root))
    detail["id"] = "future_detail"
    detail["title"] = "Future detail"
    detail["plan"] = "plans/01_camera/02_future_detail.md"
    detail["depends_on"] = ["move"]
    detail["provides"] = ["geometry"]
    detail["mutates"]["roles"] = ["world.future_detail"]
    detail["mutates"]["script_spans"] = ["build/units/01_camera/02_future_detail.py"]
    detail["evaluation"]["claims"][0]["repair_owner"] = "future_detail"
    detail["evaluation"]["claims"][0]["subject_roles"] = ["world.future_detail"]
    doc["layers"][0]["stages"].append(detail)
    _write(tmp_path / "layers.json", doc)
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": [row]})
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)

    assert any(
        finding.check == "geometry-vis-dependency"
        and finding.blocking
        and finding.where == "layer 1 unit move"
        and "future_detail" in finding.what
        and finding.fix == GEOMETRY_VIS_DEPENDENCY_RULE
        for finding in findings
    )

    root["provides"] = []
    _write(tmp_path / "layers.json", doc)
    findings, _ = _check_evidence_coherence(tmp_path)
    assert not any(finding.check == "geometry-vis-dependency" for finding in findings)


def test_mutual_geometry_visibility_cycle_is_one_typed_design_finding(
    tmp_path: Path,
) -> None:
    left = _detail_unit(provides=["geometry"])
    right = replace(
        _detail_unit(provides=["geometry"]),
        id="rim_geometry",
        mutates=replace(
            left.mutates,
            roles=("world.rim",),
            script_spans=("build/units/02/rim_geometry.py",),
        ),
    )
    rows = (
        _vis_row(row_id="vis.detail", roles=["world.detail"]),
        _vis_row(row_id="vis.rim", roles=["world.rim"]),
    )

    cycles = geometry_vis_dependency_cycles((left, right), rows, "1")

    assert len(cycles) == 1
    assert cycles[0].unit_ids == ("detail", "rim_geometry")
    assert cycles[0].contract_ids == ("vis.detail", "vis.rim")
    assert cycles[0].roles == ("world.detail", "world.rim")
    assert cycles[0].edges == (
        ("detail", "rim_geometry"),
        ("rim_geometry", "detail"),
    )

    doc = _layer_doc(temporal_id="vis.detail")
    root = doc["layers"][0]["stages"][0]
    root["provides"] = ["geometry"]
    root["mutates"]["roles"] = ["world.detail"]
    rim = json.loads(json.dumps(root))
    rim["id"] = "rim_geometry"
    rim["title"] = "Rim geometry"
    rim["plan"] = "plans/01_camera/02_rim_geometry.md"
    rim["mutates"]["roles"] = ["world.rim"]
    rim["mutates"]["script_spans"] = ["build/units/01_camera/02_rim_geometry.py"]
    rim["evaluation"]["claims"][0]["id"] = "claim.rim"
    rim["evaluation"]["claims"][0]["repair_owner"] = "rim_geometry"
    rim["evaluation"]["claims"][0]["subject_roles"] = ["world.rim"]
    rim["evaluation"]["claims"][0]["evidence"] = [
        {"kind": "scene_contract", "id": "vis.rim"}
    ]
    doc["layers"][0]["stages"].append(rim)
    _write(tmp_path / "layers.json", doc)
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": list(rows)})
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)
    cycle_findings = [finding for finding in findings if finding.check == "geometry-vis-cycle"]

    assert len(cycle_findings) == 1
    assert cycle_findings[0].blocking
    assert cycle_findings[0].where == "layer 1 units move, rim_geometry"
    assert "move->rim_geometry" in cycle_findings[0].what
    assert "rim_geometry->move" in cycle_findings[0].what
    assert cycle_findings[0].fix == GEOMETRY_VIS_CYCLE_RULE
    assert not any(finding.check == "geometry-vis-dependency" for finding in findings)


def test_geometry_protected_vis_failure_blocks_the_executable_verdict() -> None:
    unit = _detail_unit(provides=["geometry"])
    axes = [("form", "declared form")]
    evidence = [
        {"id": "contract.detail", "pass": True, "authoritative": True},
        {"id": "vis.core", "pass": False, "authoritative": True},
    ]
    own = _executable_unit_verdict(unit, 40, axes, evidence)
    assert own is not None and own["pass"] is True
    blocked = _executable_unit_verdict(
        unit, 40, axes, evidence, extra_required_ids={"vis.core"}
    )
    assert blocked is not None and blocked["pass"] is False
    assert "vis.core" in {row["id"] for row in blocked["evidence_failures"]}

    scoped = _scope_unit_evidence(evidence, unit, 40, extra_ids={"vis.core"})
    assert {row["id"] for row in scoped} == {"contract.detail", "vis.core"}
    fog = _detail_unit(provides=[])
    fog_scoped = _scope_unit_evidence(evidence, fog, 40)
    assert {row["id"] for row in fog_scoped} == {"contract.detail"}
