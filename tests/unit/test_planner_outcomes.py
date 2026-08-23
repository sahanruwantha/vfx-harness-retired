from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from vfx_harness.agents import planner
from vfx_harness.observability import run_artifacts


def test_two_pass_seeds_canonical_gate_candidate_from_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = tmp_path / "plans" / "global.draft.md"
    draft.parent.mkdir()
    draft.write_text("# exact draft\n", encoding="utf-8")
    final = tmp_path / "plans" / "global.md"
    final.write_text("# stale candidate\n", encoding="utf-8")
    shot = SimpleNamespace(folder=tmp_path)

    async def fake_generate(*args, **kwargs):
        if kwargs.get("verify_draft"):
            assert final.read_bytes() == draft.read_bytes()
            final.write_text("# verified\n", encoding="utf-8")
            return final
        return draft

    monkeypatch.setattr(planner, "load_shot", lambda folder: shot)
    monkeypatch.setattr(planner, "generate_plan", fake_generate)
    monkeypatch.setattr(
        planner.Settings,
        "from_environment",
        lambda **kwargs: SimpleNamespace(planner_model="model"),
    )
    monkeypatch.setattr(
        run_artifacts,
        "ensure",
        lambda *args, **kwargs: SimpleNamespace(scratch=tmp_path / "scratch"),
    )

    async def invoke():
        return await planner.generate_plan_two_pass(
            tmp_path, verify_only=True, workspace=tmp_path
        )

    result = anyio.run(invoke)

    assert result == final
    assert final.read_text(encoding="utf-8") == "# verified\n"
    assert draft.read_text(encoding="utf-8") == "# exact draft\n"


def test_target_validation_feedback_closes_the_warm_loop(tmp_path: Path) -> None:
    """The materialization schema exists nowhere the session can read; the validator's
    field-precise errors are its only documentation, so they must arrive on every write
    of the target — and only the target."""
    from vfx_harness.agents.plan_guardrails import target_validation_feedback

    target = tmp_path / "jit-layer-1.json"
    errors: list[str] = ["layers[0].stages[0].plan must be a non-empty string"]
    matcher = target_validation_feedback(target, lambda: list(errors))
    hook = matcher.hooks[0]

    async def exercise():
        failing = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(target)}}, "t1", None
        )
        errors.clear()
        passing = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(target)}}, "t2", None
        )
        other = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "x.json")}},
            "t3", None,
        )
        return failing, passing, other

    failing, passing, other = anyio.run(exercise)

    assert "VALIDATION FAILED" in failing["hookSpecificOutput"]["additionalContext"]
    assert "stages[0].plan" in failing["hookSpecificOutput"]["additionalContext"]
    assert "VALIDATION PASSED" in passing["hookSpecificOutput"]["additionalContext"]
    assert other == {}


def test_materialization_kickoff_carries_row_and_readable_paths(tmp_path: Path) -> None:
    """Run 20260823T125746Z-9cd0b8: the kickoff named only the bundle hash, so the
    session probed six wrong bundle locations, was denied, reconstructed its layer row
    from prose, and failed structural validation on every field. The kickoff must carry
    the exact row and the readable authority paths."""
    bundle_root = tmp_path / "runs" / "r1" / "checkpoints" / "plans" / "bundles" / "abc"
    bundle_root.mkdir(parents=True)
    row = {
        "id": "1", "script": "build/01_boot.py", "title": "Bootstrap",
        "primary_judge": 1, "judge": [{"frame": 1, "ref": "refs/a.png"}],
        "owns": ["camera_path_fidelity"], "evidence_domains": ["scene"],
        "reads": "authored brief", "execution": "jit_deferred", "stages": [],
    }
    (bundle_root / "layers.json").write_text(
        json.dumps({"schema": 5, "layers": [row]}), encoding="utf-8"
    )
    bundle = SimpleNamespace(root=bundle_root, content_hash="abc123")
    layer = SimpleNamespace(id="1", title="Bootstrap", jit=SimpleNamespace())

    kickoff = planner._materialization_kickoff(
        tmp_path, layer, bundle, "runs/r2/scratch/jit-layer-1.json"
    )

    assert "runs/r1/checkpoints/plans/bundles/abc/" in kickoff
    assert '"script": "build/01_boot.py"' in kickoff
    assert "state/plan-resolutions.jsonl" in kickoff
    assert "plans/outcomes/" in kickoff
    # The schema example must cover the fields attempts 2-3 died discovering.
    for field in ('"plan"', '"protects"', '"control_roles"', '"proposition"',
                  '"requirement_bindings"', '"completion"'):
        assert field in kickoff, f"schema example missing {field}"


