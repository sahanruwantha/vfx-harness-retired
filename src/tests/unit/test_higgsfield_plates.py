"""Higgsfield construction plates require a parent crop (HIR-0162)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from vfx_harness.assets import higgsfield, plates


def _grey_png(path: Path) -> Path:
    Image.new("RGB", (8, 8), (40, 40, 40)).save(path)
    return path


def _white_png(path: Path) -> Path:
    Image.new("RGB", (8, 8), (255, 255, 255)).save(path)
    return path


def test_relight_and_orbit_pass_parent_image_to_higgsfield(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, ...]] = []
    parent = _grey_png(tmp_path / "crop.png")

    def fake_cli(*args: str, timeout: float = 3600.0) -> list:
        captured.append(args)
        return [{"id": "job-1", "result_url": "https://example.test/plate.png"}]

    def fake_download(url: str, out: Path) -> Path:
        out.write_bytes(parent.read_bytes())
        return out

    monkeypatch.setattr(higgsfield, "run_cli", fake_cli)
    monkeypatch.setattr(higgsfield, "_download", fake_download)

    relit = plates.isolate_relight(parent, tmp_path / "relight.png")
    orbit = plates.orbit_view(parent, tmp_path / "right.png", camera="right")

    assert relit.is_file()
    assert orbit.is_file()
    assert len(captured) == 2
    for args in captured:
        assert "--image" in args
        assert str(parent) in args
        assert args[args.index("--image") + 1] == str(parent)
    orbit_prompt = captured[1][captured[1].index("--prompt") + 1]
    assert "right" in orbit_prompt


def test_orbit_refuses_unknown_camera_and_missing_parent(tmp_path: Path) -> None:
    parent = _grey_png(tmp_path / "crop.png")
    with pytest.raises(ValueError, match="orbit_view camera"):
        plates.orbit_view(parent, tmp_path / "out.png", camera="top-down")
    with pytest.raises(FileNotFoundError, match="parent plate not found"):
        plates.isolate_relight(tmp_path / "missing.png", tmp_path / "out.png")
    with pytest.raises(ValueError, match="parent plate"):
        higgsfield.edit_image([], "invent the object", tmp_path / "out.png")


def test_identity_gate_rejects_empty_white_plate(tmp_path: Path) -> None:
    parent = _grey_png(tmp_path / "crop.png")
    white = _white_png(tmp_path / "blank.png")
    gaps = plates.plate_identity_gaps(parent, white)
    assert gaps
    assert "empty white field" in gaps[0]
    assert plates.plate_identity_gaps(parent, parent) == ()
