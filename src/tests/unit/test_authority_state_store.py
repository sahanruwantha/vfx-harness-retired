from __future__ import annotations

import hashlib

import pytest

from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
)
from vfx_harness.orchestration.authority_state_store import (
    AUTHORITY_STATE_CURRENT,
    AUTHORITY_STATE_PENDING,
    AuthorityStateStoreError,
    decode_pointer_bytes,
    install_authority_state_record,
    read_authority_state_record,
    read_current_bytes,
    read_pending_bytes,
    remove_pending,
    replace_current_bytes,
    replace_pending_bytes,
    require_no_pending_authority_state,
)


def test_authority_state_objects_are_content_addressed_and_idempotent(tmp_path) -> None:
    record = {"schema": "fixture/v1", "value": ["a", "b"]}
    first = install_authority_state_record(tmp_path, record)
    second = install_authority_state_record(tmp_path, record)

    expected = canonical_json_bytes(record)
    assert first == second
    assert first.payload == expected
    assert first.sha256 == hashlib.sha256(expected).hexdigest()
    assert first.locator == (
        f"state/authority-state/objects/{first.sha256}/record.json"
    )

    parsed, stored = read_authority_state_record(
        tmp_path,
        locator=first.locator,
        sha256=first.sha256,
    )
    assert parsed == record
    assert stored == first


def test_authority_state_object_rejects_content_address_path_substitution(
    tmp_path,
) -> None:
    stored = install_authority_state_record(
        tmp_path,
        {"schema": "fixture/v1", "value": 1},
    )
    object_root = tmp_path / stored.locator
    object_root = object_root.parent
    (object_root / "unexpected.json").write_text("{}", encoding="utf-8")

    with pytest.raises(
        AuthorityStateStoreError,
        match=r"exactly record\.json",
    ):
        install_authority_state_record(
            tmp_path,
            {"schema": "fixture/v1", "value": 1},
        )


def test_pending_pointer_is_a_fail_closed_visibility_barrier(tmp_path) -> None:
    pending = canonical_json_bytes(
        {"schema": "fixture-pending/v1", "intent": "intent-1"}
    )
    current = canonical_json_bytes(
        {"schema": "fixture-head/v1", "revision": 1}
    )

    assert read_pending_bytes(tmp_path) is None
    replace_pending_bytes(tmp_path, pending)
    assert read_pending_bytes(tmp_path) == pending
    assert decode_pointer_bytes(pending, "fixture pending")["intent"] == "intent-1"
    with pytest.raises(AuthorityStateStoreError, match="transition is pending"):
        require_no_pending_authority_state(tmp_path)
    with pytest.raises(AuthoritySelectionConflict, match="transition is pending"):
        read_authority_selection_heads(tmp_path)
    assert read_authority_selection_heads(
        tmp_path,
        allow_pending_authority_state=True,
    ).token.plan_revision == 0

    replace_current_bytes(tmp_path, current)
    assert read_current_bytes(tmp_path) == current
    remove_pending(tmp_path)
    require_no_pending_authority_state(tmp_path)
    assert read_pending_bytes(tmp_path) is None
    assert (tmp_path / AUTHORITY_STATE_CURRENT).read_bytes() == current
    assert not (tmp_path / AUTHORITY_STATE_PENDING).exists()


def test_authority_state_pointer_and_object_paths_reject_symlinks(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (state / "authority-state").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AuthorityStateStoreError):
        replace_pending_bytes(
            tmp_path,
            canonical_json_bytes({"schema": "fixture/v1"}),
        )
    with pytest.raises(AuthorityStateStoreError):
        install_authority_state_record(
            tmp_path,
            {"schema": "fixture-object/v1"},
        )
