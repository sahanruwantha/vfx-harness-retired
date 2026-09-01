"""Selection-first locking for every ordinary authority-bound state write."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

import pytest

from vfx_harness.orchestration import authority_selection_transaction
from vfx_harness.orchestration import unit_state_lock as state_lock
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    authority_selection_lock,
    durable_replace_file_bytes,
)


def test_serialized_state_mutation_enters_selection_before_state(monkeypatch) -> None:
    events: list[str] = []

    @contextmanager
    def selection_guard(folder, *, exclusive):
        assert folder == Path("/shot")
        assert exclusive is False
        events.append("selection-enter")
        try:
            yield
        finally:
            events.append("selection-exit")

    @contextmanager
    def state_guard(path, *, exclusive):
        assert path == Path("/shot/state/work-units/layer_camera.json")
        assert exclusive is True
        events.append("state-enter")
        try:
            yield
        finally:
            events.append("state-exit")

    monkeypatch.setattr(state_lock, "authority_selection_lock", selection_guard)
    monkeypatch.setattr(state_lock, "_locked_state_path", state_guard)

    @state_lock.serialized_state_mutation(state_lock.unit_state_path)
    def mutate(folder, layer_id):
        events.append("mutation")
        return f"{folder}:{layer_id}"

    assert mutate(Path("/shot"), "camera") == "/shot:camera"
    assert events == [
        "selection-enter",
        "state-enter",
        "mutation",
        "state-exit",
        "selection-exit",
    ]


def test_nested_selection_guards_reuse_one_kernel_lock(tmp_path, monkeypatch) -> None:
    operations: list[int] = []
    monkeypatch.setattr(
        authority_selection_transaction.fcntl,
        "lockf",
        lambda _descriptor, operation: operations.append(operation),
    )

    with authority_selection_lock(tmp_path, exclusive=True), authority_selection_lock(tmp_path, exclusive=False):
        pass

    assert operations == [fcntl.LOCK_EX, fcntl.LOCK_UN]


def test_nested_selection_guard_refuses_shared_to_exclusive_upgrade(
    tmp_path,
    monkeypatch,
) -> None:
    operations: list[int] = []
    monkeypatch.setattr(
        authority_selection_transaction.fcntl,
        "lockf",
        lambda _descriptor, operation: operations.append(operation),
    )

    with (
        authority_selection_lock(tmp_path, exclusive=False),
        pytest.raises(AuthoritySelectionConflict, match="cannot upgrade"),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass

    assert operations == [fcntl.LOCK_EX, fcntl.LOCK_UN]


def test_nested_selection_guard_refuses_same_thread_parent_substitution(tmp_path) -> None:
    parent = tmp_path / authority_selection_transaction.AUTHORITY_SELECTION_LOCK.parent
    retired = parent.with_name("authority-selection-retired")
    conflict: AuthoritySelectionConflict | None = None

    with authority_selection_lock(tmp_path, exclusive=False):
        parent.rename(retired)
        parent.mkdir()
        try:
            with authority_selection_lock(tmp_path, exclusive=False):
                pass
        except AuthoritySelectionConflict as exc:
            conflict = exc
        finally:
            parent.rmdir()
            retired.rename(parent)

    assert conflict is not None
    assert "authority selection lock path changed during nested lock" in str(conflict)


def test_ordinary_state_mutation_blocks_selection_transition(tmp_path) -> None:
    mutation_entered = Event()
    release_mutation = Event()
    transition_attempted = Event()
    transition_acquired = Event()
    failures: list[BaseException] = []

    @state_lock.serialized_state_mutation(state_lock.unit_state_path)
    def mutate(_folder, _layer_id):
        mutation_entered.set()
        if not release_mutation.wait(timeout=5):
            raise AssertionError("test did not release state mutation")

    def run_mutation() -> None:
        try:
            mutate(tmp_path, "camera")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def run_transition() -> None:
        try:
            transition_attempted.set()
            with authority_selection_lock(tmp_path, exclusive=True):
                transition_acquired.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    mutation_thread = Thread(target=run_mutation)
    transition_thread = Thread(target=run_transition)
    mutation_thread.start()
    assert mutation_entered.wait(timeout=5)
    transition_thread.start()
    assert transition_attempted.wait(timeout=5)
    assert not transition_acquired.wait(timeout=0.1)

    release_mutation.set()
    mutation_thread.join(timeout=5)
    transition_thread.join(timeout=5)

    assert not mutation_thread.is_alive()
    assert not transition_thread.is_alive()
    assert transition_acquired.is_set()
    assert failures == []


@pytest.mark.parametrize(
    "path",
    (
        "plans/current.json",
        "state/jit-layers/current.json",
        "state/authority-state/current.json",
        "state/authority-state/pending.json",
    ),
)
def test_ordinary_durable_writer_refuses_selected_authority_heads(
    tmp_path,
    path: str,
) -> None:
    with pytest.raises(
        AuthoritySelectionConflict,
        match="require the authority-pointer transaction",
    ):
        durable_replace_file_bytes(tmp_path, path, b"not-authority")


def test_ordinary_durable_writer_still_creates_and_flushes_normal_parent(tmp_path) -> None:
    target = tmp_path / "state" / "jit-overlays" / "base.json"
    durable_replace_file_bytes(tmp_path, target, b"ordinary-record")
    assert target.read_bytes() == b"ordinary-record"
