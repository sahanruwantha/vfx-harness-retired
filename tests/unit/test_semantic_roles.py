"""One role alphabet: tagging, matching, and both-sides misses (HIR-0022 / HIR-0018).

Run 20260825T143912Z-0b5ab4 stored ``'cam.blockout_fg,cam.blockout_depth_tiers'`` as one
custom-property token. ``check_scene(object='cam_rig.camera')`` looked up a display name
that was actually a role. These tests pin both failures as unrepresentable.
"""

from __future__ import annotations

import inspect

import pytest

from vfx_harness.blender.checks import visual_subject_error
from vfx_harness.domain.semantic_roles import (
    format_object_miss,
    match_semantic,
    pick_objects,
    selector_punctuation_error,
    validate_role_token,
)
from vfx_harness.evidence.scene_checks import _blender_probe, _control_script, validate_row


def test_comma_joined_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="commas are not membership"):
        validate_role_token("cam.blockout_fg,cam.blockout_depth_tiers")


def test_whitespace_and_wildcard_tags_are_rejected() -> None:
    with pytest.raises(ValueError, match="dotted token"):
        validate_role_token("cam blockout")
    with pytest.raises(ValueError, match="dotted token"):
        validate_role_token("cam.blockout_*")
    with pytest.raises(ValueError, match="dotted token"):
        validate_role_token("")


def test_legal_dotted_tokens_round_trip() -> None:
    assert validate_role_token("cam_rig.camera") == "cam_rig.camera"
    assert validate_role_token("lookdev.pool_light_a") == "lookdev.pool_light_a"
    assert validate_role_token(" world.bloom.compositor ") == "world.bloom.compositor"


def test_match_semantic_is_fnmatch_and_empty_misses() -> None:
    assert match_semantic("cam.blockout_fg", ["cam.blockout_*"])
    assert not match_semantic("cam.blockout_fg", ["lookdev.*"])
    assert not match_semantic("cam.blockout_fg", [])
    assert match_semantic("cam.blockout_fg", "cam.blockout_fg")


def test_name_miss_names_present_roles() -> None:
    inventory = [
        {"name": "camera", "role": "cam_rig.camera"},
        {"name": "proxy", "role": "cam.blockout_fg"},
    ]
    text = format_object_miss(inventory=inventory, name="cam_rig.camera")
    assert "no object named 'cam_rig.camera'" in text
    assert "cam_rig.camera" in text.split("roles present:")[1]
    assert "pass role=" in text


def test_role_miss_names_present_names_and_roles() -> None:
    inventory = [{"name": "camera", "role": "cam_rig.camera"}]
    text = format_object_miss(inventory=inventory, role="iris_face")
    assert "role 'iris_face' matched no objects" in text
    assert "cam_rig.camera" in text
    assert "camera" in text


def test_shared_role_names_object_equals_not_a_more_exact_role() -> None:
    """HIR-0041: check_scene(role=world.volumetric_haze) already passed an exact
    role that nine hosts share. 'pass an exact role' is the wrong next action."""
    from vfx_harness.domain.semantic_roles import format_object_ambiguous

    text = format_object_ambiguous(
        role="world.volumetric_haze",
        hits=[
            {"name": "atmo_haze_domain", "role": "world.volumetric_haze"},
            {"name": "atmo_haze_shaft_blocker_00", "role": "world.volumetric_haze"},
        ],
    )
    assert "matched 2 objects" in text
    assert "object='atmo_haze_domain'" in text
    assert "OMIT role=" in text
    assert "mutually exclusive" in text
    assert "pass an exact role" not in text


def test_pick_objects_hits_role_and_refuses_both() -> None:
    inventory = [
        {"name": "camera", "role": "cam_rig.camera"},
        {"name": "rig", "role": "cam_rig"},
    ]
    hits = pick_objects(inventory, role="cam_rig.camera")
    assert [row["name"] for row in hits] == ["camera"]
    with pytest.raises(ValueError, match="not both"):
        pick_objects(inventory, role="cam_rig", name="camera")


def test_contract_selector_refuses_comma_membership() -> None:
    error = selector_punctuation_error(
        {"roles": ["cam.blockout_fg,cam.blockout_depth_tiers"]}
    )
    assert error is not None and "commas" in error
    row = {
        "id": "row",
        "kind": "object_count",
        "axis": "a",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "roles": ["cam.blockout_fg,cam.blockout_depth_tiers"],
    }
    assert "commas" in (validate_row(row) or "")
    assert selector_punctuation_error({"roles": ["cam.blockout_*"]}) is None


def test_probe_and_control_script_call_the_one_matcher() -> None:
    probe = inspect.getsource(_blender_probe)
    assert "_m=_checks.match_semantic" in probe
    assert "def _m(v,pats)" not in probe
    script = _control_script(
        {
            "graph": "world",
            "node_roles": ["world.bloom.compositor"],
            "socket": "Threshold",
        }
    )
    assert "match=_checks.match_semantic" in script
    assert "def match(value, patterns)" not in script


def test_reproduction_hint_addresses_role_not_display_name() -> None:
    from vfx_harness.agents.builder import _reproduction_hint

    hint = _reproduction_hint(
        {
            "kind": "bbox_width",
            "roles": ["cam.blockout_fg"],
            "objects": ["proxy"],
            "frame": 1,
        }
    )
    assert "role='cam.blockout_fg'" in hint
    assert "object='proxy'" not in hint


def test_visual_checks_route_light_hosts_to_contribution_pass() -> None:
    error = visual_subject_error("framing", "key_light", "LIGHT")

    assert error is not None
    assert "has no rendered bounding box" in error
    assert "render_pass(light='key_light'" in error
    assert visual_subject_error("motion", "key_light", "LIGHT") is None
    assert visual_subject_error("framing", "hero", "MESH") is None
