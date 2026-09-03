"""Turn budgets for planning sessions, derived from the work the session must author.

A materialization stages one bounded unit per turn cycle and pays a teaching rejection
or two per unit; a fixed 24-turn cap fit a two-unit layer and exhausted on a four-unit,
21-requirement layer with every unit still unstaged for publication
(run 20260903T002758Z-1b6807, HIR-0177). The budget is proportional to what the sparse
row asks the layer to close; it is still a cap, and an exhausted session still publishes
nothing.

The global verify pass audits one layer at a time as well: a fixed six-turn cap covered a
three-layer plan and exhausted on the six-layer hansa_silk_road and caesar_curia drafts
before the audit reached their last layers (OBS-55). Its budget is proportional to the
layers the draft's ownership mapping declares, under the same explicit ceiling.
"""

from __future__ import annotations

MATERIALIZATION_TURN_FLOOR = 24
MATERIALIZATION_TURNS_PER_REQUIREMENT = 2
MATERIALIZATION_TURN_CEILING = 96


def materialization_turn_budget(requested: int, owned_requirements: int) -> int:
    """The larger of the requested cap and the floor plus two turns per owned requirement."""

    if int(requested) < 1:
        raise ValueError("materialization turn budget must be positive")
    owned = max(0, int(owned_requirements))
    scaled = MATERIALIZATION_TURN_FLOOR + MATERIALIZATION_TURNS_PER_REQUIREMENT * owned
    return min(MATERIALIZATION_TURN_CEILING, max(int(requested), scaled))


PLAN_VERIFY_TURN_FLOOR = 6
PLAN_VERIFY_TURNS_PER_LAYER = 2
PLAN_VERIFY_TURN_CEILING = 24


def plan_verify_turn_budget(requested: int, layers: int) -> int:
    """The larger of the requested cap and the floor plus two turns per drafted layer."""

    if int(requested) < 1:
        raise ValueError("plan verify turn budget must be positive")
    scaled = PLAN_VERIFY_TURN_FLOOR + PLAN_VERIFY_TURNS_PER_LAYER * max(0, int(layers))
    return min(PLAN_VERIFY_TURN_CEILING, max(int(requested), scaled))


__all__ = [
    "MATERIALIZATION_TURNS_PER_REQUIREMENT",
    "MATERIALIZATION_TURN_CEILING",
    "MATERIALIZATION_TURN_FLOOR",
    "PLAN_VERIFY_TURNS_PER_LAYER",
    "PLAN_VERIFY_TURN_CEILING",
    "PLAN_VERIFY_TURN_FLOOR",
    "materialization_turn_budget",
    "plan_verify_turn_budget",
]
