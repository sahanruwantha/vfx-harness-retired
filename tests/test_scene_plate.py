"""The Higgsfield plate realizer, tested against a fake generator — no CLI, no ffmpeg."""

from __future__ import annotations

import asyncio
from pathlib import Path

from develop.ledger import BeatEntry, Intent, Mode, Realization
from scene import plate as plate_mod
from scene.plate import make_plate_generator, plate_prompt


def _beat(subject: str = "the Silk Road server-tower monolith") -> BeatEntry:
    entry = BeatEntry(
        id="beat-01",
        intent=Intent(function="cold open", subject=subject, evidence="[Doc 1]", heading=f"1. {subject}", index=0),
    )
    entry.mode = Mode("MOTION_GRAPHICS")
    entry.realization = Realization("A monolith of servers rises from the dark.")
    return entry


def _fake_jpeg(src, dst):
    Path(dst).write_bytes(b"\xff\xd8\xff\xe0jpg")
    return Path(dst)


def test_plate_prompt_has_subject_and_cinematic_style():
    prompt = plate_prompt("the Silk Road tower")
    assert "the Silk Road tower" in prompt
    assert "cinematic" in prompt and "no text" in prompt


def test_plate_generator_produces_clip_with_jpeg_frame(tmp_path, monkeypatch):
    seen: dict[str, object] = {}

    def fake_gen(prompt, dest, *, image_references=None, aspect_ratio=None, quality=None, **_kw):
        seen["prompt"] = prompt
        seen["aspect"] = aspect_ratio
        Path(dest).write_bytes(b"PNGDATA")
        return Path(dest)

    monkeypatch.setattr(plate_mod, "_to_jpeg", _fake_jpeg)
    gen = make_plate_generator(out_dir=tmp_path, generate_image=fake_gen)

    clip = asyncio.run(gen(_beat()))

    assert clip.licence == "KNOWN"
    assert clip.fetched_path is not None and str(clip.fetched_path).endswith(".jpg")
    assert clip.frames and clip.frames[0].jpeg_b64
    assert clip.render_meta["kind"] == "plate"
    assert "the Silk Road server-tower monolith" in str(seen["prompt"])
    assert seen["aspect"] == "16:9"


def test_plate_generation_failure_becomes_gap(tmp_path):
    def boom(prompt, dest, **_kw):
        raise RuntimeError("higgsfield unavailable")

    gen = make_plate_generator(out_dir=tmp_path, generate_image=boom)
    clip = asyncio.run(gen(_beat()))

    assert clip.frames == ()
    assert clip.acquisition_gap is not None
    assert "plate generation failed" in clip.acquisition_gap


def test_plate_is_cached_across_calls(tmp_path, monkeypatch):
    count = {"gen": 0}

    def fake_gen(prompt, dest, **_kw):
        count["gen"] += 1
        Path(dest).write_bytes(b"PNGDATA")
        return Path(dest)

    monkeypatch.setattr(plate_mod, "_to_jpeg", _fake_jpeg)
    gen = make_plate_generator(out_dir=tmp_path, generate_image=fake_gen)

    asyncio.run(gen(_beat("same subject")))
    asyncio.run(gen(_beat("same subject")))

    assert count["gen"] == 1  # the PNG was cached; generation ran once
