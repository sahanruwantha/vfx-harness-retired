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

from vfx_harness.agents.builder import verdicts
from vfx_harness.domain.verdict_deciders import (
    CONTRACT_GAP_DECIDERS,
    EXECUTABLE_DECIDER,
    JUDGMENT_ONLY_DECIDERS,
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

    # JUDGMENT_ONLY_DECIDERS carries the reason and the unproven boundary.
    unclassified = emitted - MECHANICAL_VERDICT_DECIDERS - JUDGMENT_ONLY_DECIDERS
    assert not unclassified, (
        "verdicts.py emits decided_by values the receipt cannot record and nobody has "
        f"classified: {sorted(unclassified)}"
    )


def test_the_gap_deciders_are_disjoint_from_the_executable_one() -> None:
    """What this file does NOT yet assert, stated so nobody reads more into it.

    The behavioural invariant -- a canonical row carrying a gap decider with pass=True
    raises -- is NOT tested here. Asserting it needs a minted LayerFinalizationReceipt,
    which needs a full shot layout (layers.json, sealed outcome sources, a real replay
    receipt and judge point); `make_layer_finalization_receipt` refuses a non-empty
    canonical without `seal_outcome_sources=True` and then wants the shot tree.

    An earlier version of this test asserted on `inspect.getsource(receipts)` -- that the
    guard's identifier and message string appear in the file. Those pass if the guard is
    deleted and its message left in a comment, and fail on a pure rename that preserves
    behaviour: a description standing in for the artifact, in the test whose name was the
    invariant. Removed rather than left to imply coverage it did not have (caught by
    vfx-harness-4d).

    What remains here is the vocabulary shape, which is real and cheap; the AST test above
    is what actually protects the emitter/receipt pairing.
    """

    assert EXECUTABLE_DECIDER not in CONTRACT_GAP_DECIDERS
    assert not (CONTRACT_GAP_DECIDERS & JUDGMENT_ONLY_DECIDERS)
    assert not (MECHANICAL_VERDICT_DECIDERS & JUDGMENT_ONLY_DECIDERS)
