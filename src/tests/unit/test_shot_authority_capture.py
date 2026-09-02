"""Typed shot-authority capture capability and inner-lock ordering (HIR-0172)."""

from __future__ import annotations

import fcntl
import os
from copy import copy
from dataclasses import replace
from pathlib import Path
from threading import Thread

import pytest

from vfx_harness.orchestration import authority_selection_transaction
from vfx_harness.orchestration.authority_selection_transaction import (
    AUTHORITY_SELECTION_LOCK,
    AuthoritySelectionConflict,
    authority_selection_lock,
)
from vfx_harness.orchestration.shot_authority_capture import (
    ShotAuthorityCaptureCapability,
    ShotAuthorityWriterCapability,
    current_shot_authority_capture,
    current_shot_authority_writer,
    ordered_authority_inner_lock,
    require_live_shot_authority_writer,
    shot_authority_capture,
    shot_authority_writer_fence,
)


def test_capture_reuses_the_permanent_selection_inode_and_is_reentrant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[int] = []
    monkeypatch.setattr(
        authority_selection_transaction.fcntl,
        "lockf",
        lambda _descriptor, operation: operations.append(operation),
    )

    with shot_authority_capture(tmp_path) as outer:
        assert outer.lock_path == tmp_path / AUTHORITY_SELECTION_LOCK
        assert current_shot_authority_capture(tmp_path) is outer
        with shot_authority_capture(tmp_path) as inner:
            assert inner is outer

    assert operations == [fcntl.LOCK_EX, fcntl.LOCK_UN]
    with pytest.raises(AuthoritySelectionConflict, match="must acquire"):
        current_shot_authority_capture(tmp_path)


def test_capture_refuses_a_shared_to_exclusive_semantic_upgrade(tmp_path: Path) -> None:
    with (
        authority_selection_lock(tmp_path, exclusive=False),
        pytest.raises(AuthoritySelectionConflict, match="cannot upgrade"),
        shot_authority_capture(tmp_path),
    ):
        pass


def test_shared_writer_refuses_capture_upgrade_and_capture_can_nest_writer(
    tmp_path: Path,
) -> None:
    with shot_authority_writer_fence(tmp_path) as writer:
        assert isinstance(writer, ShotAuthorityWriterCapability)
        assert not isinstance(writer, ShotAuthorityCaptureCapability)
        assert current_shot_authority_writer(tmp_path) is writer
        assert require_live_shot_authority_writer(writer, tmp_path) == tmp_path
        with (
            pytest.raises(AuthoritySelectionConflict, match="cannot upgrade"),
            shot_authority_capture(tmp_path),
        ):
            pass

    with (
        shot_authority_capture(tmp_path) as capture,
        shot_authority_writer_fence(tmp_path) as nested,
    ):
        assert nested is capture


def test_path_alias_reuses_one_capability_and_different_shot_nesting_refuses(
    tmp_path: Path,
) -> None:
    shot = tmp_path / "shot"
    other = tmp_path / "other"
    shot.mkdir()
    other.mkdir()
    alias = shot / "not-created" / ".."

    with shot_authority_writer_fence(shot) as outer:
        with shot_authority_writer_fence(alias) as nested:
            assert nested is outer
            assert current_shot_authority_writer(alias) is outer
            assert require_live_shot_authority_writer(outer, alias) == shot
        with (
            pytest.raises(AuthoritySelectionConflict, match="same canonical shot"),
            shot_authority_writer_fence(other),
        ):
            pass
        with pytest.raises(AuthoritySelectionConflict, match="different canonical shot"):
            require_live_shot_authority_writer(outer, other)


def test_capture_capability_cannot_be_constructed_by_a_caller(tmp_path: Path) -> None:
    with pytest.raises(AuthoritySelectionConflict, match="issued only"):
        ShotAuthorityCaptureCapability(object(), tmp_path, tmp_path / "lock")
    with pytest.raises(AuthoritySelectionConflict, match="issued only"):
        ShotAuthorityWriterCapability(object(), tmp_path, tmp_path / "lock")


def test_inner_locks_accept_only_the_closed_forward_order(tmp_path: Path) -> None:
    with shot_authority_capture(tmp_path) as capability:
        with (
            ordered_authority_inner_lock(capability, "unit_state"),
            ordered_authority_inner_lock(capability, "shot_ledger"),
            ordered_authority_inner_lock(capability, "plan_resolution"),
            ordered_authority_inner_lock(capability, "judgment_debt"),
            ordered_authority_inner_lock(
                capability,
                "judgment_payment_attempt",
            ),
        ):
            pass

        with ordered_authority_inner_lock(capability, "judgment_debt"):
            with (
                pytest.raises(AuthoritySelectionConflict, match="order violation"),
                ordered_authority_inner_lock(capability, "plan_resolution"),
            ):
                pass
            with (
                pytest.raises(AuthoritySelectionConflict, match="order violation"),
                ordered_authority_inner_lock(capability, "judgment_debt"),
            ):
                pass

        with (
            pytest.raises(AuthoritySelectionConflict, match="unknown"),
            ordered_authority_inner_lock(capability, "invented"),
        ):
            pass


