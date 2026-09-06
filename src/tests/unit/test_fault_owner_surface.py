"""The surface asks for what the field accepts, and echoes what it recorded.

caesar_curia's builder found two fault owners and recorded one. That was not carelessness:
`CANNOT_EXPRESS_DESCRIPTION` said "a sealed upstream unit", "its id", "the semantic owner"
-- singular three times against an array field, and the handler reads it as a set. The
builder answered the question the surface posed, the controller's span check saw one layer,
and it dispatched a finding it should have refused.

And the same file already said the plural: `CANNOT_EXPRESS_SCHEMA` describes the field as
"optional upstream unit **ids**". One quantity, two derivations, in text -- and the wrong one
was longer, carried the rationale, and is what a model reads for intent.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from vfx_harness.blender.tools import reports

OPTIONS = {
    "camera_rig": {"id": "camera_rig", "layer": "1"},
    "chamber_shell": {"id": "chamber_shell", "layer": "2"},
    "camera_aim": {"id": "camera_aim", "layer": "1"},
}


def test_no_tool_description_describes_one_of_its_array_fields_in_the_singular() -> None:
    """The general form, and the one this defect is an instance of.

    `CANNOT_EXPRESS_SCHEMA`'s own field description already said "optional upstream unit
    **ids**". The tool prose thirty lines away said "**its** id". One quantity, two
    derivations, in text -- and the wrong one was longer, carried the rationale, and is
    what a model reads for intent. The caesar_curia driver checked the whole tool surface:
    this was the only array field whose description was singular.
    """
    singular = re.compile(r"\b(?:its|a single|the one)\b", re.IGNORECASE)
    offenders = []
    for name in dir(reports):
        schema = getattr(reports, name)
        if not (name.endswith("_SCHEMA") and isinstance(schema, dict)):
            continue
        for field, spec in (schema.get("properties") or {}).items():
            if not isinstance(spec, dict) or spec.get("type") != "array":
                continue
            described = str(spec.get("description") or "")
            if described and singular.search(described):
                offenders.append(f"{name}.{field}: {described}")

    assert not offenders, offenders


def test_the_tool_prose_agrees_with_its_own_field_description() -> None:
    """Both say the field takes many. Only one used to."""
    field = str(
        (reports.CANNOT_EXPRESS_SCHEMA["properties"]["fault_owner_units"]).get("description") or ""
    )
    prose = reports.CANNOT_EXPRESS_DESCRIPTION

    assert "ids" in field
    assert "EVERY one of their ids" in prose
    # The singular forms that produced the defect.
    assert "a sealed upstream unit" not in prose
    assert "include its id" not in prose


def test_it_says_the_controller_reads_only_the_typed_list() -> None:
    text = reports.CANNOT_EXPRESS_DESCRIPTION

    assert "controller reads only this list" in text
    assert "invisible" in text


def test_it_states_the_cross_layer_consequence_so_omitting_one_is_not_rewarded() -> None:
    text = reports.CANNOT_EXPRESS_DESCRIPTION

    assert "list them all" in text
    assert "routes to reviewed authority" in text
    assert "correct outcome" in text


def test_the_echo_names_each_owner_with_its_layer() -> None:
    echoed = reports.describe_recorded_fault_owners(["camera_rig"], OPTIONS)

    assert "camera_rig (layer 1)" in echoed
    assert "one owning layer" in echoed


def test_the_echo_states_the_cross_layer_routing_for_this_finding() -> None:
    echoed = reports.describe_recorded_fault_owners(["camera_rig", "chamber_shell"], OPTIONS)

    assert "camera_rig (layer 1)" in echoed
    assert "chamber_shell (layer 2)" in echoed
    assert "span layers 1, 2" in echoed
    assert "routes to reviewed authority" in echoed


def test_the_echo_never_states_what_a_different_owner_set_would_have_done() -> None:
    """The objection that reshaped this: an echo naming dispatchability as a property of
    the chosen set teaches the builder that dropping an owner buys a dispatch, at the
    moment it still has turns to act on it. Raised by the hansa_silk_road driver.
    """
    for owners in (["camera_rig"], ["camera_rig", "camera_aim"], ["camera_rig", "chamber_shell"], []):
        echoed = reports.describe_recorded_fault_owners(owners, OPTIONS)
        lowered = echoed.lower()
        assert "dispatchable" not in lowered
        assert "would" not in lowered
        assert "instead" not in lowered


def test_two_owners_in_one_layer_are_not_reported_as_spanning() -> None:
    echoed = reports.describe_recorded_fault_owners(["camera_rig", "camera_aim"], OPTIONS)

    assert "one owning layer" in echoed
    assert "span layers" not in echoed


def test_an_empty_owner_set_says_so_rather_than_nothing() -> None:
    echoed = reports.describe_recorded_fault_owners([], OPTIONS)

    assert "No fault owners recorded" in echoed


def test_the_handler_echoes_through_that_function_rather_than_a_second_string() -> None:
    """Parsed, not grepped: a substring check passes with the call deleted."""
    tree = ast.parse(Path(inspect.getfile(reports)).read_text(encoding="utf-8"))
    site = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "record_cannot_express"
    )
    called = {
        node.func.id
        for node in ast.walk(site)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "describe_recorded_fault_owners" in called


def test_a_second_call_replaces_the_record_and_the_echo_says_so() -> None:
    """The echo promises a correction path, so the path is proven rather than assumed.

    The caesar_curia driver read the overwrite and the rounds-zeroing and rated this
    medium confidence, having not run a double call. This runs it.
    """
    state = {
        "fault_owner_options": [
            {"id": "camera_rig", "layer": "1"},
            {"id": "chamber_shell", "layer": "2"},
        ],
        "image_debts": [],
    }
    reports.record_cannot_express(
        state,
        {"contract_ids": ["sc-dais-depth-bbox"], "reason": "first", "fault_owner_units": ["camera_rig"]},
    )
    assert state["cannot_express"]["fault_owner_units"] == ["camera_rig"]

    second = reports.record_cannot_express(
        state,
        {
            "contract_ids": ["sc-dais-depth-bbox"],
            "reason": "second",
            "fault_owner_units": ["camera_rig", "chamber_shell"],
        },
    )

    assert state["cannot_express"]["fault_owner_units"] == ["camera_rig", "chamber_shell"]
    assert state["cannot_express"]["reason"] == "second"
    assert "the later call replaces this one" in str(second)


def test_the_echo_states_the_consequence_of_omission_not_the_sufficiency_of_what_is_there() -> None:
    """The existing read-back echoed contract_ids, classification and the full prose
    reason, and omitted `fault_owner_units` -- confirming the channel the controller
    ignores and hiding the one it reads. Raised by the caesar_curia driver.
    """
    for owners in (["camera_rig"], ["camera_rig", "chamber_shell"], []):
        echoed = reports.describe_recorded_fault_owners(owners, OPTIONS)
        assert "controller reads only this list" in echoed
        assert "invisible to it" in echoed
        assert "call cannot_express_in_scope again now" in echoed
