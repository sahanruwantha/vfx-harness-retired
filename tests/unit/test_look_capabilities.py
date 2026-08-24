"""Look scope follows typed unit authority, not axis-identifier scanning.

Run 20260823T154920Z: `iris_seal_readability` contains no look word, so
`axis_feedback_groups` returned nothing and the builder was told appearance was out of
scope — while that unit's own plan owned layered machined metal, seams, fasteners, and
extreme-close-range readability. Identifiers are names; capabilities are authority."""

from __future__ import annotations

import pytest

from vfx_harness.agents.build_prompts import (
    axis_feedback_groups,
    capability_feedback_groups,
)
from vfx_harness.domain.work_units import LOOK_CAPABILITIES, parse_look_capabilities


def test_appearance_unit_whose_axis_has_no_look_word_still_gets_feedback() -> None:
    axes = [("iris_seal_readability", "layered machined metal, seams, fasteners")]
    assert axis_feedback_groups(axes) == frozenset()  # the historical silence

    groups = capability_feedback_groups(("material", "detail"))

    assert "detail" in groups and "color" in groups


def test_layout_unit_declaring_nothing_receives_no_look_prescription() -> None:
    assert capability_feedback_groups(()) == frozenset()
    assert capability_feedback_groups(("motion",)) == frozenset({"motion"})


def test_capabilities_are_validated_against_a_closed_vocabulary() -> None:
    assert parse_look_capabilities(["material", "detail"], "unit.look") == (
        "material",
        "detail",
    )
    with pytest.raises(ValueError, match="unknown capability"):
        parse_look_capabilities(["shiny"], "unit.look")
    # The error must name the accepted set — the session cannot look it up otherwise.
    try:
        parse_look_capabilities(["shiny"], "unit.look")
    except ValueError as exc:
        for name in LOOK_CAPABILITIES:
            assert name in str(exc)


def test_declared_capabilities_beat_identifier_scanning_in_the_tool_policy() -> None:
    from vfx_harness.blender.tools import build_blender_tools

    class _Session:
        pass

    server, _names = build_blender_tools(
        _Session(), layer_id="1", feedback_groups=["detail", "color"]
    )
    assert server is not None  # wiring accepts the typed override


def test_work_unit_parses_and_defaults_capabilities() -> None:
    from tests.architecture.test_staged_architecture import _unit

    unit = _unit("blockout")
    assert unit.look_capabilities == ()  # legacy rows stay valid and fall back

    from tests.architecture.test_staged_architecture import _claim
    from vfx_harness.domain.work_units import WorkUnit

    row = {
        "id": "surfacing", "title": "Surfacing", "plan": "plans/units/surfacing.md",
        "depends_on": [],
        "mutates": {"mode": "scoped", "roles": [], "controls": [],
                    "script_spans": ["build/units/01/surfacing.py"]},
        "protects": {"selector": "all_active_upstream_interfaces",
                     "resolve_to_explicit_ids_at": "freeze"},
        "evaluation": {"primary_judge": 40,
                       "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                       "temporal_evidence": "none",
                       "claims": [_claim("surfacing")]},
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["material"],
    }
    parsed = WorkUnit.parse(row, "unit.surfacing")

    assert parsed.look_capabilities == ("material",)
    assert capability_feedback_groups(parsed.look_capabilities) == frozenset(
        {"detail", "color"}
    )
