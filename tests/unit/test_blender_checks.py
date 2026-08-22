from __future__ import annotations


def test_blender_check_loader_resolves_dependency_free_geometry_module() -> None:
    from vfx_harness.blender import checks

    result = checks.framing_from_ndc([(0.4, 0.4, 1.0), (0.6, 0.6, 1.0)])

    assert result["ok"] and result["on_screen"] == 1.0
