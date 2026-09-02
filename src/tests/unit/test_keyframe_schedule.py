"""keyframe_schedule path misses are measured fails that name both sides (HIR-0050)."""

from __future__ import annotations

from vfx_harness.agents.builder import _scene_contract_issue
from vfx_harness.evidence.scene_checks import (
    _blender_probe,
    _evidence,
    _holds,
    keyframe_schedule_matching_frames,
    keyframe_schedule_miss_note,
    keyframe_schedule_path_aliases,
    keyframe_schedule_path_miss_value,
    keyframe_schedule_present_paths,
    validate_row,
)


def _schedule_row(**overrides) -> dict:
    row = {
        "id": "light-arc-schedule",
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "lighting",
        "kind": "keyframe_schedule",
        "roles": ["lookdev.lighting_rig"],
        "samples": [
            {"frame": 1, "values": {"energy": 0.2}},
            {"frame": 72, "values": {"energy": 0.55}},
        ],
        "op": "max",
        "hi": 0.05,
    }
    row.update(overrides)
    return row


def test_energy_aliases_object_and_data_block_paths() -> None:
    assert keyframe_schedule_path_aliases("energy") == ("energy", "data.energy")
    assert keyframe_schedule_path_aliases("location") == ("location", "data.location")
    assert keyframe_schedule_path_aliases("data.energy") == ("data.energy",)
    assert keyframe_schedule_path_aliases('["energy"]') == ('["energy"]',)


def test_object_data_energy_keys_satisfy_energy_sample() -> None:
    frames = keyframe_schedule_matching_frames(
        object_paths={"data.energy": {1, 72, 150, 204, 240}},
        data_paths={},
        sample_path="energy",
    )
    assert frames == {1, 72, 150, 204, 240}


def test_datablock_energy_keys_satisfy_energy_sample() -> None:
    frames = keyframe_schedule_matching_frames(
        object_paths={},
        data_paths={"energy": {1, 72}},
        sample_path="energy",
    )
    assert frames == {1, 72}


def test_custom_energy_prop_is_not_a_silent_alias() -> None:
    frames = keyframe_schedule_matching_frames(
        object_paths={'["energy"]': {1, 72, 150, 204, 240}},
        data_paths={},
        sample_path="energy",
    )
    assert frames == set()
    present = keyframe_schedule_present_paths(['["energy"]'], [])
    note = keyframe_schedule_miss_note(
        "Lamp",
        "energy",
        actual_frames=frames,
        expected_frames={1, 72, 150, 204, 240},
        present_paths=present,
    )
    assert "aliases ['energy', 'data.energy']" in note
    assert "fcurve data_paths present" in note
    assert '["energy"]' in note


def test_location_keys_still_match_without_data_block() -> None:
    frames = keyframe_schedule_matching_frames(
        object_paths={"location": {1, 240}},
        data_paths={},
        sample_path="location",
    )
    assert frames == {1, 240}


def test_path_miss_value_exceeds_hi() -> None:
    row = _schedule_row()
    value = keyframe_schedule_path_miss_value(row["hi"])
    assert value > float(row["hi"])
    assert _holds(row, value) is False
    assert _holds(row, 0.0) is True


def test_probe_resolves_data_paths_and_does_not_raise_a_path_miss() -> None:
    row = _schedule_row()
    assert validate_row(row) is None
    probe = _blender_probe([row], 1)
    compile(probe, "<keyframe-schedule-probe>", "exec")
    assert "actual_frames!=expected_frames" in probe
    assert "_path_aliases" in probe
    assert "_schedule_frames" in probe
    assert "fcurve data_paths present" in probe
    assert "data." in probe
    assert "scheduled object has no action" not in probe
    assert "raise ValueError(f'{path} keyframes" not in probe


def test_path_miss_evidence_is_a_failing_measurement_not_an_error() -> None:
    row = _schedule_row()
    miss = keyframe_schedule_path_miss_value(row["hi"])
    note = keyframe_schedule_miss_note(
        "Lamp",
        "energy",
        actual_frames=[],
        expected_frames={1, 72},
        present_paths=['["energy"]'],
    )
    packed = _evidence(
        [row],
        [{
            "id": row["id"],
            "value": miss,
            "note": note,
            "objects": ["Lamp"],
            "roles": [],
            "controls": [],
            "materials": [],
            "material_roles": [],
            "nodes": [],
            "error": "",
        }],
    )[0]
    assert packed["pass"] is False
    assert packed["value"] == miss
    assert "error" not in packed
    assert "fcurve data_paths present" in packed["note"]
    text = _scene_contract_issue(packed)
    assert "INAPPLICABLE" not in text
    assert "executable contract fails" in text
    assert "fcurve data_paths present" in text


