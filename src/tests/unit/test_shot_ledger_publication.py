from __future__ import annotations

import copy
import gc
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path
from threading import Thread

import pytest

from vfx_harness.domain.brief import Shot
from vfx_harness.observability import run_owner_fork_guard, run_owner_fork_registry
from vfx_harness.observability.prepared_publication import (
    FilePublicationConflict,
    prepare_file_update,
    publish_file_update,
)
from vfx_harness.orchestration import shot_ledger_publication
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    durable_remove_pointer,
    durable_replace_file_bytes,
    durable_replace_pointer_bytes,
    durable_replace_pointer_json,
)
from vfx_harness.orchestration.ledger import Ledger, LedgerSaveConflict
from vfx_harness.orchestration.shot_authority_capture import (
    shot_authority_writer_fence,
)
from vfx_harness.orchestration.shot_ledger_lock import (
    ledger_lock,
    shot_ledger_mutation_lock,
)
from vfx_harness.orchestration.shot_ledger_publication import (
    PreparedShotLedgerPublication,
)


def _shot(root: Path) -> Shot:
    return Shot(
        folder=root,
        frontmatter={"id": "shot-ledger-fixture", "frames": 1, "fps": 24},
        body="fixture",
    )


@pytest.mark.parametrize("operation", ["prepare", "publish"])
@pytest.mark.parametrize("basename", ["shot.json", "shot.json.lock"])
def test_generic_prepared_publication_cannot_write_shot_ledger(
    tmp_path: Path,
    operation: str,
    basename: str,
) -> None:
    def update(_current: bytes | None) -> tuple[bytes, None]:
        return b'{}\n', None

    with pytest.raises(
        FilePublicationConflict,
        match="shot-ledger destination requires its exact typed owner authorization",
    ):
        if operation == "prepare":
            prepare_file_update(
                tmp_path,
                tmp_path / basename,
                update,
                authority_binding="raw-shot-ledger",
            )
        else:
            publish_file_update(
                tmp_path,
                tmp_path / basename,
                update,
                authority_binding="raw-shot-ledger",
            )


@pytest.mark.parametrize("basename", ["shot.json", "shot.json.lock"])
def test_rerooted_generic_publication_cannot_hide_shot_ledger(
    tmp_path: Path,
    basename: str,
) -> None:
    shot = tmp_path / "shot"
    shot.mkdir()

    with pytest.raises(FilePublicationConflict, match="shot-ledger"):
        prepare_file_update(
            tmp_path,
            shot / basename,
            lambda _current: (b'{}\n', None),
            authority_binding="rerooted-shot-ledger",
        )


@pytest.mark.parametrize(
    ("writer", "payload"),
    [
        (durable_replace_pointer_bytes, b'{}\n'),
        (durable_replace_file_bytes, b'{}\n'),
        (durable_replace_pointer_json, {}),
    ],
)
@pytest.mark.parametrize("rerooted", [False, True])
@pytest.mark.parametrize("basename", ["shot.json", "shot.json.lock"])
def test_generic_durable_replace_cannot_write_shot_ledger(
    tmp_path: Path,
    writer,
    payload,
    rerooted: bool,
    basename: str,
) -> None:
    target = Path("nested") / basename if rerooted else Path(basename)
    with pytest.raises(
        AuthoritySelectionConflict,
        match="typed shot-ledger publication transaction",
    ):
        writer(tmp_path, target, payload)
    assert not (tmp_path / target).exists()
    if rerooted:
        assert not (tmp_path / "nested").exists()


@pytest.mark.parametrize("rerooted", [False, True])
@pytest.mark.parametrize("basename", ["shot.json", "shot.json.lock"])
def test_generic_durable_remove_cannot_delete_shot_ledger(
    tmp_path: Path,
    rerooted: bool,
    basename: str,
) -> None:
    target = tmp_path / "nested" / basename if rerooted else tmp_path / basename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'{"sentinel":true}\n')

    with pytest.raises(
        AuthoritySelectionConflict,
        match="typed shot-ledger publication transaction",
    ):
        durable_remove_pointer(tmp_path, target)

    assert target.read_bytes() == b'{"sentinel":true}\n'


