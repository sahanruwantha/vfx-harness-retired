"""Canonical run-id requirements for plan-consumer allocation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import plan_consumer_view_allocation as allocation
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerViewMutationConflict,
)


@pytest.mark.parametrize(
    "run_id",
    [".", "..", "latest", ".plan-eval-legacy", "nested/run", ""],
)
def test_invalid_run_id_refuses_before_consumer_view_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    shot = tmp_path / "shot"
    root = shot / "runs" / run_id
    scratch = root / "scratch"
    scratch.mkdir(parents=True)
    sentinel = scratch / "sentinel.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")

    def refuse_mkdir(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid run id reached consumer-view directory allocation")

    monkeypatch.setattr(os, "mkdir", refuse_mkdir)
    layout = RunLayout(shot=shot, run_id=run_id, root=root)

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="plan-consumer run_id must match",
    ), allocation.allocating_plan_consumer_view(layout):
        pytest.fail("invalid run id yielded a consumer-view allocation")

    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
    assert list(scratch.glob(".plan-consumer-view.tmp-*")) == []
