"""Per-layer work-unit state mutations are serialized read/replace transactions."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import (
    fixture_completion_authorization,
    fixture_live_completion_authority,
    freeze_unit,
    legacy_apply_replan,
    publish_passed_evaluation,
)
from vfx_harness.orchestration import authority_selection_transaction as selection_tx
from vfx_harness.orchestration import unit_state, unit_state_claims
from vfx_harness.orchestration import unit_state_lock as state_lock_module
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.unit_state_lock import unit_state_lock

_PLAN_A = "a" * 64
_PLAN_B = "b" * 64
_TOKEN = AuthoritySelectionToken(0, None, 0, None)

_WHOLE_FILE_MUTATIONS = (
    unit_state.initialize,
    unit_state.supersede_layer_units,
    unit_state.transition,
    unit_state.freeze_checkpoint,
    unit_state.block_dependents,
    unit_state.invalidate_checkpoint,
    unit_state.record_hypothesis_falsification,
    legacy_apply_replan,
    unit_state_claims.claim_ready_unit_for_planning,
    unit_state_claims.claim_ready_unit_for_build,
    unit_state_claims.complete_unit_attempt,
    unit_state_claims.fail_unit_attempt,
    unit_state_claims.release_unit_attempt,
    unit_state_claims.release_unclaimed_unit_for_retry,
)

_SELECTION_THEN_STATE_MUTATIONS = (
    unit_state.transition,
    unit_state.freeze_checkpoint,
    unit_state.record_hypothesis_falsification,
    unit_state_claims.claim_ready_unit_for_planning,
    unit_state_claims.claim_ready_unit_for_build,
    unit_state_claims.complete_unit_attempt,
    unit_state_claims.fail_unit_attempt,
    unit_state_claims.release_unit_attempt,
    unit_state_claims.release_unclaimed_unit_for_retry,
)


def _claim_for_build(folder, units, unit_id: str, *, plan_hash: str = _PLAN_A):
    authorization = fixture_completion_authorization(folder, "1")
    with fixture_live_completion_authority(authorization):
        planning = unit_state_claims.claim_ready_unit_for_planning(
            folder,
            "1",
            unit_id,
            units,
            expected_plan_hash=plan_hash,
            eligible_passed=None,
            completion_authorization=authorization,
            run_id=f"run-{unit_id}",
            selection_token=_TOKEN,
            reason="fixture readiness",
        )
        return unit_state_claims.claim_ready_unit_for_build(
            folder,
            "1",
            unit_id,
            units,
            planning,
            expected_plan_hash=plan_hash,
            eligible_passed=None,
            completion_authorization=authorization,
            run_id=planning.run_id,
            selection_token=_TOKEN,
            reason="fixture gated plan",
        )


def _freeze(folder, unit, claim) -> None:
    freeze_unit(folder, "1", unit, claim, selection_token=_TOKEN)


def _run_thread(errors: list[BaseException], operation) -> None:
    try:
        operation()
    except BaseException as exc:  # pragma: no cover - surfaced by the parent assertion
        errors.append(exc)


def test_every_whole_file_state_mutation_uses_the_serialized_boundary() -> None:
    assert all(hasattr(mutation, "__wrapped__") for mutation in _WHOLE_FILE_MUTATIONS)
    assert all(
        hasattr(mutation.__wrapped__, "__wrapped__")
        for mutation in _SELECTION_THEN_STATE_MUTATIONS
    )


def test_concurrent_transitions_preserve_both_unit_histories(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("form"), _unit("camera"))
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    attempts = {unit.id: _claim_for_build(tmp_path, units, unit.id) for unit in units}
    for unit in units:
        _freeze(tmp_path, unit, attempts[unit.id])

    original_write = unit_state._write
    first_at_write = Event()
    release_first = Event()
    second_finished = Event()
    errors: list[BaseException] = []

    def blocked_first_write(path, value) -> None:
        if current_thread().name == "state-first":
            first_at_write.set()
            assert release_first.wait(2)
        original_write(path, value)

    monkeypatch.setattr(unit_state, "_write", blocked_first_write)
    first = Thread(
        name="state-first",
        target=_run_thread,
        args=(
            errors,
            lambda: unit_state.transition(
                tmp_path,
                "1",
                "form",
                "evaluating",
                reason="first",
                attempt=attempts["form"],
                selection_token=_TOKEN,
            ),
        ),
    )

    def second_operation() -> None:
        try:
            unit_state.transition(
                tmp_path,
                "1",
                "camera",
                "evaluating",
                reason="second",
                attempt=attempts["camera"],
                selection_token=_TOKEN,
            )
        finally:
            second_finished.set()

    second = Thread(
        name="state-second",
        target=_run_thread,
        args=(errors, second_operation),
    )
    first.start()
    assert first_at_write.wait(2)
    second.start()
    assert not second_finished.wait(0.1)
    release_first.set()
    first.join(2)
    second.join(2)

    assert errors == []
    state = unit_state.load(tmp_path, "1")
    assert state["units"]["form"]["status"] == "evaluating"
    assert state["units"]["camera"]["status"] == "evaluating"


def test_unit_state_lock_refuses_parent_rename_recreate_split(tmp_path: Path) -> None:
    state_parent = tmp_path / "state" / "work-units"
    state_parent.mkdir(parents=True)
    retired = state_parent.with_name("work-units-retired")
    entered = Event()
    errors: list[BaseException] = []

    with (
        pytest.raises(ValueError, match="parent lineage changed during lock exit"),
        unit_state_lock(tmp_path, "1", exclusive=True),
    ):
        state_parent.rename(retired)
        state_parent.mkdir()

        def contend() -> None:
            try:
                with unit_state_lock(tmp_path, "1", exclusive=True):
                    entered.set()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        contender = Thread(target=contend)
        contender.start()
        contender.join(2)
        assert not contender.is_alive()

    assert entered.is_set() is False
    assert len(errors) == 1
    assert "another filesystem inode" in str(errors[0])


def test_unit_state_load_refuses_mid_read_parent_rebind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    unit_state.initialize(tmp_path, "1", (_unit("form"),), plan_hash=_PLAN_A)
    state_parent = tmp_path / "state" / "work-units"
    retired = state_parent.with_name("work-units-retired")
    replacement = b'{"schema":1,"units":{},"marker":"replacement"}\n'
    original_read = state_lock_module.os.read
    rebound = False

    def read_after_rebind(descriptor: int, size: int) -> bytes:
        nonlocal rebound
        if not rebound:
            rebound = True
            state_parent.rename(retired)
            state_parent.mkdir()
            (state_parent / "layer_1.json").write_bytes(replacement)
        return original_read(descriptor, size)

    monkeypatch.setattr(state_lock_module.os, "read", read_after_rebind)

    with pytest.raises(ValueError, match="state parent lineage changed"):
        unit_state.load(tmp_path, "1")

    assert rebound is True
    assert (state_parent / "layer_1.json").read_bytes() == replacement
    assert (retired / "layer_1.json").read_bytes() != replacement


def test_unit_state_write_never_publishes_into_recreated_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    unit_state.initialize(tmp_path, "1", (_unit("form"),), plan_hash=_PLAN_A)
    state_parent = tmp_path / "state" / "work-units"
    retired = state_parent.with_name("work-units-retired")
    replacement = b'{"schema":1,"units":{},"marker":"replacement"}\n'
    original_replace = state_lock_module.os.replace
    rebound = False

    def replace_after_rebind(source, destination, *args, **kwargs) -> None:
        nonlocal rebound
        if not rebound:
            rebound = True
            state_parent.rename(retired)
            state_parent.mkdir()
            (state_parent / "layer_1.json").write_bytes(replacement)
        original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(state_lock_module.os, "replace", replace_after_rebind)

    with pytest.raises(ValueError, match="state parent lineage changed"):
        unit_state.supersede_layer_units(
            tmp_path,
            "1",
            owner="fixture",
            trigger="injected parent rebind",
            evidence=["fixture:rebind"],
            plan_hash=_PLAN_B,
        )

    assert rebound is True
    assert (state_parent / "layer_1.json").read_bytes() == replacement
    assert (retired / "layer_1.json").read_bytes() != replacement


def test_invalidation_and_transition_cannot_lose_each_others_state(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("form"), _unit("camera"))
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    form_attempt = _claim_for_build(tmp_path, units, "form")
    _freeze(tmp_path, units[0], form_attempt)
    unit_state.transition(
        tmp_path,
        "1",
        "form",
        "evaluating",
        reason="judge",
        attempt=form_attempt,
        selection_token=_TOKEN,
    )
    publish_passed_evaluation(tmp_path, "1", units[0], form_attempt)
    unit_state_claims.complete_unit_attempt(
        tmp_path,
        "1",
        "form",
        units,
        form_attempt,
        expected_plan_hash=_PLAN_A,
        selection_token=_TOKEN,
        reason="accepted",
        evidence=["fixture:pass"],
    )
    camera_attempt = _claim_for_build(tmp_path, units, "camera")
    _freeze(tmp_path, units[1], camera_attempt)

    original_write = unit_state._write
    invalidation_at_write = Event()
    release_invalidation = Event()
    transition_finished = Event()
    errors: list[BaseException] = []

    def blocked_invalidation_write(path, value) -> None:
        if current_thread().name == "state-invalidation":
            invalidation_at_write.set()
            assert release_invalidation.wait(2)
        original_write(path, value)

    monkeypatch.setattr(unit_state, "_write", blocked_invalidation_write)
    invalidation = Thread(
        name="state-invalidation",
        target=_run_thread,
        args=(
            errors,
            lambda: unit_state.invalidate_checkpoint(
                tmp_path,
                "1",
                "form",
                units,
                reason="checkpoint contract failed",
                evidence=["run:failure"],
            ),
        ),
    )

    def transition_operation() -> None:
        try:
            unit_state.transition(
                tmp_path,
                "1",
                "camera",
                "evaluating",
                reason="judge",
                attempt=camera_attempt,
                selection_token=_TOKEN,
            )
        finally:
            transition_finished.set()

    transition = Thread(
        name="state-transition",
        target=_run_thread,
        args=(errors, transition_operation),
    )
    invalidation.start()
    assert invalidation_at_write.wait(2)
    transition.start()
    assert not transition_finished.wait(0.1)
    release_invalidation.set()
    invalidation.join(2)
    transition.join(2)

    assert errors == []
    state = unit_state.load(tmp_path, "1")
    form = state["units"]["form"]
    assert form["status"] == "retryable"
    assert form["invalidated_checkpoints"][-1]["evidence"] == ["run:failure"]
    assert state["units"]["camera"]["status"] == "evaluating"


def test_invalidation_cannot_be_overwritten_by_a_stale_checkpoint_freeze(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("form"),)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    attempt = _claim_for_build(tmp_path, units, "form")

    original_write = unit_state._write
    invalidation_at_write = Event()
    release_invalidation = Event()
    freeze_finished = Event()
    invalidation_errors: list[BaseException] = []
    freeze_errors: list[BaseException] = []

    def blocked_invalidation_write(path, value) -> None:
        if current_thread().name == "state-invalidation":
            invalidation_at_write.set()
            assert release_invalidation.wait(2)
        original_write(path, value)

    monkeypatch.setattr(unit_state, "_write", blocked_invalidation_write)
    invalidation = Thread(
        name="state-invalidation",
        target=_run_thread,
        args=(
            invalidation_errors,
            lambda: unit_state.invalidate_checkpoint(
                tmp_path,
                "1",
                "form",
                units,
                reason="candidate provenance failed",
                evidence=["run:failure"],
            ),
        ),
    )

    def freeze_operation() -> None:
        try:
            unit_state.freeze_checkpoint(
                tmp_path,
                "1",
                units[0],
                active_contract_ids=(),
                candidate_hash="stale-candidate",
                settings_hash="settings",
                script_hash="script",
                input_hash="input",
                attempt=attempt,
                selection_token=_TOKEN,
            )
        finally:
            freeze_finished.set()

    freeze = Thread(
        name="state-freeze",
        target=_run_thread,
        args=(freeze_errors, freeze_operation),
    )
    invalidation.start()
    assert invalidation_at_write.wait(2)
    freeze.start()
    assert not freeze_finished.wait(0.1)
    release_invalidation.set()
    invalidation.join(2)
    freeze.join(2)

    assert invalidation_errors == []
    assert len(freeze_errors) == 1
    assert "stale" in str(freeze_errors[0])
    state = unit_state.load(tmp_path, "1")
    assert state["units"]["form"]["status"] == "retryable"
    assert "checkpoint" not in state["units"]["form"]
    assert state["invalidations"][-1]["evidence"] == ["run:failure"]


def test_replan_and_transition_preserve_both_transactions(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("form"),)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    attempt = _claim_for_build(tmp_path, units, "form")
    _freeze(tmp_path, units[0], attempt)

    original_write = unit_state._write
    replan_at_write = Event()
    release_replan = Event()
    transition_finished = Event()
    errors: list[BaseException] = []

    def blocked_replan_write(path, value) -> None:
        if current_thread().name == "state-replan":
            replan_at_write.set()
            assert release_replan.wait(2)
        original_write(path, value)

    monkeypatch.setattr(unit_state, "_write", blocked_replan_write)
    replan = Thread(
        name="state-replan",
        target=_run_thread,
        args=(
            errors,
            lambda: legacy_apply_replan(
                tmp_path,
                "1",
                units,
                units,
                old_plan_hash=_PLAN_A,
                new_plan_hash=_PLAN_B,
                owner="test",
                trigger="sibling authority changed",
                evidence=["layers.json sha256 plan-b"],
            ),
        ),
    )

    def transition_operation() -> None:
        try:
            unit_state.transition(
                tmp_path,
                "1",
                "form",
                "evaluating",
                reason="judge",
                attempt=attempt,
                selection_token=_TOKEN,
            )
        finally:
            transition_finished.set()

    transition = Thread(
        name="state-transition",
        target=_run_thread,
        args=(errors, transition_operation),
    )
    replan.start()
    assert replan_at_write.wait(2)
    transition.start()
    assert not transition_finished.wait(0.1)
    release_replan.set()
    replan.join(2)
    transition.join(2)

    assert len(errors) == 1
    assert "active attempt" in str(errors[0]) or "stale" in str(errors[0])
    state = unit_state.load(tmp_path, "1")
    assert state["plan_hash"] == _PLAN_B
    assert state["replans"][-1]["new_plan_hash"] == _PLAN_B
    assert state["units"]["form"]["status"] == "retryable"


def test_relative_shot_root_writes_exact_state_path(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    shot = Path("relative-shot")
    shot.mkdir()
    unit = _unit("form")

    unit_state.initialize(shot, "1", (unit,), plan_hash=_PLAN_A)

    assert Path("relative-shot/state/work-units/layer_1.json").is_file()
    assert not Path("relative-shot/relative-shot/state/work-units/layer_1.json").exists()


def test_state_write_flushes_staging_and_destination_around_rename(
    tmp_path,
    monkeypatch,
) -> None:
    unit = _unit("form")
    unit_state.initialize(tmp_path, "1", (unit,), plan_hash=_PLAN_A)
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def record_fsync(descriptor: int) -> None:
        events.append("fsync-parent" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "fsync-file")
        real_fsync(descriptor)

    def record_replace(*args, **kwargs) -> None:
        events.append("replace")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(selection_tx.os, "fsync", record_fsync)
    monkeypatch.setattr(selection_tx.os, "replace", record_replace)

    unit_state.transition(tmp_path, "1", unit.id, "blocked", reason="fixture")

    assert events[-5:] == [
        "fsync-file",
        "fsync-parent",
        "replace",
        "fsync-parent",
        "fsync-parent",
    ]


def test_replace_failure_preserves_prior_state_and_cleans_temporary(
    tmp_path,
    monkeypatch,
) -> None:
    unit = _unit("form")
    unit_state.initialize(tmp_path, "1", (unit,), plan_hash=_PLAN_A)
    path = unit_state._path(tmp_path, "1")
    before = path.read_bytes()

    def fail_replace(*_args, **_kwargs) -> None:
        raise OSError("injected rename failure")

    monkeypatch.setattr(selection_tx.os, "replace", fail_replace)
    with pytest.raises(AuthoritySelectionConflict, match="durably replace"):
        unit_state.transition(tmp_path, "1", unit.id, "blocked", reason="fixture")

    assert path.read_bytes() == before
    assert list(path.parent.glob(f".{path.name}.prepared.*")) == []
    staging = tmp_path / state_lock_module.STATE_STAGING_DIR
    assert list(staging.glob(f".{path.name}.prepared.*")) == []


def test_process_death_before_state_replace_leaves_only_inert_staging_orphan(
    tmp_path: Path,
) -> None:
    unit = _unit("form")
    unit_state.initialize(tmp_path, "1", (unit,), plan_hash=_PLAN_A)
    path = unit_state._path(tmp_path, "1")
    before = path.read_bytes()
    script = """