def test_capture_capability_is_thread_bound(tmp_path: Path) -> None:
    errors: list[BaseException] = []
    with shot_authority_capture(tmp_path) as capability:

        def use_on_another_thread() -> None:
            try:
                with ordered_authority_inner_lock(capability, "plan_resolution"):
                    pass
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = Thread(target=use_on_another_thread)
        thread.start()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], AuthoritySelectionConflict)
    assert "not active on this thread" in str(errors[0])


def test_inner_lock_refuses_expired_capture_capability(tmp_path: Path) -> None:
    with shot_authority_capture(tmp_path) as capability:
        pass

    with (
        pytest.raises(AuthoritySelectionConflict, match="not active"),
        ordered_authority_inner_lock(capability, "plan_resolution"),
    ):
        pass


def test_live_writer_refuses_copied_and_dataclass_replaced_capabilities(
    tmp_path: Path,
) -> None:
    with shot_authority_writer_fence(tmp_path) as capability:
        copied = copy(capability)
        assert copied is not capability
        with pytest.raises(AuthoritySelectionConflict, match="not active"):
            require_live_shot_authority_writer(copied, tmp_path)
        with pytest.raises(AuthoritySelectionConflict, match="issued only"):
            replace(capability)


def test_live_writer_refuses_expired_capability(tmp_path: Path) -> None:
    with shot_authority_writer_fence(tmp_path) as capability:
        assert require_live_shot_authority_writer(capability, tmp_path) == tmp_path

    with pytest.raises(AuthoritySelectionConflict, match="not active"):
        require_live_shot_authority_writer(capability, tmp_path)


def test_live_writer_refuses_foreign_thread(tmp_path: Path) -> None:
    errors: list[BaseException] = []
    with shot_authority_writer_fence(tmp_path) as capability:

        def require_on_another_thread() -> None:
            try:
                require_live_shot_authority_writer(capability, tmp_path)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = Thread(target=require_on_another_thread)
        thread.start()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], AuthoritySelectionConflict)
    assert "not active on this thread or process instance" in str(errors[0])


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_live_writer_refuses_capability_in_fork_child(tmp_path: Path) -> None:
    with shot_authority_writer_fence(tmp_path) as capability:
        read_descriptor, write_descriptor = os.pipe()
        child = os.fork()
        if child == 0:  # pragma: no branch - parent asserts the child result
            os.close(read_descriptor)
            try:
                require_live_shot_authority_writer(capability, tmp_path)
            except AuthoritySelectionConflict:
                result = b"refused"
            else:
                result = b"accepted"
            os.write(write_descriptor, result)
            os.close(write_descriptor)
            os._exit(0)

        os.close(write_descriptor)
        result = os.read(read_descriptor, 32)
        os.close(read_descriptor)
        _pid, status = os.waitpid(child, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert result == b"refused"


def test_live_writer_refuses_replaced_selection_lock_inode(tmp_path: Path) -> None:
    with shot_authority_writer_fence(tmp_path) as capability:
        lock_path = capability.lock_path
        retained = lock_path.with_name("selection.retained")
        lock_path.rename(retained)
        lock_path.write_bytes(b"replacement")
        try:
            with pytest.raises(AuthoritySelectionConflict, match="path changed"):
                require_live_shot_authority_writer(capability, tmp_path)
        finally:
            lock_path.unlink()
            retained.rename(lock_path)


def test_live_writer_refuses_shot_root_transplant(tmp_path: Path) -> None:
    shot = tmp_path / "shot"
    shot.mkdir()
    retired = tmp_path / "retired-shot"

    with shot_authority_writer_fence(shot) as capability:
        shot.rename(retired)
        shot.mkdir()
        try:
            with pytest.raises(AuthoritySelectionConflict, match="path changed"):
                require_live_shot_authority_writer(capability, shot)
        finally:
            shot.rmdir()
            retired.rename(shot)


def test_live_writer_refuses_different_live_selection_generation(
    tmp_path: Path,
) -> None:
    lock_key = str(tmp_path / AUTHORITY_SELECTION_LOCK)
    held = authority_selection_transaction._held_selection_locks()
    with shot_authority_writer_fence(tmp_path) as capability:
        original = held[lock_key]
        held[lock_key] = replace(original)
        try:
            with pytest.raises(AuthoritySelectionConflict, match="lease generation"):
                require_live_shot_authority_writer(capability, tmp_path)
        finally:
            held[lock_key] = original
