"""What the critic may be ASKED is bounded by what the plate can SHOW.

hansa_silk_road layer 2 sealed all four units, then a composed critic panel returned a
unanimous REVISE: "the tower reads dark with vertical detail instead of uniform flat
grey". That is a literally accurate description of the plate it was given -- a Workbench
solid render, materials suppressed, for a `workbench_solid` judgment debt about mesh
construction. The facade's emissive window mask was not dim in that image, it was absent
by design, and the critic was handed an appearance rubric anyway. The REVISE read as a
verdict on the build and was recorded as a content gap in an HIR before anyone opened the
two plates (HIR-0241).

`_unit_raster_mode` already decides the medium. The rubric now comes from the same value,
so it cannot drift from the render.
"""
from __future__ import annotations

from types import SimpleNamespace

from vfx_harness.agents.build_prompts import critic_prompt


class _M(SimpleNamespace):
    pass


def _milestone():
    return _M(id="2@f51", ref="refs/frame_2s.jpg", frame=51, reads="the hero tower dominates")


def _shot():
    return SimpleNamespace(id="hansa", folder=".", frames=440, frontmatter={})


AXES = [("hero_dominance", "the hero tower dominates the frame")]


def test_solid_plate_tells_the_critic_materials_are_suppressed():
    prompt = critic_prompt(_shot(), _milestone(), "cand.png", AXES, render_medium="solid").rubric
    low = prompt.lower()
    assert "workbench solid" in low, "a solid plate must be named as such to the critic"
    assert "suppressed" in low
    # the specific failure: absence of shader-carried detail scored as a defect
    assert "not a defect" in low or "is not a defect" in low, (
        "the critic must be told that absent material detail is not a defect on this plate:\n"
        + prompt[:600]
    )


def test_solid_plate_forbids_scoring_appearance_down():
    prompt = critic_prompt(_shot(), _milestone(), "cand.png", AXES, render_medium="solid").rubric
    low = prompt.lower()
    for word in ("colour", "emission", "reflectivity"):
        assert word in low, f"solid rubric must name {word} as unjudgeable here"


def test_eevee_plate_gets_no_suppression_block():
    """A beauty plate must keep the full appearance rubric."""
    prompt = critic_prompt(_shot(), _milestone(), "cand.png", AXES, render_medium="eevee").rubric
    assert "workbench solid" not in prompt.lower()


def test_solid_unit_reaches_the_prompt_with_its_block():
    """The medium the builder derives must be the medium the rubric is chosen by.

    This pins the join rather than either side: a unit carrying a workbench_solid debt
    must select the solid plate, and that same value must produce the solid block. If
    `_unit_raster_mode` ever stops returning "solid" for such a unit, or the block stops
    keying on it, this fails while both halves still pass their own tests.

    Note it does NOT discriminate the mechanism's absence any better than the tests above:
    every test here fails on TypeError pre-fix, because the parameter IS the interface and
    landed with the block. A discriminator that fails on an absent string would have to
    reach `critic_prompt` through `_critique`, which is async and needs a Blender session,
    or assert on source text -- which this repo removed a test for (1510b3f) and rightly.
    """
    from vfx_harness.agents.builder.evidence import _unit_raster_mode

    unit = SimpleNamespace(
        judgment_observation_medium="workbench_solid",
        look_capabilities=("material", "color", "detail"),
    )
    medium = _unit_raster_mode(unit)
    assert medium == "solid", "a workbench_solid debt must select the solid plate"

    prompt = critic_prompt(_shot(), _milestone(), "cand.png", AXES, render_medium=medium).rubric
    assert "workbench solid" in prompt.lower(), (
        "the medium the builder derives must reach the critic's rubric"
    )


def test_absent_medium_is_unchanged():
    """Callers that do not know the medium keep today's behaviour."""
    prompt = critic_prompt(_shot(), _milestone(), "cand.png", AXES).rubric
    assert "workbench solid" not in prompt.lower()
