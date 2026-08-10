"""Critic verdicts and the lever → layer routing that drives the loop.

A critic never returns "bad". It returns a :class:`Verdict` that attributes the failure to
a :class:`Lever`, and the lever decides which :class:`Layer` reopens. This is what keeps the
loop from thrashing: a footage failure and a script failure route to different fixes, so the
scheduler spends the next round on the right subsystem. See ``docs/back-half-architecture.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from contracts.ledger import Layer


class Lever(str, Enum):
    """The corrective action a rejecting verdict prescribes."""

    RE_SOURCE = "re_source"  # footage wrong, but archival may exist → try acquiring again
    SWITCH_TO_MG = "switch_to_mg"  # nothing acquirable → stop hunting, generate it
    FILL_RESEARCH = "fill_research"  # a claim has no dossier support → run targeted research
    REWRITE_REGISTER = "rewrite_register"  # grounded but wrong voice → rewrite the VO
    REDESIGN_GRAMMAR = "redesign_grammar"  # modes incoherent / thrashing → redo the grammar
    HUMAN_ACQUIRE = "human_acquire"  # real gap the system cannot close → escalate to director


# Which layer each lever reopens. ``HUMAN_ACQUIRE`` closes nothing — it escalates.
# ``SWITCH_TO_MG`` is an override, not a re-decision: it *sets* the mode to motion-graphics
# and reopens only the downstream realization (handled specially in the scheduler), so its
# reopen target is REALIZATION. ``REDESIGN_GRAMMAR`` genuinely re-decides modes, so it targets
# MODE and applies to every beat (a GLOBAL lever).
_LEVER_REOPENS: dict[Lever, Layer | None] = {
    Lever.RE_SOURCE: Layer.CLIP,
    Lever.SWITCH_TO_MG: Layer.REALIZATION,
    Lever.FILL_RESEARCH: Layer.REALIZATION,
    Lever.REWRITE_REGISTER: Layer.REALIZATION,
    Lever.REDESIGN_GRAMMAR: Layer.MODE,
    Lever.HUMAN_ACQUIRE: None,
}

# Levers whose reopen applies to the whole film, not one beat.
GLOBAL_LEVERS: frozenset[Lever] = frozenset({Lever.REDESIGN_GRAMMAR})

# Levers that require a step outside the back half (re-entering research / a human).
EXTERNAL_LEVERS: frozenset[Lever] = frozenset({Lever.FILL_RESEARCH, Lever.HUMAN_ACQUIRE})


def reopen_target(lever: Lever) -> Layer | None:
    """The layer this lever reopens, or ``None`` if it escalates instead."""
    return _LEVER_REOPENS[lever]


@dataclass(frozen=True)
class Verdict:
    """One critic's ruling on one beat's layer."""

    beat: str
    layer: Layer
    passed: bool
    lever: Lever | None = None
    reason: str = ""
    dimensions: Mapping[str, bool] = field(default_factory=dict)
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.passed and self.lever is None:
            raise ValueError(f"failing verdict for {self.beat} must name a lever")
        if self.passed and self.lever is not None:
            raise ValueError(f"passing verdict for {self.beat} must not name a lever")

    @property
    def severity(self) -> float:
        """Confidence-weighted urgency, for tie-breaking which failure to act on first."""
        return 0.0 if self.passed else self.confidence
