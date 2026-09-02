"""Process-wide registry of fork-visible descriptor ownership rows.

Every row joins a numeric slot to the file identity captured at ownership so a
forked child, a later admission check, or an interrupted cleanup can decide from
identity rather than from a reusable number.  Callers hold the process-wide fork
barrier while calling any ``*_locked`` function; that barrier is what keeps these
rows consistent with the fork child's cleanup callback.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from vfx_harness.observability.run_owner_descriptor_identity import (
    PROCESS_CLEANUP_POISON,
    GuardedDescriptor,
    RunOwnerForkGuardCleanupError,
    RunOwnerForkGuardError,
)


@dataclass(frozen=True, slots=True)
class InertDescriptorRecord:
    guarded: GuardedDescriptor
    owner_token: object
    in_flight: bool

    def is_current(self) -> bool:
        return self.guarded.is_current()

PENDING_DESCRIPTORS: dict[object, list[GuardedDescriptor]] = {}

ACTIVE_DESCRIPTORS: dict[object, tuple[GuardedDescriptor, ...]] = {}

INERT_DESCRIPTORS: dict[int, InertDescriptorRecord] = {}

INERT_CLOSES_IN_FLIGHT: set[int] = set()

ORPHANED_AUTHORITY_TOKENS: set[object] = set()

UNPROVEN_DESCRIPTORS: set[int] = set()

BLOCKABLE_SIGNALS = signal.valid_signals() - {signal.SIGKILL, signal.SIGSTOP}

CHILD_AUTHORITY_CLEANUP_EXIT = 86

NEUTRALIZER_DESCRIPTOR, _neutralizer_writer = os.pipe2(
    getattr(os, "O_CLOEXEC", 0)
)

os.close(_neutralizer_writer)

NEUTRALIZER_IDENTITY = GuardedDescriptor.capture(NEUTRALIZER_DESCRIPTOR)

def replace_active_descriptors_locked(
    token: object,
    expected: tuple[GuardedDescriptor, ...],
    retained: tuple[GuardedDescriptor, ...],
) -> None:
    """Shrink one active row while its owning close holds the fork guard."""

    observed = ACTIVE_DESCRIPTORS.get(token)
    if observed != expected:
        raise RunOwnerForkGuardError(
            "active descriptor row changed before partial cleanup publication"
        )
    if any(item not in expected for item in retained):
        raise RunOwnerForkGuardError(
            "partial cleanup retained an identity outside its active lease"
        )
    if retained:
        ACTIVE_DESCRIPTORS[token] = retained
    else:
        ACTIVE_DESCRIPTORS.pop(token, None)

@contextmanager
def block_descriptor_cleanup_signals() -> Iterator[None]:
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, BLOCKABLE_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)

def publish_neutralized_descriptor(
    token: object,
    expected: tuple[GuardedDescriptor, ...],
    descriptor: GuardedDescriptor,
) -> tuple[GuardedDescriptor, ...]:
    retained = tuple(item for item in expected if item != descriptor)
    replace_active_descriptors_locked(token, expected, retained)
    return retained

def neutral_descriptor(descriptor: int) -> GuardedDescriptor:
    return NEUTRALIZER_IDENTITY.rebased(descriptor)

def live_inert_descriptors_locked(
    *,
    owner_token: object | None = None,
) -> tuple[GuardedDescriptor, ...]:
    retained: list[GuardedDescriptor] = []
    for descriptor, record in tuple(INERT_DESCRIPTORS.items()):
        if owner_token is not None and record.owner_token is not owner_token:
            continue
        if record.guarded.is_current_or_unproven():
            retained.append(record.guarded)
        elif not record.in_flight:
            INERT_DESCRIPTORS.pop(descriptor, None)
    return tuple(retained)

def live_orphan_authority_locked() -> tuple[GuardedDescriptor, ...]:
    retained: list[GuardedDescriptor] = []
    for token in tuple(ORPHANED_AUTHORITY_TOKENS):
        live = tuple(
            guarded
            for guarded in ACTIVE_DESCRIPTORS.get(token, ())
            if guarded.is_current_or_unproven()
        )
        if live:
            ACTIVE_DESCRIPTORS[token] = live
            retained.extend(live)
        else:
            ACTIVE_DESCRIPTORS.pop(token, None)
            ORPHANED_AUTHORITY_TOKENS.discard(token)
    return tuple(retained)

def drain_inert_descriptors_locked(
    *,
    owner_token: object | None = None,
) -> tuple[BaseException, ...]:
    errors: list[BaseException] = []
    if INERT_CLOSES_IN_FLIGHT:
        errors.append(
            RunOwnerForkGuardError(
                "cannot drain an in-flight neutral descriptor generation"
            )
        )
    for record in tuple(INERT_DESCRIPTORS.values()):
        if owner_token is not None and record.owner_token is not owner_token:
            continue
        inert = record.guarded
        if record.in_flight:
            errors.append(
                RunOwnerForkGuardError(
                    "cannot drain an in-flight neutral descriptor generation"
                )
            )
            continue
        if not inert.is_current():
            INERT_DESCRIPTORS.pop(inert.descriptor, None)
            continue
        try:
            with block_descriptor_cleanup_signals():
                current = INERT_DESCRIPTORS.get(inert.descriptor)
                if current != record:
                    raise RunOwnerForkGuardError(
                        "neutral descriptor generation changed before drain"
                    )
                # This is the one and only raw-close attempt for this numeric
                # generation. Remove retry authority before calling close.
                INERT_DESCRIPTORS.pop(inert.descriptor, None)
                INERT_CLOSES_IN_FLIGHT.add(inert.descriptor)
                try:
                    os.close(inert.descriptor)
                except BaseException as exc:
                    errors.append(exc)
                    PROCESS_CLEANUP_POISON.append(exc)
                finally:
                    INERT_CLOSES_IN_FLIGHT.discard(inert.descriptor)
        except BaseException as exc:
            errors.append(exc)
    return tuple(errors)

def require_descriptor_acquisition_admission_locked() -> None:
    retained_neutral = live_inert_descriptors_locked()
    retained_authority = live_orphan_authority_locked()
    retained_unproven = tuple(sorted(UNPROVEN_DESCRIPTORS))
    errors = tuple(PROCESS_CLEANUP_POISON)
    if INERT_CLOSES_IN_FLIGHT:
        errors = (
            *errors,
            RunOwnerForkGuardError(
                "cannot admit acquisition during an in-flight neutral close"
            ),
        )
    if errors or retained_neutral or retained_authority or retained_unproven:
        raise RunOwnerForkGuardCleanupError(
            errors,
            retained_authority=retained_authority,
            retained_neutral=retained_neutral,
            retained_unproven=retained_unproven,
        )

def reconcile_active_descriptors_locked(
    token: object,
    requested: tuple[GuardedDescriptor, ...],
) -> tuple[tuple[GuardedDescriptor, ...], BaseException | None]:
    observed = ACTIVE_DESCRIPTORS.get(token, ())
    if observed == requested:
        return requested, None
    candidates = (*observed, *requested)
    live_by_descriptor: dict[int, GuardedDescriptor] = {}
    for guarded in candidates:
        if guarded.is_current():
            live_by_descriptor[guarded.descriptor] = guarded
    reconciled = tuple(
        live_by_descriptor[descriptor]
        for descriptor in sorted(live_by_descriptor)
    )
    if reconciled:
        ACTIVE_DESCRIPTORS[token] = reconciled
    else:
        ACTIVE_DESCRIPTORS.pop(token, None)
    return reconciled, RunOwnerForkGuardError(
        "active lease registry differed from its local guarded identities; "
        "cleanup reconciled every still-live exact descriptor"
    )

def retained_rows_locked(
    token: object,
) -> tuple[tuple[GuardedDescriptor, ...], tuple[GuardedDescriptor, ...]]:
    """Registry rows for one token that are still live or cannot be read."""

    authority = tuple(
        guarded
        for guarded in ACTIVE_DESCRIPTORS.get(token, ())
        if guarded.is_current_or_unproven()
    )
    if authority:
        ACTIVE_DESCRIPTORS[token] = authority
    else:
        ACTIVE_DESCRIPTORS.pop(token, None)
    neutral = tuple(
        record.guarded
        for record in tuple(INERT_DESCRIPTORS.values())
        if record.owner_token is token
        and record.guarded.is_current_or_unproven()
    )
    return authority, neutral
