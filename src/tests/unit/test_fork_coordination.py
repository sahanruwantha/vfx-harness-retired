"""Process-wide fork coordinator fail-closed behavior."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from vfx_harness.observability import fork_coordination


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_child_terminates_if_any_participant_reset_fails() -> None:
    script = """
import os

from vfx_harness.observability import fork_coordination

def fail_reset():
    raise KeyboardInterrupt("injected child participant reset failure")

fork_coordination.register_fork_participant(
    "test.failing-child-reset",
    lock_factory=None,
    after_in_child=fail_reset,
)
child = os.fork()
if child == 0:
    os._exit(0)
waited, status = os.waitpid(child, 0)
assert waited == child
assert os.waitstatus_to_exitcode(status) == 87
"""
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_child_terminates_if_coordinator_state_reset_fails() -> None:
    script = """
import os

from vfx_harness.observability import fork_coordination

parent_pid = os.getpid()

class FailingChildClear(list):
    def clear(self):
        if os.getpid() != parent_pid:
            raise MemoryError("injected coordinator child reset failure")
        super().clear()

fork_coordination._PREFORK_LOCKS = FailingChildClear()
child = os.fork()
if child == 0:
    os._exit(0)
waited, status = os.waitpid(child, 0)
assert waited == child
assert os.waitstatus_to_exitcode(status) == 87
"""
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_child_terminates_if_barrier_reinitialization_fails() -> None:
    script = """
import os

from vfx_harness.observability import fork_coordination

parent_pid = os.getpid()
original_rlock = fork_coordination.threading.RLock

def fail_child_barrier_reset():
    if os.getpid() != parent_pid:
        raise MemoryError("injected child barrier reset failure")
    return original_rlock()

fork_coordination.threading.RLock = fail_child_barrier_reset
child = os.fork()
if child == 0:
    os._exit(0)
waited, status = os.waitpid(child, 0)
assert waited == child
assert os.waitstatus_to_exitcode(status) == 87
"""
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_participant_name_cannot_be_rebound() -> None:
    name = f"test.unique-participant.{id(object())}"
    fork_coordination.register_fork_participant(
        name,
        lock_factory=None,
        after_in_child=lambda: None,
    )
    with pytest.raises(RuntimeError, match="already registered"):
        fork_coordination.register_fork_participant(
            name,
            lock_factory=None,
            after_in_child=lambda: None,
        )
