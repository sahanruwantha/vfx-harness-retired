"""Asset library + acquire_asset — cache/manifest + the generate→image→3D orchestration (no network)."""

from __future__ import annotations

import asyncio

from scene.assets import Asset, AssetLibrary, acquire_asset, asset_image_prompt


def test_asset_image_prompt_asks_for_a_clean_isolated_subject():
    p = asset_image_prompt("a brass typewriter")
    assert "a brass typewriter" in p and "SINGLE isolated object" in p and "neutral grey background" in p


def test_library_add_get_list_and_persist(tmp_path):
    lib = AssetLibrary(root=tmp_path)
    glb = tmp_path / "tower.glb"; glb.write_bytes(b"glTF")
    a = lib.add("a green tower", glb, kind="mesh")
    assert isinstance(a, Asset) and a.path == str(glb)
    assert lib.get("a green tower") is a or lib.get("a green tower").path == str(glb)  # cache hit
    assert lib.get("  A GREEN TOWER ") is not None  # key normalises case/space
    assert [x.description for x in lib.list()] == ["a green tower"]
    # a second instance reloads from the manifest on disk
    assert AssetLibrary(root=tmp_path).get("a green tower") is not None


def test_get_misses_when_the_file_is_gone(tmp_path):
    lib = AssetLibrary(root=tmp_path)
    glb = tmp_path / "x.glb"; glb.write_bytes(b"glTF")
    lib.add("a chair", glb)
    glb.unlink()  # asset file deleted
    assert lib.get("a chair") is None  # not returned if the file no longer exists


def test_key_varies_with_refs():
    assert AssetLibrary.key("a tower") != AssetLibrary.key("a tower", refs=["ref.jpg"])


def _fakes(tmp_path, calls):
    def generate_image(prompt, dest, image_references=None, **kw):
        calls.append(("gen", prompt, tuple(image_references or ())))
        from pathlib import Path
        Path(dest).write_bytes(b"PNG")
        return Path(dest)

    def image_to_glb(image_path, dest, **kw):
        calls.append(("glb", str(image_path)))
        from pathlib import Path
        Path(dest).write_bytes(b"glTF")
        return Path(dest)

    return generate_image, image_to_glb


def test_acquire_generates_then_caches(tmp_path):
    lib = AssetLibrary(root=tmp_path)
    calls: list = []
    gen, glb = _fakes(tmp_path, calls)

    a = asyncio.run(acquire_asset(description="a green Silk Road tower", library=lib,
                                  refs=["frame_00366.jpg"], generate_image=gen, image_to_glb=glb))
    assert a.kind == "mesh" and a.path.endswith(".glb")
    assert [c[0] for c in calls] == ["gen", "glb"]  # generated the image, then converted to 3D
    assert calls[0][2] == ("frame_00366.jpg",)  # reference conditioned the generation
    assert "SINGLE isolated object" in calls[0][1]  # the clean-subject prompt

    # a second acquire for the same description is a cache HIT — no regeneration
    calls.clear()
    b = asyncio.run(acquire_asset(description="a green Silk Road tower", library=lib,
                                  refs=["frame_00366.jpg"], generate_image=gen, image_to_glb=glb))
    assert b.key == a.key and calls == []  # served from the library, no gen/convert calls