import os
import sys

from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration import unit_state_lock as state_lock

real_replace = state_lock.os.replace

def die_before_state_replace(source, target, *args, **kwargs):
    if target == "layer_1.json" and kwargs.get("src_dir_fd") != kwargs.get("dst_dir_fd"):
        os._exit(73)
    return real_replace(source, target, *args, **kwargs)

state_lock.os.replace = die_before_state_replace
unit_state.transition(sys.argv[1], "1", "form", "blocked", reason="crash fixture")
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        cwd=Path(__file__).parents[3],
    )

    assert crashed.returncode == 73
    assert path.read_bytes() == before
    assert sorted(child.name for child in path.parent.iterdir()) == [
        "layer_1.json",
        "layer_1.json.lock",
    ]
    staging = tmp_path / state_lock_module.STATE_STAGING_DIR
    assert len(list(staging.glob(f".{path.name}.prepared.*"))) == 1

    unit_state.transition(tmp_path, "1", unit.id, "blocked", reason="next write")

    assert unit_state.load(tmp_path, "1")["units"][unit.id]["status"] == "blocked"


def test_state_write_refuses_prepared_name_substitution_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = _unit("form")
    unit_state.initialize(tmp_path, "1", (unit,), plan_hash=_PLAN_A)
    path = unit_state._path(tmp_path, "1")
    staging = tmp_path / state_lock_module.STATE_STAGING_DIR
    before = path.read_bytes()
    real_fsync = state_lock_module.os.fsync
    injected = False

    def substitute_after_file_flush(descriptor: int) -> None:
        nonlocal injected
        real_fsync(descriptor)
        if injected or not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return
        prepared = list(staging.glob(f".{path.name}.prepared.*"))
        if not prepared:
            # Selection-first mutations durably flush the permanent selection lock
            # before the state writer allocates its prepared member.
            return
        temporary = prepared[0]
        substitute = staging / "substitute-state"
        substitute.write_bytes(b'{"attacker":true}\n')
        os.replace(substitute, temporary)
        injected = True

    monkeypatch.setattr(state_lock_module.os, "fsync", substitute_after_file_flush)

    with pytest.raises(
        AuthoritySelectionConflict,
        match="changed before publication",
    ):
        unit_state.transition(tmp_path, "1", unit.id, "blocked", reason="fixture")

    assert path.read_bytes() == before
    leftovers = list(staging.glob(f".{path.name}.prepared.*"))
    assert len(leftovers) == 1
    assert leftovers[0].read_bytes() == b'{"attacker":true}\n'


