"""Hybrid composite code-gen — pure (no Blender)."""

from __future__ import annotations

from scene.hybrid import hybrid_stage_code, plate_backdrop_code


def test_stage_sets_transparent_film_shadow_ground_and_camera():
    code = hybrid_stage_code(8.0)
    assert "film_transparent = True" in code
    assert "is_shadow_catcher = True" in code  # subject sits in the plate, not floating
    assert "scene.camera = cam" in code
    assert "primitive_plane_add" in code  # the shadow ground


def test_stage_frames_taller_subjects_further_back():
    near = hybrid_stage_code(2.0)
    far = hybrid_stage_code(10.0)
    # camera y is negative and larger-magnitude for a taller subject (pulled back to frame it)
    y_near = float(near.split("camera_add(location=(0, ", 1)[1].split(",", 1)[0])
    y_far = float(far.split("camera_add(location=(0, ", 1)[1].split(",", 1)[0])
    assert y_far < y_near < 0


def test_plate_backdrop_is_a_camera_locked_emissive_plane():
    code = plate_backdrop_code("/tmp/plate.jpg", distance=100.0)
    assert "ShaderNodeEmission" in code and "ShaderNodeTexImage" in code  # unlit plate
    assert "plane.parent = cam" in code  # camera-locked billboard
    assert "primitive_plane_add" in code
    assert "bpy.data.images.load('/tmp/plate.jpg'" in code
    assert "film_transparent = False" in code  # real geometry, not a compositor comp


# --- hybrid realizer (plate backdrop + desk-built subject) — fakes, no Blender ----------

import asyncio

from develop.ledger import BeatEntry, Clip, Intent
from footage.inspect import FrameSample
from scene.harness import Iteration
from scene.hybrid import hybrid_subject_brief, make_hybrid_realizer


def _beat():
    return BeatEntry(id="b1", intent=Intent("reconstruct", "a lone tower", "[Doc 1]", "1. Tower", 0))


class _FakeBridge:
    def __init__(self):
        self.code_seen = []

    def run_python(self, code):
        self.code_seen.append(code)
        return {"ok": True, "result": {}}


def _plate_ok(path):
    async def gen(entry):
        return Clip(fetched_path=path, licence="KNOWN", render_meta={"kind": "plate"})
    return gen


def _plate_fail():
    async def gen(entry):
        return Clip(licence="KNOWN", acquisition_gap="higgsfield out of credits")
    return gen


def test_hybrid_subject_brief_keeps_the_plate_backdrop():
    b = hybrid_subject_brief("a tower")
    assert "SUBJECT" in b and "Backdrop" in b and "do NOT build your own sky" in b


def test_hybrid_realizer_sets_backdrop_then_builds_subject_over_it(tmp_path):
    bridge = _FakeBridge()
    plate = tmp_path / "plate.png"; plate.write_bytes(b"\x89PNG")
    captured = {}

    async def fake_build(*, bridge, brief, reference_images, subject, out_dir, prepare, **kw):
        prepare()  # the realizer's prepare wires camera + backdrop
        captured["brief"] = brief
        it = Iteration(0, "", str(tmp_path / "r0.jpg"), FrameSample("00:00", 0.0, "SU1H"),
                       verdict=None, score=0.71)
        from scene.harness import ShotResult
        return ShotResult(iterations=[it])

    realizer = make_hybrid_realizer(bridge=bridge, out_dir=tmp_path, plate_generator=_plate_ok(plate), build=fake_build)
    clip = asyncio.run(realizer(_beat()))

    assert clip.render_meta["kind"] == "hybrid" and clip.render_meta["score"] == "0.71"
    assert str(plate) in clip.render_meta["plate"]
    assert "SUBJECT" in captured["brief"]  # the subject brief reached the desk
    # prepare wired the hero camera then the plate backdrop, in order
    assert any("scene.camera = cam" in c for c in bridge.code_seen)
    assert any('bpy.data.images.load' in c and str(plate) in c for c in bridge.code_seen)


def test_hybrid_realizer_gaps_when_the_plate_is_unavailable(tmp_path):
    realizer = make_hybrid_realizer(bridge=_FakeBridge(), out_dir=tmp_path, plate_generator=_plate_fail(),
                                    build=None)
    clip = asyncio.run(realizer(_beat()))
    assert clip.frames == () and clip.acquisition_gap and "plate unavailable" in clip.acquisition_gap
