"""Derived write-cluster gate and typed publish interfaces (HIR-0083, HIR-0084)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.unit.test_plan_improvements import _layer_doc, _write
from tests.unit.test_plan_records import _add_deferred_layer, _candidate, _jit_payload
from tests.unit.test_plan_records import _write as _write_plan
from vfx_harness.agents.unit_scope import (
    compile_predecessor_interface,
    compile_scope_with_predecessors,
    compile_unit_scope,
)
from vfx_harness.domain.atomicity import (
    ATOMICITY_RULE,
    HELPER_INSTRUMENT_FAMILY,
    KIND_INSTRUMENT_FAMILY,
    UNRESOLVED_FAMILY_RULE,
    atomicity_gaps,
    instrument_family_for_row,
    write_clusters,
)
from vfx_harness.domain.publish_interfaces import (
    REFERENCE_ONLY_EXPORTS_RULE,
    SCHEMA,
    compile_unit_publish_interfaces,
    parse_publish_interface,
)
from vfx_harness.domain.work_units import (
    CONSUME_INTERFACE_RULE,
    WorkUnit,
    ready_units,
    unit_requires_surface_visibility,
)
from vfx_harness.evaluation.plan_gate import _check_evidence_coherence
from vfx_harness.evidence.scene_checks import SUPPORTED_KINDS
from vfx_harness.orchestration.unit_state import (
    digest_matched_passed,
    initialize,
    ready_from_durable_state,
    replan_effects,
    unit_digest,
)


def _claim(uid: str, *, roles: list[str], contract_id: str, repair_owner: str | None = None) -> dict:
    return {
        "id": f"claim.{uid}",
        "proposition": f"{uid} holds",
        "axis": "form",
        "property": "object_count",
        "subject_roles": roles,
        "subject_controls": [],
        "moments": [1],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": repair_owner or uid,
        "asserts": "scene",
        "evidence": [{"kind": "scene_contract", "id": contract_id}],
    }


def _unit(
    uid: str,
    *,
    roles: list[str],
    contract_id: str,
    depends_on: list[str] | None = None,
    dresses: list[str] | None = None,
    provides: list[str] | None = None,
    controls: list[str] | None = None,
    publishes: list[dict] | None = None,
    consumes: list[dict] | None = None,
    extra_contracts: list[str] | None = None,
) -> WorkUnit:
    subject = roles or dresses or ["x.role"]
    claim = _claim(uid, roles=subject, contract_id=contract_id)
    if extra_contracts:
        claim["evidence"] = (
            list(claim["evidence"])
            + [{"kind": "scene_contract", "id": cid} for cid in extra_contracts]
        )
    row = {
        "id": uid,
        "title": uid,
        "plan": f"plans/units/{uid}.md",
        "depends_on": depends_on or [],
        "mutates": {
            "mode": "scoped",
            "roles": roles,
            "controls": controls or [],
            "dresses": dresses or [],
            "script_spans": [f"build/units/01/{uid}.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 1,
            "judge": [{"frame": 1, "ref": "refs/a.png"}],
            "temporal_evidence": "none",
            "claims": [claim],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "provides": provides or [],
    }
    if publishes is not None:
        row["publishes"] = publishes
    if consumes is not None:
        row["consumes"] = consumes
    return WorkUnit.parse(row, f"unit.{uid}")


def _instance_publish(role: str, contract_id: str, iid: str = "iris.blade.instance_interface") -> list[dict]:
    return [{
        "id": iid,
        "kind": "instance_source",
        "schema": SCHEMA,
        "exports": {"role": role, "mesh_contract_id": contract_id},
    }]


def _consume(producer: str, interface_id: str, kind: str = "instance_source") -> list[dict]:
    return [{"producer": producer, "interface_id": interface_id, "kind": kind}]


def _energy_row(row_id: str, roles: list[str]) -> dict:
    return {
        **_count_row(row_id, roles, kind="object_property"),
        "property": "data.energy",
        "op": "min",
        "lo": 1,
    }


def _count_row(row_id: str, roles: list[str], *, kind: str = "object_count") -> dict:
    return {
        "id": row_id,
        "kind": kind,
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "form",
        "roles": roles,
        "op": "eq",
        "value": 1,
    }


def test_kind_registry_covers_every_supported_scene_kind() -> None:
    assert set(KIND_INSTRUMENT_FAMILY) == set(SUPPORTED_KINDS)


def test_helper_registry_covers_every_injected_helper() -> None:
    from vfx_harness.agents.unit_scope import helper_inventory

    names = {row["name"] for row in helper_inventory()}
    assert names == set(HELPER_INSTRUMENT_FAMILY)


@pytest.mark.parametrize("role", ["product.camera_target", "motion.aim_control"])
def test_fixed_transform_control_is_one_placement_cluster(role: str) -> None:
    """A fixed Empty/control is placement work, not mesh plus keyframe work."""
    unit = _unit(
        "fixed_target",
        roles=[role],
        contract_id="target-x",
        extra_contracts=["target-static"],
        controls=["target.hold"],
    )
    rows = [
        {
            **_count_row("target-x", [role], kind="object_property"),
            "property": "location.0",
            "op": "band",
            "lo": -0.1,
            "hi": 0.1,
        },
        {
            **_count_row("target-static", [role], kind="animation_count"),
            "op": "max",
            "hi": 0,
        },
    ]

    assert instrument_family_for_row(rows[0]) == "control"
    assert instrument_family_for_row(rows[1]) is None
    assert [cluster.label() for cluster in write_clusters(unit, rows)] == [
        f"{role}/control_host/control"
    ]
    assert not atomicity_gaps([unit], rows, layer_id="1")


def test_positive_animation_count_remains_keyframe_work() -> None:
    row = {
        **_count_row("animated", ["motion.control"], kind="animation_count"),
        "op": "min",
        "lo": 1,
    }

    assert instrument_family_for_row(row) == "keyframe"


def test_mixed_light_and_volume_namespaces_are_two_clusters() -> None:
    unit = _unit(
        "lighting_atmosphere",
        roles=["world.lighting_rig", "world.atmosphere_volume"],
        contract_id="light-count",
        extra_contracts=["volume-density"],
        controls=["lighting_arc_intensity", "atmosphere_density"],
    )
    rows = [
        _energy_row("light-count", ["world.lighting_rig"]),
        {
            **_count_row("volume-density", ["world.atmosphere_volume"], kind="node_socket_value"),
            "graph": "world",
            "socket": "Density",
            "node_roles": ["world.atmosphere_volume"],
        },
    ]
    clusters = write_clusters(unit, rows)
    labels = {cluster.label() for cluster in clusters}
    assert any("world.lighting_rig" in label for label in labels)
    assert any("world.atmosphere_volume" in label for label in labels)
    assert len(clusters) > 1
    gaps = atomicity_gaps([unit], rows, layer_id="2")
    mixed = [gap for gap in gaps if gap.code == "mixed_clusters"]
    assert mixed and "world.lighting_rig" in mixed[0].detail
    assert "world.atmosphere_volume" in mixed[0].detail
    assert ATOMICITY_RULE in mixed[0].detail


def test_authored_coherent_family_does_not_publish_two_clusters() -> None:
    unit = _unit(
        "atmosphere_and_bloom",
        roles=["world.bloom", "world.atmosphere_volume"],
        contract_id="bloom-nodes",
        extra_contracts=["volume-density"],
    )
    rows = [
        {
            **_count_row("bloom-nodes", ["world.bloom"], kind="compositor_enabled"),
            "node_group_roles": ["world.bloom"],
        },
        {
            **_count_row("volume-density", ["world.atmosphere_volume"], kind="node_socket_value"),
            "graph": "world",
            "socket": "Density",
            "node_roles": ["world.atmosphere_volume"],
        },
    ]
    raw = {"id": "atmosphere_and_bloom", "coherent_family": "atmosphere"}
    gaps = atomicity_gaps([unit], rows, layer_id="2", raw_stages=[raw])
    codes = {gap.code for gap in gaps}
    assert "padding" in codes
    assert "mixed_clusters" in codes


def test_dressing_is_not_a_geometry_write_cluster() -> None:
    unit = _unit(
        "materials",
        roles=["lookdev.primary_material"],
        contract_id="mat-count",
        dresses=["cam.blockout_fg"],
    )
    rows = [
        {
            "id": "mat-count",
            "kind": "material_count",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "form",
            "material_roles": ["lookdev.primary_material"],
            "op": "min",
            "lo": 1,
        }
    ]
    clusters = write_clusters(unit, rows)
    assert len(clusters) == 1
    assert clusters[0].role_namespace == "lookdev.primary_material"
    assert not atomicity_gaps([unit], rows, layer_id="2")


def test_visibility_observation_is_not_a_second_cluster() -> None:
    rows = [
        _count_row("cam-count", ["cam.rig"]),
        {
            "id": "hero-vis",
            "kind": "visible_fraction",
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "1",
            "lifecycle": "layer",
            "axis": "camera_framing",
            "roles": ["world.proxy_core"],
            "frame": 1,
            "op": "min",
            "lo": 0.25,
        },
    ]
    claim = _claim("camera", roles=["cam.rig"], contract_id="cam-count")
    claim["evidence"] = [
        {"kind": "scene_contract", "id": "cam-count"},
        {"kind": "scene_contract", "id": "hero-vis"},
    ]
    parsed = WorkUnit.parse(
        {
            "id": "camera",
            "title": "camera",
            "plan": "plans/units/camera.md",
            "depends_on": [],
            "mutates": {
                "mode": "scoped",
                "roles": ["cam.rig"],
                "controls": [],
                "script_spans": ["build/units/01/camera.py"],
            },
            "protects": {
                "selector": "all_active_upstream_interfaces",
                "resolve_to_explicit_ids_at": "freeze",
            },
            "evaluation": {
                "primary_judge": 1,
                "judge": [{"frame": 1, "ref": "refs/a.png"}],
                "temporal_evidence": "none",
                "claims": [claim],
            },
            "completion": "all_required_claims_and_protected_contracts_pass",
            "provides": ["camera"],
        },
        "unit.camera",
    )
    assert len(write_clusters(parsed, rows)) == 1
    assert not [
        gap
        for gap in atomicity_gaps([parsed], rows, layer_id="1")
        if gap.code == "mixed_clusters"
    ]


def test_assembly_may_not_mutate_consumed_producer_roles() -> None:
    blade = _unit(
        "iris_blade_master",
        roles=["iris.blade_master"],
        contract_id="blade-mesh",
        provides=["geometry"],
        publishes=_instance_publish("iris.blade_master", "blade-mesh"),
    )
    assembly = _unit(
        "iris_assembly",
        roles=["iris.assembly", "iris.blade_master"],
        contract_id="assembly-count",
        depends_on=["iris_blade_master"],
        consumes=_consume("iris_blade_master", "iris.blade.instance_interface"),
    )
    rows = [
        {
            **_count_row("blade-mesh", ["iris.blade_master"], kind="mesh_vertex_count"),
            "frame": 1,
        },
        _count_row("assembly-count", ["iris.assembly"]),
    ]
    mixed = _unit(
        "iris_together",
        roles=["iris.blade_master", "iris.assembly"],
        contract_id="together",
    )
    assert any(gap.code == "mixed_clusters" for gap in atomicity_gaps([mixed], rows, layer_id="3"))
    gaps = atomicity_gaps([blade, assembly], rows, layer_id="3")
    assert any(
        gap.unit_id == "iris_assembly" and gap.code == "consumed_mutation"
        for gap in gaps
    )
    hidden = _unit(
        "iris_hidden",
        roles=["world.lighting_rig", "world.atmosphere_volume"],
        contract_id="together",
        depends_on=["iris_blade_master"],
        consumes=_consume("iris_blade_master", "iris.blade.instance_interface"),
    )
    hidden_gaps = atomicity_gaps(
        [blade, hidden],
        [
            *rows,
            _energy_row("light-count", ["world.lighting_rig"]),
            {
                **_count_row("volume-density", ["world.atmosphere_volume"], kind="node_socket_value"),
                "graph": "world",
                "socket": "Density",
                "node_roles": ["world.atmosphere_volume"],
            },
        ],
        layer_id="3",
    )
    assert any(
        gap.unit_id == "iris_hidden" and gap.code == "mixed_clusters"
        for gap in hidden_gaps
    )


def test_assembly_consumes_instance_source_without_mutating_producer() -> None:
    blade = _unit(
        "iris_blade_master",
        roles=["iris.blade_master"],
        contract_id="blade-mesh",
        provides=["geometry"],
        publishes=_instance_publish("iris.blade_master", "blade-mesh"),
    )
    assembly = _unit(
        "iris_assembly",
        roles=["iris.assembly"],
        contract_id="assembly-count",
        depends_on=["iris_blade_master"],
        consumes=_consume("iris_blade_master", "iris.blade.instance_interface"),
        provides=["geometry"],
    )
    rows = [
        {
            **_count_row("blade-mesh", ["iris.blade_master"], kind="mesh_vertex_count"),
            "frame": 1,
        },
        _count_row("assembly-count", ["iris.assembly"]),
    ]
    gaps = atomicity_gaps([blade, assembly], rows, layer_id="3")
    assert not [
        gap
        for gap in gaps
        if gap.unit_id == "iris_assembly"
        and gap.code in {"mixed_clusters", "consumed_mutation", "missing_consumption", "incompatible_interface"}
    ]


def test_prose_origin_export_is_refused() -> None:
    unit = _unit(
        "blade", roles=["iris.blade_master"], contract_id="blade-mesh", provides=["geometry"]
    )
    with pytest.raises(ValueError, match="hinge") as raised:
        parse_publish_interface(
            {
                "id": "iris.blade.instance_interface",
                "kind": "instance_source",
                "schema": SCHEMA,
                "exports": {"origin": "hinge"},
            },
            "publishes[0]",
            legal_tokens={"iris.blade_master", "blade-mesh"},
            layer_id="3",
            unit_id="blade",
        )
    assert REFERENCE_ONLY_EXPORTS_RULE in str(raised.value)
    legal = compile_unit_publish_interfaces(
        unit,
        layer_id="3",
        bound_contract_ids=["blade-mesh"],
        authored=[{
            "id": "iris.blade.instance_interface",
            "kind": "instance_source",
            "schema": SCHEMA,
            "exports": {
                "role": "iris.blade_master",
                "mesh_contract_id": "blade-mesh",
            },
        }],
        instrument_family="mesh",
        unit_digest="abc",
    )
    assert legal[0].kind == "instance_source"
    assert dict(legal[0].exports)["mesh_contract_id"] == "blade-mesh"


def test_stale_producer_digest_omits_publish_interfaces() -> None:
    unit = _unit(
        "blade", roles=["iris.blade_master"], contract_id="blade-mesh", provides=["geometry"]
    )
    card = compile_unit_scope(
        unit=unit,
        layer_id="3",
        contracts=[
            _count_row("blade-mesh", ["iris.blade_master"], kind="mesh_vertex_count") | {"frame": 1}
        ],
        helpers=(),
        unit_digest="producer-digest",
    )
    assert card["publish_interfaces"]
    live = compile_predecessor_interface(
        card,
        producer_digest="producer-digest",
        durable_hash="producer-digest",
        durable_status="passed",
    )
    assert live["publish_interfaces"]
    stale = compile_predecessor_interface(
        card,
        producer_digest="producer-digest",
        durable_hash="other-digest",
        durable_status="passed",
    )
    assert stale["publish_interfaces"] == []
    assert stale["dependency_status"] == "stale"
    missing_hash = compile_predecessor_interface(
        card,
        producer_digest="producer-digest",
        durable_hash="",
        durable_status="passed",
    )
    assert missing_hash["publish_interfaces"] == []
    assert missing_hash["dependency_status"] == "stale"
    superseded = compile_predecessor_interface(
        card,
        producer_digest="producer-digest",
        durable_hash="producer-digest",
        durable_status="superseded",
    )
    assert superseded["publish_interfaces"] == []


def test_assembly_is_unready_until_producer_digest_and_interface_match(tmp_path: Path) -> None:
    blade = _unit(
        "iris_blade_master",
        roles=["iris.blade_master"],
        contract_id="blade-mesh",
        provides=["geometry"],
        publishes=_instance_publish("iris.blade_master", "blade-mesh"),
    )
    assembly = _unit(
        "iris_assembly",
        roles=["iris.assembly"],
        contract_id="assembly-count",
        depends_on=["iris_blade_master"],
        consumes=_consume("iris_blade_master", "iris.blade.instance_interface"),
        provides=["geometry"],
    )
    initialize(tmp_path, "3", (blade, assembly), plan_hash="plan-v1")
    from vfx_harness.orchestration.unit_state import load, transition

    for status in ("planning", "building", "frozen", "evaluating", "passed"):
        transition(tmp_path, "3", blade.id, status, reason="test")
    state = load(tmp_path, "3")
    passed = {uid for uid, row in state["units"].items() if row["status"] == "passed"}
    sealed = digest_matched_passed(state, (blade, assembly))
    ready = ready_units((blade, assembly), passed, sealed_producers=sealed)
    assert [unit.id for unit in ready] == ["iris_assembly"]

    state["units"][blade.id]["unit_hash"] = "0" * 64
    stale_ready = ready_units(
        (blade, assembly),
        passed,
        sealed_producers=digest_matched_passed(state, (blade, assembly)),
    )
    assert stale_ready == ()
    assert unit_digest(blade) != "0" * 64

    derived_only = _unit(
        "iris_blade_master",
        roles=["iris.blade_master"],
        contract_id="blade-mesh",
        provides=["geometry"],
    )
    unmatched = ready_units(
        (derived_only, assembly),
        {"iris_blade_master"},
        sealed_producers={"iris_blade_master"},
    )
    assert unmatched == ()
    gaps = atomicity_gaps(
        [derived_only, assembly],
        [_count_row("blade-mesh", ["iris.blade_master"], kind="mesh_vertex_count")],
        layer_id="3",
    )
    assert any(gap.code == "incompatible_interface" for gap in gaps)
    assert CONSUME_INTERFACE_RULE in " ".join(gap.detail for gap in gaps)

    status_only = _unit(
        "iris_finish_after_blade",
        roles=["iris.finish"],
        contract_id="finish-count",
        depends_on=["iris_blade_master"],
        provides=["geometry"],
    )
    ready_status = ready_units(
        (blade, status_only),
        {"iris_blade_master"},
        sealed_producers={"iris_blade_master"},
    )
    assert ready_status == (status_only,)
    assert not [
        gap
        for gap in atomicity_gaps(
            [blade, status_only],
            [_count_row("finish-count", ["iris.finish"], kind="mesh_vertex_count")],
            layer_id="3",
        )
        if gap.code == "missing_consumption"
    ]


@pytest.mark.parametrize(
    ("producer_id", "successor_id"),
    [
        ("product_master", "product_assembly"),
        ("motion_curve", "motion_finish"),
    ],
)
def test_ready_query_refreshes_state_after_producer_passes(
    tmp_path: Path, producer_id: str, successor_id: str
) -> None:
    """A scheduling decision may not reuse the snapshot from before a unit ran."""
    producer = _unit(
        producer_id,
        roles=[f"{producer_id}.source"],
        contract_id=f"{producer_id}.contract",
    )
    successor = _unit(
        successor_id,
        roles=[f"{successor_id}.result"],
        contract_id=f"{successor_id}.contract",
        depends_on=[producer_id],
    )
    initialize(tmp_path, "1", (producer, successor), plan_hash="plan-v1")
    from vfx_harness.orchestration.unit_state import load, transition

    stale = load(tmp_path, "1")
    for status in ("planning", "building", "frozen", "evaluating", "passed"):
        transition(tmp_path, "1", producer_id, status, reason="test")

    assert digest_matched_passed(stale, (producer, successor)) == set()
    ready = ready_from_durable_state(
        tmp_path,
        "1",
        (producer, successor),
        eligible_passed={producer_id},
    )
    assert [unit.id for unit in ready] == [successor_id]


@pytest.mark.parametrize("role", ["product.camera_target", "motion.aim_control"])
def test_control_only_unit_has_no_rendered_visibility_debt(role: str) -> None:
    control = _unit("control_point", roles=[role], contract_id="point-placement")
    geometry = _unit(
        "rendered_subject",
        roles=[role],
        contract_id="subject-shape",
        provides=["geometry"],
    )

    assert unit_requires_surface_visibility(control) is False
    assert unit_requires_surface_visibility(geometry) is True


def test_authored_interface_change_invalidates_producer_digest() -> None:
    blade = _unit(
        "iris_blade_master",
        roles=["iris.blade_master"],
        contract_id="blade-mesh",
        provides=["geometry"],
        publishes=_instance_publish("iris.blade_master", "blade-mesh"),
    )
    changed = _unit(
        "iris_blade_master",
        roles=["iris.blade_master"],
        contract_id="blade-mesh",
        provides=["geometry"],
        publishes=_instance_publish(
            "iris.blade_master", "blade-mesh", iid="iris.blade.instance_interface.v2"
        ),
    )
    assembly = _unit(
        "iris_assembly",
        roles=["iris.assembly"],
        contract_id="assembly-count",
        depends_on=["iris_blade_master"],
        consumes=_consume("iris_blade_master", "iris.blade.instance_interface"),
        provides=["geometry"],
    )
    assert unit_digest(blade) != unit_digest(changed)
    effects = replan_effects((blade, assembly), (changed, assembly))
    assert effects["changed"] == ["iris_blade_master"]
    assert "iris_assembly" in effects["invalidated"]


def test_same_namespace_light_and_volume_without_write_kinds_is_unresolved() -> None:
    unit = _unit(
        "rig_mix",
        roles=["world.rig.light", "world.rig.volume"],
        contract_id="rig-count",
        controls=["rig_energy", "rig_density"],
    )
    rows = [_count_row("rig-count", ["world.rig.light", "world.rig.volume"])]
    assert write_clusters(unit, rows) == ()
    gaps = atomicity_gaps([unit], rows, layer_id="2")
    unresolved = [gap for gap in gaps if gap.code == "unresolved_family"]
    assert unresolved
    assert "world.rig" in unresolved[0].detail
    assert UNRESOLVED_FAMILY_RULE in unresolved[0].detail
    assert not [gap for gap in gaps if gap.code == "mixed_clusters"]


def test_same_namespace_light_and_volume_write_kinds_are_two_clusters() -> None:
    unit = _unit(
        "rig_mix",
        roles=["world.rig.light", "world.rig.volume"],
        contract_id="rig-energy",
        extra_contracts=["rig-density"],
        controls=["rig_energy", "rig_density"],
    )
    rows = [
        _energy_row("rig-energy", ["world.rig.light"]),
        {
            **_count_row("rig-density", ["world.rig.volume"], kind="node_socket_value"),
            "graph": "world",
            "socket": "Density",
            "node_roles": ["world.rig.volume"],
        },
    ]
    labels = {cluster.label() for cluster in write_clusters(unit, rows)}
    assert any("/light" in label and "world.rig" in label for label in labels)
    assert any("/volume" in label and "world.rig" in label for label in labels)
    mixed = [gap for gap in atomicity_gaps([unit], rows, layer_id="2") if gap.code == "mixed_clusters"]
    assert mixed


def test_same_namespace_partial_family_coverage_is_unresolved() -> None:
    unit = _unit(
        "rig_mix",
        roles=["world.rig.light", "world.rig.volume"],
        contract_id="rig-energy",
    )
    rows = [_energy_row("rig-energy", ["world.rig.light"])]
    labels = {cluster.label() for cluster in write_clusters(unit, rows)}
    assert labels == {"world.rig/control_host/light"}
    gaps = atomicity_gaps([unit], rows, layer_id="2")
    assert any(gap.code == "unresolved_family" for gap in gaps)


def test_coordination_uses_exact_control_role_mapping_not_participant_names() -> None:
    unit = _unit(
        "balance",
        roles=["world.light.rig", "world.volume.fog", "asset.hero.mesh"],
        contract_id="hero-mesh",
        extra_contracts=["light-energy", "volume-density"],
        controls=["light_level", "fog_density"],
        provides=["geometry"],
    )
    unit = replace(
        unit,
        mutates=replace(
            unit.mutates,
            control_roles=(
                ("fog_density", ("world.volume.fog",)),
                ("light_level", ("world.light.rig",)),
            ),
        ),
        evaluation=replace(
            unit.evaluation,
            claims=(
                replace(
                    unit.evaluation.claims[0],
                    kind="interaction",
                    coordination_owner="balance",
                    participants=("lighting", "atmosphere"),
                    controls=("light_level", "fog_density"),
                ),
            ),
        ),
    )
    rows = [
        _count_row("hero-mesh", ["asset.hero.mesh"], kind="mesh_vertex_count"),
        _energy_row("light-energy", ["world.light.rig"]),
        {
            **_count_row("volume-density", ["world.volume.fog"], kind="node_socket_value"),
            "graph": "world",
            "socket": "Density",
            "node_roles": ["world.volume.fog"],
        },
    ]
    assert [cluster.label() for cluster in write_clusters(unit, rows)] == [
        "asset.hero/geometry/mesh"
    ]
    assert not atomicity_gaps([unit], rows, layer_id="2")

    unbounded = replace(
        unit,
        evaluation=replace(
            unit.evaluation,
            claims=(
                replace(
                    unit.evaluation.claims[0],
                    participants=("world.light.rig", "world.volume.fog"),
                    controls=("missing_control",),
                ),
            ),
        ),
    )
    gaps = atomicity_gaps([unbounded], rows, layer_id="2")
    assert any(gap.code == "invalid_coordination" for gap in gaps)
    assert any(gap.code == "mixed_clusters" for gap in gaps)


def test_builder_card_includes_authored_interfaces_digest_and_predecessors() -> None:
    blade = _unit(
        "iris_blade_master",
        roles=["iris.blade_master"],
        contract_id="blade-mesh",
        provides=["geometry"],
        publishes=_instance_publish("iris.blade_master", "blade-mesh"),
    )
    assembly = _unit(
        "iris_assembly",
        roles=["iris.assembly"],
        contract_id="assembly-count",
        depends_on=["iris_blade_master"],
        consumes=_consume("iris_blade_master", "iris.blade.instance_interface"),
        provides=["geometry"],
    )
    rows = [
        {
            **_count_row("blade-mesh", ["iris.blade_master"], kind="mesh_vertex_count"),
            "frame": 1,
        },
        _count_row("assembly-count", ["iris.assembly"]),
    ]
    card = compile_scope_with_predecessors(
        unit=assembly,
        layer_id="3",
        contracts=rows,
        units=(blade, assembly),
        durable_state={
            "units": {
                blade.id: {"status": "passed", "unit_hash": unit_digest(blade)},
                assembly.id: {"status": "pending", "unit_hash": unit_digest(assembly)},
            }
        },
        helpers=(),
    )
    assert card["producer_unit_digest"] == unit_digest(assembly)
    assert card["consumes"] == [{
        "producer": "iris_blade_master",
        "interface_id": "iris.blade.instance_interface",
        "kind": "instance_source",
    }]
    assert any(row.get("kind") == "instance_source" for row in card["predecessor_publish_interfaces"])
    assert card["predecessor_publish_interfaces"][0]["producer"]["unit_digest"] == unit_digest(blade)
    live = compile_unit_scope(
        unit=assembly, layer_id="3", contracts=rows, unit_digest=unit_digest(assembly)
    )
    assert live["producer_unit_digest"] == unit_digest(assembly)
    assert live["publish_interfaces"]


def test_builder_card_exposes_only_exact_consumed_interfaces() -> None:
    blade = _unit(
        "iris_blade_master",
        roles=["iris.blade_master"],
        contract_id="blade-mesh",
        provides=["geometry"],
        publishes=[
            *_instance_publish("iris.blade_master", "blade-mesh"),
            {
                "id": "iris.blade.asset_interface",
                "kind": "asset_source",
                "exports": {"role": "iris.blade_master"},
            },
        ],
    )
    assembly = _unit(
        "iris_assembly",
        roles=["iris.assembly"],
        contract_id="assembly-count",
        depends_on=["iris_blade_master"],
        consumes=_consume("iris_blade_master", "iris.blade.instance_interface"),
        provides=["geometry"],
    )
    rows = [
        _count_row("blade-mesh", ["iris.blade_master"], kind="mesh_vertex_count"),
        _count_row("assembly-count", ["iris.assembly"]),
    ]
    card = compile_scope_with_predecessors(
        unit=assembly,
        layer_id="3",
        contracts=rows,
        units=(blade, assembly),
        durable_state={
            "units": {
                blade.id: {"status": "passed", "unit_hash": unit_digest(blade)},
            }
        },
        helpers=(),
    )
    expected = [("iris.blade.instance_interface", "instance_source")]
    assert [
        (row["id"], row["kind"])
        for row in card["predecessor_publish_interfaces"]
    ] == expected
    assert [
        (row["id"], row["kind"])
        for row in card["predecessor_interfaces"][0]["publish_interfaces"]
    ] == expected


def test_plan_gate_names_mixed_light_and_volume_clusters(tmp_path: Path) -> None:
    doc = _layer_doc()
    unit = doc["layers"][0]["stages"][0]
    unit["id"] = "lighting_atmosphere"
    unit["mutates"] = {
        **unit["mutates"],
        "roles": ["world.lighting_rig", "world.atmosphere_volume"],
        "controls": ["lighting_arc_intensity", "atmosphere_density"],
        "control_roles": {
            "lighting_arc_intensity": ["world.lighting_rig"],
            "atmosphere_density": ["world.atmosphere_volume"],
        },
    }
    unit["evaluation"]["claims"][0]["id"] = "mixed-claim"
    unit["evaluation"]["claims"][0]["repair_owner"] = "lighting_atmosphere"
    unit["evaluation"]["claims"][0]["subject_roles"] = ["world.lighting_rig"]
    unit["evaluation"]["claims"][0]["evidence"] = [
        {"kind": "scene_contract", "id": "light-count"},
        {"kind": "scene_contract", "id": "volume-density"},
    ]
    _write(tmp_path / "layers.json", doc)
    _write(
        tmp_path / "scene_checks.json",
        {
            "schema": 2,
            "contracts": [
                _count_row("light-count", ["world.lighting_rig"])
                | {
                    "owner_layer": "1",
                    "fault_owner": "1",
                    "activates_at": "1",
                    "axis": "camera_framing",
                },
                {
                    **_count_row(
                        "volume-density",
                        ["world.atmosphere_volume"],
                        kind="node_socket_value",
                    ),
                    "owner_layer": "1",
                    "fault_owner": "1",
                    "activates_at": "1",
                    "axis": "camera_framing",
                    "graph": "world",
                    "socket": "Density",
                    "node_roles": ["world.atmosphere_volume"],
                },
            ],
        },
    )
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})
    findings, _ = _check_evidence_coherence(tmp_path)
    mixed = [finding for finding in findings if finding.check == "unit-atomicity"]
    assert mixed
    assert mixed[0].blocking
    assert "world.lighting_rig" in mixed[0].what
    assert "world.atmosphere_volume" in mixed[0].what


def test_plan_gate_rejects_control_owned_point_projection(tmp_path: Path) -> None:
    doc = _layer_doc()
    unit = doc["layers"][0]["stages"][0]
    unit["provides"] = []
    claim = unit["evaluation"]["claims"][0]
    claim["property"] = "projected_origin_x"
    claim["asserts"] = "projected_composition"
    claim["evidence"] = [{"kind": "scene_contract", "id": "target-x"}]
    _write(tmp_path / "layers.json", doc)
    _write(tmp_path / "scene_checks.json", {
        "schema": 2,
        "contracts": [{
            "id": "target-x",
            "kind": "projected_origin_x",
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "1",
            "lifecycle": "layer",
            "axis": "camera_framing",
            "roles": ["hero"],
            "frame": 1,
            "op": "band",
            "lo": 0.45,
            "hi": 0.55,
        }],
    })
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)

    point = [finding for finding in findings if finding.check == "point-projection-owner"]
    assert point and point[0].blocking
    assert "does not provide camera" in point[0].what


@pytest.mark.parametrize("role", ["product.camera_target", "motion.aim_control"])
def test_camera_observes_point_only_through_consumed_interface(
    tmp_path: Path, role: str
) -> None:
    doc = _layer_doc(temporal_id="target-x")
    camera = doc["layers"][0]["stages"][0]
    camera["id"] = "camera"
    camera["plan"] = "plans/01_camera/01_camera.md"
    camera["depends_on"] = ["target"]
    camera["provides"] = ["camera"]
    camera["mutates"] = {
        **camera["mutates"],
        "roles": ["camera.rig"],
        "controls": [],
        "control_roles": {},
    }
    camera["evaluation"]["claims"][0].update({
        "id": "camera-aligns-target",
        "property": "projected_origin_x",
        "subject_roles": ["camera.rig", role],
        "subject_controls": [],
        "repair_owner": "camera",
        "asserts": "projected_composition",
        "evidence": [{"kind": "scene_contract", "id": "target-x"}],
    })
    target = json.loads(json.dumps(camera))
    target["id"] = "target"
    target["plan"] = "plans/01_camera/00_target.md"
    target["depends_on"] = []
    target["provides"] = []
    target.pop("consumes", None)
    target["mutates"] = {
        **target["mutates"],
        "roles": [role],
        "controls": ["target.hold"],
        "control_roles": {"target.hold": [role]},
        "script_spans": ["build/units/01_camera/00_target.py"],
    }
    target["evaluation"]["claims"] = [{
        **target["evaluation"]["claims"][0],
        "id": "target-exists",
        "property": "object_count",
        "subject_roles": [role],
        "subject_controls": ["target.hold"],
        "repair_owner": "target",
        "asserts": "scene",
        "evidence": [{"kind": "scene_contract", "id": "target-count"}],
    }]
    target["publishes"] = [
        {
            "id": "target.point",
            "kind": "placement_control",
            "exports": {"role": role},
        },
        {
            "id": "target.other",
            "kind": "placement_control",
            "exports": {"control": "target.hold"},
        },
    ]
    camera["consumes"] = [{
        "producer": "target",
        "interface_id": "target.point",
        "kind": "placement_control",
    }]
    doc["layers"][0]["stages"] = [target, camera]
    _write(tmp_path / "layers.json", doc)
    _write(tmp_path / "scene_checks.json", {
        "schema": 2,
        "contracts": [
            _count_row("target-count", [role]),
            {
                **_count_row("target-x", [role], kind="projected_origin_x"),
                "frame": 1,
                "op": "band",
                "lo": 0.45,
                "hi": 0.55,
            },
        ],
    })
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)
    assert not any(f.check == "point-projection-interface" for f in findings)
    assert not any(
        f.check in {"role-selector-closure", "control-selector-closure"}
        and "target-x" in f.where
        for f in findings
    )

    camera["consumes"] = [{
        "producer": "target",
        "interface_id": "target.other",
        "kind": "placement_control",
    }]
    _write(tmp_path / "layers.json", doc)
    findings, _ = _check_evidence_coherence(tmp_path)
    wrong = [f for f in findings if f.check == "point-projection-interface"]
    assert wrong and wrong[0].blocking
    assert "consumes no compatible typed interface" in wrong[0].what

    camera.pop("consumes")
    _write(tmp_path / "layers.json", doc)
    findings, _ = _check_evidence_coherence(tmp_path)
    interface = [f for f in findings if f.check == "point-projection-interface"]
    assert interface and interface[0].blocking
    assert "consumes no compatible typed interface" in interface[0].what


def test_camera_cannot_mutate_the_point_it_observes(tmp_path: Path) -> None:
    doc = _layer_doc(temporal_id="target-x")
    camera = doc["layers"][0]["stages"][0]
    camera["provides"] = ["camera"]
    camera["mutates"]["roles"] = ["camera.rig", "target.point"]
    camera["evaluation"]["claims"][0].update({
        "subject_roles": ["camera.rig", "target.point"],
        "repair_owner": "move",
        "asserts": "projected_composition",
    })
    _write(tmp_path / "layers.json", doc)
    _write(tmp_path / "scene_checks.json", {
        "schema": 2,
        "contracts": [{
            **_count_row("target-x", ["target.point"], kind="projected_origin_x"),
            "frame": 1,
            "op": "band",
            "lo": 0.45,
            "hi": 0.55,
        }],
    })
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})

    findings, _ = _check_evidence_coherence(tmp_path)

    interface = [f for f in findings if f.check == "point-projection-interface"]
    assert interface and "also mutates observed selector" in interface[0].what


def test_materialization_refuses_mixed_clusters_and_keeps_single_cluster(
    tmp_path: Path,
) -> None:
    from vfx_harness.observability import run_artifacts
    from vfx_harness.orchestration.jit_materialization import inspect_materialization
    from vfx_harness.orchestration.plan_authority import publish_current

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "atomicity")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    polish = document["layer"]["stages"][0]
    polish["mutates"] = {
        **polish["mutates"],
        "roles": ["world.lighting_rig", "world.atmosphere_volume"],
        "controls": [],
        "control_roles": {},
    }
    polish["evaluation"]["claims"][0]["subject_roles"] = ["world.lighting_rig"]
    polish["evaluation"]["claims"][0]["subject_controls"] = []
    _write_plan(payload, document)
    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )
    assert materialized is None
    text = "\n".join(findings)
    assert "world.lighting_rig" in text
    assert "world.atmosphere_volume" in text
    assert ATOMICITY_RULE in text

    payload = _jit_payload(tmp_path, bundle.content_hash)
    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )
    assert materialized is not None
    assert not any("write-cluster" in line for line in findings)


def test_unknown_publish_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown"):
        parse_publish_interface(
            {
                "id": "x.publish",
                "kind": "hinge_prose",
                "schema": SCHEMA,
                "exports": {"role": "iris.blade_master"},
            },
            "publishes[0]",
            legal_tokens={"iris.blade_master"},
            layer_id="3",
            unit_id="blade",
        )
