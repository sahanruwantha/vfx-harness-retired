"""One unit originates a capability; the rest must depend on it (HIR-0192).

Run 20260904T020917Z-7afeae broke the accepted chain mid-layer. Layer 1 declared two
units with `provides: ["camera"]` and no edge between them:

    cam_path   depends_on=['cam_targets']  provides=['camera']  mutates camera.rig
    cam_lens   depends_on=[]               provides=['camera']  mutates camera.lens

`cam_path` creates the camera; `cam_lens` only sets its focal length. Replay order is
topological over `depends_on` with authored position as the only tie-break (HIR-0119), so
`cam_lens.py` replayed first, onto a scene with no camera:

    CHAIN BROKEN — cam_lens.py no longer composes onto the scene built by the layers
    before it: AttributeError: 'NoneType' object has no attribute 'data'

`provides` is additive mutation authority, not a label (HIR-0112). Declaring it does not
create the host, so a declarer that creates nothing must reach the one that does.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vfx_harness.domain.capability_origin import capability_origin_gaps, describe_gap


def _unit(unit_id: str, depends_on=(), provides=()) -> SimpleNamespace:
    return SimpleNamespace(
        id=unit_id, depends_on=tuple(depends_on), provides=tuple(provides)
    )


def _observed_layer(lens_depends_on=()) -> list[SimpleNamespace]:
    """The exact layer-1 shape of run 20260904T020917Z-7afeae."""
    return [
        _unit("cam_targets"),
        _unit("cam_path", ["cam_targets"], ["camera"]),
        _unit("cam_lens", lens_depends_on, ["camera"]),
        _unit("cam_grade"),
        _unit("cam_conceal", ["cam_grade", "cam_path"]),
        _unit("cam_vis", ["cam_targets", "cam_path"]),
    ]


def test_two_unordered_declarers_are_refused() -> None:
    [gap] = capability_origin_gaps(_observed_layer())
    assert gap.capability == "camera"
    assert gap.unordered_ids == ("cam_lens", "cam_path")
    assert "authored position" in describe_gap(gap)


def test_the_edge_that_would_have_prevented_it_clears_the_finding() -> None:
    assert not capability_origin_gaps(_observed_layer(lens_depends_on=["cam_path"]))


def test_a_transitive_edge_is_enough() -> None:
    """The declarer need only reach the originator, not depend on it directly."""
    units = [
        _unit("origin", provides=["camera"]),
        _unit("middle", ["origin"]),
        _unit("modifier", ["middle"], ["camera"]),
    ]
    assert not capability_origin_gaps(units)


def test_a_single_declarer_is_always_fine() -> None:
    assert not capability_origin_gaps(_observed_layer()[:2])


def test_a_layer_with_no_capabilities_is_fine() -> None:
    assert not capability_origin_gaps([_unit("a"), _unit("b", ["a"])])


def test_each_capability_is_judged_separately() -> None:
    """A clean camera chain does not excuse an unordered geometry pair."""
    units = [
        _unit("cam", provides=["camera"]),
        _unit("cam_tweak", ["cam"], ["camera"]),
        _unit("geo_a", provides=["geometry"]),
        _unit("geo_b", provides=["geometry"]),
    ]
    [gap] = capability_origin_gaps(units)
    assert gap.capability == "geometry"
    assert gap.unordered_ids == ("geo_a", "geo_b")


def test_three_unordered_declarers_are_all_named() -> None:
    units = [_unit(f"u{i}", provides=["camera"]) for i in range(3)]
    [gap] = capability_origin_gaps(units)
    assert gap.unordered_ids == ("u0", "u1", "u2")


@pytest.mark.parametrize("capability", ["camera", "geometry", "anything"])
def test_the_rule_is_not_specific_to_cameras(capability: str) -> None:
    units = [_unit("a", provides=[capability]), _unit("b", provides=[capability])]
    [gap] = capability_origin_gaps(units)
    assert gap.capability == capability
