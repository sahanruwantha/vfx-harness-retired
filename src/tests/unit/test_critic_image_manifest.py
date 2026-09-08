"""The critic describes the same complete image list that its transport attaches."""

import asyncio
from types import SimpleNamespace

import pytest

from vfx_harness.agents import build_prompts, critic_images
from vfx_harness.agents.builder import critic
from vfx_harness.blender.session import BlenderError
from vfx_harness.orchestration.ledger import Milestone


def panel(index):
    return {"id": f"detail-{index}", "image_rel": f"focus-{index}.png", "axis": "form",
            "crop": [0, 0, 0.5, 0.5], "source_frame": 7, "reference": "reference.png",
            "res_pct": 100, "reason": "inspect edge"}


@pytest.mark.parametrize("focus_count", [0, 1, 2])
def test_prompt_and_attachments_agree_when_focus_precedes_motion(tmp_path, monkeypatch, focus_count):
    panels = [panel(index) for index in range(focus_count)]
    paths = ["reference.png", "candidate.png", *(p["image_rel"] for p in panels), "motion.png", "prior.png"]
    for path in paths:
        (tmp_path / path).write_bytes(b"fixture")
    monkeypatch.setattr(critic, "_image_block", lambda path: {"type": "image", "path": path.name})
    shot = SimpleNamespace(folder=tmp_path, id="fixture")
    blocks = critic._critic_image_blocks(
        shot, paths[0], paths[1], focus_panels=panels, motion_rel="motion.png",
        motion_frames=[5, 7, 9], prior_rel="prior.png", prior_mean=3,
    )
    assert [block["path"] for block in blocks if block["type"] == "image"] == paths
    assert f"Image {3 + focus_count}: MOTION STRIP (motion.png)" in blocks[0]["text"]
    assert "PREVIOUS ATTEMPT — CONTEXT ONLY" in blocks[-2]["text"]
    assert "Do NOT score this image" in blocks[-2]["text"]
    motion_index = next(i for i, block in enumerate(blocks) if block.get("path") == "motion.png")
    assert blocks[motion_index - 1]["text"] == "MOTION STRIP, frames [5, 7, 9]"
    prompt = build_prompts.critic_prompt(
        shot, Milestone("surface", 7, paths[0], "form"), paths[1], [("form", "Visible form")],
        motion_rel="motion.png", motion_frames=[5, 7, 9], focus_panels=panels,
    )
    assert "image labelled MOTION STRIP" in prompt
    assert "THIRD" not in prompt


@pytest.mark.parametrize("missing", ["motion", "prior", "focus"])
def test_declared_missing_images_refuse_before_provider(tmp_path, monkeypatch, missing):
    for path in ("reference.png", "candidate.png", "focus-0.png", "motion.png", "prior.png"):
        (tmp_path / path).write_bytes(b"fixture")
    (tmp_path / {"motion": "motion.png", "prior": "prior.png", "focus": "focus-0.png"}[missing]).unlink()
    monkeypatch.setattr(critic, "_focus_references", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(critic, "_claim_context", lambda *_args, **_kwargs: ([], {}, set()))
    monkeypatch.setattr(critic, "query", lambda **_kwargs: pytest.fail("missing image reached provider"))
    monkeypatch.setattr(critic, "_image_block", lambda _path: pytest.fail("partial manifest was encoded"))
    shot = SimpleNamespace(folder=tmp_path, id="fixture", frontmatter={"type": "still"}, frames=9)
    with pytest.raises(BlenderError, match="missing"):
        asyncio.run(critic._critique(
            shot, Milestone("surface", 7, "reference.png", "form"), "candidate.png",
            [("form", "Visible form")], SimpleNamespace(), False,
            motion_evidence=("motion.png", [5, 7, 9]), prior_rel="prior.png",
            focus_panels=[panel(0)], selected_authority=SimpleNamespace(),
        ))


def test_extra_focus_panels_are_not_silently_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(critic, "_image_block", lambda _path: pytest.fail("oversized manifest was encoded"))
    with pytest.raises(ValueError, match="at most two"):
        critic._critic_image_blocks(SimpleNamespace(folder=tmp_path), "reference.png", "candidate.png",
                                    focus_panels=[panel(i) for i in range(3)])


@pytest.mark.parametrize("optional", ["motion_rel", "prior_rel"])
def test_empty_declared_optional_path_is_not_an_absent_image(tmp_path, optional):
    with pytest.raises(ValueError, match="explicit image path"):
        critic._critic_image_blocks(SimpleNamespace(folder=tmp_path), "reference.png", "candidate.png",
                                    **{optional: ""})


@pytest.mark.parametrize("path", [None, "", " ", 12])
def test_manifest_requires_explicit_paths(path):
    with pytest.raises(ValueError, match="explicit image path"):
        critic_images.compile_images((("reference", "reference.png"), ("candidate", path)))
