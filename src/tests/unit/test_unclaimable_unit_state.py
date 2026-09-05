"""A unit no builder may claim names the transaction that clears it.

HIR-0214 fixed this for ``hypothesis_falsified``: the claim refused correctly, but with
a traceback from a boundary that held the finding.  Every other unclaimable state kept
the old behaviour, so an operator interrupt — including one taken to honour a budget
ceiling — left the unit in ``building`` and terminalized the run as an unclassified
``harness_defect`` routed to engineering, while ``vfx units retry`` cleared it in
seconds and nothing said so.
"""

from __future__ import annotations

import inspect

import pytest

from vfx_harness.orchestration import unit_state_queries
from vfx_harness.orchestration.unit_state_claims import PLANNING_CLAIMABLE_STATES
from vfx_harness.orchestration.unit_state_lifecycle import TRANSITIONS


def _state(status: str) -> dict:
    return {"units": {"u1": {"status": status}}}


def _answer(monkeypatch: pytest.MonkeyPatch, status: str):
    monkeypatch.setattr(unit_state_queries, "load", lambda folder, layer_id: _state(status))
    return unit_state_queries.unclaimable_state("/nonexistent", "2", "u1")


def test_building_names_the_retry_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact state an operator interrupt leaves behind."""

    answer = _answer(monkeypatch, "building")
    assert answer is not None, "a `building` unit is unclaimable and must say so"
    status, next_action = answer
    assert status == "building"
    assert "vfx units retry" in next_action
    assert "--layer 2" in next_action and "--unit u1" in next_action
    assert "--reason" in next_action and "--evidence" in next_action


def test_claimable_states_are_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    for status in sorted(PLANNING_CLAIMABLE_STATES):
        assert _answer(monkeypatch, status) is None, status


def test_falsified_is_left_to_its_own_typed_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """HIR-0214 already publishes a finding-bearing stop; two stops would race."""

    assert _answer(monkeypatch, "hypothesis_falsified") is None


def test_terminal_states_refuse_a_retry_they_cannot_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`passed` and `superseded` do not reach `retryable`; naming one would misdirect."""

    for status in ("passed", "superseded"):
        answer = _answer(monkeypatch, status)
        assert answer is not None, status
        _, next_action = answer
        assert "vfx units retry" not in next_action, status
        assert "does not reach `retryable`" in next_action


def test_next_action_is_derived_from_the_closed_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The general invariant: the sentence follows TRANSITIONS, not a hand-kept list.

    A new lifecycle edge to ``retryable`` must change this answer without anyone
    editing the message, or the two derivations drift.
    """

    expected_retry = {
        status
        for status, successors in TRANSITIONS.items()
        if "retryable" in successors
        and status not in PLANNING_CLAIMABLE_STATES
        and status != "hypothesis_falsified"
    }
    named_retry = set()
    for status in TRANSITIONS:
        answer = _answer(monkeypatch, status)
        if answer is not None and "vfx units retry" in answer[1]:
            named_retry.add(status)
    assert named_retry == expected_retry


def test_missing_unit_is_not_an_unclaimable_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unit_state_queries, "load", lambda folder, layer_id: {"units": {}})
    assert unit_state_queries.unclaimable_state("/nonexistent", "2", "u1") is None


def test_the_typed_exception_reaches_status_not_only_the_console() -> None:
    """The transaction must survive to status.json, not print and vanish.

    A message that only reaches the console has the same reachability problem as the
    unclassified boundary this fix replaces: the operator reading the documented order
    (runs/latest.json -> status.json -> summary.json) would never see it.
    """

    import vfx_harness.agents.builder.cli as builder_cli
    from vfx_harness.agents.builder.models import UnclaimableUnit
    from vfx_harness.observability import run_artifacts

    source = inspect.getsource(builder_cli)
    assert "except UnclaimableUnit as e:" in source, "the CLI must translate the typed stop"
    handler = source.split("except UnclaimableUnit as e:", 1)[1].split("except ", 1)[0]
    assert "log(" in handler, "operator needs the console line"
    assert "RequestedExit" in handler, "and status.json needs the detail"

    exit_ = run_artifacts.RequestedExit(7, "UNCLAIMABLE UNIT — vfx units retry ...")
    assert "vfx units retry" in exit_.detail
    assert issubclass(UnclaimableUnit, RuntimeError)


def test_the_state_machine_has_one_membership_everywhere() -> None:
    """This fix reads TRANSITIONS; two other modules name the same state set.

    ``unclaimable_state`` decides from ``TRANSITIONS`` and ``PLANNING_CLAIMABLE_STATES``.
    Two further modules hold the same vocabulary under different names, with four
    refusal sites between them.  They agree today; nothing makes them.  If one is
    widened alone, a unit could be a legal state at the boundary that admits it and an
    unknown one at the boundary that refuses it — accepted here, refused there, which is
    the shape of six defects found on 2026-09-05.

    ``TRANSITIONS`` is a dict, so its key set is a fourth spelling of this vocabulary
    that a scan for collection *constants* does not see.
    """

    from vfx_harness.domain.stop_transaction_state import UNIT_STATUSES
    from vfx_harness.domain.work_units.parsing import UNIT_STATES

    states = set(TRANSITIONS)
    assert states == set(UNIT_STATES), sorted(states ^ set(UNIT_STATES))
    assert states == set(UNIT_STATUSES), sorted(states ^ set(UNIT_STATUSES))
    assert set(PLANNING_CLAIMABLE_STATES) <= states

    successors = {s for edges in TRANSITIONS.values() for s in edges}
    assert successors <= states, sorted(successors - states)
