"""Active-unit scope is compiled, not guessed from siblings or helper source."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vfx_harness.agents.build_prompts import builder_kickoff
from vfx_harness.agents.unit_scope import (
    compile_scope_with_predecessors,
    compile_unit_scope,
    format_unit_scope_card,
    helper_inventory,
)
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration.ledger import Milestone

_WORKER = Path(__file__).resolve().parents[3] / "src" / "vfx_harness" / "blender" / "worker.py"


def _claim(uid: str, *, contract_id: str, frame: int = 1) -> dict:
    return {
        "id": f"claim.{uid}",
        "proposition": f"{uid} has the declared state",
        "axis": "form",
        "property": f"state.{uid}",
        "subject_roles": [f"{uid}.role"],
        "subject_controls": [],
        "moments": [frame],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": uid,
        "asserts": "scene",
        "evidence": [{"kind": "scene_contract", "id": contract_id}],
    }


def _unit(
    uid: str,
    *,
    roles: list[str],
    contract_id: str,
    extra_contract: str | None = None,
    provides: list[str] | None = None,
    construction: dict | None = None,
) -> WorkUnit:
    evaluation: dict = {
        "primary_judge": 1,
        "judge": [{"frame": 1, "ref": "refs/a.png"}],
        "temporal_evidence": "none",
        "claims": [_claim(uid, contract_id=contract_id)],
    }
    if extra_contract:
        evaluation["composition_context"] = {
            "frames": [1],
            "contract_ids": [extra_contract],
        }
    row = {
        "id": uid,
        "title": uid,
        "plan": f"plans/{uid}.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": roles,
            "controls": [f"{uid}.gain"],
            "script_spans": [f"build/units/01/{uid}.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": evaluation,
        "completion": "all_required_claims_and_protected_contracts_pass",
        "provides": provides or [],
    }
    if construction is not None:
        row["construction"] = construction
    return WorkUnit.parse(row, f"unit.{uid}")


def _contracts() -> list[dict]:
    return [
        {
            "id": "cam-spine",
            "kind": "keyframe_exists",
            "roles": ["cam_rig"],
            "frame": 1,
        },
        {
            "id": "cam-vis-f1",
            "kind": "visible_fraction",
            "roles": ["cam.proxy.interior"],
            "frame": 1,
        },
        {
            "id": "fg-exist",
            "kind": "object_count",
            "roles": ["cam.blockout_fg"],
            "frame": 1,
        },
    ]


def test_helper_inventory_is_worker_helpers_not_a_private_copy() -> None:
    inventory = helper_inventory()
    names = {row["name"] for row in inventory}
    declared = set(re.findall(r'"(bvfx_\w+)":', _WORKER.read_text(encoding="utf-8")))
    assert names == declared
    assert "bvfx_camera_rig" in names
    assert "bvfx_role" in names
    by_name = {row["name"]: row for row in inventory}
    assert "role" in by_name["bvfx_camera_rig"]["signature"]
    assert "role" in by_name["bvfx_role"]["signature"]
    assert by_name["bvfx_light"]["signature"].endswith("-> 'bpy.types.Object'")
    assert by_name["bvfx_camera_rig"]["signature"].endswith(
        "-> 'tuple[bpy.types.Object, bpy.types.Object]'"
    )


def test_unit_scope_card_is_the_active_unit_not_a_sibling() -> None:
    camera = _unit("camera_rig", roles=["cam_rig"], contract_id="cam-spine", extra_contract="cam-vis-f1")
    _blockout = _unit("blockout_proxies", roles=["cam.blockout_fg"], contract_id="fg-exist")
    card = compile_unit_scope(unit=camera, layer_id="1", contracts=_contracts())
    assert card["unit_id"] == "camera_rig"
    assert card["mutates"]["roles"] == ["cam_rig"]
    assert [row["id"] for row in card["contracts"]] == ["cam-spine", "cam-vis-f1"]
    dumped = format_unit_scope_card(card)
    assert "cam.blockout_fg" not in dumped
    assert "fg-exist" not in dumped
    assert "cam_rig" in dumped
    assert "bvfx_role" in dumped
    assert "owed image-contract debts" in dumped
    assert "(none)" in dumped.split("owed image-contract debts")[1].split("run_bpy")[0]


def test_geometry_scope_compiles_diagnostic_artist_view_without_acceptance() -> None:
    geometry = _unit(
        "building_mass",
        roles=["building.mass.tower"],
        contract_id="fg-exist",
        provides=["geometry"],
    )
    card = compile_unit_scope(unit=geometry, layer_id="2", contracts=_contracts())

    assert card["diagnostic_instruments"] == [{
        "name": "inspect_view",
        "purpose": "orbit/elevation/solo inspection of owned form by semantic role",
        "views": ["through_camera", "orbit", "front", "right", "back", "left", "top"],
        "acceptance_evidence": False,
    }]


def test_early_geometry_producer_gets_nonpayable_deferred_bbox_forecast() -> None:
    from dataclasses import replace

    mass = _unit(
        "building_mass",
        roles=["building.mass.tower"],
        contract_id="fg-exist",
        provides=["geometry"],
    )
    roof = _unit(
        "building_roof",
        roles=["building.roof.silhouette"],
        contract_id="cam-spine",
        provides=["geometry"],
    )
    roof = replace(roof, depends_on=(mass.id,))
    deferred = {
        "id": "building-bbox-f176",
        "kind": "bbox_height",
        "roles": ["building"],
        "frame": 176,
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "2",
        "lifecycle": "persistent",
        "axis": "camera_path",
        "op": "min",
        "lo": 0.85,
    }
    contracts = [*_contracts(), deferred]

    mass_card = compile_scope_with_predecessors(
        unit=mass,
        layer_id="2",
        contracts=contracts,
        units=(mass, roof),
        helpers=(),
    )
    assert mass_card["deferred_subject_forecasts"] == [{
        **deferred,
        # A floor on a growing union is repairable by later geometry, so this row has no
        # side it can cross irrecoverably and IS unconditionally diagnostic (HIR-0212).
        "status": "diagnostic_only",
        "irreversible_bound": None,
        # Who shares the union and who still adds to it after this unit (HIR-0197).
        "union_producers": ["building_mass", "building_roof"],
        "pending_producers": ["building_roof"],
    }]
    assert "building-bbox-f176" not in {
        row["id"] for row in mass_card["contracts"]
    }
    assert mass_card["deferred_subject_payments"] == []
    formatted = format_unit_scope_card(mass_card)
    # The heading no longer claims only the payer can satisfy these rows: that sentence
    # was the persuasive half of the defect a builder reasoned from (HIR-0212).
    assert "alone can satisfy" not in formatted
    assert "CONDITIONAL" in formatted
    assert "building-bbox-f176" in formatted.split("deferred subject forecasts")[1]

    roof_card = compile_scope_with_predecessors(
        unit=roof,
        layer_id="2",
        contracts=contracts,
        units=(mass, roof),
        helpers=(),
    )
    assert roof_card["deferred_subject_forecasts"] == []
    # The dependency-complete producer pays the row: the card names it as required,
    # exactly as the runtime already requires it (HIR-0174).
    assert roof_card["deferred_subject_payments"] == [{
        **deferred,
        "required_before_freeze": True,
        "acceptance_evidence": True,
        "union_producers": ["building_mass", "building_roof"],
        "pending_producers": [],
    }]
    roof_formatted = format_unit_scope_card(roof_card)
    payments_section = roof_formatted.split("deferred subject rows this unit pays")[1]
    assert "building-bbox-f176" in payments_section.split("deferred subject forecasts")[0]
    assert "REQUIRED BEFORE FREEZE" in roof_formatted


def test_unit_scope_keeps_exact_evaluator_fields_for_bound_contract() -> None:
    unit = _unit("material", roles=["lookdev.material"], contract_id="roughness")
    contracts = [{
        "id": "roughness",
        "kind": "node_socket_value",
        "material_roles": ["lookdev.material"],
        "node_roles": ["lookdev.material.bsdf"],
        "node_types": ["ShaderNodeBsdfPrincipled"],
        "socket": "Roughness",
        "direction": "input",
        "op": "band",
        "lo": 0.35,
        "hi": 0.75,
        "frame": 1,
    }]

    card = compile_unit_scope(unit=unit, layer_id="1", contracts=contracts)
    row = card["contracts"][0]

    assert row["node_types"] == ["ShaderNodeBsdfPrincipled"]
    assert row["socket"] == "Roughness"
    assert row["direction"] == "input"
    assert (row["lo"], row["hi"]) == (0.35, 0.75)
    formatted = format_unit_scope_card(card)
    assert '"node_roles":["lookdev.material.bsdf"]' in formatted
    assert '"socket":"Roughness"' in formatted
    assert '"lo":0.35' in formatted


def test_unit_scope_carries_the_exact_derived_write_cluster() -> None:
    unit = _unit("material", roles=["lookdev.material"], contract_id="roughness")
    contracts = [{
        "id": "roughness",
        "kind": "node_socket_value",
        "material_roles": ["lookdev.material"],
        "node_roles": ["lookdev.material.bsdf"],
        "graph": "material",
        "socket": "Roughness",
        "op": "band",
        "lo": 0.35,
        "hi": 0.75,
        "frame": 1,
    }]

    card = compile_unit_scope(unit=unit, layer_id="1", contracts=contracts)

    assert card["write_clusters"] == [{
        "role_namespace": "lookdev.material",
        "host_class": "control_host",
        "instrument_family": "shading",
    }]
    assert {row["kind"] for row in card["publish_interfaces"]} == {"material_slot"}


def test_unit_scope_lists_owed_image_contract_debts() -> None:
    look = {
        "id": "claim.look",
        "proposition": "the plate matches the owned look",
        "axis": "form",
        "property": "render_region_stat",
        "subject_roles": ["cam_rig"],
        "subject_controls": [],
        "moments": [1],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "camera_rig",
        "asserts": "image",
        "evidence": [{"kind": "image_contract", "id": "cam-look-f1"}],
    }
    row = {
        "id": "camera_rig",
        "title": "camera_rig",
        "plan": "plans/camera_rig.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": ["cam_rig"],
            "controls": ["camera_rig.gain"],
            "script_spans": ["build/units/01/camera_rig.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 1,
            "judge": [{"frame": 1, "ref": "refs/a.png"}],
            "temporal_evidence": "none",
            "claims": [_claim("camera_rig", contract_id="cam-spine"), look],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["material"],
    }
    unit = WorkUnit.parse(row, "unit.camera_rig")
    card = compile_unit_scope(unit=unit, layer_id="1", contracts=_contracts())
    assert card["image_debts"] == [
        {"id": "cam-look-f1", "axis": "form", "frame": 1, "property": "render_region_stat"}
    ]
    dumped = format_unit_scope_card(card)
    assert "cam-look-f1" in dumped
    assert "owed image-contract debts" in dumped


def test_unit_scope_names_earlier_layer_on_fault_owner_options() -> None:
    camera = _unit("camera_rig", roles=["cam_rig"], contract_id="cam-spine")
    card = compile_unit_scope(unit=camera, layer_id="1", contracts=_contracts())
    card["fault_owner_options"] = [
        {
            "id": "camera_path",
            "layer": "1",
            "roles": ["camera"],
            "controls": [],
        }
    ]
    dumped = format_unit_scope_card(card)
    assert "`camera_path` layer=1" in dumped


def test_unknown_bound_contract_names_requested_and_present() -> None:
    unit = _unit("camera_rig", roles=["cam_rig"], contract_id="missing-spine")
    with pytest.raises(ValueError, match="missing-spine") as caught:
        compile_unit_scope(unit=unit, layer_id="1", contracts=_contracts())
    message = str(caught.value)
    assert "cam-spine" in message
    assert "present:" in message


def test_kickoff_carries_the_compiled_card(tmp_path: Path) -> None:
    (tmp_path / "brief.md").write_text(
        "---\nid: fixture\nframes: 24\nfps: 24\n---\nA fixture shot.\n",
        encoding="utf-8",
    )
    (tmp_path / "refs").mkdir()
    from vfx_harness.domain.brief import load_shot

    shot = load_shot(tmp_path)
    camera = _unit("camera_rig", roles=["cam_rig"], contract_id="cam-spine")
    card = compile_unit_scope(unit=camera, layer_id="1", contracts=_contracts())
    text = builder_kickoff(
        shot,
        Milestone("1@camera_rig", 1, "refs/a.png", "rig exists", ()),
        unit_scope=card,
    )
    assert "UNIT SCOPE CARD" in text
    assert "cam_rig" in text
    assert "bvfx_camera_rig" in text
    assert "cam.blockout_fg" not in text
    helpers = {row["name"] for row in card["helpers"]}
    assert "bvfx_import_asset" in helpers
    assert "bvfx_import_construction" not in helpers
    assert "AVAILABLE ASSETS" not in text
    assert "bvfx_import_construction()" not in text


def test_generate_unit_scope_hides_import_asset_and_kickoff_names_construction(
    tmp_path: Path,
) -> None:
    (tmp_path / "brief.md").write_text(
        "---\nid: fixture\nframes: 24\nfps: 24\n---\nA fixture shot.\n",
        encoding="utf-8",
    )
    (tmp_path / "refs").mkdir()
    from vfx_harness.domain.brief import load_shot

    unit = _unit(
        "prop_source",
        roles=["prop.shell"],
        contract_id="prop-count",
        provides=["geometry"],
        construction={"route": "generate", "witnesses": ["refobs-abc123"]},
    )
    contracts = [
        {
            "id": "prop-count",
            "kind": "object_count",
            "roles": ["prop.shell"],
            "frame": 1,
            "op": "eq",
            "value": 1,
        }
    ]
    card = compile_unit_scope(unit=unit, layer_id="2", contracts=contracts)
    names = {row["name"] for row in card["helpers"]}
    assert "bvfx_import_construction" in names
    assert "bvfx_import_asset" not in names
    assert card["construction"]["route"] == "generate"
    dumped = format_unit_scope_card(card)
    assert "bvfx_import_construction" in dumped
    assert "construction.route: generate" in dumped

    shot = load_shot(tmp_path)
    text = builder_kickoff(
        shot,
        Milestone("2@prop_source", 1, "refs/a.png", "source exists", ()),
        unit_scope=card,
    )
    assert "bvfx_import_construction()" in text
    assert "AVAILABLE ASSETS" not in text
