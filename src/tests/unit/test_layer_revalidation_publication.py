"""Runtime-check revalidation stages content before its short authority commit."""

from __future__ import annotations

import json
import os
import stat

import pytest

from vfx_harness.evidence import checks, layer_revalidation_projection


def _builder_check() -> dict:
    return {
        "id": "builder-check",
        "layer": "1",
        "frame": 1,
        "metric": "mean",
        "region": [0.0, 0.0, 1.0, 1.0],
        "op": ">=",
        "lo": 1.0,
        "origin": "builder",
    }


def test_guarded_revalidation_commit_is_metadata_only(tmp_path, monkeypatch) -> None:
    spec = tmp_path / "runtime_checks.json"
    spec.write_text(json.dumps([_builder_check()]) + "\n", encoding="utf-8")
    prepared = checks.prepare_layer_revalidation(tmp_path, "1", lambda _check: None)
    assert prepared.replacement_path is not None
    assert prepared.replacement_path.is_file()
    original_fsync = checks.os.fsync
    fsync_kinds: list[str] = []

    def metadata_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsync_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        assert stat.S_ISDIR(mode), "guarded commit performed regular-file fsync"
        original_fsync(descriptor)

    def unexpected_hash(*_args, **_kwargs):
        raise AssertionError("guarded commit rehashed runtime-check bytes")

    monkeypatch.setattr(checks.os, "fsync", metadata_fsync)
    monkeypatch.setattr(checks.hashlib, "sha256", unexpected_hash)
    try:
        result = checks.commit_layer_revalidation(prepared)
    finally:
        checks.discard_layer_revalidation(prepared)

    assert result == {
        "kept": 0,
        "dropped": [
            ("builder-check", "missing payment schema vfx-harness.image-payment/v2")
        ],
    }
    assert json.loads(spec.read_text(encoding="utf-8")) == []
    assert fsync_kinds == ["directory"]


def test_revalidation_commit_refuses_changed_source_and_discards_stage(tmp_path) -> None:
    spec = tmp_path / "runtime_checks.json"
    spec.write_text(json.dumps([_builder_check()]) + "\n", encoding="utf-8")
    prepared = checks.prepare_layer_revalidation(tmp_path, "1", lambda _check: None)
    staged = prepared.replacement_path
    assert staged is not None
    spec.write_text("[]\n", encoding="utf-8")

    try:
        with pytest.raises(ValueError, match="changed after revalidation"):
            checks.commit_layer_revalidation(prepared)
    finally:
        checks.discard_layer_revalidation(prepared)

    assert not staged.exists()
    assert spec.read_text(encoding="utf-8") == "[]\n"


def test_terminal_revalidation_projection_reconciles_exact_bytes_once(tmp_path) -> None:
    spec = tmp_path / "runtime_checks.json"
    spec.write_text(json.dumps([_builder_check()]) + "\n", encoding="utf-8")
    prepared = checks.prepare_layer_revalidation(tmp_path, "1", lambda _check: None)
    projection = layer_revalidation_projection.layer_revalidation_projection(prepared)
    checks.discard_layer_revalidation(prepared)

    first = layer_revalidation_projection.reconcile_layer_revalidation_projection(
        tmp_path,
        "1",
        projection,
    )
    first_bytes = spec.read_bytes()
    second = layer_revalidation_projection.reconcile_layer_revalidation_projection(
        tmp_path,
        "1",
        projection,
    )

    assert first == second
    assert json.loads(first_bytes) == []
    assert spec.read_bytes() == first_bytes


def test_terminal_revalidation_projection_refuses_external_source_change(tmp_path) -> None:
    spec = tmp_path / "runtime_checks.json"
    spec.write_text(json.dumps([_builder_check()]) + "\n", encoding="utf-8")
    prepared = checks.prepare_layer_revalidation(tmp_path, "1", lambda _check: None)
    projection = layer_revalidation_projection.layer_revalidation_projection(prepared)
    checks.discard_layer_revalidation(prepared)
    spec.write_text('[{"foreign":true}]\n', encoding="utf-8")

    with pytest.raises(ValueError, match="changed outside"):
        layer_revalidation_projection.reconcile_layer_revalidation_projection(
            tmp_path,
            "1",
            projection,
        )
