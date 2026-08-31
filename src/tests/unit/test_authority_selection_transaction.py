"""Low-level authority-selection token, lock, read, and durable-write contracts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from vfx_harness.orchestration import authority_selection_transaction as selection_tx
from vfx_harness.orchestration.authority_selection_transaction import (
    AUTHORITY_SELECTION_LOCK,
    AuthoritySelectionConflict,
    AuthoritySelectionToken,
    authority_selection_lock,
    authority_selection_token_from_pointer_bytes,
    durable_remove_pointer,
    durable_replace_pointer_bytes,
    durable_replace_pointer_json,
    durably_ensure_real_directory,
    pointer_sha256,
    read_optional_pointer_bytes,
    require_matching_authority_selection_token,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _present_token() -> AuthoritySelectionToken:
    return AuthoritySelectionToken(
        plan_revision=3,
        plan_pointer_sha256=_digest("plan-pointer"),
        jit_revision=8,
        jit_pointer_sha256=_digest("jit-pointer"),
    )


def test_selection_token_round_trips_absent_and_present_heads() -> None:
    absent = AuthoritySelectionToken(0, None, 0, None)
    present = _present_token()

    assert absent.to_dict() == {
        "schema": "vfx-harness.authority-selection-token/v1",
        "plan_revision": 0,
        "plan_pointer_sha256": None,
        "jit_revision": 0,
        "jit_pointer_sha256": None,
    }
    assert AuthoritySelectionToken.from_dict(absent.to_dict()) == absent
    assert AuthoritySelectionToken.from_dict(present.to_dict(), "token") == present
    with pytest.raises(FrozenInstanceError):
        present.plan_revision = 4  # type: ignore[misc]


@pytest.mark.parametrize(
    ("plan_revision", "plan_digest", "jit_revision", "jit_digest"),
    (
        (-1, None, 0, None),
        (True, None, 0, None),
        (0, _digest("unexpected"), 0, None),
        (1, None, 0, None),
        (1, "A" * 64, 0, None),
        (1, "short", 0, None),
        (0, None, False, None),
        (0, None, 0, _digest("unexpected-jit")),
        (0, None, 2, None),
    ),
)
def test_selection_token_rejects_invalid_revision_digest_combinations(
    plan_revision: object,
    plan_digest: object,
    jit_revision: object,
    jit_digest: object,
) -> None:
    with pytest.raises(AuthoritySelectionConflict):
        AuthoritySelectionToken(
            plan_revision=plan_revision,  # type: ignore[arg-type]
            plan_pointer_sha256=plan_digest,  # type: ignore[arg-type]
            jit_revision=jit_revision,  # type: ignore[arg-type]
            jit_pointer_sha256=jit_digest,  # type: ignore[arg-type]
        )


def test_selection_token_parser_rejects_unknown_missing_and_old_schema_fields() -> None:
    row = _present_token().to_dict()
    unknown = {**row, "run_id": "plan-run"}
    with pytest.raises(AuthoritySelectionConflict, match="fields mismatch"):
        AuthoritySelectionToken.from_dict(unknown)

    missing = deepcopy(row)
    missing.pop("jit_revision")
    with pytest.raises(AuthoritySelectionConflict, match="fields mismatch"):
        AuthoritySelectionToken.from_dict(missing)

    stale = {**row, "schema": "vfx-harness.authority-selection-token/v0"}
    with pytest.raises(AuthoritySelectionConflict, match="schema must be"):
        AuthoritySelectionToken.from_dict(stale)

    with pytest.raises(AuthoritySelectionConflict, match="must be an object"):
        AuthoritySelectionToken.from_dict([])


def test_token_derivation_hashes_exact_raw_bytes_and_preserves_absence() -> None:
    plan = b'{"revision":1}\n'
    jit = b'{"revision":2}\n'
    token = authority_selection_token_from_pointer_bytes(
        plan_revision=1,
        plan_pointer_bytes=plan,
        jit_revision=2,
        jit_pointer_bytes=jit,
    )

    assert token.plan_pointer_sha256 == hashlib.sha256(plan).hexdigest()
    assert token.jit_pointer_sha256 == hashlib.sha256(jit).hexdigest()
    assert pointer_sha256(None) is None
    assert pointer_sha256(plan) == token.plan_pointer_sha256
    with pytest.raises(AuthoritySelectionConflict, match="must be bytes"):
        pointer_sha256("not-bytes")  # type: ignore[arg-type]
    with pytest.raises(AuthoritySelectionConflict, match="revision 0"):
        authority_selection_token_from_pointer_bytes(
            plan_revision=0,
            plan_pointer_bytes=plan,
            jit_revision=0,
            jit_pointer_bytes=None,
        )


def test_exact_token_comparison_refuses_any_head_change() -> None:
    expected = _present_token()
    require_matching_authority_selection_token(expected, expected)

    changed = AuthoritySelectionToken(
        expected.plan_revision + 1,
        _digest("changed-plan-pointer"),
        expected.jit_revision,
        expected.jit_pointer_sha256,
    )
    with pytest.raises(AuthoritySelectionConflict, match="authority selection changed"):
        require_matching_authority_selection_token(expected, changed)
    with pytest.raises(AuthoritySelectionConflict, match="two typed tokens"):
        require_matching_authority_selection_token(expected, object())  # type: ignore[arg-type]


def test_optional_pointer_read_is_exact_and_missing_is_none(tmp_path: Path) -> None:
    pointer = tmp_path / "plans" / "current.json"
    assert read_optional_pointer_bytes(tmp_path, "plans/current.json") is None

    pointer.parent.mkdir()
    raw = b'{"revision":1}\n'
    pointer.write_bytes(raw)

    assert read_optional_pointer_bytes(tmp_path, "plans/current.json") == raw
    assert read_optional_pointer_bytes(tmp_path, pointer) == raw


@pytest.mark.parametrize("substitution", ["parent", "pointer", "directory"])
def test_optional_pointer_read_rejects_symlink_and_nonregular_substitution(
    tmp_path: Path,
    substitution: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "current.json").write_bytes(b"outside")
    plans = tmp_path / "plans"
    if substitution == "parent":
        plans.symlink_to(outside, target_is_directory=True)
    else:
        plans.mkdir()
        pointer = plans / "current.json"
        if substitution == "pointer":
            pointer.symlink_to(outside / "current.json")
        else:
            pointer.mkdir()

    with pytest.raises(
        AuthoritySelectionConflict,
        match=r"symlink|real regular file|real directory",
    ):
        read_optional_pointer_bytes(tmp_path, "plans/current.json")


def test_optional_pointer_read_refuses_paths_outside_the_shot(tmp_path: Path) -> None:
    with pytest.raises(AuthoritySelectionConflict, match="normalized"):
        read_optional_pointer_bytes(tmp_path, "../current.json")
    with pytest.raises(AuthoritySelectionConflict, match="escapes"):
        read_optional_pointer_bytes(tmp_path, tmp_path.parent / "current.json")


def test_selection_lock_is_permanent_regular_and_reuses_one_inode(tmp_path: Path) -> None:
    with authority_selection_lock(tmp_path, exclusive=True) as lock_path:
        assert lock_path == tmp_path / AUTHORITY_SELECTION_LOCK
        assert lock_path.is_file()
        assert not lock_path.is_symlink()
        inode = lock_path.stat().st_ino

    with authority_selection_lock(tmp_path, exclusive=False) as lock_path:
        assert lock_path.stat().st_ino == inode

    assert (tmp_path / AUTHORITY_SELECTION_LOCK).stat().st_ino == inode


def test_exclusive_selection_lock_blocks_a_shared_reader(tmp_path: Path) -> None:
    started = threading.Event()
    acquired = threading.Event()

    def take_shared_lock() -> None:
        started.set()
        with authority_selection_lock(tmp_path, exclusive=False):
            acquired.set()

    with authority_selection_lock(tmp_path, exclusive=True):
        contender = threading.Thread(target=take_shared_lock, daemon=True)
        contender.start()
        assert started.wait(timeout=1)
        assert not acquired.wait(timeout=0.05)

    assert acquired.wait(timeout=1)
    contender.join(timeout=1)
    assert not contender.is_alive()


@pytest.mark.parametrize("substitution", ["state", "authority-directory", "lock"])
def test_selection_lock_rejects_symlink_components(
    tmp_path: Path,
    substitution: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / "state"
    authority = state / "authority-selection"
    if substitution == "state":
        state.symlink_to(outside, target_is_directory=True)
    elif substitution == "authority-directory":
        state.mkdir()
        authority.symlink_to(outside, target_is_directory=True)
    else:
        authority.mkdir(parents=True)
        (authority / "selection.lock").symlink_to(outside / "lock")

    with (
        pytest.raises(AuthoritySelectionConflict, match=r"real directory|real regular file"),
        authority_selection_lock(tmp_path, exclusive=True),
    ):
        pass


@pytest.mark.parametrize("substitution", ["state-file", "authority-file", "lock-directory"])
def test_selection_lock_rejects_non_directory_and_nonregular_components(
    tmp_path: Path,
    substitution: str,
) -> None:
    state = tmp_path / "state"
    authority = state / "authority-selection"
    if substitution == "state-file":
        state.write_bytes(b"not a directory")
    elif substitution == "authority-file":
        state.mkdir()
        authority.write_bytes(b"not a directory")
    else:
        (authority / "selection.lock").mkdir(parents=True)

    with (
        pytest.raises(AuthoritySelectionConflict, match=r"real directory|real regular file"),
        authority_selection_lock(tmp_path, exclusive=False),
    ):
        pass


def test_durable_byte_replacement_uses_same_parent_and_leaves_no_temporary(
    tmp_path: Path,
) -> None:
    plans = tmp_path / "plans"
    plans.mkdir()
    pointer = plans / "current.json"
    pointer.write_bytes(b"old")

    durable_replace_pointer_bytes(tmp_path, "plans/current.json", b"new")

    assert pointer.read_bytes() == b"new"
    assert list(plans.glob(".current.json.tmp-*")) == []


def test_durable_json_replacement_is_canonical_and_returns_exact_bytes(
    tmp_path: Path,
) -> None:
    plans = tmp_path / "plans"
    plans.mkdir()
    pointer = plans / "current.json"
    value = {"schema": "pointer/v1", "revision": 1, "value": "selected"}

    raw = durable_replace_pointer_json(tmp_path, pointer, value)

    assert raw == (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    assert pointer.read_bytes() == raw
    with pytest.raises(AuthoritySelectionConflict, match="must be an object"):
        durable_replace_pointer_json(tmp_path, pointer, ["not", "an", "object"])  # type: ignore[arg-type]
    with pytest.raises(AuthoritySelectionConflict, match="finite"):
        durable_replace_pointer_json(tmp_path, pointer, {"revision": float("nan")})


def test_durable_pointer_removal_fsyncs_parent_and_preserves_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = tmp_path / "plans"
    plans.mkdir()
    pointer = plans / "current.json"
    pointer.write_bytes(b"selected")
    parent_identity = plans.stat()
    original_fsync = os.fsync
    fsynced_parent = False

    def recording_fsync(descriptor: int) -> None:
        nonlocal fsynced_parent
        observed = os.fstat(descriptor)
        if (
            observed.st_dev == parent_identity.st_dev
            and observed.st_ino == parent_identity.st_ino
        ):
            fsynced_parent = True
        original_fsync(descriptor)

    monkeypatch.setattr(selection_tx.os, "fsync", recording_fsync)

    durable_remove_pointer(tmp_path, pointer)
    durable_remove_pointer(tmp_path, pointer)

    assert not pointer.exists()
    assert fsynced_parent


@pytest.mark.parametrize("substitution", ["symlink", "directory"])
def test_durable_pointer_removal_rejects_nonregular_targets(
    tmp_path: Path,
    substitution: str,
) -> None:
    plans = tmp_path / "plans"
    plans.mkdir()
    pointer = plans / "current.json"
    if substitution == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_bytes(b"outside")
        pointer.symlink_to(outside)
    else:
        pointer.mkdir()

    with pytest.raises(AuthoritySelectionConflict, match="regular file"):
        durable_remove_pointer(tmp_path, pointer)


def test_durable_replacement_fsyncs_file_then_replaces_then_fsyncs_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = tmp_path / "plans"
    plans.mkdir()
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = os.replace

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        events.append("fsync-parent" if stat.S_ISDIR(mode) else "fsync-file")
        original_fsync(descriptor)

    def recording_replace(
        source: str,
        target: str,
        **kwargs: object,
    ) -> None:
        events.append("replace")
        original_replace(source, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(selection_tx.os, "fsync", recording_fsync)
    monkeypatch.setattr(selection_tx.os, "replace", recording_replace)

    durable_replace_pointer_bytes(tmp_path, "plans/current.json", b"selected")

    assert events == ["fsync-file", "replace", "fsync-parent"]


def test_durable_directory_retry_reflushes_visible_parent_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_fsync = os.fsync
    root_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    root_identity = os.fstat(root_descriptor)
    os.close(root_descriptor)
    crashed = False

    def crash_after_child_flush(descriptor: int) -> None:
        nonlocal crashed
        observed = os.fstat(descriptor)
        if (
            not crashed
            and stat.S_ISDIR(observed.st_mode)
            and observed.st_dev == root_identity.st_dev
            and observed.st_ino == root_identity.st_ino
        ):
            crashed = True
            raise OSError("injected crash before parent entry flush")
        original_fsync(descriptor)

    monkeypatch.setattr(selection_tx.os, "fsync", crash_after_child_flush)

    with pytest.raises(AuthoritySelectionConflict, match="durably create or flush"):
        durably_ensure_real_directory(tmp_path, tmp_path / "plans")

    assert (tmp_path / "plans").is_dir()

    events: list[str] = []

    def recording_fsync(descriptor: int) -> None:
        target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        events.append("." if target == tmp_path else target.relative_to(tmp_path).as_posix())
        original_fsync(descriptor)

    monkeypatch.setattr(selection_tx.os, "fsync", recording_fsync)

    assert durably_ensure_real_directory(tmp_path, tmp_path / "plans") == tmp_path / "plans"
    assert events == ["plans", "."]


def test_failed_replace_preserves_target_and_cleans_unique_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = tmp_path / "plans"
    plans.mkdir()
    pointer = plans / "current.json"
    pointer.write_bytes(b"old")

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(selection_tx.os, "replace", fail_replace)

    with pytest.raises(AuthoritySelectionConflict, match="durably replace"):
        durable_replace_pointer_bytes(tmp_path, pointer, b"new")

    assert pointer.read_bytes() == b"old"
    assert list(plans.glob(".current.json.tmp-*")) == []


@pytest.mark.parametrize("substitution", ["parent-symlink", "target-symlink", "target-directory"])
def test_durable_replacement_rejects_unsafe_parent_and_target(
    tmp_path: Path,
    substitution: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_pointer = outside / "current.json"
    outside_pointer.write_bytes(b"outside")
    plans = tmp_path / "plans"
    if substitution == "parent-symlink":
        plans.symlink_to(outside, target_is_directory=True)
    else:
        plans.mkdir()
        pointer = plans / "current.json"
        if substitution == "target-symlink":
            pointer.symlink_to(outside_pointer)
        else:
            pointer.mkdir()

    with pytest.raises(AuthoritySelectionConflict, match=r"real directory|regular file"):
        durable_replace_pointer_bytes(tmp_path, "plans/current.json", b"new")

    assert outside_pointer.read_bytes() == b"outside"


def test_durable_replacement_rejects_missing_parent_outside_path_and_lock_target(
    tmp_path: Path,
) -> None:
    with pytest.raises(AuthoritySelectionConflict, match="directory is missing"):
        durable_replace_pointer_bytes(tmp_path, "plans/current.json", b"new")
    with pytest.raises(AuthoritySelectionConflict, match="normalized"):
        durable_replace_pointer_bytes(tmp_path, "../current.json", b"new")

    with (
        authority_selection_lock(tmp_path, exclusive=True),
        pytest.raises(AuthoritySelectionConflict, match="not a replaceable pointer"),
    ):
        durable_replace_pointer_bytes(tmp_path, AUTHORITY_SELECTION_LOCK, b"new")


def test_durable_replacement_rejects_nonbyte_payload(tmp_path: Path) -> None:
    (tmp_path / "plans").mkdir()
    with pytest.raises(AuthoritySelectionConflict, match="payload must be bytes"):
        durable_replace_pointer_bytes(
            tmp_path,
            "plans/current.json",
            "not-bytes",  # type: ignore[arg-type]
        )
