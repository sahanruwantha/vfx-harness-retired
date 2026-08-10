"""Symbolic scene diagnostics — pure helpers."""

from __future__ import annotations

from scene.diagnostics import black_frame_hint, is_black, scene_digest

GRAPH = {
    "scene": {"active_camera": "cam", "engine": "BLENDER_EEVEE", "resolution": [768, 432]},
    "objects": [
        {"type": "MESH", "materials": []},
        {"type": "MESH", "materials": []},
        {"type": "LIGHT", "light": {"energy": 900.0}},
        {"type": "LIGHT", "light": {"energy": 100.0}},
        {"type": "CAMERA"},
    ],
}


def test_scene_digest_counts_and_energy():
    d = scene_digest(GRAPH)
    assert "5 objects" in d and "2 mesh" in d and "2 light" in d and "1 camera" in d
    assert "active_camera=cam" in d
    assert "total_light_energy=1000.0W" in d


def test_scene_digest_survives_empty():
    d = scene_digest({})
    assert "0 objects" in d


def test_is_black_threshold():
    assert is_black({"luma_mean": 0.0})
    assert is_black({"luma_mean": 0.02})
    assert not is_black({"luma_mean": 0.2})
    assert not is_black({})  # missing → treated as not-black (default 1.0)


def test_black_frame_hint_points_at_pipeline_not_geometry():
    hint = black_frame_hint(scene_digest(GRAPH))
    assert "BLACK" in hint
    assert "RENDER" in hint and "PIPELINE" in hint
    assert "compositor" in hint.lower()
    assert "2 light" in hint  # embeds the digest so the builder sees the scene has content