def test_two_pass_verify_exhaustion_falls_back_to_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs 1c18c2/7040c2/270652 each died when verify hit its ceiling, discarding a
    converging candidate the deterministic gate and repair rounds never saw. Exhaustion
    hands the on-disk candidate forward; any other terminal cause still propagates."""
    from vfx_harness.agents.resilience import AgentSessionFailure

    draft = tmp_path / "plans" / "global.draft.md"
    draft.parent.mkdir()
    draft.write_text("# exact draft\n", encoding="utf-8")
    shot = SimpleNamespace(folder=tmp_path)

    async def exhausted_verify(*args, **kwargs):
        if kwargs.get("verify_draft"):
            raise AgentSessionFailure("verify died", "max_turns_exhausted")
        return draft

    monkeypatch.setattr(planner, "load_shot", lambda folder: shot)
    monkeypatch.setattr(planner, "generate_plan", exhausted_verify)
    monkeypatch.setattr(
        planner.Settings,
        "from_environment",
        lambda **kwargs: SimpleNamespace(planner_model="model", plan_verify_max_turns=6),
    )
    monkeypatch.setattr(
        run_artifacts,
        "ensure",
        lambda *args, **kwargs: SimpleNamespace(scratch=tmp_path / "scratch"),
    )

    async def invoke():
        return await planner.generate_plan_two_pass(
            tmp_path, verify_only=True, workspace=tmp_path
        )

    result = anyio.run(invoke)

    assert result == tmp_path / "plans" / "global.md"
    assert result.read_bytes() == draft.read_bytes()

    async def terminally_failing_verify(*args, **kwargs):
        if kwargs.get("verify_draft"):
            raise AgentSessionFailure("billing", "usage_limit")
        return draft

    monkeypatch.setattr(planner, "generate_plan", terminally_failing_verify)

    async def invoke_terminal():
        return await planner.generate_plan_two_pass(
            tmp_path, verify_only=True, workspace=tmp_path
        )

    with pytest.raises(AgentSessionFailure):
        anyio.run(invoke_terminal)


def test_until_clean_main_exits_three_and_preserves_dirty_plan(tmp_path, monkeypatch) -> None:
    plan = tmp_path / "plans" / "global.md"
    plan.parent.mkdir()
    plan.write_text("# dirty but useful\n", encoding="utf-8")
    shot = SimpleNamespace(folder=tmp_path, id="dirty-plan")

    async def dirty_result(*args, **kwargs):
        return planner.PlanLoopResult(plan, "budget", 2)

    monkeypatch.setattr(planner, "load_shot", lambda folder: shot)
    monkeypatch.setattr(planner, "generate_plan_until_clean", dirty_result)
    monkeypatch.setattr(sys, "argv", ["vfx plan", str(tmp_path), "--until-clean"])
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "planner-dirty")

    with pytest.raises(planner.PlanGateFailure) as raised:
        planner.main()

    assert raised.value.code == 3
    assert plan.read_text(encoding="utf-8") == "# dirty but useful\n"
    layout = run_artifacts.select(tmp_path, "planner-dirty")
    assert layout is not None
    status = json.loads(layout.status.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["exit_code"] == 3
    assert not (tmp_path / "plan.provenance.json").exists()