def test_prepared_shot_ledger_gc_retires_its_staged_inode(tmp_path: Path) -> None:
    ledger = Ledger(_shot(tmp_path))
    ledger.data["candidate"] = {"accepted": True}
    prepared = ledger.prepare_save(authority_binding="gc-fixture")

    staged = list(tmp_path.glob(".shot.json.prepared.*"))
    assert len(staged) == 1
    del prepared
    gc.collect()

    assert list(tmp_path.glob(".shot.json.prepared.*")) == []
    assert not (tmp_path / "shot.json").exists()


def test_noop_save_revalidates_without_replacing_current_ledger(tmp_path: Path) -> None:
    shot = _shot(tmp_path)
    ledger = Ledger(shot)
    ledger.data["candidate"] = {"accepted": True}
    assert ledger.save() is None
    ledger_path = tmp_path / "shot.json"
    before = ledger_path.stat()
    before_bytes = ledger_path.read_bytes()

    Ledger(shot).save()

    after = ledger_path.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert ledger_path.read_bytes() == before_bytes


def test_post_retirement_interrupt_preserves_primary_error_and_committed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = Ledger(_shot(tmp_path))
    ledger.data["candidate"] = {"accepted": True}
    original = shot_ledger_publication._retire_prepared_shot_ledger

    def interrupt_after_retirement(prepared: PreparedShotLedgerPublication):
        original(prepared)
        raise KeyboardInterrupt("injected after typed ledger retirement")

    monkeypatch.setattr(
        shot_ledger_publication,
        "_retire_prepared_shot_ledger",
        interrupt_after_retirement,
    )

    with pytest.raises(KeyboardInterrupt, match="after typed ledger retirement"):
        ledger.save()

    assert json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))[
        "candidate"
    ] == {"accepted": True}
    gc.collect()
    assert list(tmp_path.glob(".shot.json.prepared.*")) == []


def test_prepared_shot_ledger_rejects_construction_copy_and_pickle(
    tmp_path: Path,
) -> None:
    ledger = Ledger(_shot(tmp_path))
    ledger.data["candidate"] = {"accepted": True}
    prepared = ledger.prepare_save(authority_binding="opaque-fixture")
    try:
        with pytest.raises(LedgerSaveConflict, match="minted only"):
            PreparedShotLedgerPublication()
        with pytest.raises(LedgerSaveConflict, match="cannot be copied"):
            copy.copy(prepared)
        with pytest.raises(LedgerSaveConflict, match="cannot be copied"):
            copy.deepcopy(prepared)
        with pytest.raises(LedgerSaveConflict, match="cannot be serialized"):
            pickle.dumps(prepared)
        forged = object.__new__(PreparedShotLedgerPublication)
        with pytest.raises(LedgerSaveConflict, match="unregistered"):
            _ = forged.destination
    finally:
        ledger.discard_prepared_save(prepared)


def test_prepared_shot_ledger_is_exact_thread_bound(tmp_path: Path) -> None:
    ledger = Ledger(_shot(tmp_path))
    ledger.data["candidate"] = {"accepted": True}
    prepared = ledger.prepare_save(authority_binding="thread-fixture")
    failures: list[BaseException] = []

    def inspect() -> None:
        try:
            _ = prepared.destination
        except BaseException as exc:  # asserted below
            failures.append(exc)

    thread = Thread(target=inspect)
    thread.start()
    thread.join(5)
    try:
        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], LedgerSaveConflict)
        assert "another process or thread" in str(failures[0])
    finally:
        ledger.discard_prepared_save(prepared)


