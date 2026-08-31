"""Per-layer work-unit state mutations are serialized read/replace transactions."""

from __future__ import annotations

from threading import Event, Thread, current_thread

from tests.architecture.test_staged_architecture import _unit
from vfx_harness.orchestration import unit_state

_WHOLE_FILE_MUTATIONS = (
    unit_state.initialize,
    unit_state.supersede_layer_units,
    unit_state.transition,
    unit_state.freeze_checkpoint,
    unit_state.block_dependents,
    unit_state.invalidate_checkpoint,
    unit_state.record_hypothesis_falsification,
    unit_state.apply_replan,
)


def _run_thread(errors: list[BaseException], operation) -> None:
    try:
        operation()
    except BaseException as exc:  # pragma: no cover - surfaced by the parent assertion
        errors.append(exc)


def test_every_whole_file_state_mutation_uses_the_serialized_boundary() -> None:
    assert all(hasattr(mutation, "__wrapped__") for mutation in _WHOLE_FILE_MUTATIONS)


def test_concurrent_transitions_preserve_both_unit_histories(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("form"), _unit("camera"))
    unit_state.initialize(tmp_path, "1", units, plan_hash="plan")
    for unit in units:
        unit_state.transition(tmp_path, "1", unit.id, "planning", reason="ready")

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
                "building",
                reason="first",
            ),
        ),
    )

    def second_operation() -> None:
        try:
            unit_state.transition(
                tmp_path,
                "1",
                "camera",
                "building",
                reason="second",
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
    assert state["units"]["form"]["status"] == "building"
    assert state["units"]["camera"]["status"] == "building"


def test_invalidation_and_transition_cannot_lose_each_others_state(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("form"), _unit("camera"))
    unit_state.initialize(tmp_path, "1", units, plan_hash="plan")
    unit_state.transition(tmp_path, "1", "form", "planning", reason="ready")
    unit_state.transition(tmp_path, "1", "form", "building", reason="started")
    unit_state.freeze_checkpoint(
        tmp_path,
        "1",
        units[0],
        active_contract_ids=(),
        candidate_hash="candidate",
        settings_hash="settings",
        script_hash="script",
        input_hash="input",
    )
    unit_state.transition(tmp_path, "1", "form", "evaluating", reason="judge")
    unit_state.transition(tmp_path, "1", "form", "passed", reason="accepted")
    unit_state.transition(tmp_path, "1", "camera", "planning", reason="ready")

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
                "building",
                reason="started",
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
    assert state["units"]["camera"]["status"] == "building"


def test_invalidation_cannot_be_overwritten_by_a_stale_checkpoint_freeze(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("form"),)
    unit_state.initialize(tmp_path, "1", units, plan_hash="plan")
    unit_state.transition(tmp_path, "1", "form", "planning", reason="ready")
    unit_state.transition(tmp_path, "1", "form", "building", reason="started")

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
    assert "cannot freeze form from state retryable" in str(freeze_errors[0])
    state = unit_state.load(tmp_path, "1")
    assert state["units"]["form"]["status"] == "retryable"
    assert "checkpoint" not in state["units"]["form"]
    assert state["invalidations"][-1]["evidence"] == ["run:failure"]


def test_replan_and_transition_preserve_both_transactions(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("form"),)
    unit_state.initialize(tmp_path, "1", units, plan_hash="plan-a")
    unit_state.transition(tmp_path, "1", "form", "planning", reason="ready")

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
            lambda: unit_state.apply_replan(
                tmp_path,
                "1",
                units,
                units,
                old_plan_hash="plan-a",
                new_plan_hash="plan-b",
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
                "building",
                reason="started",
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

    assert errors == []
    state = unit_state.load(tmp_path, "1")
    assert state["plan_hash"] == "plan-b"
    assert state["replans"][-1]["new_plan_hash"] == "plan-b"
    assert state["units"]["form"]["status"] == "building"
