"""Canonical render capture binds actual worker settings to candidate bytes."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from vfx_harness.agents.builder.evidence import (
    RENDER_CAPTURE_SCHEMA,
    _stash_render_with_receipt,
)
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.ledger import Milestone


class _Session:
    def __init__(self, source) -> None:
        self.source = source

    def render_full(self, *, frame, mode, scale):
        return {
            "image_path": str(self.source),
            "frame": frame,
            "mode": mode,
            "resolution": [1920, 1080, int(scale * 100)],
            "render_state": {
                "engine": "BLENDER_WORKBENCH",
                "workbench_shading": "SOLID",
            },
            "warnings": ["fixture warning"],
        }


def test_stash_render_returns_hash_pinned_worker_receipt(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    run_artifacts.create(tmp_path, "canonical-render-capture")
    source = tmp_path / "source.png"
    source.write_bytes(b"canonical candidate bytes")
    milestone = Milestone("2", 40, "refs/hall.png", "hall")

    render_rel, receipt = _stash_render_with_receipt(
        _Session(source),
        SimpleNamespace(folder=tmp_path),
        milestone,
        "canonical",
        scale=0.5,
        mode="solid",
    )

    candidate = tmp_path / render_rel
    assert candidate.read_bytes() == source.read_bytes()
    assert receipt["schema"] == RENDER_CAPTURE_SCHEMA
    assert receipt["frame"] == 40
    assert receipt["mode"] == "solid"
    assert receipt["resolution"] == [1920, 1080, 50]
    assert receipt["png_sha256"] == hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert len(receipt["capture_digest"]) == 64


@pytest.mark.parametrize(
    "result",
    [
        {"frame": 41, "mode": "solid", "resolution": [1, 1, 50], "render_state": {}},
        {"frame": 40, "mode": "eevee", "resolution": [1, 1, 50], "render_state": {}},
        {"frame": 40, "mode": "solid", "resolution": [1, 1], "render_state": {}},
    ],
)
def test_stash_render_refuses_mismatched_or_incomplete_worker_receipt(
    tmp_path,
    monkeypatch,
    result,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    run_artifacts.create(tmp_path, "bad-canonical-render-capture")
    source = tmp_path / "source.png"
    source.write_bytes(b"candidate")
    result = {"image_path": str(source), "warnings": [], **result}
    session = SimpleNamespace(render_full=lambda **_kwargs: result)

    with pytest.raises(ValueError, match="receipt"):
        _stash_render_with_receipt(
            session,
            SimpleNamespace(folder=tmp_path),
            Milestone("2", 40, "refs/hall.png", "hall"),
            "canonical",
            mode="solid",
        )
