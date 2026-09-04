"""The unit card states a forecast row's condition, not a verdict it cannot yet reach.

caesar run 20260904T191549Z-e048a6, layer 2 floor_aisle_unit. The builder quoted the card
and reasoned correctly from it:

    "the compiled unit-scope card explicitly lists this unit's deferred-payment rows as
     `(none)` and classifies those three as `diagnostic_only` forecasts ... not this
     unit's responsibility since I'm not the dependency-complete env.* payer yet"

Sixty seconds later the evaluator failed the unit on those three ids as bound executable
contracts. The rule in deferred_subject is conditional on a live measurement; the card
asserted the unconditional negative at kickoff, three lines below the sharing data added
to warn about exactly this (HIR-0212).
"""

from __future__ import annotations

from vfx_harness.agents.unit_scope import format_unit_scope_card
from vfx_harness.evidence.scene_checks import deferred_subject_irreversible_bound

BAND = {"id": "cam-bbox-f1-env", "kind": "bbox_height", "op": "band", "lo": 0.5, "hi": 0.95}


def test_the_irreversible_bound_is_the_side_a_union_cannot_return_from() -> None:
    assert deferred_subject_irreversible_bound(BAND) == {
        "kind": "bbox_height",
        "side": "grows",
        "bound": 0.95,
        "crosses_when": "above",
    }
    assert deferred_subject_irreversible_bound(
        {"kind": "bbox_top_y", "op": "min", "lo": 0.25}
    ) == {"kind": "bbox_top_y", "side": "falls", "bound": 0.25, "crosses_when": "below"}
    # A repairable side and a non-union kind have no irreversible bound to state.
    assert deferred_subject_irreversible_bound({"kind": "bbox_height", "op": "min", "lo": 0.1}) is None
    assert deferred_subject_irreversible_bound({"kind": "object_count", "op": "min", "lo": 1}) is None


def test_the_card_never_calls_a_forecast_row_diagnostic_only() -> None:
    """The exact word the builder quoted must not appear as an unconditional claim."""
    card = {
        "deferred_subject_forecasts": [
            {
                **BAND,
                "status": "conditional",
                "diagnostic_while_inside_bound": True,
                "irreversible_bound": deferred_subject_irreversible_bound(BAND),
                "union_producers": ["hall_shell_unit", "floor_aisle_unit", "columns_unit"],
                "pending_producers": ["columns_unit"],
            }
        ],
    }

    rendered = format_unit_scope_card(card)

    assert "diagnostic only" not in rendered.lower()
    assert "CONDITIONAL" in rendered
    assert "REQUIRED BEFORE FREEZE for THIS unit the moment it crosses" in rendered
    # The bound and the sharers reach the builder, not just the status.
    assert '"bound":0.95' in rendered and '"side":"grows"' in rendered
    assert "columns_unit" in rendered
    assert "another unit's business" in rendered


def test_the_heading_no_longer_says_only_the_payer_can_satisfy_them() -> None:
    """That sentence is what made a partial producer discount its own reading."""
    rendered = format_unit_scope_card({"deferred_subject_forecasts": []})

    assert "alone can satisfy these rows" not in rendered


def test_a_passing_critic_verdict_carries_how_it_was_decided() -> None:
    """hansa run 20260904T210930Z-29f9d8: canonical[2].verdict.decided_by was empty.

    Every early-exit branch labelled itself -- checks, no_optical_signal,
    actionable_panel_dissent -- and the ordinary passing critic verdict labelled nothing.
    One consumer defaulted it to "critic"; the layer evaluation receipt required it. The
    first composed critic row that ever PASSED reached mint with an empty field
    (HIR-0211).
    """
    from vfx_harness.agents.builder.critic import _decided

    assert _decided({"mean": 4.5, "pass": True})["decided_by"] == "critic"
    # A branch that already said how it decided keeps its own answer.
    for existing in ("checks", "no_optical_signal", "actionable_panel_dissent"):
        assert _decided({"decided_by": existing})["decided_by"] == existing
    # Empty and whitespace are not answers; the receipt rejects both.
    assert _decided({"decided_by": ""})["decided_by"] == "critic"
    assert _decided({"decided_by": "   "})["decided_by"] == "critic"
