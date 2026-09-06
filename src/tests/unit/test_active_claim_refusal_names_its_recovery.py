"""A refusal that holds the claim names it, and names the transaction that clears it.

caesar_curia, run `20260906T042509Z-6b0960`, exit through the unclassified boundary:

    harness_defect: The 'build' boundary returned without typed stop authority; it raised
    vfx_harness.orchestration.layer_finalization_state.LayerFinalizationConflict:
    layer 1 already has an active finalization claim

Correct HIR-0170 behaviour and $0.00 spent. But the driver then had to grep
`state/work-units/layer_1.json` for the claim id that this exact frame already held, and
the message named no recovery -- while `vfx finalizations release` cleared it in one
command. The message reaches `summary.json` through the envelope's `detail`, so it is the
surface an operator reads.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from vfx_harness.orchestration import layer_finalization_state as state


def test_the_refusal_names_the_claim_the_revision_and_the_owner() -> None:
    message = state.describe_active_claim_conflict(
        "1",
        {
            "claim_id": "lfc-c9083faca23744c2",
            "attempt_revision": 3,
            "run_id": "20260906T002035Z-f1ad1d",
        },
    )

    assert "layer 1 already has an active finalization claim lfc-c9083faca23744c2" in message
    assert "attempt_revision 3" in message
    assert "minted by run 20260906T002035Z-f1ad1d" in message


def test_it_names_the_exact_transaction_with_the_claim_id_filled_in() -> None:
    message = state.describe_active_claim_conflict("1", {"claim_id": "lfc-abc"})

    assert (
        "vfx finalizations release <shot> --layer 1 --claim-id lfc-abc "
        "--reason <why> --evidence <path>"
    ) in message


def test_it_states_the_precondition_before_the_command() -> None:
    """Release is not a retry. A live owner may still hold the claim (HIR-0170)."""
    message = state.describe_active_claim_conflict("2", {"claim_id": "lfc-abc"})

    assert "builder fence proves no live owner" in message
    assert message.index("no live owner") < message.index("vfx finalizations release")


def test_a_claim_missing_its_optional_fields_still_produces_a_usable_message() -> None:
    message = state.describe_active_claim_conflict("1", {})

    assert "<unknown>" in message
    assert "attempt_revision" not in message
    assert "minted by run" not in message
    assert "vfx finalizations release" in message


def test_the_claim_site_raises_through_that_function_and_not_a_second_string() -> None:
    """Parsed, not grepped.

    A substring check on the source passes with the call deleted and its text left in a
    comment, and fails on a rename that preserves behaviour. This asserts a call node
    with that callee inside `claim_layer_finalization`, which is neither.
    """
    tree = ast.parse(Path(inspect.getfile(state)).read_text(encoding="utf-8"))
    site = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "claim_layer_finalization"
    )
    called = {
        node.func.id
        for node in ast.walk(site)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "describe_active_claim_conflict" in called

    # And no raise in that function re-states the active-claim condition in its own words.
    literals = [
        node.value
        for node in ast.walk(site)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not [text for text in literals if "already has an active finalization claim" in text]


def test_the_exception_carries_the_rendered_message() -> None:
    with pytest.raises(state.LayerFinalizationConflict) as excinfo:
        raise state.LayerFinalizationConflict(
            state.describe_active_claim_conflict("1", {"claim_id": "lfc-abc"})
        )

    assert "vfx finalizations release" in str(excinfo.value)
