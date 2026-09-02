"""Identity-bound cleanup helpers for opaque prepared-file transactions."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path

from vfx_harness.observability.prepared_publication_capabilities import (
    PreparedPublicationRegistryError,
)
from vfx_harness.observability.run_owner_fork_guard import GuardedDescriptor


def unlink_guarded_temporary(
    descriptors: Sequence[GuardedDescriptor],
    parent_descriptor: int,
    name: str,
) -> bool:
    """Unlink a name only while it names one exact retained regular inode."""

    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return True
    for guarded in descriptors:
        if not guarded.is_current():
            continue
        held = os.fstat(guarded.descriptor)
        if stat.S_ISREG(held.st_mode) and stat.S_ISREG(named.st_mode) and (
            held.st_dev,
            held.st_ino,
        ) == (named.st_dev, named.st_ino):
            os.unlink(name, dir_fd=parent_descriptor)
            return True
    return False


def raise_prepared_transaction_errors(
    body_error: BaseException | None,
    cleanup_error: BaseException | None,
    destination: Path,
) -> None:
    """Raise one causal transaction error without erasing cleanup diagnostics."""

    if cleanup_error is not None:
        failure = PreparedPublicationRegistryError(
            f"prepared side-file transaction cleanup failed: {destination}"
        )
        if body_error is not None:
            failure.add_note(
                "publication cleanup diagnostic: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
            for note in getattr(cleanup_error, "__notes__", ()):
                failure.add_note(str(note))
            raise failure from body_error
        raise failure from cleanup_error
    if body_error is not None:
        raise body_error


__all__ = ["raise_prepared_transaction_errors", "unlink_guarded_temporary"]
