"""The animation layer — pure helpers (no Blender)."""

from __future__ import annotations

from scene.animation import barrel_roll_code, sample_indices


def test_sample_indices_picks_evenly_spaced_including_ends():
    assert sample_indices(48, 3) == [0, 24, 47]
    assert sample_indices(10, 3) == [0, 5, 9]
    assert sample_indices(2, 3) == [0, 1]  # fewer frames than samples → all
    assert sample_indices(0) == []


def test_barrel_roll_code_keyframes_camera_and_sets_frame_range():
    code = barrel_roll_code(frames=48)
    assert "scene.frame_start, scene.frame_end = 1, 48" in code
    assert 'cam.keyframe_insert("rotation_euler"' in code
    assert 'cam.keyframe_insert("location"' in code
    assert "scene.camera = cam" in code


def test_barrel_roll_has_blackout_and_two_worlds():
    code = barrel_roll_code(frames=48)
    assert "towerA" in code and "towerB" in code  # green world + purple world
    assert "(0.0, 0.0, 0.0, 1)" in code  # world keyed to black at the inversion (the blackout)
    assert "motion_blur" in code  # sells the speed / hides the seam
