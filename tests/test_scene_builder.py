"""Scene builder pure helpers (no SDK). Desk behaviour is covered in test_scene_desk.py."""

from __future__ import annotations

from agents.scene_builder import animation_block


def test_animation_block_names_the_arc():
    b = animation_block(30)
    assert "30-frame" in b and "start pose" in b and "keyframe_insert" in b


def test_animation_block_sets_the_frame_range():
    b = animation_block(48)
    assert "frame_end = 48" in b and "frame_start = 1" in b
