from __future__ import annotations

import json
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import claim_for_build, legacy_apply_replan
from vfx_harness.agents.builder.attempt_guard import (
    UnitAttemptAuthorityLost,
    UnitAttemptGuard,
)
from vfx_harness.agents.builder.authority import AuthorityBoundLedger
from vfx_harness.domain.brief import Shot
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.ledger import Ledger, LedgerSaveConflict, Milestone
from vfx_harness.orchestration.shot_authority_capture import (
    shot_authority_writer_fence,
)


def _shot(tmp_path) -> Shot:
    return Shot(
        folder=tmp_path,
        frontmatter={"id": "ledger-fixture", "frames": 1, "fps": 24},
        body="fixture",
    )


def _building_guard(tmp_path, *, plan_hash: str):
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _unit("hero")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    claim = claim_for_build(
        tmp_path,
        "1",
        units,
        unit.id,
        plan_hash=plan_hash,
        selection_token=token,
    )
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        claim,
        expected_plan_hash=plan_hash,
        selected_authority=SimpleNamespace(selection_token=token),
    )
    return unit, units, guard


def test_two_writer_ledger_cas_has_one_explicit_loser(tmp_path, monkeypatch) -> None:
    shot = _shot(tmp_path)
    baseline = Ledger(shot)
    baseline.data["seed"] = {"kept": True}
    baseline.save()

    writers = {name: Ledger(shot) for name in ("writer_a", "writer_b")}
    for name, ledger in writers.items():
        ledger.data[name] = {"value": name}

    prepared = Barrier(2)
    prepare_counts = dict.fromkeys(writers, 0)
    successes: list[str] = []
    failures: dict[str, BaseException] = {}

    for name, ledger in writers.items():
        original = ledger.prepare_save

        def prepare(*, authority_binding=None, derived_index=None, _name=name, _original=original):
            candidate = _original(
                authority_binding=authority_binding,
                derived_index=derived_index,
            )
            prepare_counts[_name] += 1
            prepared.wait(5)
            return candidate

        monkeypatch.setattr(ledger, "prepare_save", prepare)

    def save(name: str) -> None:
        try:
            writers[name].save()
            successes.append(name)
        except BaseException as exc:  # asserted below
            failures[name] = exc

    threads = [Thread(target=save, args=(name,)) for name in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert prepare_counts == {"writer_a": 1, "writer_b": 1}
    assert len(successes) == 1
    loser = next(name for name in writers if name not in successes)
    assert isinstance(failures[loser], LedgerSaveConflict)
    assert "no update was written" in str(failures[loser])

    stored = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    winner = successes[0]
    assert stored["seed"] == {"kept": True}
    assert stored[winner] == {"value": winner}
    assert loser not in stored
    assert list(tmp_path.glob(".shot.json.prepared.*")) == []


def test_prepared_ledger_is_opaque_and_requires_its_exact_binding(tmp_path) -> None:
    shot = _shot(tmp_path)
    ledger = Ledger(shot)
    ledger.data["candidate"] = {"accepted": True}
    prepared = ledger.prepare_save(authority_binding="fixture-authority")
    assert prepared.destination == tmp_path / "shot.json"
    assert len(prepared.payload_sha256) == 64
    with pytest.raises(AttributeError):
        _ = prepared.temporary
    with pytest.raises(AttributeError):
        _ = prepared.authority_binding
    try:
        with (
            shot_authority_writer_fence(tmp_path) as capability,
            pytest.raises(LedgerSaveConflict, match="authority binding changed"),
        ):
            ledger.commit_prepared_save(
                prepared,
                authority_binding="different-authority",
                writer_capability=capability,
            )
        assert not (tmp_path / "shot.json").exists()
        with shot_authority_writer_fence(tmp_path) as capability:
            ledger.commit_prepared_save(
                prepared,
                authority_binding="fixture-authority",
                writer_capability=capability,
            )
    finally:
        if not (tmp_path / "shot.json").exists():
            ledger.discard_prepared_save(prepared)

    assert json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))[
        "candidate"
    ] == {
        "accepted": True
    }
    with pytest.raises(LedgerSaveConflict, match="consumed"):
        _ = prepared.destination


def test_replan_does_not_wait_for_attempt_bound_ledger_prepare(
    tmp_path,
    monkeypatch,
) -> None:
    plan_hash = "d" * 64
    unit, units, guard = _building_guard(tmp_path, plan_hash=plan_hash)
    ledger = AuthorityBoundLedger(
        _shot(tmp_path),
        guard.selected_authority,
        execution_guard=guard,
    )
    milestone = Milestone("1@hero", 1, "refs/hero.png", "fixture")
    ledger._slot(milestone)["status"] = "in_progress"

    original_prepare = Ledger.prepare_save
    prepare_started = Event()
    release_prepare = Event()
    replan_done = Event()
    captured_bindings: list[str] = []
    failures: list[BaseException] = []

    def blocked_prepare(self, *, authority_binding=None, derived_index=None):
        candidate = original_prepare(
            self,
            authority_binding=authority_binding,
        )
        assert authority_binding is not None
        captured_bindings.append(authority_binding)
        prepare_started.set()
        assert release_prepare.wait(5)
        return candidate

    monkeypatch.setattr(Ledger, "prepare_save", blocked_prepare)

    def save() -> None:
        try:
            ledger.save()
        except BaseException as exc:  # asserted below
            failures.append(exc)

    def replan() -> None:
        try:
            legacy_apply_replan(
                tmp_path,
                "1",
                units,
                units,
                old_plan_hash=plan_hash,
                new_plan_hash=plan_hash,
                owner="fixture",
                trigger="revoke during ledger preparation",
                evidence=["fixture:replan"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    save_thread = Thread(target=save)
    replan_thread = Thread(target=replan)
    save_thread.start()
    assert prepare_started.wait(2)
    replan_thread.start()
    try:
        assert replan_done.wait(2), "replan waited on ledger read/merge/write/fsync"
    finally:
        release_prepare.set()
    save_thread.join(5)
    replan_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], UnitAttemptAuthorityLost)
    binding = json.loads(captured_bindings[0])
    assert binding["attempt"]["claim_id"] == guard.claim.claim_id
    assert binding["selection_token"] == guard.selected_authority.selection_token.to_dict()
    assert not (tmp_path / "shot.json").exists()
    assert list(tmp_path.glob(".shot.json.prepared.*")) == []


def test_unit_attempt_cannot_publish_layer_milestone_status(tmp_path) -> None:
    unit, _units, guard = _building_guard(tmp_path, plan_hash="e" * 64)
    ledger = AuthorityBoundLedger(
        _shot(tmp_path),
        guard.selected_authority,
        execution_guard=guard,
    )

    with pytest.raises(
        ValueError,
        match="cannot mutate another milestone",
    ):
        ledger.mark(
            Milestone("1", 1, "refs/layer.png", "fixture layer"),
            "passed",
        )

    assert guard.ledger_publication_scope.milestone_id == f"1@{unit.id}"
    assert not (tmp_path / "shot.json").exists()
