"""Materialization kickoff compiles the judgment-property rule from the sparse row."""

from __future__ import annotations

from vfx_harness.agents.planner import kickoff


def test_camera_layer_kickoff_names_camera_framing_as_its_only_judgment_property() -> None:
    block = kickoff._judgment_property_authority_block(
        {"id": "1", "jit": {"provides": {"camera": ["camera.*"]}}}
    )
    assert "['camera_framing', 'reference_identity', 'subject_appearance']" in block
    assert "must be 'camera_framing'" in block
    assert "refused here" in block


def test_form_layer_kickoff_sends_camera_framing_to_the_camera_owner() -> None:
    block = kickoff._judgment_property_authority_block({"id": "2", "jit": {"provides": {}}})
    assert "does not provide camera" in block
    assert "'subject_appearance' or 'reference_identity'" in block
