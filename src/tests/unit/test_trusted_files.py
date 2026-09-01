from __future__ import annotations

from pathlib import Path

import pytest

from vfx_harness.blender.filesystem_confinement import (
    BlenderError,
    prepared_worker_command,
)
from vfx_harness.infrastructure import trusted_files
from vfx_harness.orchestration import plan_bundle_integrity


def test_plan_read_rejects_ancestor_rebound_during_descriptor_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = tmp_path / "shot"
    build = shot / "build"
    build.mkdir(parents=True)
    selected = build / "unit.py"
    selected.write_bytes(b"trusted unit bytes")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / selected.name).write_bytes(b"attacker bytes")
    retired = shot / "build-retired"
    original_read = trusted_files.os.read
    swapped = False

    def swap_ancestor_after_held_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped
        payload = original_read(descriptor, count)
        if not swapped:
            swapped = True
            build.rename(retired)
            build.symlink_to(outside, target_is_directory=True)
        return payload

    monkeypatch.setattr(trusted_files.os, "read", swap_ancestor_after_held_read)

    with pytest.raises(
        plan_bundle_integrity.PlanPublicationError,
        match=r"symlink|rebound",
    ):
        plan_bundle_integrity.read_real_file(
            shot,
            selected,
            "selected work-unit authority",
        )

    assert swapped is True
    assert (retired / selected.name).read_bytes() == b"trusted unit bytes"
    assert (outside / selected.name).read_bytes() == b"attacker bytes"


def test_retained_binding_rejects_real_directory_ancestor_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "shot"
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    source = inputs / "payload.bin"
    source.write_bytes(b"same bytes")
    snapshot = trusted_files.read_trusted_file(root, source, "fixture input")
    retired = root / "inputs-retired"
    inputs.rename(retired)
    inputs.mkdir()
    source.write_bytes(b"same bytes")

    with pytest.raises(trusted_files.TrustedFileError, match=r"changed|rebound"):
        trusted_files.require_trusted_file_unchanged(
            snapshot.binding,
            "fixture input",
        )


def test_retained_binding_ignores_sibling_directory_metadata_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "shot"
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    source = inputs / "payload.bin"
    source.write_bytes(b"stable bytes")
    snapshot = trusted_files.read_trusted_file(root, source, "fixture input")

    (inputs / "unrelated-receipt.json").write_text("{}\n", encoding="utf-8")

    trusted_files.require_trusted_file_unchanged(snapshot.binding, "fixture input")


def test_worker_readable_root_refuses_recreated_directory(
    tmp_path: Path,
) -> None:
    readable = tmp_path / "replay"
    readable.mkdir()
    (readable / "unit.py").write_text("pass\n", encoding="utf-8")
    writable = tmp_path / "worker"
    writable.mkdir()
    binding = trusted_files.bind_trusted_directory(
        tmp_path,
        readable,
        "fixture replay root",
    )
    retired = tmp_path / "replay-retired"
    readable.rename(retired)
    readable.mkdir()
    (readable / "unit.py").write_text("pass\n", encoding="utf-8")

    with pytest.raises(
        BlenderError,
        match="changed before worker launch",
    ), prepared_worker_command(
        ["blender"],
        writable_roots=(writable,),
        readable_roots=(readable,),
        readable_root_bindings=(binding,),
    ):
        pytest.fail("worker launch must not inherit a recreated replay root")
