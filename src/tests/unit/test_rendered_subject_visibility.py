"""bbox_* and visible_fraction measure the RENDERED subject (HIR-0196).

Run 20260904T105849Z-0c9a45's `exterior_ground` was required to pay `bbox-ext-f1`
(exterior.* bbox_height in 0.15..0.35 at frame 1) and measured 0.3936. It ablated its own
contribution the only way a builder can:

    I toggled hide_render/hide_viewport on all 4 objects I created (ground_island,
    streetlight_0..2) and the bbox_height read back identically (0.3936) with them hidden

and concluded the overflow was not its doing. The reading could not have changed: the
projection helper iterated every selected object and never consulted render visibility, so
the instrument was incapable of answering the question the builder asked it. A metric that
cannot respond to the mutation a builder makes is a loop that does not close.

The same blindness let a subject satisfy `visible_fraction` while hidden from render,
which the per-role AND rule exists to prevent.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vfx_harness.blender.checks import renders_in_frame


def _evaluated(*, hidden: bool) -> SimpleNamespace:
    return SimpleNamespace(hide_render=hidden, type="MESH")


def test_a_hidden_object_does_not_render() -> None:
    assert not renders_in_frame(_evaluated(hidden=True))


def test_a_visible_object_renders() -> None:
    assert renders_in_frame(_evaluated(hidden=False))


def test_an_object_without_the_attribute_is_treated_as_rendering() -> None:
    """Non-object hosts must not be silently dropped from a measurement."""
    assert renders_in_frame(SimpleNamespace(type="MESH"))


@pytest.mark.parametrize("flag", [True, 1, "yes"])
def test_any_truthy_hide_render_excludes(flag: object) -> None:
    assert not renders_in_frame(SimpleNamespace(hide_render=flag))


@pytest.mark.parametrize("flag", [False, 0, None, ""])
def test_any_falsey_hide_render_includes(flag: object) -> None:
    assert renders_in_frame(SimpleNamespace(hide_render=flag))


def test_the_projection_helper_consults_render_visibility() -> None:
    """The bbox path must call the predicate, or the ablation loop stays open."""
    from pathlib import Path

    probe = (
        Path(__file__).resolve().parents[2]
        / "vfx_harness"
        / "evidence"
        / "scene_checks"
        / "probe.py"
    ).read_text(encoding="utf-8")
    body = probe[probe.index("def _projected("):probe.index("def _property(")]
    assert "renders_in_frame" in body, (
        "_projected must exclude objects hidden from render; without it a bbox_* row "
        "measures geometry the frame does not contain and cannot answer an ablation"
    )
    assert "hidden" in body


def test_the_visibility_sampler_consults_render_visibility() -> None:
    from pathlib import Path

    checks = (
        Path(__file__).resolve().parents[2] / "vfx_harness" / "blender" / "checks.py"
    ).read_text(encoding="utf-8")
    body = checks[checks.index("def surface_visible_fraction("):]
    body = body[: body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
    assert "renders_in_frame" in body, (
        "surface_visible_fraction must not count a hidden subject as seen; a hidden "
        "subject reads 0.0, a failing measurement (HIR-0019)"
    )
