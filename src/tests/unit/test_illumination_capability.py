"""Shading is a light modifier; only a source establishes that a prefix can be lit.

hansa_silk_road layer 2 owed four image debts and none could be paid. `propose_checks`
refused every one as NOT NECESSARY: the pre-unit adversary already read
`region_mean ~ 0.0835` and `frame_mean ~ 0.083`, because `inspect_scene` reported
`world=None` and zero light objects anywhere in the cumulative scene through layer 2.

Both bootstrap gates passed. `image-signal-bootstrap` (HIR-0110) was satisfied by a
`shading` facade; `image-subject-bootstrap` (HIR-0160) by a `mesh` tower. The debt was
unpayable in both directions -- darkness bounds trivially met by a black adversary,
brightness bounds with nothing to illuminate the surface -- and the materializer authored
them twice, on two independent designs, because the gate said the layer may owe them.
"""

from __future__ import annotations

from vfx_harness.domain.image_signal import (
    IMAGE_SIGNAL_DEPENDENCY_RULE,
    IMAGE_SIGNAL_FAMILIES,
    IMAGE_SIGNAL_MODIFIER_FAMILIES,
    IMAGE_SIGNAL_SOURCE_FAMILIES,
)
from vfx_harness.domain.work_units import UNIT_PROVIDES


def test_shading_is_a_modifier_and_not_a_source() -> None:
    assert "shading" in IMAGE_SIGNAL_MODIFIER_FAMILIES
    assert "shading" not in IMAGE_SIGNAL_SOURCE_FAMILIES
    assert frozenset({"light", "volume", "compositor"}) == IMAGE_SIGNAL_SOURCE_FAMILIES


def test_the_pixel_affecting_set_is_unchanged_in_value() -> None:
    """Splitting the set must not change what counts as pixel-affecting at all.

    The distinction is about establishing that something EMITS, not about which families
    can alter a plate. Witness guidance still names all four.
    """
    assert IMAGE_SIGNAL_FAMILIES == IMAGE_SIGNAL_SOURCE_FAMILIES | IMAGE_SIGNAL_MODIFIER_FAMILIES
    assert frozenset({"light", "shading", "volume", "compositor"}) == IMAGE_SIGNAL_FAMILIES


def test_illumination_is_a_declared_capability_beside_camera_and_geometry() -> None:
    """A role name or look label never implies it, exactly as for camera (HIR-0098)."""
    assert {"camera", "geometry", "illumination"} == UNIT_PROVIDES


def test_the_rule_names_the_declaration_and_says_why_shading_is_not_enough() -> None:
    rule = IMAGE_SIGNAL_DEPENDENCY_RULE
    assert 'provides: ["illumination"]' in rule
    assert "A shading cluster alone is not a source" in rule
    assert "renders black" in rule
    # And it still names the other legal routes rather than only refusing.
    assert "same-layer dependency" in rule
    assert "earlier materialized layer" in rule
