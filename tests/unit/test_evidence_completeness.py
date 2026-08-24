"""Required evidence cannot pass by absence.

`may_seal` was presence-based: it required at least one current-layer contract and no
failures. A contract that was never evaluated — selector matched nothing, probe errored,
frame group never ran — is neither, so a unit could seal while a required claim had no
evidence at all. Run 20260823T154920Z reported "2/4 pass" against a six-contract view
and nothing asked which two were missing."""

from __future__ import annotations

from vfx_harness.blender.tools import _scene_completion_state


def _row(cid: str, *, owner: str = "1", passes: bool = True) -> dict:
    return {"id": cid, "authoritative": True, "owner_layer": owner, "pass": passes}


def test_absent_required_evidence_blocks_sealing() -> None:
    evidence = [_row("produced")]
    required = {"produced", "never_evaluated"}

    state = _scene_completion_state(evidence, "1", required)

    assert state["missing"] == ["never_evaluated"]
    assert state["may_seal"] is False
    assert state["interfaces_ready"] is False
    assert not state["failures"]  # the point: absence is not a failure row


def test_complete_and_passing_evidence_still_seals() -> None:
    evidence = [_row("a"), _row("b")]

    state = _scene_completion_state(evidence, "1", {"a", "b"})

    assert state["missing"] == []
    assert state["may_seal"] is True


def test_failing_evidence_still_blocks_when_complete() -> None:
    evidence = [_row("a"), _row("b", passes=False)]

    state = _scene_completion_state(evidence, "1", {"a", "b"})

    assert state["missing"] == []
    assert state["may_seal"] is False


def test_legacy_units_without_a_required_set_are_unchanged() -> None:
    """A layer with no declaring unit cannot know its required ids; behaviour there
    stays presence-based rather than failing closed on unknowable completeness."""
    evidence = [_row("a")]

    state = _scene_completion_state(evidence, "1", None)

    assert state["missing"] == []
    assert state["may_seal"] is True


def test_upstream_only_evidence_never_seals_the_current_layer() -> None:
    evidence = [_row("upstream", owner="0")]

    state = _scene_completion_state(evidence, "1", {"upstream"})

    assert state["may_seal"] is False  # no current-layer contract
    assert state["interfaces_ready"] is True
