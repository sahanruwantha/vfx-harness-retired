"""One process-wide fork barrier and ordered registry-lock coordinator.

Python invokes independently registered ``before`` callbacks in reverse order.
Registering one callback per authority registry therefore creates an accidental
lock inversion with descriptor acquisition.  This module is the sole callback
owner: normal registry access takes the barrier before its local lock, and fork
takes that same barrier before every registered local lock.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol


class ForkParticipantLock(Protocol):
    """Minimal lock surface required by the coordinator."""

    def acquire(self) -> bool: ...

    def release(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _ForkParticipant:
    name: str
    lock_factory: Callable[[], ForkParticipantLock] | None
    after_in_child: Callable[[], None]


_BARRIER = threading.RLock()
_PARTICIPANTS: list[_ForkParticipant] = []
_PARTICIPANT_NAMES: set[str] = set()
_PREFORK_LOCKS: list[ForkParticipantLock] = []
_CHILD_COORDINATION_FAILURE_EXIT = 87


def acquire_fork_barrier() -> None:
    """Enter the global barrier before descriptor or registry mutation."""

    _BARRIER.acquire()


def release_fork_barrier() -> None:
    """Leave one matching global barrier acquisition."""

    _BARRIER.release()


@contextmanager
def fork_barrier() -> Iterator[None]:
    """Serialize one operation against fork without taking a local lock."""

    acquire_fork_barrier()
    try:
        yield
    finally:
        release_fork_barrier()


@contextmanager
def fork_coordinated_lock(lock: ForkParticipantLock) -> Iterator[None]:
    """Take one participant lock in the only legal global-to-local order."""

    acquire_fork_barrier()
    try:
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
    finally:
        release_fork_barrier()


def register_fork_participant(
    name: str,
    *,
    lock_factory: Callable[[], ForkParticipantLock] | None,
    after_in_child: Callable[[], None],
) -> None:
    """Register one stable child reset and optional module-local lock.

    Participants are acquired in reverse registration order, matching Python's
    historical ``before`` ordering.  They are reset in registration order in
    the child, matching historical ``after_in_child`` ordering.  A participant
    name is process-global and cannot be replaced silently.
    """

    if not name:
        raise ValueError("fork participant name must be non-empty")
    with fork_barrier():
        if name in _PARTICIPANT_NAMES:
            raise RuntimeError(f"fork participant is already registered: {name}")
        _PARTICIPANTS.append(
            _ForkParticipant(name, lock_factory, after_in_child)
        )
        _PARTICIPANT_NAMES.add(name)


def _before_fork() -> None:
    acquire_fork_barrier()
    _PREFORK_LOCKS.clear()
    try:
        for participant in reversed(_PARTICIPANTS):
            if participant.lock_factory is None:
                continue
            lock = participant.lock_factory()
            lock.acquire()
            _PREFORK_LOCKS.append(lock)
    except BaseException:
        for lock in reversed(_PREFORK_LOCKS):
            lock.release()
        _PREFORK_LOCKS.clear()
        release_fork_barrier()
        raise


def _after_fork_parent() -> None:
    for lock in reversed(_PREFORK_LOCKS):
        lock.release()
    _PREFORK_LOCKS.clear()
    release_fork_barrier()


def _after_fork_child() -> None:
    global _BARRIER

    # Only the forking thread survives.  Participant callbacks clear inherited
    # authority and replace their module locks; inherited acquired lock objects
    # are deliberately abandoned rather than released into an invalid owner
    # generation.
    try:
        for participant in _PARTICIPANTS:
            participant.after_in_child()
        _PREFORK_LOCKS.clear()
        _BARRIER = threading.RLock()
    except BaseException:
        # No application stack can safely reconcile a partially reset authority
        # registry generation in the child.
        os._exit(_CHILD_COORDINATION_FAILURE_EXIT)


os.register_at_fork(
    before=_before_fork,
    after_in_parent=_after_fork_parent,
    after_in_child=_after_fork_child,
)


__all__ = [
    "ForkParticipantLock",
    "acquire_fork_barrier",
    "fork_barrier",
    "fork_coordinated_lock",
    "register_fork_participant",
    "release_fork_barrier",
]
