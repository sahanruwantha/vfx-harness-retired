"""Plan-evaluation CLI run-layout regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.evaluation import cli
from vfx_harness.observability.run_artifacts import RunLayout


def test_plan_evaluation_uses_a_domain_valid_temporary_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = tmp_path / "shot"
    pointer = shot / cli.POINTER
    pointer.parent.mkdir(parents=True)
    pointer.write_text("{}\n", encoding="utf-8")
    observed: list[RunLayout] = []

    monkeypatch.setattr(cli, "resolve_current", lambda _shot: None)

    def prepare(layout: RunLayout) -> Path:
        observed.append(layout)
        assert require_run_id(layout.run_id) == layout.run_id
        return layout.root

    monkeypatch.setattr(cli, "prepare_consumer_view", prepare)
    result = SimpleNamespace(shot="", clean=True)
    monkeypatch.setattr(cli._pg, "run", lambda _root, _plan: result)
    monkeypatch.setattr(cli._pg, "report", lambda _result: "clean")

    assert cli._cmd_plan([str(shot)]) == 0
    assert len(observed) == 1
    assert observed[0].run_id.startswith("plan-eval-")
    assert not observed[0].root.exists()
