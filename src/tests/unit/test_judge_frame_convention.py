"""The 1-based frame convention is compiled, not guessed (HIR-0175)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vfx_harness.agents.global_planning_policy import frame_contract
from vfx_harness.domain.work_units.claims import JudgePoint


def test_judge_frame_rejection_teaches_the_convention() -> None:
    with pytest.raises(ValueError, match="frames are 1-based") as exc:
        JudgePoint.parse({"frame": 0, "ref": "refs/frame_0s.jpg"}, "layers[0].judge[0]")
    assert "round(t*fps)+1" in str(exc.value)
    assert "found 0" in str(exc.value)
    assert JudgePoint.parse({"frame": 1, "ref": "refs/frame_0s.jpg"}, "j").frame == 1


def test_global_kickoff_states_the_convention(tmp_path, monkeypatch) -> None:
    (tmp_path / "brief.md").write_text("---\nid: conv\n---\nbrief\n", encoding="utf-8")
    shot = SimpleNamespace(
        id="conv", folder=tmp_path, frames=225, fps=25, engine="BLENDER_EEVEE", refs=[]
    )

    text = frame_contract(shot)

    assert "Frames are 1-based: frame 1 is t=0.0s" in text
    assert "frame(t) = round(t*25)+1" in text
    assert "1..225" in text
