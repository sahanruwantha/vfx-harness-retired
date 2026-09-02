from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vfx_harness.observability import (
    prepared_publication,
)
from vfx_harness.observability import (
    prepared_publication_descriptors as descriptors,
)


def _prepare(shot: Path, name: str = "result.json"):
    update = prepared_publication.prepare_file_update(
        shot,
        name,
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="prepared-fork-cleanup-test",
    )
    assert update.publication is not None
    return update.publication


def _record(publication):
    return prepared_publication._registry.require_live_prepared_file(publication)


def _wait_for_child_json(child: int, reader: int) -> dict[str, object]:
    payload = os.read(reader, 16_384)
    os.close(reader)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert payload
    return json.loads(payload)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_prepared_registry_access_cannot_invert_the_process_fork_barrier() -> None:
    script = """
import os
import threading
import time

from vfx_harness.observability import prepared_publication_registry as registry
from vfx_harness.observability.run_owner_fork_guard import (
    managed_fork_protected_acquisition,
)

entered = threading.Event()
failures = []

def worker():
    try:
        with managed_fork_protected_acquisition():
            entered.set()
            time.sleep(0.25)
            token = object()
            registry.arm_descriptor_acquisition(token)
            registry.abort_descriptor_acquisition(token)
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
def test_fork_child_neutralizes_every_exact_authority_before_any_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _prepare(tmp_path)
    targets = {
        guarded.descriptor for guarded in _record(publication).guarded_descriptors
    }
    parent_pid = os.getpid()
    actual_close = descriptors.os.close
    actual_dup2 = descriptors.os.dup2
    reader, writer = os.pipe()
    events: list[tuple[str, int]] = []

    def track_child_close(descriptor: int) -> None:
        if os.getpid() != parent_pid and descriptor in targets:
            events.append(("close", descriptor))
        actual_close(descriptor)

    def track_child_dup2(
        source: int,
        destination: int,
        *,
        inheritable: bool = True,
    ) -> int:
        if os.getpid() != parent_pid and destination in targets:
            events.append(("neutralize", destination))
        return actual_dup2(source, destination, inheritable=inheritable)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(descriptors.os, "close", track_child_close)
            patch.setattr(descriptors.os, "dup2", track_child_dup2)
            child = os.fork()
            if child == 0:  # pragma: no cover - assertions are sent to parent
                actual_close(reader)
                os.write(writer, json.dumps(events).encode())
                actual_close(writer)
                os._exit(0)
            actual_close(writer)
            observed = _wait_for_child_json(child, reader)
        assert [event[0] for event in observed] == [
            *("neutralize" for _target in targets),
            *("close" for _target in targets),
        ]
        assert {event[1] for event in observed[: len(targets)]} == targets
        assert {event[1] for event in observed[len(targets) :]} == targets
    finally:
        prepared_publication.discard_prepared_file(publication)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_fork_cleanup_surrenders_preexisting_same_neutralizer_descriptor(
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions are sent to parent
        os.close(reader)
        result: dict[str, object]
        try:
            target = os.open(
                tmp_path / "preexisting-neutral.lock",
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            original_guard = descriptors.GuardedDescriptor.capture(target)
            actual_close = descriptors.os.close
            descriptors.os.dup2(
                descriptors._NEUTRALIZER_DESCRIPTOR,
                target,
                inheritable=False,
            )
            close_calls = 0

            def track_target_close(descriptor: int) -> None:
                nonlocal close_calls
                if descriptor == target:
                    close_calls += 1
                actual_close(descriptor)

            descriptors.os.close = track_target_close
            descriptors.neutralize_fork_child_descriptors((original_guard,))
            try:
                descriptors.require_prepared_descriptor_admission()
            except prepared_publication.FilePublicationConflict as exc:
                refusal = str(exc)
            else:
                refusal = ""
            result = {
                "admission_poisoned": "admission is poisoned" in refusal,
                "close_not_attempted": close_calls == 0,
                "record_not_invented": target
                not in descriptors._INERT_DESCRIPTORS,
                "same_neutral_preserved": descriptors._guard_state(
                    descriptors._neutral_guard(target)
                ) == "current",
                "slot_surrendered": target
                in descriptors._AMBIGUOUS_CLOSE_SLOTS,
            }
        except BaseException as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        os.write(writer, json.dumps(result, sort_keys=True).encode())
        os._exit(0)

    os.close(writer)
    result = _wait_for_child_json(child, reader)
    assert result == {
        "admission_poisoned": True,
        "close_not_attempted": True,
        "record_not_invented": True,
        "same_neutral_preserved": True,
        "slot_surrendered": True,
    }


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_fork_child_terminates_if_exact_lock_authority_cannot_be_neutralized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _prepare(tmp_path)
    record = _record(publication)
    lock_descriptor = record.lock_descriptor
    parent_pid = os.getpid()
    actual_dup2 = descriptors.os.dup2

    def fail_child_lock_neutralization(
        source: int,
        destination: int,
        *,
        inheritable: bool = True,
    ) -> int:
        if os.getpid() != parent_pid and destination == lock_descriptor:
            raise OSError("injected fork-child neutralization refusal")
        return actual_dup2(source, destination, inheritable=inheritable)

    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(descriptors.os, "dup2", fail_child_lock_neutralization)
            child = os.fork()
            if child == 0:  # pragma: no cover - terminal handler exits first
                os._exit(0)
            waited, status = os.waitpid(child, 0)
        assert waited == child
        assert os.waitstatus_to_exitcode(status) == (
            descriptors._FORK_AUTHORITY_FAILURE_EXIT_CODE
        )
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)

    probe = os.open(record.lock_parent / record.lock_name, os.O_RDWR)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        os.close(probe)
        prepared_publication.discard_prepared_file(publication)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_fork_child_close_without_effect_poison_refuses_fresh_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _prepare(tmp_path)
    record = _record(publication)
    target = record.temporary_descriptor
    target_guard = record.temporary_guard
    parent_pid = os.getpid()
    actual_close = descriptors.os.close
    reader, writer = os.pipe()
    failed = False

    def fail_child_target_close(descriptor: int) -> None:
        nonlocal failed
        if os.getpid() != parent_pid and descriptor == target and not failed:
            failed = True
            raise OSError("injected fork-child close without effect")
        actual_close(descriptor)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(descriptors.os, "close", fail_child_target_close)
            child = os.fork()
            if child == 0:  # pragma: no cover - assertions are sent to parent
                actual_close(reader)
                try:
                    try:
                        _prepare(tmp_path, "child.json")
                    except prepared_publication.FilePublicationConflict as exc:
                        refusal = str(exc)
                    else:
                        refusal = ""
                    result = {
                        "authority_released": not target_guard.is_current(),
                        "neutral_before": descriptors._guard_state(
                            descriptors._neutral_guard(target)
                        ) == "current",
                        "poison_refused": "admission is poisoned" in refusal,
                        "slot_preserved": descriptors._guard_state(
                            descriptors._neutral_guard(target)
                        ) == "current",
                    }
                except BaseException as exc:
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                os.write(writer, json.dumps(result, sort_keys=True).encode())
                actual_close(writer)
                os._exit(0)
            actual_close(writer)
            result = _wait_for_child_json(child, reader)
        assert result == {
            "authority_released": True,
            "neutral_before": True,
            "poison_refused": True,
            "slot_preserved": True,
        }
    finally:
        prepared_publication.discard_prepared_file(publication)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_ambiguous_close_with_same_neutralizer_reuse_is_never_retried(
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions are sent to parent
        os.close(reader)
        result: dict[str, object]
        try:
            publication = _prepare(tmp_path)
            record = _record(publication)
            target = record.temporary_descriptor
            original_guard = record.temporary_guard
            actual_close = descriptors.os.close
            actual_dup2 = descriptors.os.dup2
            target_close_calls = 0
            replacement: int | None = None

            def close_then_reuse_neutralizer(descriptor: int) -> None:
                nonlocal replacement, target_close_calls
                if descriptor == target:
                    target_close_calls += 1
                    if target_close_calls == 1:
                        actual_close(descriptor)
                        replacement = actual_dup2(
                            descriptors._NEUTRALIZER_DESCRIPTOR,
                            target,
                            inheritable=False,
                        )
                        raise OSError("injected ambiguous close after fd reuse")
                actual_close(descriptor)

            descriptors.os.close = close_then_reuse_neutralizer
            try:
                prepared_publication.discard_prepared_file(publication)
            except prepared_publication.FilePublicationConflict:
                cleanup_refused = True
            else:
                cleanup_refused = False
            descriptors.neutralize_fork_child_descriptors((original_guard,))
            try:
                _prepare(tmp_path, "later.json")
            except prepared_publication.FilePublicationConflict as exc:
                admission_refusal = str(exc)
            else:
                admission_refusal = ""
            result = {
                "admission_poisoned": "admission is poisoned"
                in admission_refusal,
                "cleanup_refused": cleanup_refused,
                "one_close_attempt": target_close_calls == 1,
                "record_surrendered": target
                not in descriptors._INERT_DESCRIPTORS,
                "replacement_reused_slot": replacement == target,
                "reused_neutral_preserved": descriptors._guard_state(
                    descriptors._neutral_guard(target)
                ) == "current",
                "slot_surrendered": target
                in descriptors._AMBIGUOUS_CLOSE_SLOTS,
            }
        except BaseException as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        os.write(writer, json.dumps(result, sort_keys=True).encode())
        os._exit(0)

    os.close(writer)
    result = _wait_for_child_json(child, reader)
    assert result == {
        "admission_poisoned": True,
        "cleanup_refused": True,
        "one_close_attempt": True,
        "record_surrendered": True,
        "replacement_reused_slot": True,
        "reused_neutral_preserved": True,
        "slot_surrendered": True,
    }
