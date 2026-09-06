"""A plan that cannot light a layer it promised appearance for fails at publication.

`image` and `lit` are different questions, and conflating them breaks in both directions:
demanding a light for every image layer refuses legitimate Workbench-solid form judgment,
and demanding none admits an EEVEE judgment on a scene with no lamp and no world, whose
plate is black and whose debt is unpayable in either direction.

The declaration exists because a sparse layer carries no units. There is nothing to derive
a medium from, and inferring it from layer titles or axis names is the role-name heuristic
HIR-0098 retired for camera (ADR-0011).
"""

from __future__ import annotations

import pytest

from vfx_harness.domain.judgment_debt_models import (
    LIT_OBSERVATION_MEDIA,
    OBSERVATION_MEDIA,
    RENDER_MODE_BY_MEDIUM,
    medium_requires_illumination,
)
from vfx_harness.domain.lit_judgment_scope import (
    IMAGE_MEDIA_FIELD,
    parse_image_media,
    requires_illumination,
    unreachable_illumination,
)
from vfx_harness.domain.work_units import (
    GLOBAL_SCENE_CAPABILITIES,
    LAYER_EXCLUSIVE_CAPABILITIES,
    allowed_unit_provides,
)


def test_the_gate_rejects_a_lit_judgment_with_no_reachable_illumination():
    refusal = unreachable_illumination(
        layer_id="2",
        declared_media=["eevee"],
        capability_closure={"2": {"camera"}},
    )

    assert refusal is not None
    # The refusal has to be actionable as a plan edit, so it names both repairs and the
    # third option the operator may not have considered.
    assert "provides: {\"illumination\"" in refusal
    assert "depend on an earlier layer" in refusal
    assert 'workbench_solid' in refusal


def test_the_gate_accepts_a_lit_judgment_with_a_reachable_provider():
    assert (
        unreachable_illumination(
            layer_id="2",
            declared_media=["eevee"],
            capability_closure={"2": {"camera", "illumination"}},
        )
        is None
    )


def test_the_gate_permits_solid_mode_judgment_on_an_unlit_scene():
    """Workbench solid shades from the viewport's own model: no lamp, no world, no block."""
    assert (
        unreachable_illumination(
            layer_id="2",
            declared_media=["workbench_solid"],
            capability_closure={"2": {"camera"}},
        )
        is None
    )
    assert not requires_illumination(["workbench_solid"])


def test_a_mixed_declaration_is_blocked_by_its_lit_half():
    refusal = unreachable_illumination(
        layer_id="3",
        declared_media=["workbench_solid", "eevee"],
        capability_closure={"3": set()},
    )

    assert refusal is not None
    assert "'eevee'" in refusal or "['eevee']" in refusal


def test_lit_media_are_derived_from_the_render_mode_not_listed():
    """A new medium inherits the answer from the mode it realises.

    Hand-keeping the lit set means a medium added later is absent from it and silently
    reads as "needs no light", which is the failure direction that renders black.
    """
    assert frozenset(
        medium for medium, mode in RENDER_MODE_BY_MEDIUM.items() if mode != "solid"
    ) == LIT_OBSERVATION_MEDIA
    assert set(RENDER_MODE_BY_MEDIUM) == set(OBSERVATION_MEDIA), (
        "every medium must map to a render mode, or its lighting need cannot be derived"
    )
    assert medium_requires_illumination("eevee")
    assert not medium_requires_illumination("workbench_solid")


def test_an_unknown_medium_is_refused_naming_the_accepted_set():
    with pytest.raises(ValueError, match="workbench_solid"):
        medium_requires_illumination("cycles")
    with pytest.raises(ValueError, match="unknown observation media: cycles"):
        parse_image_media(["cycles"], f"layers[0].{IMAGE_MEDIA_FIELD}")
    with pytest.raises(ValueError, match="non-empty list"):
        parse_image_media([], f"layers[0].{IMAGE_MEDIA_FIELD}")


def test_illumination_is_globally_declarable_without_becoming_layer_exclusive():
    """The two properties are separate, and deriving one from the other retires HIR-0234.

    A camera grant makes the layer camera-only. An illumination grant must not, because an
    emissive facade is the light and lives on an ordinary look layer -- so a unit on a
    layer with no global grant must still be able to declare it.
    """
    assert "illumination" in GLOBAL_SCENE_CAPABILITIES
    assert "illumination" not in LAYER_EXCLUSIVE_CAPABILITIES
    assert allowed_unit_provides({"jit": {"provides": {}}}) == frozenset(
        {"geometry", "illumination"}
    )
    assert allowed_unit_provides({"jit": {"provides": {"illumination": ["sign.*"]}}}) == (
        frozenset({"geometry", "illumination"})
    )
    assert allowed_unit_provides({"jit": {"provides": {"camera": ["cam.*"]}}}) == frozenset(
        {"camera"}
    )


def test_the_same_proposition_at_two_media_is_split_by_the_declaration_alone():
    """The discriminator is the declared medium, never the statement.

    Shape taken from hansa_silk_road, which produced six debts carrying one verbatim
    statement -- three `workbench_solid`, three `eevee` -- from a single materialization.
    The proposition is about construction method: windows visible in a solid plate mean
    modeled geometry, windows absent mean a shader mask. The solid three are the correct
    ones and the eevee three are over-specified, and *nothing in the sentence says which*.

    A gate that inferred the medium from the proposition would have to get that right, and
    it cannot: the two groups are textually identical. So the declaration is the only thing
    that can carry it, which is the argument for declaring rather than inferring.
    """
    statement = (
        "Hero windows should come from a repeatable facade module or shader mask "
        "rather than individually modeled rooms."
    )
    unlit_closure = {"2": {"camera"}}

    solid = unreachable_illumination(
        layer_id="2", declared_media=["workbench_solid"], capability_closure=unlit_closure
    )
    lit = unreachable_illumination(
        layer_id="2", declared_media=["eevee"], capability_closure=unlit_closure
    )

    assert solid is None, f"a form proposition must survive an unlit plan: {statement!r}"
    assert lit is not None, f"the same words at eevee must be refused: {statement!r}"
