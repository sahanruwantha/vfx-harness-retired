"""Completion-debt publication never retains work-unit state locks over ledger I/O."""

from __future__ import annotations

import json
from threading import Event, Thread
from types import SimpleNamespace

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import ABSENT_SELECTION_TOKEN, pass_unit
from vfx_harness.agents.builder.unit_completion import resolve_completed_unit
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.orchestration import plan_due, unit_state
from vfx_harness.orchestration.plan_authority import PlanBundle


def _completion_bundle(tmp_path) -> PlanBundle:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "obligations.json").write_text(
        json.dumps(
            {
                "schema": "vfx-harness.obligations/v1",
                "obligations": [
                    {
                        "id": "O-form",
                        "statement": "prove the completed form",
                        "requirement_ids": ["R-form"],
                        "owner": "1.form",
                        "due": {
                            "kind": "unit_completion",
                            "layer": "1",
                            "unit": "form",
                        },
                        "evidence": [
                            {"kind": "scene_contract", "id": "contract.form"}
                        ],
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "assumptions.json").write_text(
        json.dumps(
            {
                "schema": "vfx-harness.assumptions/v1",
                "assumptions": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return PlanBundle(
        shot=tmp_path,
        run_id="plan-run",
        root=root,
        content_hash="b" * 64,
        artifacts=(),
        outcome="clean_with_deferred",
    )


def test_resolution_io_does_not_block_invalidation_and_stale_row_is_inert(
    tmp_path,
    monkeypatch,
) -> None:
    unit = _unit("form")
    units = (unit,)
    plan_hash = "a" * 64
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    pass_unit(
        tmp_path,
        "1",
        unit,
        units,
        plan_hash=plan_hash,
        selection_token=ABSENT_SELECTION_TOKEN,
    )
    receipt = UnitCompletionReceipt.parse(
        unit_state.load(tmp_path, "1")["units"][unit.id]["completion_receipt"]
    )
    selected = SimpleNamespace(
        plan=SimpleNamespace(bundle=_completion_bundle(tmp_path)),
        selection_token=ABSENT_SELECTION_TOKEN,
    )
    publication_started = Event()
    release_publication = Event()
    replan_done = Event()
    errors: list[BaseException] = []
    original_write = plan_due.atomic_write

    def blocked_write(path, text):
        publication_started.set()
        assert release_publication.wait(5)
        return original_write(path, text)

    monkeypatch.setattr(plan_due, "atomic_write", blocked_write)

    def resolve() -> None:
        try:
            resolve_completed_unit(
                tmp_path,
                "1",
                unit,
                units,
                receipt,
                expected_plan_hash=plan_hash,
                selected_authority=selected,
            )
        except BaseException as exc:  # asserted below
            errors.append(exc)

    def invalidate() -> None:
        try:
            unit_state.apply_replan(
                tmp_path,
                "1",
                units,
                units,
                old_plan_hash=plan_hash,
                new_plan_hash=plan_hash,
                owner="fixture",
                trigger="invalidate while completion resolution is publishing",
                evidence=["fixture:concurrent-invalidation"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    resolution_thread = Thread(target=resolve)
    invalidation_thread = Thread(target=invalidate)
    resolution_thread.start()
    assert publication_started.wait(2)
    invalidation_thread.start()
    try:
        assert replan_done.wait(2), "invalidation waited on completion resolution I/O"
    finally:
        release_publication.set()
    resolution_thread.join(5)
    invalidation_thread.join(5)

    assert not resolution_thread.is_alive()
    assert not invalidation_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], plan_due.PlanDueError)
    assert "O-form" in str(errors[0])
    stale_rows = plan_due.load_resolutions(
        tmp_path / "state" / plan_due.RESOLUTIONS,
        bundle_hash=selected.plan.bundle.content_hash,
        current_completion_receipts={},
    )
    assert ("obligation", "O-form") not in stale_rows
