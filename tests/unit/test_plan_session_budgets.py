from __future__ import annotations

import pytest

from vfx_harness.agents.plan_tools import _SpikeBudget
from vfx_harness.infrastructure.config import Settings


def test_spike_budget_allows_initial_attempt_and_one_failed_retry() -> None:
    budget = _SpikeBudget(session_cap=8, attempts_per_hypothesis=2)
    key = _SpikeBudget.key([{"id": "SC-onset", "kind": "onset_order"}])

    assert budget.refusal(key) is None
    budget.record(key, ran_blender=True, failed=True)
    assert budget.refusal(key) is None
    budget.record(key, ran_blender=True, failed=True)

    refusal = budget.refusal(key)
    assert refusal is not None and "planner_start" in refusal
    other = _SpikeBudget.key([{"id": "SC-other", "kind": "frame_delta"}])
    assert budget.refusal(other) is None


def test_spike_budget_success_does_not_burn_the_hypothesis() -> None:
    budget = _SpikeBudget(attempts_per_hypothesis=2)
    key = _SpikeBudget.key([])

    assert key == "exploratory"
    for _ in range(3):
        budget.record(key, ran_blender=True, failed=False)
    assert budget.refusal(key) is None


def test_spike_budget_validation_failures_count_without_blender_spend() -> None:
    budget = _SpikeBudget(session_cap=2, attempts_per_hypothesis=2)
    key = _SpikeBudget.key([{"kind": "onset_order"}])

    budget.record(key, ran_blender=False, failed=True)
    budget.record(key, ran_blender=False, failed=True)

    assert budget.total == 0
    assert budget.refusal(key) is not None
    assert budget.refusal(_SpikeBudget.key([])) is None


def test_spike_budget_session_cap_refuses_every_hypothesis() -> None:
    budget = _SpikeBudget(session_cap=1)
    budget.record("a", ran_blender=True, failed=False)

    refusal = budget.refusal("anything-else")
    assert refusal is not None and "budget exhausted" in refusal


def test_plan_max_turns_setting_parses_and_rejects_nonsense(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VFXH_PLAN_MAX_TURNS", raising=False)
    assert Settings.from_environment(load_dotenv_file=False).plan_max_turns == 100

    monkeypatch.setenv("VFXH_PLAN_MAX_TURNS", "60")
    assert Settings.from_environment(load_dotenv_file=False).plan_max_turns == 60

    monkeypatch.setenv("VFXH_PLAN_MAX_TURNS", "0")
    with pytest.raises(ValueError, match="VFXH_PLAN_MAX_TURNS"):
        Settings.from_environment(load_dotenv_file=False)