def test_ledger_lock_handoff_interruption_retires_exact_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[run_owner_fork_guard.GuardedDescriptor] = []
    original = run_owner_fork_guard.ForkProtectedAcquisition.handoff

    def interrupt_after_handoff(
        acquisition: run_owner_fork_guard.ForkProtectedAcquisition,
        descriptors: tuple[int, ...],
    ) -> object:
        captured.extend(
            run_owner_fork_guard.GuardedDescriptor.capture(descriptor)
            for descriptor in descriptors
        )
        original(acquisition, descriptors)
        raise KeyboardInterrupt("injected after ledger descriptor handoff")

    monkeypatch.setattr(
        run_owner_fork_guard.ForkProtectedAcquisition,
        "handoff",
        interrupt_after_handoff,
    )
    with (
        pytest.raises(KeyboardInterrupt, match="after ledger descriptor handoff"),
        ledger_lock(tmp_path / "shot.json", exclusive=True),
    ):
        pytest.fail("interrupted ledger handoff admitted the lock body")

    assert len(captured) == 2
    assert all(not descriptor.is_current() for descriptor in captured)

    monkeypatch.setattr(
        run_owner_fork_guard.ForkProtectedAcquisition,
        "handoff",
        original,
    )
    with ledger_lock(
        tmp_path / "shot.json",
        exclusive=True,
        blocking=False,
    ):
        pass


def test_ledger_lock_cleanup_never_closes_reused_same_inode_descriptor(
    tmp_path: Path,
) -> None:
    script = """
import json
import os
import sys
from pathlib import Path

from vfx_harness.observability import run_owner_fork_guard, run_owner_fork_registry
from vfx_harness.orchestration import shot_ledger_lock as lock_module
from vfx_harness.orchestration.shot_ledger_lock import ledger_lock

root = Path(sys.argv[1])
ledger_path = root / "shot.json"
lock_path = root / "shot.json.lock"
original_close = os.close
original_validate = lock_module._require_lock_namespace_current
reused = []
interrupted = False

def close_then_reuse_and_interrupt(descriptor):
    global interrupted
    if not interrupted:
        interrupted = True
        original_close(descriptor)
        replacement = os.open(
            lock_path,
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
        )
        assert replacement == descriptor
        reused.append(replacement)
        raise KeyboardInterrupt(
            "injected after ledger descriptor close and same-inode reuse"
        )
    original_close(descriptor)

try:
    try:
        with ledger_lock(ledger_path, exclusive=True):
            lock_module._require_lock_namespace_current = lambda *_args: None
            os.close = close_then_reuse_and_interrupt
    except KeyboardInterrupt as exc:
        assert "after ledger descriptor close and same-inode reuse" in str(exc)
    else:
        raise AssertionError("ambiguous close did not preserve its interruption")
    finally:
        os.close = original_close
        lock_module._require_lock_namespace_current = original_validate

    assert interrupted
    assert len(reused) == 1
    replacement = reused[0]
    assert os.fstat(replacement).st_ino == lock_path.stat().st_ino

    admitted = False
    try:
        with ledger_lock(ledger_path, exclusive=True, blocking=False):
            admitted = True
    except run_owner_fork_guard.RunOwnerForkGuardCleanupError:
        pass
    else:
        raise AssertionError("process poison admitted another ledger lease")
    assert os.fstat(replacement).st_ino == lock_path.stat().st_ino

    drain_refused = False
    try:
        run_owner_fork_guard.drain_inert_descriptors()
    except run_owner_fork_guard.RunOwnerForkGuardCleanupError:
        drain_refused = True
    assert os.fstat(replacement).st_ino == lock_path.stat().st_ino
    print(json.dumps({
        "admitted": admitted,
        "drain_refused": drain_refused,
        "interrupted": interrupted,
        "replacement_preserved": True,
    }, sort_keys=True))
finally:
    os.close = original_close
    lock_module._require_lock_namespace_current = original_validate
    for descriptor in reused:
        original_close(descriptor)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=True,
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert json.loads(completed.stdout) == {
        "admitted": False,
        "drain_refused": True,
        "interrupted": True,
        "replacement_preserved": True,
    }


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_directory_traversal_fork_retires_every_intermediate_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = tmp_path / "deep" / "shot"
    shot.mkdir(parents=True)
    ledger_path = shot / "shot.json"
    result_reader, result_writer = os.pipe()
    forked = False
    child_pids: list[int] = []
    observed_tokens: list[object] = []
    original = run_owner_fork_guard.ForkProtectedAcquisition.retire

    def fork_while_intermediates_are_pending(
        acquisition: run_owner_fork_guard.ForkProtectedAcquisition,
        descriptor: int,
    ) -> None:
        nonlocal forked

        if not forked:
            forked = True
            pending = vars(run_owner_fork_registry)["PENDING_DESCRIPTORS"]
            active = vars(run_owner_fork_registry)["ACTIVE_DESCRIPTORS"]
            rows = tuple(pending[acquisition.token])
            assert len(rows) >= 2
            observed_tokens.append(acquisition.token)
            pid = os.fork()
            if pid == 0:  # pragma: no cover - assertions execute in the parent
                os.close(result_reader)
                child_pending = vars(run_owner_fork_registry)["PENDING_DESCRIPTORS"]
                child_active = vars(run_owner_fork_registry)["ACTIVE_DESCRIPTORS"]
                clean = (
                    acquisition.token not in child_pending
                    and acquisition.token not in child_active
                    and all(not guarded.is_current() for guarded in rows)
                )
                os.write(result_writer, b"clean" if clean else b"leaked")
                os.close(result_writer)
                os._exit(0)
            child_pids.append(pid)
            assert acquisition.token in pending
            assert acquisition.token not in active
        original(acquisition, descriptor)

    monkeypatch.setattr(
        run_owner_fork_guard.ForkProtectedAcquisition,
        "retire",
        fork_while_intermediates_are_pending,
    )
    with ledger_lock(ledger_path, exclusive=True):
        pass

    os.close(result_writer)
    child_result = os.read(result_reader, 16)
    os.close(result_reader)
    assert len(child_pids) == 1
    waited, status = os.waitpid(child_pids[0], 0)
    assert waited == child_pids[0]
    assert os.waitstatus_to_exitcode(status) == 0
    assert child_result == b"clean"

    pending = vars(run_owner_fork_registry)["PENDING_DESCRIPTORS"]
    active = vars(run_owner_fork_registry)["ACTIVE_DESCRIPTORS"]
    assert observed_tokens
    assert all(token not in pending and token not in active for token in observed_tokens)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_forked_child_cannot_use_parent_prepared_ledger(tmp_path: Path) -> None:
    ledger = Ledger(_shot(tmp_path))
    ledger.data["candidate"] = {"accepted": True}
    prepared = ledger.prepare_save(authority_binding="fork-fixture")
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions execute in the parent
        os.close(reader)
        try:
            _ = prepared.destination
        except LedgerSaveConflict:
            os.write(writer, b"rejected")
        else:
            os.write(writer, b"accepted")
        finally:
            os.close(writer)
        os._exit(0)

    os.close(writer)
    child_result = os.read(reader, 32)
    os.close(reader)
    waited, status = os.waitpid(pid, 0)
    assert waited == pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert child_result == b"rejected"

    try:
        with shot_authority_writer_fence(tmp_path) as capability:
            ledger.commit_prepared_save(
                prepared,
                authority_binding="fork-fixture",
                writer_capability=capability,
            )
    finally:
        ledger.discard_prepared_save(prepared)
    assert (tmp_path / "shot.json").is_file()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_shot_ledger_registry_access_cannot_invert_process_fork_barrier() -> None:
    script = """
