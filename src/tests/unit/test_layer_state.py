from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.run_owner_support import owned_run, pass_run
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import layer_state


@pytest.mark.parametrize("shot_name", ["static-product", "outdoor-motion"])
def test_layer_state_read_is_pure_and_resolves_durable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shot_name: str
) -> None:
    shot = tmp_path / shot_name
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    assert layer_state.load(shot) == {}
    assert layer_state.path_for(shot) == shot / "state" / "layer_state.json"
    assert not (shot / "runs").exists()


def test_layer_state_survives_run_boundaries_without_becoming_run_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "animated-character"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    with owned_run(shot, "run-first") as (first, lease):
        started = layer_state.start(shot, "form", [(7, "refs/profile.png")])

        assert started["attempts"] == 1
        assert (shot / "state" / "layer_state.json").is_file()
        assert not (first.root / "state").exists()
        pass_run(first, lease)

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with owned_run(shot, "run-second") as (second, _lease):
        carried = layer_state.start(shot, "form", [(19, "refs/action.png")])
        latest = json.loads((shot / "runs" / "latest.json").read_text(encoding="utf-8"))

    assert carried["attempts"] == 2
    assert carried["frames"] == {"19": {"ref": "refs/action.png"}}
    assert latest["run_id"] == second.run_id
    assert latest["state"] == "running"
    assert not (second.root / "state").exists()
