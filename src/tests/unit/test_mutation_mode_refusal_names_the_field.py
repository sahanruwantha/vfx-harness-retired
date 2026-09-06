"""A mode refusal names the field that fired it and its value (HIR-0233).

hansa_silk_road layer 2 staged a measurement-only unit as:

    {"mode": "none", "role_members": [], "controls": [], "control_roles": {},
     "script_spans": ["build/units/02/hero_placement.py"]}

and was refused with `mode 'none' cannot declare mutation targets`. Every field the
materializer thought of as a mutation target was empty, so it read the refusal against
its own payload, concluded it had declared none, and **retried the identical shape on the
next unit**. `script_spans` is what fired -- a script span is a file the unit writes --
and nothing in the message said so, named the field, or showed the value.

Same signature as the `control_roles` defect (HIR-0217): a form that cannot be accepted,
and no information about what would be.
"""

from __future__ import annotations

import pytest

from vfx_harness.domain.work_units.claims import MutationScope

SPAN = "build/units/02/hero_placement.py"


def test_the_refusal_names_the_field_and_the_value_that_fired_it() -> None:
    with pytest.raises(ValueError) as excinfo:
        MutationScope.parse(
            {"mode": "none", "roles": [], "controls": [], "dresses": [], "script_spans": [SPAN]},
            "unit.mutates",
        )
    message = str(excinfo.value)
    assert "script_spans=" in message, message
    assert SPAN in message, "the refusal must show the value, not just the field"
    # And it must not read as though the payload satisfied the rule.
    assert "declares no mutation, but" in message


def test_the_refusal_explains_why_a_script_span_counts() -> None:
    """The materializer's model was that only roles and controls are mutation."""
    with pytest.raises(ValueError, match="a script span is a file this unit writes"):
        MutationScope.parse(
            {"mode": "none", "roles": [], "controls": [], "script_spans": [SPAN]},
            "unit.mutates",
        )


def test_the_refusal_names_both_legal_next_actions() -> None:
    with pytest.raises(ValueError) as excinfo:
        MutationScope.parse({"mode": "none", "script_spans": [SPAN]}, "unit.mutates")
    message = str(excinfo.value)
    assert "keep mode 'none' for a unit that mutates nothing" in message
    assert "use mode 'scoped'" in message


def test_every_offending_field_is_listed_not_just_the_first() -> None:
    """A one-at-a-time refusal costs a turn per field on a bounded budget (HIR-0201)."""
    with pytest.raises(ValueError) as excinfo:
        MutationScope.parse(
            {
                "mode": "none",
                "roles": ["hero.tower"],
                "controls": ["hero.ctl"],
                "dresses": ["other.*"],
                "script_spans": [SPAN],
            },
            "unit.mutates",
        )
    message = str(excinfo.value)
    for field in ("roles=", "controls=", "dresses=", "script_spans="):
        assert field in message, f"{field} missing from {message}"


def test_the_scoped_refusal_says_what_was_empty_and_offers_the_other_mode() -> None:
    with pytest.raises(ValueError) as excinfo:
        MutationScope.parse(
            {"mode": "scoped", "roles": [], "controls": [], "dresses": [], "script_spans": []},
            "unit.mutates",
        )
    message = str(excinfo.value)
    assert "are all empty" in message
    assert "use mode 'none' for a unit that mutates nothing" in message


def test_a_genuinely_empty_none_scope_is_still_accepted() -> None:
    """The rule is unchanged; only what it says when it fires."""
    scope = MutationScope.parse({"mode": "none"}, "unit.mutates")
    assert scope.roles == () and scope.controls == ()
    assert scope.script_spans == () and scope.dresses == ()


def test_a_scoped_scope_with_only_a_script_span_is_still_accepted() -> None:
    scope = MutationScope.parse(
        {"mode": "scoped", "script_spans": [SPAN]}, "unit.mutates"
    )
    assert scope.script_spans == (SPAN,)
