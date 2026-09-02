"""Non-destructive consumer-view retirement and recovery regressions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vfx_harness.orchestration import plan_consumer_view_cleanup as cleanup
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
    PlanConsumerViewMutationConflict,
)


def _identity(path: Path) -> PlanConsumerDirectoryIdentity:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        return PlanConsumerDirectoryIdentity.capture(descriptor)
    finally:
        os.close(descriptor)


def _parent_descriptor(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )


def test_retirement_race_cannot_replace_foreign_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "owned"
    source.mkdir()
    expected = _identity(source)
    retired_name = cleanup._retired_name(source.name, expected)
    original_rename = cleanup._rename_child_noreplace
    foreign_identity: PlanConsumerDirectoryIdentity | None = None

    def occupy_immediately_before_noreplace(
        parent_descriptor: int,
        source_name: str,
        destination_name: str,
    ) -> None:
        nonlocal foreign_identity
        os.mkdir(destination_name, dir_fd=parent_descriptor)
        foreign_identity = cleanup._optional_identity(
            parent_descriptor,
            destination_name,
        )
        original_rename(parent_descriptor, source_name, destination_name)

    parent = _parent_descriptor(tmp_path)
    try:
        monkeypatch.setattr(
            cleanup,
            "_rename_child_noreplace",
            occupy_immediately_before_noreplace,
        )
        with pytest.raises(
            PlanConsumerViewMutationConflict,
            match="destination became occupied",
        ):
            cleanup.retire_owned_directory(parent, source.name, expected)

        assert cleanup._optional_identity(parent, source.name) == expected
        assert cleanup._optional_identity(parent, retired_name) == foreign_identity
    finally:
        os.close(parent)


def test_retirement_cannot_report_success_when_both_names_are_absent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "owned"
    held = tmp_path / "held-owned"
    source.mkdir()
    expected = _identity(source)
    source.rename(held)

    parent = _parent_descriptor(tmp_path)
    try:
        with pytest.raises(
            PlanConsumerViewMutationConflict,
            match="cannot prove either its exact source",
        ):
            cleanup.retire_owned_directory(parent, source.name, expected)
    finally:
        os.close(parent)

    assert _identity(held) == expected


def test_exact_tombstone_beside_foreign_source_fails_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "owned"
    source.mkdir()
    expected = _identity(source)
    retired_name = cleanup._retired_name(source.name, expected)

    parent = _parent_descriptor(tmp_path)
    try:
        cleanup.retire_owned_directory(parent, source.name, expected)
        source.mkdir()
        foreign = _identity(source)
        with pytest.raises(
            PlanConsumerViewMutationConflict,
            match="foreign source beside its exact tombstone",
        ):
            cleanup.retire_owned_directory(parent, source.name, expected)

        assert cleanup._optional_identity(parent, retired_name) == expected
        assert cleanup._optional_identity(parent, source.name) == foreign
    finally:
        os.close(parent)


def test_retirement_retry_fsyncs_preexisting_exact_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "owned"
    source.mkdir()
    expected = _identity(source)
    retired_name = cleanup._retired_name(source.name, expected)
    parent = _parent_descriptor(tmp_path)
    original_fsync = cleanup.os.fsync
    calls = 0

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected first parent fsync failure")
        original_fsync(descriptor)

    try:
        monkeypatch.setattr(cleanup.os, "fsync", fail_first_fsync)
        with pytest.raises(OSError, match="injected first parent fsync failure"):
            cleanup.retire_owned_directory(parent, source.name, expected)
        assert cleanup._optional_identity(parent, source.name) is None
        assert cleanup._optional_identity(parent, retired_name) == expected

        cleanup.retire_owned_directory(parent, source.name, expected)
        assert calls == 2
    finally:
        os.close(parent)
