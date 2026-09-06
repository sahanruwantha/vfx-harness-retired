"""The closed vocabulary of mechanically decided verdicts.

A verdict on a group with no ``qualified_qualitative_required`` claim must be decided by
the harness, never by a model.  Two boundaries need that set: ``agents/builder/verdicts``
emits it and ``domain/layer_evaluation_receipts`` admits it.  Held apart, the emitter grew
three structural refusals the receipt had never heard of, and each one turned a correct
diagnosis into a ValueError at receipt-minting time — the boundary that exists to record
the finding refused to record it.

Kept here, in a leaf with no imports, so both sides read one definition.
"""

from __future__ import annotations

# Decided from executable evidence: the frame was measured and passed or failed.
EXECUTABLE_DECIDER = "unit_executable_evidence"

# Decided from authority shape: the frame could not be settled at all, and which
# structural fact prevented it.  Every one is derived from the plan and the claims, so
# all are mechanical; none is a model judgment.
CONTRACT_GAP_DECIDERS = frozenset(
    {
        "look_without_image_domain",
        "uncovered_judge_frame",
        "lookless_requires_executable_claims",
    }
)

MECHANICAL_VERDICT_DECIDERS = frozenset({EXECUTABLE_DECIDER}) | CONTRACT_GAP_DECIDERS
