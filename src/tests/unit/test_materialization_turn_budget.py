"""Materialization turn budgets scale with owned requirements (HIR-0177)."""

from __future__ import annotations

import pytest

from vfx_harness.agents.planner.budget import (
    MATERIALIZATION_TURN_CEILING,
    MATERIALIZATION_TURN_FLOOR,
    materialization_turn_budget,
)


def test_budget_is_the_larger_of_requested_and_scaled_and_stays_capped() -> None:
    assert materialization_turn_budget(24, 0) == MATERIALIZATION_TURN_FLOOR
    assert materialization_turn_budget(24, 9) == 42
    assert materialization_turn_budget(24, 21) == 66
    assert materialization_turn_budget(80, 3) == 80
    assert materialization_turn_budget(24, 1000) == MATERIALIZATION_TURN_CEILING
    with pytest.raises(ValueError, match="positive"):
        materialization_turn_budget(0, 4)
