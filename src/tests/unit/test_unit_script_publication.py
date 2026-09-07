from __future__ import annotations

import hashlib
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import claim_for_build, legacy_apply_replan
from vfx_harness.agents.builder.attempt_guard import (
    UnitAttemptAuthorityLost,
    UnitAttemptGuard,
)
from vfx_harness.agents.builder.unit_runtime import publish_candidate_script
from vfx_harness.blender import session as blender_session
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)


def _building_guard(tmp_path, *, plan_hash: str):
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _unit("hero")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    attempt = claim_for_build(
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
        attempt,
        expected_plan_hash=plan_hash,
        selected_authority=SimpleNamespace(selection_token=token),
    )
    return unit, units, guard


def test_candidate_script_publication_uses_prepared_parent_bytes(tmp_path) -> None:
    _unit_value, _units, guard = _building_guard(
        tmp_path,
        plan_hash="a" * 64,
    )
    candidate = (
        tmp_path
        / "runs"
        / guard.claim.run_id
        / "scratch"
        / "unit-candidates"
        / f"{guard.claim.claim_id}.py"
    )
    candidate.parent.mkdir(parents=True)
    payload = b"# exact evaluated candidate\npass\n"
    candidate.write_bytes(payload)

    observed = publish_candidate_script(
        tmp_path,
        candidate,
        "build/units/01/hero.py",
        guard,
    )

    destination = tmp_path / "build" / "units" / "01" / "hero.py"
    assert observed == hashlib.sha256(payload).hexdigest()
    assert destination.read_bytes() == payload
    assert list(destination.parent.glob(".hero.py.prepared.*")) == []


def test_publication_refuses_bytes_substituted_after_freeze(tmp_path) -> None:
    _unit_value, _units, guard = _building_guard(tmp_path, plan_hash="a" * 64)
    candidate = (
        tmp_path / "runs" / guard.claim.run_id / "scratch" / "unit-candidates"
        / f"{guard.claim.claim_id}.py"
    )
    candidate.parent.mkdir(parents=True)
    frozen_digest = hashlib.sha256(b"# frozen\npass\n").hexdigest()
    candidate.write_bytes(b"# substituted\npass\n")
    with pytest.raises(ValueError, match="differs from the frozen candidate"):
        publish_candidate_script(
            tmp_path, candidate, "build/units/01/hero.py", guard,
            expected_sha256=frozen_digest,
        )
    destination = tmp_path / "build/units/01/hero.py"
    assert not destination.exists()
    assert list(destination.parent.glob(".hero.py.prepared.*")) == []


def test_replan_does_not_wait_for_candidate_script_staging(
    tmp_path,
    monkeypatch,
) -> None:
    plan_hash = "b" * 64
    unit, units, guard = _building_guard(
        tmp_path,
        plan_hash=plan_hash,
    )
    candidate = (
        tmp_path
        / "runs"
        / guard.claim.run_id
        / "scratch"
        / "unit-candidates"
        / f"{guard.claim.claim_id}.py"
    )
    destination = tmp_path / "build" / "units" / "01" / "hero.py"
    prepare_started = Event()
    release_prepare = Event()
    replan_done = Event()
    discarded = Event()
    commits: list[object] = []
    failures: list[BaseException] = []
    prepared = object()

    def prepare(*_args, **_kwargs):
        prepare_started.set()
        assert release_prepare.wait(5)
        return prepared

    monkeypatch.setattr(
        blender_session,
        "prepare_durable_parent_publish",
        prepare,
    )
    monkeypatch.setattr(
        blender_session,
        "commit_durable_parent_publish",
        lambda value: commits.append(value),
    )
    monkeypatch.setattr(
        blender_session,
        "discard_prepared_parent_publish",
        lambda value: discarded.set() if value is prepared else None,
    )

    def publish() -> None:
        try:
            publish_candidate_script(
                tmp_path,
                candidate,
                "build/units/01/hero.py",
                guard,
            )
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
                trigger="revoke during canonical script staging",
                evidence=["fixture:replan"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    publish_thread = Thread(target=publish)
    replan_thread = Thread(target=replan)
    publish_thread.start()
    assert prepare_started.wait(2)
    replan_thread.start()
    try:
        assert replan_done.wait(2), "replan waited on candidate script read/copy/fsync"
    finally:
        release_prepare.set()
    publish_thread.join(5)
    replan_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], UnitAttemptAuthorityLost)
    assert discarded.is_set()
    assert commits == []
    assert not destination.exists()
