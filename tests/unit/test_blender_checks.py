from __future__ import annotations


def test_blender_check_loader_resolves_dependency_free_geometry_module() -> None:
    from vfx_harness.blender import checks

    # two points inside the frustum: screen (0.4, 0.3) and (0.6, 0.6)
    result = checks.frustum_union_ndc([(-0.2, 0.4, 0.0, 1.0), (0.2, -0.2, 0.0, 1.0)])

    assert result is not None
    assert result["bbox"] == [0.4, 0.3, 0.6, 0.6]
    assert result["points_inside"] == 2
