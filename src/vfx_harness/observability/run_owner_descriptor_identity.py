"""Exact descriptor identity contracts shared by fork-safe authority registries.

A numeric file descriptor is never authority by itself.  Every registry in this
package joins the number to the device, inode, type, and special-device identity
captured at ownership, and every later observation is tri-state: the slot still
names that identity, the slot is provably closed (``EBADF``), or the slot cannot
be read.  An unreadable slot never collapses into "closed": it poisons this
process for engineering and stays retained until that review.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass


class RunOwnerForkGuardError(RuntimeError):
    """A descriptor acquisition crossed a process boundary or registry invariant."""


class RunOwnerForkGuardCleanupError(RunOwnerForkGuardError):
    """A managed acquisition retained exact live descriptors after cleanup failed."""

    def __init__(
        self,
        errors: tuple[BaseException, ...],
        retained: tuple[GuardedDescriptor, ...] = (),
        *,
        retained_authority: tuple[GuardedDescriptor, ...] = (),
        retained_neutral: tuple[GuardedDescriptor, ...] = (),
        retained_unproven: tuple[int, ...] = (),
    ) -> None:
        self.errors = errors
        if retained and (retained_authority or retained_neutral):
            raise ValueError(
                "cleanup failure cannot mix legacy and classified retained rows"
            )
        self.retained_authority = retained_authority or retained
        self.retained_neutral = retained_neutral
        self.retained_unproven = retained_unproven
        self.retained = (*self.retained_authority, *self.retained_neutral)
        super().__init__(
            "fork-protected acquisition cleanup could not neutralize every "
            "descriptor; "
            f"retained={tuple(item.descriptor for item in self.retained)!r}; "
            "retained_authority="
            f"{tuple(item.descriptor for item in self.retained_authority)!r}; "
            "retained_neutral="
            f"{tuple(item.descriptor for item in self.retained_neutral)!r}; "
            f"retained_unproven={self.retained_unproven!r}; "
            "route to engineering"
        )

    @property
    def retains_authority(self) -> bool:
        """Whether any numeric slot may still carry authority in this process."""

        return bool(self.retained or self.retained_unproven)


PROCESS_CLEANUP_POISON: list[BaseException] = []


def record_unproven_identity(
    descriptor: int,
    cause: OSError,
) -> RunOwnerForkGuardError:
    """Poison this process because one numeric slot's identity cannot be observed."""

    failure = RunOwnerForkGuardError(
        f"descriptor {descriptor} identity is unreadable ({cause}); "
        "absence is unproven"
    )
    PROCESS_CLEANUP_POISON.append(failure)
    return failure


@dataclass(frozen=True, slots=True)
class GuardedDescriptor:
    """A numeric descriptor joined to the file identity captured at ownership."""

    descriptor: int
    device: int
    inode: int
    file_type: int
    special_device: int

    @classmethod
    def capture(cls, descriptor: int) -> GuardedDescriptor:
        observed = os.fstat(descriptor)
        return cls(
            descriptor=descriptor,
            device=observed.st_dev,
            inode=observed.st_ino,
            file_type=stat.S_IFMT(observed.st_mode),
            special_device=observed.st_rdev,
        )

    def rebased(self, descriptor: int) -> GuardedDescriptor:
        """The same file identity expected at another numeric slot."""

        return GuardedDescriptor(
            descriptor=descriptor,
            device=self.device,
            inode=self.inode,
            file_type=self.file_type,
            special_device=self.special_device,
        )

    def is_current(self) -> bool:
        """Whether the slot names this identity; raise when absence is unproven."""

        try:
            observed = os.fstat(self.descriptor)
        except OSError as exc:
            if exc.errno == errno.EBADF:
                return False
            raise record_unproven_identity(self.descriptor, exc) from exc
        return (
            observed.st_dev == self.device
            and observed.st_ino == self.inode
            and stat.S_IFMT(observed.st_mode) == self.file_type
            and observed.st_rdev == self.special_device
        )

    def is_current_or_unproven(self) -> bool:
        """Conservative retention: an unreadable slot counts as still owned."""

        try:
            return self.is_current()
        except RunOwnerForkGuardError:
            return True


__all__ = [
    "PROCESS_CLEANUP_POISON",
    "GuardedDescriptor",
    "RunOwnerForkGuardCleanupError",
    "RunOwnerForkGuardError",
    "record_unproven_identity",
]
