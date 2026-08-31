"""Crash-durable immutable JIT view-store contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.authority_selection_transaction import (
    durably_ensure_real_directory,
)
from vfx_harness.orchestration.jit_materialization.errors import (
    MaterializationSelectionConflict,
)
from vfx_harness.orchestration.jit_materialization.view_store import (
    durably_install_or_flush_view_directory,
)


def _members() -> dict[str, bytes]:
    return {
        "acceptance.json": b"[]\n",
        "layers.json": b'{"layers":[]}\n',
    }


def test_existing_exact_view_is_fully_reflushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state" / "jit-layers" / "views" / ("a" * 64)
    members = _members()
    assert durably_install_or_flush_view_directory(tmp_path, root, members) == root
    reflushed: list[Path] = []
    original_file_fsync = plan_bundle_integrity._fsync_regular_file

    def recording_file_fsync(path: Path) -> None:
        reflushed.append(path)
        original_file_fsync(path)

    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_regular_file",
        recording_file_fsync,
    )

    durably_install_or_flush_view_directory(tmp_path, root, members)

    assert {path.relative_to(root).as_posix() for path in reflushed} == set(members)


def test_view_store_refuses_symlink_target_without_touching_destination(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "state" / "jit-layers" / "views"
    durably_ensure_real_directory(tmp_path, parent)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside")
    root = parent / ("b" * 64)
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        MaterializationSelectionConflict,
        match=r"durably install|real directory",
    ):
        durably_install_or_flush_view_directory(tmp_path, root, _members())

    assert sentinel.read_bytes() == b"outside"
    assert {path.name for path in outside.iterdir()} == {"sentinel"}


@pytest.mark.parametrize(
    "root",
    (
        Path("state/jit-layers/views/not-a-digest"),
        Path("state/jit-layers/elsewhere") / ("c" * 64),
        Path("plans/views") / ("d" * 64),
    ),
)
def test_view_store_refuses_noncanonical_content_address(
    tmp_path: Path,
    root: Path,
) -> None:
    with pytest.raises(MaterializationSelectionConflict, match="materialized view"):
        durably_install_or_flush_view_directory(tmp_path, tmp_path / root, _members())
