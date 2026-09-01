"""In-worker immutable byte transport for promoted construction replay."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def read_verified_replay_snapshot(
    snapshot: str | Path,
    *,
    replay_root: str | Path,
    expected_sha256: str,
) -> bytes:
    """Read one real replay snapshot descriptor and require its expected bytes."""

    path = Path(snapshot).absolute()
    root = Path(replay_root).absolute()
    if path.parent.resolve() != root.resolve():
        raise ValueError("construction replay snapshot is outside worker artifacts")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
            digest.update(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    actual = digest.hexdigest()
    if actual != expected_sha256 or raw[:4] != b"glTF":
        raise ValueError(
            "construction replay snapshot mismatch: "
            f"expected {expected_sha256}, found {actual}"
        )
    return raw


@contextmanager
def immutable_glb_path(payload: bytes, *, expected_sha256: str) -> Iterator[str]:
    """Expose captured bytes through one held memfd for Blender's importer reopen."""

    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256 or payload[:4] != b"glTF":
        raise ValueError(
            f"prepared construction bytes mismatch: expected {expected_sha256}, found {actual}"
        )
    if not hasattr(os, "memfd_create"):
        raise RuntimeError("prepared construction replay requires Linux memfd_create")
    descriptor = os.memfd_create(
        "vfx-harness-construction.glb",
        getattr(os, "MFD_CLOEXEC", 0),
    )
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise RuntimeError("prepared construction memfd short write")
            remaining = remaining[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield f"/proc/self/fd/{descriptor}"
    finally:
        os.close(descriptor)
