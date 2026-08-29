from __future__ import annotations

import inspect


def test_blender_check_loader_resolves_dependency_free_geometry_module() -> None:
    from vfx_harness.blender import checks

    # two points inside the frustum: screen (0.4, 0.3) and (0.6, 0.6)
    result = checks.frustum_union_ndc([(-0.2, 0.4, 0.0, 1.0), (0.2, -0.2, 0.0, 1.0)])

    assert result is not None
    assert result["bbox"] == [0.4, 0.3, 0.6, 0.6]
    assert result["points_inside"] == 2


def test_live_and_contract_visibility_call_one_canonical_sampler() -> None:
    from vfx_harness.blender import checks
    from vfx_harness.evidence.scene_checks import _blender_probe

    live = inspect.getsource(checks.check_visibility)
    sampler = inspect.getsource(checks.surface_visible_fraction)
    row = {
        "id": "vis",
        "kind": "visible_fraction",
        "axis": "composition",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "roles": ["subject.hero"],
        "frame": 1,
        "op": "min",
        "lo": 0.3,
    }
    contract = _blender_probe([row], 1)

    assert "surface_visible_fraction" in live
    assert "_checks.surface_visible_fraction" in contract
    assert "ray_cast" in sampler
    assert "frac >= 0.5" not in live
