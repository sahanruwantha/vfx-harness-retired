"""Typed shot-authority capture capability and inner-lock ordering (HIR-0172)."""

from __future__ import annotations

import fcntl
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
        with (
            pytest.raises(AuthoritySelectionConflict, match="same canonical shot"),
            shot_authority_writer_fence(other),
        ):
            pass


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