import os
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from vfx_harness.domain.brief import Shot
from vfx_harness.observability.run_owner_fork_guard import (
    managed_fork_protected_acquisition,
)
from vfx_harness.orchestration.ledger import Ledger, LedgerSaveConflict

with TemporaryDirectory() as temporary:
    root = Path(temporary)
    shot = Shot(
        folder=root,
        frontmatter={"id": "fork-order-fixture", "frames": 1, "fps": 24},
        body="fixture",
    )
    ledger = Ledger(shot)
    ledger.data["candidate"] = {"accepted": True}
    prepared = ledger.prepare_save(authority_binding="fork-order-fixture")
    entered = threading.Event()
    failures = []

    def worker():
        try:
            with managed_fork_protected_acquisition():
                entered.set()
                time.sleep(0.25)
                try:
                    _ = prepared.destination
                except LedgerSaveConflict as exc:
                    if "another process or thread" not in str(exc):
                        raise
                else:
                    raise AssertionError("cross-thread prepared ledger was accepted")
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert entered.wait(1)
    child = os.fork()
    if child == 0:
        os._exit(0)
    waited, status = os.waitpid(child, 0)
    thread.join(2)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert not thread.is_alive()
    assert not failures
    ledger.discard_prepared_save(prepared)
print("fork-complete")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.stdout.strip() == "fork-complete"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_forked_child_does_not_pin_parent_ledger_lock(tmp_path: Path) -> None:
    ledger_path = tmp_path / "shot.json"
    ready_reader, ready_writer = os.pipe()
    release_reader, release_writer = os.pipe()
    result_reader, result_writer = os.pipe()

    with ledger_lock(ledger_path, exclusive=True):
        pid = os.fork()
        if pid == 0:  # pragma: no cover - assertions execute in the parent
            os.close(ready_reader)
            os.close(release_writer)
            os.close(result_reader)
            os.write(ready_writer, b"ready")
            os.close(ready_writer)
            os.read(release_reader, 1)
            os.close(release_reader)
            try:
                with ledger_lock(
                    ledger_path,
                    exclusive=True,
                    blocking=False,
                ):
                    result = b"released"
            except BaseException as exc:
                result = f"blocked:{type(exc).__name__}".encode()
            os.write(result_writer, result)
            os.close(result_writer)
            os._exit(0)

        os.close(ready_writer)
        os.close(release_reader)
        os.close(result_writer)
        assert os.read(ready_reader, 5) == b"ready"
        os.close(ready_reader)

    os.write(release_writer, b"x")
    os.close(release_writer)
    child_result = os.read(result_reader, 64)
    os.close(result_reader)
    waited, status = os.waitpid(pid, 0)
    assert waited == pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert child_result == b"released"


