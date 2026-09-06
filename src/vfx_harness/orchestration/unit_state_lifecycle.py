"""Closed lifecycle edges and claim admissibility shared by work-unit transactions.

This module is dependency-free on purpose.  The claim-admissibility sets live here
rather than in ``unit_state_claims`` because the transaction that enforces them and the
query that explains them must read one definition: the claims module imports
``unit_state`` -> ``unit_state_queries``, so a reader importing back closed a cycle and
had to be exempted.  Removing the cycle beats exempting it (AGENTS.md).
"""

from __future__ import annotations

TRANSITIONS = {
    "pending": {"planning", "blocked", "superseded"},
    "planning": {"building", "blocked", "failed", "retryable", "superseded"},
    "building": {
        "frozen",
        "blocked",
        "failed",
        "hypothesis_falsified",
        "retryable",
        "superseded",
    },
    "frozen": {
        "evaluating",
        "building",
        "blocked",
        "failed",
        "retryable",
        "superseded",
    },
    "evaluating": {
        "passed",
        "repairing",
        "blocked",
        "failed",
        "hypothesis_falsified",
        "retryable",
        "superseded",
    },
    "repairing": {
        "building",
        "frozen",
        "blocked",
        "failed",
        "hypothesis_falsified",
        "retryable",
        "superseded",
    },
    "retryable": {"planning", "building", "blocked", "failed", "superseded"},
    "blocked": {"pending", "planning", "superseded"},
    "failed": {"retryable", "superseded"},
    "hypothesis_falsified": {"superseded"},
    "passed": {"superseded"},
    "superseded": set(),
}


# The states ``claim_ready_unit_for_planning`` may claim from.  Every other member of
# TRANSITIONS is unclaimable, and ``unclaimable_state`` explains which transaction
# clears it -- both derived here so they cannot disagree.
PLANNING_CLAIMABLE_STATES = frozenset({"pending", "blocked", "retryable"})

# The states ``release_unclaimed_unit_for_retry`` accepts.  Derived, not listed: it is
# exactly "can reach retryable", which is what ``unclaimable_state`` tells an operator
# to run that transaction for.  Held apart, a hand-kept list could omit a state the
# advice names -- advice that resolves to a refusal is worse than no advice.
UNCLAIMED_RETRY_STATES = frozenset(
    state for state, successors in TRANSITIONS.items() if "retryable" in successors
)
