"""Kernel-proven plan-consumer directory creation refuses swapped or unproven inodes."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from vfx_harness.observability.run_owner_fork_guard import (
    managed_fork_protected_acquisition,
)
from vfx_harness.orchestration import plan_consumer_owned_directory
from vfx_harness.orchestration.plan_consumer_owned_directory import (
    create_and_capture_empty_directory,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
    PlanConsumerViewMutationConflict,
)

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux fanotify")


@linux_only
def test_created_directory_is_adopted_with_its_kernel_identity(tmp_path: Path) -> None:
    parent = os.open(tmp_path, _DIRECTORY_FLAGS)
    try:
        with managed_fork_protected_acquisition() as acquisition:
            descriptor, identity = create_and_capture_empty_directory(
                parent,
                "child",
                acquisition,
                where="owned directory",
            )
            observed = os.stat(tmp_path / "child")
            assert identity == PlanConsumerDirectoryIdentity(
                observed.st_dev,
                observed.st_ino,
                stat.S_IFMT(observed.st_mode),
            )
            assert os.listdir(descriptor) == []
            # The fanotify watch was retired; only the adopted child stays pending.
            assert acquisition.guarded_descriptors((descriptor,))
            acquisition.retire(descriptor)
    finally:
        os.close(parent)


@linux_only
def test_adoption_refuses_a_directory_swapped_between_create_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = os.open(tmp_path, _DIRECTORY_FLAGS)
    original_open = plan_consumer_owned_directory.open_beneath_directory
    moved = tmp_path / "victim.moved"

    def swap_then_open(parent_descriptor: int, name: str) -> int:
        if name == "victim" and not moved.exists():
            # A same-UID racer moves the created inode away and plants another
            # directory under the exact name before the caller can open it.
            (tmp_path / name).rename(moved)
            (tmp_path / name).mkdir()
        return original_open(parent_descriptor, name)

    monkeypatch.setattr(
        plan_consumer_owned_directory,
        "open_beneath_directory",
        swap_then_open,
    )
    try:
        with pytest.raises(
            PlanConsumerViewMutationConflict,
            match="different from the kernel-observed mkdir target",
        ), managed_fork_protected_acquisition() as acquisition:
            create_and_capture_empty_directory(
                parent,
                "victim",
                acquisition,
                where="owned directory",
            )
    finally:
        os.close(parent)
    # Nothing was adopted or deleted: both inodes remain for engineering.
    assert moved.is_dir()
    assert (tmp_path / "victim").is_dir()


def test_unsupported_fanotify_platform_fails_closed_before_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plan_consumer_owned_directory, "_FANOTIFY_INIT", None)
    parent = os.open(tmp_path, _DIRECTORY_FLAGS)
    try:
        with pytest.raises(
            PlanConsumerViewMutationConflict,
            match="requires Linux fanotify target-FID reporting",
        ), managed_fork_protected_acquisition() as acquisition:
            create_and_capture_empty_directory(
                parent,
                "child",
                acquisition,
                where="owned directory",
            )
    finally:
        os.close(parent)
    assert not (tmp_path / "child").exists()