def test_nonblocking_mutation_lock_refuses_busy_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / "shot.json"
    failures: list[BaseException] = []

    def mutate() -> None:
        try:
            with (
                shot_authority_writer_fence(tmp_path) as capability,
                shot_ledger_mutation_lock(ledger_path, capability),
            ):
                pytest.fail("busy ledger lock cannot admit a canonical mutation")
        except BaseException as exc:  # asserted below
            failures.append(exc)

    with ledger_lock(ledger_path, exclusive=True):
        thread = Thread(target=mutate)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()

    assert len(failures) == 1
    assert isinstance(failures[0], LedgerSaveConflict)
    assert "ledger lock is busy" in str(failures[0])


def test_substituted_staging_name_cannot_publish_or_be_deleted(tmp_path: Path) -> None:
    ledger = Ledger(_shot(tmp_path))
    ledger.data["candidate"] = {"accepted": True}
    prepared = ledger.prepare_save(authority_binding="substitution-fixture")
    staged = list(tmp_path.glob(".shot.json.prepared.*"))
    assert len(staged) == 1
    registered_name = staged[0]
    moved = tmp_path / "attacker-held-original"
    registered_name.rename(moved)
    substitute = b'{"candidate":{"accepted":"substituted"}}\n'
    registered_name.write_bytes(substitute)

    try:
        with (
            shot_authority_writer_fence(tmp_path) as capability,
            pytest.raises(LedgerSaveConflict, match=r"staged|prepared"),
        ):
            ledger.commit_prepared_save(
                prepared,
                authority_binding="substitution-fixture",
                writer_capability=capability,
            )
    finally:
        ledger.discard_prepared_save(prepared)

    assert not (tmp_path / "shot.json").exists()
    assert registered_name.read_bytes() == substitute
    assert json.loads(moved.read_text(encoding="utf-8"))["candidate"] == {
        "accepted": True
    }


def test_exact_consumed_cleanup_is_inert_but_forgery_still_fails(
    tmp_path: Path,
) -> None:
    ledger = Ledger(_shot(tmp_path))
    ledger.data["candidate"] = {"accepted": True}
    prepared = ledger.prepare_save(authority_binding="consumed-fixture")
    with shot_authority_writer_fence(tmp_path) as capability:
        ledger.commit_prepared_save(
            prepared,
            authority_binding="consumed-fixture",
            writer_capability=capability,
        )

    ledger.discard_prepared_save(prepared)
    ledger.discard_prepared_save(prepared)
    with pytest.raises(LedgerSaveConflict, match="already consumed"):
        _ = prepared.destination

    forged = object.__new__(PreparedShotLedgerPublication)
    with pytest.raises(LedgerSaveConflict, match="unregistered"):
        ledger.discard_prepared_save(forged)
