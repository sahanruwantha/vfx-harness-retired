"""A data-block property row needs a carrier-producing unit in its closure (HIR-0185)."""

from __future__ import annotations

from vfx_harness.domain.data_block_carriers import (
    data_block_carrier_gaps,
    data_block_property_family,
    describe_gap,
    row_data_block_paths,
)
from vfx_harness.domain.work_units import WorkUnit


def _unit(
    unit_id: str, *, roles: list[str], contract_id: str, kind: str, depends_on=(), provides=(), controls=()
) -> WorkUnit:
    return WorkUnit.parse(
        {
            "id": unit_id,
            "title": unit_id,
            "plan": f"plans/units/{unit_id}.md",
            "depends_on": list(depends_on),
            "mutates": {
                "mode": "scoped",
                "roles": roles,
                "controls": list(controls),
                "control_roles": dict.fromkeys(controls, roles),
                "script_spans": [f"build/units/02/{unit_id}.py"],
            },
            "protects": {"selector": "all_active_upstream_interfaces", "resolve_to_explicit_ids_at": "freeze"},
            "look_capabilities": [],
            "provides": list(provides),
            "evaluation": {
                "primary_judge": 1,
                "judge": [{"frame": 1, "ref": "refs/a.png"}],
                "temporal_evidence": "keyframes" if kind == "keyframe_schedule" else "none",
                "claims": [
                    {
                        "id": f"{unit_id}-claim",
                        "proposition": f"{unit_id} does its work",
                        "axis": "lighting",
                        "property": kind,
                        "subject_roles": roles,
                        "subject_controls": list(controls),
                        "moments": [1],
                        "kind": "atomic",
                        "required": True,
                        "authority": "executable_required",
                        "repair_owner": unit_id,
                        "asserts": "temporal" if kind == "keyframe_schedule" else "scene",
                        "evidence": [{"kind": "scene_contract", "id": contract_id}],
                    }
                ],
            },
            "completion": "all_required_claims_and_protected_contracts_pass",
        },
        unit_id,
    )


def _schedule(contract_id: str, *, path: str, control: str) -> dict:
    return {
        "id": contract_id,
        "kind": "keyframe_schedule",
        "control_roles": [control],
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "lighting",
        "op": "min",
        "lo": 0,
        "samples": [
            {"frame": 1, "values": {path: 100.0}},
            {"frame": 24, "values": {path: 20.0}},
        ],
    }


def _count(contract_id: str, roles: list[str]) -> dict:
    return {
        "id": contract_id,
        "kind": "object_count",
        "roles": roles,
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "lighting",
        "op": "min",
        "lo": 1,
    }


def test_property_families_and_row_paths() -> None:
    assert data_block_property_family("data.energy") == "light"
    assert data_block_property_family("data.lens") == "camera"
    assert data_block_property_family("data.dof.focus_distance") is None
    assert data_block_property_family("location") is None
    assert row_data_block_paths(_schedule("s", path="data.energy", control="flicker")) == ("data.energy",)
    assert row_data_block_paths({"kind": "object_property", "property": "data.lens"}) == ("data.lens",)
    assert row_data_block_paths({"kind": "object_property", "property": "location"}) == ()


def test_control_unit_scheduling_a_light_property_needs_a_light_producer() -> None:
    """Run 20260903T100335Z-fa5dbb: streetlights were meshes; the flicker schedule had no Light."""
    rows = [
        _schedule("flicker-schedule", path="data.energy", control="flicker"),
        _count("fixtures", ["exterior.ground.lamp"]),
    ]
    mesh = _unit(
        "ground_island",
        roles=["exterior.ground.lamp"],
        contract_id="fixtures",
        kind="object_count",
        provides=["geometry"],
    )
    flicker = _unit(
        "streetlight_flicker",
        roles=["exterior.lightctrl.flicker"],
        contract_id="flicker-schedule",
        kind="keyframe_schedule",
        depends_on=["ground_island"],
        controls=["flicker"],
    )
    gaps = data_block_carrier_gaps([mesh, flicker], rows)
    assert [(gap.unit_id, gap.contract_id, gap.path, gap.family) for gap in gaps] == [
        ("streetlight_flicker", "flicker-schedule", "data.energy", "light")
    ]
    assert gaps[0].code == "missing_carrier"
    assert "no unit in its dependency closure or an earlier layer writes the light family" in describe_gap(gaps[0])

    # An earlier materialized layer that writes lights satisfies the row.
    assert data_block_carrier_gaps([mesh, flicker], rows, earlier_families=["light"]) == ()


def test_unknown_data_block_property_is_named() -> None:
    rows = [_schedule("odd", path="data.mystery", control="flicker")]
    unit = _unit(
        "ctl", roles=["exterior.lightctrl.flicker"], contract_id="odd", kind="keyframe_schedule", controls=["flicker"]
    )
    gaps = data_block_carrier_gaps([unit], rows)
    assert len(gaps) == 1 and gaps[0].code == "unknown_data_block_property"
    assert "not a registered data-block property" in describe_gap(gaps[0])
