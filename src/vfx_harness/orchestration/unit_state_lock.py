"""Permanent per-layer locks for whole-file work-unit state mutations."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Callable
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from threading import local
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")
STATE_DIR = "state/work-units"
_THREAD_LOCKS = local()


def unit_state_path(folder: str | Path, layer_id: str) -> Path:
    """Return the one durable state path owned by a layer."""

    safe_layer = str(layer_id).strip()
    if not safe_layer or "/" in safe_layer or "\\" in safe_layer or safe_layer in {".", ".."}:
        raise ValueError(f"invalid layer id for work-unit state: {layer_id!r}")
    return Path(folder) / STATE_DIR / f"layer_{safe_layer}.json"


@contextmanager
def _locked_state_path(state_path: Path, *, exclusive: bool = True):
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_key = os.path.realpath(lock_path)
    held = getattr(_THREAD_LOCKS, "held", None)
    if held is None:
        held = {}
        _THREAD_LOCKS.held = held
    existing = held.get(lock_key)
    if existing is not None:
        descriptor, held_exclusive, depth = existing
        if exclusive and not held_exclusive:
            raise RuntimeError(
                f"cannot upgrade a shared work-unit state lock to exclusive: {lock_path}"
            )
        held[lock_key] = (descriptor, held_exclusive, depth + 1)
        try:
            yield
        finally:
            if depth:
                held[lock_key] = (descriptor, held_exclusive, depth)
            else:
                held.pop(lock_key, None)
        return

    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ValueError(
            f"work-unit state lock must be a real regular file: {lock_path}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(
                f"work-unit state lock must be a regular file: {lock_path}"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        held[lock_key] = (descriptor, exclusive, 1)
        try:
            yield
        finally:
            held.pop(lock_key, None)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def unit_state_lock(
    folder: str | Path,
    layer_id: str,
    *,
    exclusive: bool,
):
    """Hold a shared or exclusive lock for one layer's complete state document."""

    with _locked_state_path(
        unit_state_path(folder, layer_id),
        exclusive=exclusive,
    ):
        yield


def serialized_state_mutation(
    state_path: Callable[[str | Path, str], Path],
) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
    """Decorate a mutation whose first arguments are folder and layer id."""

    def decorate(mutation: Callable[_P, _T]) -> Callable[_P, _T]:
        @wraps(mutation)
        def guarded(folder, layer_id, *args, **kwargs):
            with _locked_state_path(state_path(folder, layer_id), exclusive=True):
                return mutation(folder, layer_id, *args, **kwargs)

        return guarded

    return decorate
