from __future__ import annotations

import pytest

from vfx_harness.blender.semantic_tags import tag_control


class _Host(dict):
    name = "host"


def _validate(value: str) -> str:
    if not value or " " in value:
        raise ValueError("invalid token")
    return value


def test_control_tag_preserves_existing_semantic_role() -> None:
    host = _Host(bvfx_role="world.lighting_rig", bvfx_owner_layer="2")

    tag_control(host, "lighting_arc_intensity", "2", validate=_validate)

    assert host["bvfx_role"] == "world.lighting_rig"
    assert host["bvfx_control"] == "lighting_arc_intensity"


def test_control_tag_gives_untagged_node_legacy_role_identity() -> None:
    node = _Host()

    tag_control(node, "lookdev.motion_blur_node", "2", validate=_validate)

    assert node["bvfx_role"] == "lookdev.motion_blur_node"
    assert node["bvfx_control"] == "lookdev.motion_blur_node"
    assert node["bvfx_owner_layer"] == "2"


def test_control_tag_refuses_cross_owner_retag() -> None:
    host = _Host(bvfx_role="cam_rig", bvfx_owner_layer="1")

    with pytest.raises(ValueError, match="owned by layer 1"):
        tag_control(host, "lighting_arc_intensity", "2", validate=_validate)
