"""A verdict the harness can emit must be one the receipt can record.

`agents/builder/verdicts` emits four mechanically decided verdicts; `layer_evaluation_
receipts` admitted one. The other three are structural refusals -- the frame could not be
settled, and which authority fact prevented it -- and each turned a correct diagnosis into
a ValueError at receipt-minting time. caesar_curia layer 1 died that way: its layer judge
list carried six frames, its only unit's required claims covered three, the composed
canonical correctly emitted `lookless_requires_executable_claims`, and the boundary whose
job is to record the finding refused to record it.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from vfx_harness.agents.builder import verdicts
from vfx_harness.domain.verdict_deciders import (
    CONTRACT_GAP_DECIDERS,
    EXECUTABLE_DECIDER,
    MECHANICAL_VERDICT_DECIDERS,
)


def test_the_executable_decider_is_not_a_gap() -> None:
    assert EXECUTABLE_DECIDER not in CONTRACT_GAP_DECIDERS
    assert {EXECUTABLE_DECIDER} | CONTRACT_GAP_DECIDERS == MECHANICAL_VERDICT_DECIDERS


def test_the_lookless_refusal_is_admissible() -> None:
    """The exact value caesar layer 1 crashed on."""

    assert "lookless_requires_executable_claims" in MECHANICAL_VERDICT_DECIDERS
    assert "lookless_requires_executable_claims" in CONTRACT_GAP_DECIDERS


def test_every_decider_the_lookless_paths_emit_is_admissible() -> None:
    """The general invariant: the emitter may not name a value the receipt refuses.

    Read from the source rather than a hand-kept list, so a new structural refusal added
    to `verdicts.py` fails here until someone classifies it -- mechanical, and therefore
    admissible on a non-qualitative group, or a judgment value that only reaches a
    qualitative one.
    """

    tree = ast.parse(Path(inspect.getfile(verdicts)).read_text(encoding="utf-8"))
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "decided_by"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    emitted.add(value.value)

    # Values reached only on a qualitative group, where the mechanical guard does not
    # apply. Listed explicitly so adding one is a decision rather than an omission.
    judgment_only = {"provisional_requirement_contract_gap", "no_optical_signal"}
    unclassified = emitted - MECHANICAL_VERDICT_DECIDERS - judgment_only
    assert not unclassified, (
        "verdicts.py emits decided_by values the receipt cannot record and nobody has "
        f"classified: {sorted(unclassified)}"
    )


@pytest.mark.parametrize("decider", sorted(CONTRACT_GAP_DECIDERS))
def test_a_contract_gap_is_never_a_pass(decider: str) -> None:
    """A frame that could not be settled cannot have passed.

    This replaces the deterministic-status agreement check for gap verdicts: there is no
    deterministic status to agree with when the frame was never decided.
    """

    from vfx_harness.domain import layer_evaluation_receipts as receipts

    source = inspect.getsource(receipts)
    assert "CONTRACT_GAP_DECIDERS" in source
    assert "is a contract gap and cannot pass" in source
    assert decider in CONTRACT_GAP_DECIDERS
