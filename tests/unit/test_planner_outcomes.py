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
