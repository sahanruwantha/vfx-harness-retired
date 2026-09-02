"""Typed semantic writer and terminal-capture fences for shot authority.

Both capabilities are leases over the permanent authority-selection inode. They are
bound to one canonical shot namespace, process instance, and thread; an inherited or
transferred capability is inert. Shared writer fences may nest beneath an exclusive
capture, but a shared lease can never be upgraded in place.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from threading import get_ident, local
from typing import ParamSpec, TypeVar

from vfx_harness.observability import fork_coordination
from vfx_harness.orchestration.authority_selection_process_registry import (
    current_process_token,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    _require_current_authority_selection_binding,
    authority_selection_lock,
    canonical_authority_shot_path,
)

_P = ParamSpec("_P")
_T = TypeVar("_T")
_THREAD_FENCES = local()

_INNER_LOCK_RANK = {
    "unit_state": 40,
    "shot_ledger": 50,
    "plan_resolution": 60,
    "judgment_debt": 70,
    "judgment_payment_attempt": 80,
}


@dataclass(frozen=True, slots=True, init=False, eq=False)
class ShotAuthorityWriterCapability:
    """Opaque proof of a semantic shared writer lease for one exact shot."""

    _shot: Path
    _lock_path: Path
    _selection_binding: object
    _process_id: int
    _process_token: str
    _thread_id: int
    _exclusive: bool

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise AuthoritySelectionConflict("shot-authority capabilities are issued only by a writer or capture context")

    @property
    def shot(self) -> Path:
        return self._shot

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def exclusive(self) -> bool:
        return self._exclusive


@dataclass(frozen=True, slots=True, init=False, eq=False)
class ShotAuthorityCaptureCapability(ShotAuthorityWriterCapability):
    """Opaque proof of an exclusive terminal authority-capture lease."""


class _ActiveFence:
    __slots__ = ("capability", "depth", "inner_locks")

    def __init__(self, capability: ShotAuthorityWriterCapability) -> None:
        self.capability = capability
        self.depth = 1
        self.inner_locks: list[tuple[str, int]] = []


def _fences() -> dict[str, _ActiveFence]:
    fences = getattr(_THREAD_FENCES, "fences", None)
    if fences is None:
        fences = {}
        _THREAD_FENCES.fences = fences
    return fences


def _clear_child_thread_fences() -> None:
    _THREAD_FENCES.fences = {}


fork_coordination.register_fork_participant(
    "orchestration.shot_authority_capture",
    lock_factory=None,
    after_in_child=_clear_child_thread_fences,
)


def _require_active(
    capability: ShotAuthorityWriterCapability,
) -> _ActiveFence:
    if not isinstance(capability, ShotAuthorityWriterCapability):
        raise AuthoritySelectionConflict("authority writer requires a typed shot-authority writer capability")
    if (
        capability._process_id != os.getpid()
        or capability._process_token != current_process_token()
        or capability._thread_id != get_ident()
    ):
        raise AuthoritySelectionConflict("shot-authority capability is not active on this thread or process instance")
    active = _fences().get(str(capability.shot))
    if active is None or active.capability is not capability:
        raise AuthoritySelectionConflict("shot-authority capability is not active on this thread")
    binding = _require_current_authority_selection_binding(
        capability.shot,
        exclusive=capability.exclusive,
        expected_binding=capability._selection_binding,
    )
    if capability.lock_path != binding.lock_path:
        raise AuthoritySelectionConflict("shot-authority capability names a different selection-lock path")
    return active


def _issue_capability(
    *,
    shot: Path,
    lock_path: Path,
    exclusive: bool,
) -> ShotAuthorityWriterCapability:
    selection_binding = _require_current_authority_selection_binding(
        shot,
        exclusive=exclusive,
    )
    capability_type = ShotAuthorityCaptureCapability if exclusive else ShotAuthorityWriterCapability
    capability = object.__new__(capability_type)
    object.__setattr__(capability, "_shot", shot)
    object.__setattr__(capability, "_lock_path", lock_path)
    object.__setattr__(capability, "_selection_binding", selection_binding)
    object.__setattr__(capability, "_process_id", os.getpid())
    object.__setattr__(capability, "_process_token", current_process_token())
    object.__setattr__(capability, "_thread_id", get_ident())
    object.__setattr__(capability, "_exclusive", exclusive)
    return capability


@contextmanager
def _shot_authority_fence(
    shot_folder: str | Path,
    *,
    exclusive: bool,
) -> Iterator[ShotAuthorityWriterCapability]:
    shot = canonical_authority_shot_path(shot_folder)
    key = str(shot)
    fences = _fences()
    active = fences.get(key)
    if active is not None:
        _require_active(active.capability)
        with authority_selection_lock(shot, exclusive=exclusive):
            active.depth += 1
            try:
                yield active.capability
            finally:
                active.depth -= 1
        return
    if fences:
        active_shots = ", ".join(sorted(fences))
        raise AuthoritySelectionConflict(
            f"nested shot-authority fences require the same canonical shot; active={active_shots}; requested={shot}"
        )
    with authority_selection_lock(shot, exclusive=exclusive) as lock_path:
        capability = _issue_capability(
            shot=shot,
            lock_path=lock_path,
            exclusive=exclusive,
        )
        active = _ActiveFence(capability)
        fences[key] = active
        try:
            yield capability
        finally:
            try:
                if active.inner_locks:
                    raise AuthoritySelectionConflict(
                        "shot-authority fence exited while an inner authority lock remained active"
                    )
            finally:
                fences.pop(key, None)


@contextmanager
def shot_authority_writer_fence(
    shot_folder: str | Path,
) -> Iterator[ShotAuthorityWriterCapability]:
    """Hold the permanent selection fence as a semantic shared writer lease."""

    with _shot_authority_fence(shot_folder, exclusive=False) as capability:
        yield capability


@contextmanager
def shot_authority_capture(
    shot_folder: str | Path,
) -> Iterator[ShotAuthorityCaptureCapability]:
    """Hold the permanent selection fence as an exclusive terminal capture."""

    with _shot_authority_fence(shot_folder, exclusive=True) as capability:
        if not isinstance(capability, ShotAuthorityCaptureCapability):
            raise AuthoritySelectionConflict("cannot upgrade a shared shot-authority writer fence to terminal capture")
        yield capability


@contextmanager
def ordered_authority_inner_lock(
    capability: ShotAuthorityWriterCapability,
    kind: str,
) -> Iterator[None]:
    """Authorize one narrower lock only in the closed global acquisition order."""

    active = _require_active(capability)
    try:
        rank = _INNER_LOCK_RANK[kind]
    except KeyError as exc:
        legal = ", ".join(_INNER_LOCK_RANK)
        raise AuthoritySelectionConflict(
            f"unknown authority inner-lock kind {kind!r}; expected one of: {legal}"
        ) from exc
    if active.inner_locks and rank <= active.inner_locks[-1][1]:
        held_kind = active.inner_locks[-1][0]
        raise AuthoritySelectionConflict(
            f"authority inner-lock order violation: cannot acquire {kind} after {held_kind}"
        )
    active.inner_locks.append((kind, rank))
    try:
        yield
    finally:
        observed = active.inner_locks.pop()
        if observed != (kind, rank):
            raise AuthoritySelectionConflict("authority inner-lock stack changed before ordered release")


def current_shot_authority_writer(
    shot_folder: str | Path,
) -> ShotAuthorityWriterCapability:
    """Return the exact active writer or capture capability for this shot."""

    shot = canonical_authority_shot_path(shot_folder)
    active = _fences().get(str(shot))
    if active is None:
        raise AuthoritySelectionConflict("authority writer must acquire the shot-authority writer fence first")
    _require_active(active.capability)
    return active.capability


def require_live_shot_authority_writer(
    capability: ShotAuthorityWriterCapability,
    shot_folder: str | Path,
) -> Path:
    """Prove ``capability`` is the exact live writer generation for ``shot_folder``.

    The returned path is the canonical shot root validated by the retained shot,
    selection-parent, and selection-lock descriptors.  Callers must invoke this at
    the final mutation boundary; possession of an expired or copied value is not
    publication authority.
    """

    if not isinstance(capability, ShotAuthorityWriterCapability):
        raise AuthoritySelectionConflict("authority writer requires a typed shot-authority writer capability")
    shot = canonical_authority_shot_path(shot_folder)
    try:
        capability_shot = capability.shot
    except AttributeError as exc:
        raise AuthoritySelectionConflict(
            "shot-authority capability was not issued by a writer or capture context"
        ) from exc
    if capability_shot != shot:
        raise AuthoritySelectionConflict("shot-authority capability belongs to a different canonical shot")
    _require_active(capability)
    return shot


def current_shot_authority_capture(
    shot_folder: str | Path,
) -> ShotAuthorityCaptureCapability:
    """Return the exact active exclusive capture capability or fail closed."""

    capability = current_shot_authority_writer(shot_folder)
    if not isinstance(capability, ShotAuthorityCaptureCapability):
        raise AuthoritySelectionConflict("authority terminalizer requires the exclusive shot-authority capture fence")
    return capability


def shot_authority_writer(
    function: Callable[_P, _T],
) -> Callable[_P, _T]:
    """Guard an ordinary authority writer with the semantic shared fence."""

    @wraps(function)
    def guarded(shot_folder, *args, **kwargs):
        with shot_authority_writer_fence(shot_folder):
            return function(shot_folder, *args, **kwargs)

    return guarded


__all__ = [
    "ShotAuthorityCaptureCapability",
    "ShotAuthorityWriterCapability",
    "current_shot_authority_capture",
    "current_shot_authority_writer",
    "ordered_authority_inner_lock",
    "require_live_shot_authority_writer",
    "shot_authority_capture",
    "shot_authority_writer",
    "shot_authority_writer_fence",
]
