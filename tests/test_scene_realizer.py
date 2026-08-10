"""The render_3d realizer, tested against a fake bridge — no Blender.

Covers the SceneSpec adapter and the build → save → render → Clip path, including the failure
routes that must degrade to an acquisition gap rather than a bad clip.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from develop.ledger import BeatEntry, Intent, Realization
from scene.realizer import (
    SceneSpec,
    asset_prompt,
    make_reconstruction_builder,
    make_scene_builder,
    scene_spec_from_beat,
)


class FakeBridge:
    """Stands in for a started BlenderBridge with the four calls the realizer makes."""

    def __init__(self, *, build_ok: bool = True, image: str = "ZmFrZQ==") -> None:
        self.build_ok = build_ok
        self.image = image
        self.render_count = 0
        self.calls: list[str] = []

    def run_python(self, code: str) -> dict:
        self.calls.append("run_python")
        if not self.build_ok:
            return {"ok": False, "stdout": "", "error": "Traceback: boom"}
        return {"ok": True, "stdout": "", "result": {"objects": ["Ground", "Subject"]}}

    def save_blend(self, **params) -> dict:
        self.calls.append("save_blend")
        return {"path": params["path"], "bytes": 2048}

    def render(self, **params) -> dict:
        self.render_count += 1
        self.calls.append("render")
        return {
            "path": params["path"],
            "engine": "FAKE_EEVEE",
            "resolution": params.get("resolution"),
            "format": "JPEG",
            "bytes": 999,
            "image_b64": self.image,
        }

    def import_glb(self, **_params) -> dict:
        self.calls.append("import_glb")
        return {"objects": ["node"], "roots": ["node"], "mesh_count": 1}

    def normalize_asset(self, **_params) -> dict:
        self.calls.append("normalize_asset")
        return {"controller": "AssetCtrl", "scale_factor": 1.5, "bound_min": [-0.5, -0.5, 0.0], "bound_max": [0.5, 0.5, 1.5]}


def _beat(subject: str = "the shredder room") -> BeatEntry:
    entry = BeatEntry(
        id="beat-01",
        intent=Intent(
            function="reconstruct",
            subject=subject,
            evidence="filing [Doc 1]",
            heading="1. Reconstruction — the shredder room",
            index=0,
        ),
    )
    entry.realization = Realization("Inside the room where the documents were destroyed.")
    return entry


def test_spec_adapter_reads_refs_and_derives_duration(tmp_path) -> None:
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "photo1.jpg").write_bytes(b"x")
    (refs / "photo2.jpg").write_bytes(b"y")

    spec = scene_spec_from_beat(_beat(), refs_dir=refs)

    assert spec.subject == "the shredder room"
    assert spec.evidence == "filing [Doc 1]"
    assert len(spec.references) == 2
    assert spec.duration_s >= 10  # floored by estimate_seconds


def test_render_produces_clip_with_jpeg_frame(tmp_path) -> None:
    bridge = FakeBridge()
    render = make_scene_builder(bridge=bridge, out_dir=tmp_path)

    clip = asyncio.run(render(_beat()))

    assert clip.licence == "KNOWN"
    assert clip.fetched_path is not None
    assert len(clip.frames) == 1
    assert clip.frames[0].jpeg_b64 == "ZmFrZQ=="
    assert clip.blend_path is not None
    assert clip.render_meta["subject"] == "the shredder room"
    assert clip.render_meta["engine"] == "FAKE_EEVEE"
    assert bridge.calls == ["run_python", "save_blend", "render"]


def test_build_failure_becomes_acquisition_gap(tmp_path) -> None:
    bridge = FakeBridge(build_ok=False)
    render = make_scene_builder(bridge=bridge, out_dir=tmp_path)

    clip = asyncio.run(render(_beat()))

    assert clip.frames == ()
    assert clip.acquisition_gap is not None
    assert "the shredder room" in clip.acquisition_gap
    assert bridge.render_count == 0  # never rendered a failed build


def test_empty_render_becomes_gap_but_keeps_blend(tmp_path) -> None:
    bridge = FakeBridge(image="")  # render wrote nothing decodable
    render = make_scene_builder(bridge=bridge, out_dir=tmp_path)

    clip = asyncio.run(render(_beat()))

    assert clip.frames == ()
    assert clip.acquisition_gap is not None
    assert clip.blend_path is not None  # the .blend survived for a refine pass


# --- reconstruction builder (Higgsfield → Tripo → import → normalize) ----------------


def test_asset_prompt_mentions_subject_and_product_shot() -> None:
    spec = SceneSpec(beat_id="b", subject="a brass paper shredder", evidence="", vo="", references=(), duration_s=10)
    prompt = asset_prompt(spec)
    assert "a brass paper shredder" in prompt
    assert "product photograph" in prompt


def _fake_convert(image, dest, **_kw):
    Path(dest).write_bytes(b"glTF")
    return Path(dest)


def test_reconstruction_generates_imports_and_normalizes(tmp_path) -> None:
    bridge = FakeBridge()
    seen: dict[str, object] = {}

    def gen(prompt, dest, *, image_references=None, aspect_ratio=None, quality=None, **_kw):
        seen["prompt"] = prompt
        seen["refs"] = image_references
        Path(dest).write_bytes(b"png")
        return Path(dest)

    builder = make_reconstruction_builder(asset_dir=tmp_path / "assets", generate_image=gen, image_to_glb=_fake_convert)
    render = make_scene_builder(bridge=bridge, out_dir=tmp_path / "out", builder=builder)

    clip = asyncio.run(render(_beat("a brass paper shredder")))

    assert clip.frames  # a real render came out the far end
    assert clip.render_meta["build"] == "reconstruction"
    assert "a brass paper shredder" in str(seen["prompt"])
    assert "import_glb" in bridge.calls and "normalize_asset" in bridge.calls


def test_reconstruction_caches_asset_across_beats(tmp_path) -> None:
    bridge = FakeBridge()
    count = {"gen": 0}

    def gen(prompt, dest, **_kw):
        count["gen"] += 1
        Path(dest).write_bytes(b"png")
        return Path(dest)

    builder = make_reconstruction_builder(asset_dir=tmp_path / "assets", generate_image=gen, image_to_glb=_fake_convert)
    render = make_scene_builder(bridge=bridge, out_dir=tmp_path / "out", builder=builder)

    asyncio.run(render(_beat("the same chair")))
    asyncio.run(render(_beat("the same chair")))

    assert count["gen"] == 1  # second beat reused the cached GLB


def test_reconstruction_gen_failure_becomes_gap(tmp_path) -> None:
    bridge = FakeBridge()

    def gen(prompt, dest, **_kw):
        raise RuntimeError("higgsfield unavailable")

    builder = make_reconstruction_builder(asset_dir=tmp_path / "assets", generate_image=gen, image_to_glb=_fake_convert)
    render = make_scene_builder(bridge=bridge, out_dir=tmp_path / "out", builder=builder)

    clip = asyncio.run(render(_beat("a chair")))

    assert clip.frames == ()
    assert clip.acquisition_gap is not None
    assert "asset generation failed" in clip.acquisition_gap
    assert bridge.render_count == 0  # never rendered a failed build