def test_state_write_refuses_post_rename_substitution_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = _unit("form")
    unit_state.initialize(tmp_path, "1", (unit,), plan_hash=_PLAN_A)
    path = unit_state._path(tmp_path, "1")
    attacker = path.parent / "attacker-state"
    attacker.write_bytes(b'{"attacker":true}\n')
    real_replace = state_lock_module.os.replace
    injected = False

    def substitute_after_replace(source, target, *args, **kwargs) -> None:
        nonlocal injected
        real_replace(source, target, *args, **kwargs)
        if not injected and target == path.name:
            injected = True
            real_replace(
                attacker.name,
                target,
                src_dir_fd=kwargs["dst_dir_fd"],
                dst_dir_fd=kwargs["dst_dir_fd"],
            )

    monkeypatch.setattr(state_lock_module.os, "replace", substitute_after_replace)

    with pytest.raises(
        AuthoritySelectionConflict,
        match="changed during publication",
    ):
        unit_state.transition(tmp_path, "1", unit.id, "blocked", reason="fixture")

    assert path.read_bytes() == b'{"attacker":true}\n'
    assert list(path.parent.glob(f".{path.name}.prepared.*")) == []
    staging = tmp_path / state_lock_module.STATE_STAGING_DIR
    assert list(staging.glob(f".{path.name}.prepared.*")) == []


def test_parent_flush_failure_never_reports_state_publication_success(
    tmp_path,
    monkeypatch,
) -> None:
    unit = _unit("form")
    unit_state.initialize(tmp_path, "1", (unit,), plan_hash=_PLAN_A)
    replaced = False
    real_fsync = os.fsync
    real_replace = os.replace

    def tracked_replace(*args, **kwargs) -> None:
        nonlocal replaced
        real_replace(*args, **kwargs)
        replaced = True

    def fail_parent_after_replace(descriptor: int) -> None:
        if replaced and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected parent fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(selection_tx.os, "replace", tracked_replace)
    monkeypatch.setattr(selection_tx.os, "fsync", fail_parent_after_replace)

    with pytest.raises(AuthoritySelectionConflict, match="durably replace"):
        unit_state.transition(tmp_path, "1", unit.id, "blocked", reason="fixture")
