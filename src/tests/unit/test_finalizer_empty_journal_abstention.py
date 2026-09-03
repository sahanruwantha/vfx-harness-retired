"""A live ``cannot_express_in_scope`` with no accepted call publishes its finding
through a harness-authored no-op candidate, never through a model finalizer session.

Run 20260903T100335Z-fa5dbb's streetlight_flicker abstained after 18 turns without one
accepted ``run_bpy`` call; the finalizer then spent 25 turns reading run metadata to
conclude there was nothing to distil (HIR-0185)."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from vfx_harness.agents.builder import unit_finalize


class _Session:
    def __init__(self, root: Path, calls: int) -> None:
        self.root = root
        self.calls = calls
        self.blender = "blender"

    def journal_destination(self, name: str) -> Path:
        return self.root / "runs" / "r1" / "checkpoints" / "journals" / name

    def journal(self, *, path: str, start, limit):
        return {"calls": self.calls, "chars": 0, "dropped": 0}


def _invoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, calls: int, comparison_state: dict):
    shot = SimpleNamespace(folder=tmp_path)
    unit = SimpleNamespace(id="flicker", frame=1, ref="refs/f1.png")
    invoked: list[str] = []

    async def forbidden_finalizer(*args, **kwargs):
        invoked.append(kwargs.get("mode", "?"))
        raise RuntimeError("model finalizer invoked")

    monkeypatch.setattr(unit_finalize, "_run_script_agent", forbidden_finalizer)
    monkeypatch.setattr(unit_finalize, "_unit_evidence_ids_by_frame", lambda *a, **k: {})
    monkeypatch.setattr(unit_finalize, "script_model", lambda: "model")
    monkeypatch.setattr(unit_finalize, "finalize_prompt", lambda *a, **k: "finalize prompt")
    monkeypatch.setattr(unit_finalize.costlog, "scoped", lambda **kwargs: nullcontext())
    monkeypatch.setattr(
        unit_finalize.run_artifacts,
        "ensure",
        lambda *a, **k: SimpleNamespace(scratch=tmp_path / "runs" / "r1" / "scratch"),
    )

    async def run():
        return await unit_finalize._persist_journal_and_finalize_script(
            shot,
            unit,
            "build/units/02/flicker.py",
            "runs/r1/scratch/candidate/flicker.py",
            [],
            _Session(tmp_path, calls),
            [],
            False,
            None,
            None,
            False,
            {"snap": {}, "round": 0},
            0,
            None,
            comparison_state,
            {},
            object(),
        )

    return run, invoked


def test_empty_journal_abstention_publishes_a_noop_candidate_without_a_model_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"cannot_express": {"contract_ids": ["streetlight-flicker-schedule"]}}
    run, invoked = _invoke(tmp_path, monkeypatch, calls=0, comparison_state=state)

    probe_ctx = anyio.run(run)

    assert invoked == []
    assert probe_ctx["comparison_state"] is state
    candidate = tmp_path / "runs" / "r1" / "scratch" / "candidate" / "flicker.py"
    text = candidate.read_text(encoding="utf-8")
    assert "cannot_express_in_scope" in text
    assert "streetlight-flicker-schedule" in text
    assert "The typed finding, not this script, is the unit outcome." in text


def test_empty_journal_without_abstention_still_runs_the_model_finalizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, invoked = _invoke(tmp_path, monkeypatch, calls=0, comparison_state={})

    with pytest.raises(RuntimeError, match="model finalizer invoked"):
        anyio.run(run)

    assert invoked == ["finalize"]
    assert not (tmp_path / "runs" / "r1" / "scratch" / "candidate" / "flicker.py").exists()


def test_accepted_calls_with_an_abstention_still_distil_through_the_finalizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"cannot_express": {"contract_ids": ["row"]}}
    run, invoked = _invoke(tmp_path, monkeypatch, calls=3, comparison_state=state)

    with pytest.raises(RuntimeError, match="model finalizer invoked"):
        anyio.run(run)

    assert invoked == ["finalize"]
