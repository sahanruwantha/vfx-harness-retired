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

# Deliberately NOT mechanical, and the exclusion is load-bearing.
#
# ``provisional_requirement_contract_gap`` reaches a group *because* a judgment debt
# exists there, and a judgment debt mints ``qualified_qualitative_required`` (AGENTS.md:
# minted by the harness, never staged).  So the group is qualitative and the mechanical
# guard never runs.  Admitting it here would classify a critic-derived verdict as
# mechanically decided, which is the one thing that guard exists to prevent.
#
# Evidence: hansa_silk_road minted a receipt carrying this value with pass=False,
# contract_gap=True, accepted, while its state/judgment-debts.jsonl held rows;
# caesar_curia layer 1 crashed on the mechanical branch with no debts file at all.
#
# KNOWN UNPROVEN BOUNDARY: if this value ever reaches a group with no minted qualitative
# claim, the receipt raises exactly as it does today.  That path could not be constructed
# from ``_provisional_composition_contract_gap`` -- it reads provisional_requirement_ids
# and provisional_debt_ids off the active unit -- but it was not proved impossible either.
# Recorded as unproven rather than assumed (vfx-harness-4d).
JUDGMENT_ONLY_DECIDERS = frozenset(
    {"provisional_requirement_contract_gap", "no_optical_signal"}
)
