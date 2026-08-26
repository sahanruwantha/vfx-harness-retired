from __future__ import annotations

import json
from pathlib import Path

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


def test_spike_budget_identity_cannot_be_evaded_by_renaming_contracts() -> None:
    first = {"id": "probe-one", "kind": "onset_order", "frames": [1, 36],
             "roles": ["iris_blade"]}
    renamed = {**first, "id": "probe-two"}

    assert _SpikeBudget.key([first]) == _SpikeBudget.key([renamed])


def test_plan_max_turns_setting_parses_and_rejects_nonsense(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VFXH_PLAN_MAX_TURNS", raising=False)
    monkeypatch.delenv("VFXH_PLAN_VERIFY_MAX_TURNS", raising=False)
    settings = Settings.from_environment(load_dotenv_file=False)
    assert settings.plan_max_turns == 12
    assert settings.plan_verify_max_turns == 6

    monkeypatch.setenv("VFXH_PLAN_MAX_TURNS", "60")
    assert Settings.from_environment(load_dotenv_file=False).plan_max_turns == 60

    monkeypatch.setenv("VFXH_PLAN_MAX_TURNS", "0")
    with pytest.raises(ValueError, match="VFXH_PLAN_MAX_TURNS"):
        Settings.from_environment(load_dotenv_file=False)


def test_reference_measurement_scope_follows_ready_units(tmp_path) -> None:
    from vfx_harness.agents.plan_tools import _ready_measure_refs

    assert _ready_measure_refs(tmp_path) is None
    (tmp_path / "layers.json").write_text(json.dumps({
        "layers": [
            {"id": "1", "execution": "ready", "judge": [{"frame": 1, "ref": "refs/a.png"}]},
            {"id": "2", "execution": "jit_deferred", "judge": [{"frame": 9, "ref": "refs/z.png"}]},
        ]
    }), encoding="utf-8")

    assert _ready_measure_refs(tmp_path) == {"refs/a.png"}


def test_global_phase_registers_only_escalation_and_gate_tools(tmp_path) -> None:
    from vfx_harness.agents.plan_tools import build_plan_tools

    _server, names = build_plan_tools(
        tmp_path,
        include_gate=True,
        enabled_tools=frozenset({"ask_supervisor", "run_gate"}),
    )

    assert {name.split("__")[-1] for name in names} == {"ask_supervisor", "run_gate"}


def test_spike_refuses_adopted_decision_and_falsification_hypotheses(tmp_path) -> None:
    from vfx_harness.agents.plan_tools import _spike_ineligibility

    state = tmp_path / "state"
    state.mkdir()
    (state / "plan-resolutions.jsonl").write_text(
        '{"id": "A2", "values": {"contract": {"kind": "keyframe_schedule", '
        '"roles": ["cam_rig"], "samples": []}}, '
        '"falsification": {"contract_ids": ["SC-L1-16-housing-bbox-height-f36"]}}\n',
        encoding="utf-8",
    )

    adopted = _spike_ineligibility(
        "", None, [{"id": "x", "kind": "object_property", "decision_id": "A5"}], tmp_path
    )
    assert adopted is not None and "adopts decision" in adopted

    falsification = _spike_ineligibility(
        "", None,
        [{"id": "SC-L1-16-housing-bbox-height-f36", "kind": "bbox_height", "frame": 36}],
        tmp_path,
    )
    assert falsification is not None and "falsification path" in falsification

    revalue = _spike_ineligibility(
        "", None,
        [{"id": "probe", "kind": "keyframe_schedule", "roles": ["cam_rig"], "samples": []}],
        tmp_path,
    )
    assert revalue is not None and "re-measures decision A2" in revalue


def test_spike_refuses_self_fulfilling_and_lighting_hypotheses(tmp_path) -> None:
    from vfx_harness.agents.plan_tools import _spike_ineligibility

    count = _spike_ineligibility(
        "", None, [{"id": "rim-count", "kind": "object_count", "roles": ["rim.*"]}], tmp_path
    )
    assert count is not None and "self-fulfilling" in count

    lit = _spike_ineligibility(
        "import bpy\nbpy.ops.object.light_add(type='AREA')\n", 36, [], tmp_path
    )
    assert lit is not None and "lighting/visibility" in lit

    # A light-free mechanism probe with a render stays eligible, as does a light-touching
    # script that never asks for a render (geometry printout only).
    assert _spike_ineligibility("import bpy\nprint('geom')\n", 36, [], tmp_path) is None
    assert _spike_ineligibility(
        "import bpy\nbpy.ops.object.light_add(type='AREA')\nprint('x')\n", None, [], tmp_path
    ) is None


def test_spike_mechanism_calibration_stays_eligible(tmp_path) -> None:
    from vfx_harness.agents.plan_tools import _spike_ineligibility

    assert _spike_ineligibility(
        "import bpy\n", None,
        [{"id": "blade-arc", "kind": "radial_inward_fraction", "frame": 36,
          "roles": ["iris_blade"], "op": "max", "hi": 0.3}],
        tmp_path,
    ) is None


def test_calibration_closes_after_initial_batch_plus_one_repair() -> None:
    from vfx_harness.agents.plan_tools import _CheckBatchBudget

    budget = _CheckBatchBudget()

    assert budget.take_batch()          # initial manifest
    budget.reset_after_batch()
    assert budget.take_single()         # probe a reject precisely
    assert budget.take_batch()          # the one repair batch
    budget.reset_after_batch()

    assert budget.closed
    assert not budget.take_batch()      # a third batch is refused
    assert not budget.take_single()     # and singles close with it


def test_calibration_singles_stay_bounded_before_any_batch() -> None:
    from vfx_harness.agents.plan_tools import _CheckBatchBudget

    budget = _CheckBatchBudget()

    assert budget.take_single()
    assert budget.take_single()
    assert not budget.take_single()     # two exploratory singles, then batch required
    assert budget.take_batch()
    budget.reset_after_batch()
    assert budget.take_single()         # reset restores precise follow-up probes


def test_materialization_session_denies_glob_and_registers_patch(tmp_path: Path) -> None:
    from vfx_harness.agents.plan_tools import build_plan_tools
    from vfx_harness.agents.planner import MATERIALIZATION_DENIED_TOOLS

    assert MATERIALIZATION_DENIED_TOOLS == ["Bash", "Edit", "Glob", "Grep"]

    candidate = tmp_path / "jit-layer-1.json"
    candidate.write_text("{}", encoding="utf-8")
    _server, names = build_plan_tools(
        tmp_path,
        enabled_tools=frozenset({"patch_materialization"}),
        candidate_materialization=candidate,
    )
    assert {name.split("__")[-1] for name in names} == {"patch_materialization"}

    _server, absent = build_plan_tools(
        tmp_path,
        enabled_tools=frozenset({"patch_materialization"}),
    )
    assert absent == []
