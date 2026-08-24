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


def test_materialization_requires_an_explicit_capability_declaration(
    tmp_path, monkeypatch
) -> None:
    """Silence is not a declaration: an omitted key is indistinguishable from
    "owns no appearance", which is how run 20260823T154920Z left an appearance-owning
    unit without image feedback. An explicit [] is the legal way to own none."""
    import json

    from tests.unit.test_plan_records import _add_deferred_layer, _candidate, _jit_payload, _write
    from vfx_harness.observability import run_artifacts
    from vfx_harness.orchestration.jit_materialization import validate_materialization
    from vfx_harness.orchestration.plan_authority import publish_current

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "capability-declaration")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    data = json.loads(payload.read_text(encoding="utf-8"))
    for stage in data["layer"]["stages"]:
        stage.pop("look_capabilities", None)
    _write(payload, data)

    with pytest.raises(ValueError, match="must declare look_capabilities"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_live_scope_rule_matches_the_canonical_replay_rule() -> None:
    """Live feedback and the deterministic gate must not disagree about scope."""
    from vfx_harness.agents.builder import _scope_added_object_errors
    from vfx_harness.blender.tools import _role_in_scope

    allowed = ("iris.blades", "iris_lights.*")
    for role, expected in (
        ("iris.blades.lead", True),      # namespace owns dot-descendants
        ("iris.blades", True),
        ("iris_lights.rim", True),
        ("iris.housing", False),         # sibling namespace is NOT owned
        ("camera", False),
        ("", False),                     # untagged helper objects
    ):
        assert _role_in_scope(role, allowed) is expected, role
        errors = _scope_added_object_errors({}, {"obj": role}, allowed)
        assert bool(errors) is (not expected), role


def test_live_scope_reports_the_real_untagged_object() -> None:
    """cam_rig_spine (run 20260824T045543Z-e0e47b) created CAM_spine with no bvfx_role.
    Canonical replay rejected it at the end; the live check said nothing. The offender
    must be named, and must keep being named until it is fixed rather than suppressed
    after first sight — builders create first and tag second."""
    from vfx_harness.blender.tools import _scope_offenders

    allowed = ("cam_rig",)
    manifest = {"CAM": "cam_rig", "CAM_spine": ""}

    first = _scope_offenders(manifest, allowed)
    second = _scope_offenders(manifest, allowed)

    assert first == ["'CAM_spine' role=<none>"]
    assert second == first, "a violation must persist until fixed, not vanish"

    fixed = _scope_offenders({"CAM": "cam_rig", "CAM_spine": "cam_rig.spine"}, allowed)
    assert fixed == []
