"""Selection races at the clean-gate materialization attestation boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.test_lifecycle_fixture import (
    _approve_hold_decision,
    _deferred_root,
    _root_materialization,
)
from tests.unit.test_plan_records import _candidate
from vfx_harness.evaluation import plan_gate
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.jit_materialization import (
    MaterializationSelectionConflict,
    finalize_materialization_candidate,
    materialization_finalization_path,
    publish_materialization,
)
from vfx_harness.orchestration.plan_authority import (
    prepare_consumer_view,
    publish_current,
    resolve_current,
)


def test_clean_gate_cannot_attest_after_semantic_authority_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receipt CAS compares revisioned heads, not just restored bundle content."""

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _deferred_root(tmp_path)
    layout = run_artifacts.create(tmp_path, "selection-a1")
    bundle_a1 = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    _approve_hold_decision(tmp_path, bundle_a1.content_hash)
    candidate = _root_materialization(tmp_path, bundle_a1.content_hash)
    consumer_view = prepare_consumer_view(layout)
    global_plan = tmp_path / "plans" / "global.md"
    plan_a = global_plan.read_bytes()

    def gate_then_aba(_folder: Path, **_kwargs):
        # A1 was published before the candidate-bound decision existed. Remove that
        # later ledger input only while reproducing the exact A plan bytes.
        decisions = tmp_path / "state" / "plan-resolutions.jsonl"
        decision_bytes = decisions.read_bytes()
        decisions.unlink()
        try:
            global_plan.write_bytes(plan_a + b"\n# generation b\n")
            publish_current(
                tmp_path,
                run_artifacts.create(tmp_path, "selection-b"),
                outcome="clean_with_deferred",
            )
            global_plan.write_bytes(plan_a)
            bundle_a2 = publish_current(
                tmp_path,
                run_artifacts.create(tmp_path, "selection-a2"),
                outcome="clean_with_deferred",
            )
        finally:
            decisions.write_bytes(decision_bytes)
        assert bundle_a2.content_hash == bundle_a1.content_hash
        assert resolve_current(tmp_path).content_hash == bundle_a1.content_hash
        # A real GateResult: production always returns one, and the gate's verdict is
        # scoped to the materializing layer before it is read (HIR-0189).
        return plan_gate.GateResult(tmp_path.name, [], {})

    monkeypatch.setattr(plan_gate, "run", gate_then_aba)

    with pytest.raises(MaterializationSelectionConflict, match="selection changed"):
        finalize_materialization_candidate(
            tmp_path,
            candidate,
            consumer_view,
        )

    assert not materialization_finalization_path(candidate).exists()


def test_gate_uses_immutable_decisions_and_refuses_live_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live ledger mutation cannot enter the staged gate or its final receipt."""

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _deferred_root(tmp_path)
    layout = run_artifacts.create(tmp_path, "gate-decision-snapshot")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    _approve_hold_decision(tmp_path, bundle.content_hash)
    candidate = _root_materialization(tmp_path, bundle.content_hash)
    consumer_view = prepare_consumer_view(layout)
    real_gate = plan_gate.run
    observed_clean: list[bool] = []

    def mutate_live_then_gate(folder: Path, **kwargs):
        (tmp_path / "state" / "plan-resolutions.jsonl").write_bytes(b"{broken")
        result = real_gate(folder, **kwargs)
        observed_clean.append(result.clean)
        return result

    monkeypatch.setattr(plan_gate, "run", mutate_live_then_gate)

    with pytest.raises(MaterializationSelectionConflict, match="decision inputs changed"):
        finalize_materialization_candidate(
            tmp_path,
            candidate,
            consumer_view,
        )

    assert observed_clean == [True]
    assert not materialization_finalization_path(candidate).exists()


def test_decision_append_after_finalization_cannot_publish_jit_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt is stale when the full decision generation gains a suffix."""

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _deferred_root(tmp_path)
    layout = run_artifacts.create(tmp_path, "post-gate-decision-append")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    _approve_hold_decision(tmp_path, bundle.content_hash)
    candidate = _root_materialization(tmp_path, bundle.content_hash)
    result = finalize_materialization_candidate(
        tmp_path,
        candidate,
        prepare_consumer_view(layout),
    )
    assert result.clean

    resolutions = tmp_path / "state" / "plan-resolutions.jsonl"
    with resolutions.open("ab") as handle:
        handle.write(b"{}\n")

    with pytest.raises(MaterializationSelectionConflict, match="planning inputs changed"):
        publish_materialization(tmp_path, candidate)

    assert not (tmp_path / "state" / "jit-layers" / "current.json").exists()
