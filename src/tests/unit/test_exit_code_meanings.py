"""One table of exit-code meanings, and exit 3 does not prescribe one cause's fix.

`application/run_shot._MEANING` was a second copy of `observability.EXIT_DETAILS`, and the
two had already drifted: code 9 read "layer ran cleanly but its VERDICT was not a pass" in
one and "has no passing terminal publication" in the other, for one digit an operator sees
from either surface.

And exit 3 read "TRUNCATED -- raise the budget or split the layer" for four distinct causes.
The hansa_silk_road driver hit it twice on an idle stream, where neither prescribed action
touches the problem: `BUILD TRUNCATED -- no SDK event for 360s after UserMessage`, heartbeat
`events=344`. Same shape as HIR-0226 one surface over -- the record is right and the prose
sends the operator somewhere else.
"""

from __future__ import annotations

from vfx_harness.application import run_shot
from vfx_harness.observability.run_artifacts import EXIT_DETAILS


def test_the_two_tables_are_one_table() -> None:
    for code, text in EXIT_DETAILS.items():
        assert run_shot._MEANING[code] == text, code


def test_run_shot_keeps_only_the_codes_the_shared_table_does_not_carry() -> None:
    extra = set(run_shot._MEANING) - set(EXIT_DETAILS)

    assert extra == {0, 1}


def test_exit_three_names_the_record_rather_than_prescribing_one_cause() -> None:
    text = EXIT_DETAILS[3]

    # The prescription that misroutes three of the four causes.
    assert "raise the budget" not in text
    assert "split the layer" not in text
    # It points at the field that actually says which cause it was.
    assert "terminal_cause" in text
    for cause in (
        "max_turns_exhausted",
        "model_budget_exhausted",
        "model_session_failure",
        "model_session_idle_timeout",
    ):
        assert cause in text


def test_every_meaning_is_a_meaning_and_not_an_instruction() -> None:
    """A digit is an operator diagnostic; the typed record carries the authority.

    Codes whose cause is single-valued may still name the next action -- 5 and 8 do. The
    rule this pins is narrower: no meaning may prescribe an action for a digit that covers
    several causes, which is what exit 3 did.
    """
    assert EXIT_DETAILS[3].startswith("MODEL PHASE DID NOT COMPLETE")