def test_none_keyframe_row_is_not_a_binding_defect() -> None:
    text = _scene_contract_issue(
        {
            "id": "light-arc-schedule",
            "metric": "keyframe_schedule",
            "value": None,
            "target": "<= 0.05",
            "error": "energy keyframes [] != [1, 72, 150, 204, 240]",
        }
    )
    assert "INAPPLICABLE" not in text
    assert "binding defect, not a build defect" not in text
    assert "not a binding defect" in text
    assert "list_keyframes" in text


def test_smooth_fraction_none_stays_inapplicable() -> None:
    text = _scene_contract_issue(
        {
            "id": "shade",
            "metric": "smooth_fraction",
            "value": None,
            "target": ">= 0.9",
            "error": "no mesh to shade",
        }
    )
    assert "INAPPLICABLE to its subject" in text
    assert "re-materialization, not a repair" in text


class _Host:
    def __init__(self, name: str, type_: str, data=None, **attrs) -> None:
        self.name = name
        self.type = type_
        self.data = data
        for key, value in attrs.items():
            setattr(self, key, value)


class _CameraData:
    lens = 32.0


def test_data_block_path_types_out_hosts_without_a_data_block() -> None:
    from vfx_harness.evidence.scene_checks import data_block_carriers

    pivot = _Host("cam_rig", "EMPTY", location=(0.0, -40.0, 160.0))
    camera = _Host("camera", "CAMERA", data=_CameraData(), location=(0.0, 0.0, 0.0))

    carriers, typed_out = data_block_carriers([pivot, camera], ["data.lens"])
    assert [host.name for host in carriers] == ["camera"]
    assert [host.name for host in typed_out] == ["cam_rig"]

    # The bare alias resolves to the data-block too: the Empty still cannot carry it.
    carriers, typed_out = data_block_carriers([pivot, camera], ["lens"])
    assert [host.name for host in carriers] == ["camera"]
    assert [host.name for host in typed_out] == ["cam_rig"]


def test_object_level_paths_keep_every_host() -> None:
    from vfx_harness.evidence.scene_checks import data_block_carriers

    pivot = _Host("cam_rig", "EMPTY", location=(0.0, -40.0, 160.0))
    camera = _Host("camera", "CAMERA", data=_CameraData(), location=(0.0, 0.0, 0.0))

    carriers, typed_out = data_block_carriers([pivot, camera], ["location"])
    assert [host.name for host in carriers] == ["cam_rig", "camera"]
    assert typed_out == []


def test_a_data_block_that_lacks_the_attribute_is_not_typed_out() -> None:
    from vfx_harness.evidence.scene_checks import data_block_carriers

    class _MeshData:
        vertices = ()

    mesh = _Host("wall", "MESH", data=_MeshData())
    pivot = _Host("marker", "EMPTY")
    carriers, typed_out = data_block_carriers([mesh, pivot], ["data.lens"])
    # The mesh stays a (failing) measurement host; only the data-less Empty is typed out.
    assert [host.name for host in carriers] == ["wall"]
    assert [host.name for host in typed_out] == ["marker"]

    carriers, typed_out = data_block_carriers([pivot], ["data.lens"])
    assert carriers == []
    assert [host.name for host in typed_out] == ["marker"]


def test_probe_types_carriers_before_measuring_property_contracts() -> None:
    row = _schedule_row(samples=[
        {"frame": 38, "values": {"data.lens": 32.0}},
        {"frame": 200, "values": {"data.lens": 38.0}},
    ])
    probe = _blender_probe([row], 38)
    compile(probe, "<carrier-probe>", "exec")
    assert "carriers,typed_out=_carriers(objects,paths)" in probe
    assert "if not carriers: raise ValueError(_nocarrier(row,paths,typed_out))" in probe
    assert "carriers,typed_out=_carriers(objects,[row['property']])" in probe
    assert "no data-block, not a carrier" in probe
    assert "matched only hosts without a data-block" in probe
