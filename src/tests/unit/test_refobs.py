"""Minted generate witnesses are crops in shot-root state/, not whole frames."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from vfx_harness.domain.refobs import UNREGISTERED_WITNESS_RULE, WHOLE_FRAME_RULE
from vfx_harness.orchestration.refobs import (
    load_witness_crop,
    mint_refobs,
    missing_witness_ids,
    refobs_dir,
)


def _still(folder: Path, *, size: int = 64) -> Path:
    refs = folder / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    path = refs / "still.png"
    Image.new("RGB", (size, size), (40, 80, 120)).save(path)
    return path


def test_mint_refobs_persists_crop_and_is_stable(tmp_path: Path) -> None:
    still = _still(tmp_path)
    box = [0.1, 0.15, 0.4, 0.55]
    token = mint_refobs(tmp_path, still, box, source_rel="refs/still.png")

    assert token.startswith("refobs-")
    record = refobs_dir(tmp_path) / f"{token}.json"
    crop = refobs_dir(tmp_path) / f"{token}.png"
    assert record.is_file()
    assert crop.is_file()
    assert crop.stat().st_size > 0
    payload = record.read_text(encoding="utf-8")
    assert "refs/still.png" in payload
    assert "vfx-harness.refobs/v1" in payload

    again = mint_refobs(tmp_path, still, box, source_rel="refs/still.png")
    assert again == token
    assert missing_witness_ids(tmp_path, (token,)) == ()
    assert load_witness_crop(tmp_path, token) == crop


def test_mint_refobs_refuses_whole_frame_and_non_refs_source(tmp_path: Path) -> None:
    still = _still(tmp_path)
    with pytest.raises(ValueError, match="full frame"):
        mint_refobs(tmp_path, still, [0.0, 0.0, 1.0, 1.0], source_rel="refs/still.png")
    with pytest.raises(ValueError, match=WHOLE_FRAME_RULE[:24]):
        mint_refobs(tmp_path, still, [0.01, 0.01, 0.99, 0.99], source_rel="refs/still.png")

    other = tmp_path / "scratch" / "plate.png"
    other.parent.mkdir()
    Image.new("RGB", (32, 32), (10, 10, 10)).save(other)
    with pytest.raises(ValueError, match="refs/"):
        mint_refobs(tmp_path, other, [0.1, 0.1, 0.4, 0.4], source_rel="scratch/plate.png")


def test_unregistered_witness_is_named(tmp_path: Path) -> None:
    missing = missing_witness_ids(tmp_path, ("refobs-deadbeef",))
    assert missing == ("refobs-deadbeef",)
    with pytest.raises(FileNotFoundError, match=UNREGISTERED_WITNESS_RULE[:24]):
        load_witness_crop(tmp_path, "refobs-deadbeef")
