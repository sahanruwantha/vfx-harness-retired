"""Scene critic: verdict parsing, reference/render block assembly, and the seam adapter (no SDK)."""

from __future__ import annotations

import asyncio

from agents.scene_critic import SceneCritique, build_content, critique_scene, parse_score, parse_verdict
from develop.ledger import BeatEntry, Clip, Intent, Layer
from develop.verdict import Lever, Verdict
from footage.inspect import FrameSample
from scene import critic as scene_critic_mod
from scene.critic import load_reference_images, make_scene_critic


def _frames(n=1):
    return [FrameSample(timecode=f"00:0{i}", seconds=float(i), jpeg_b64="QUJD") for i in range(n)]


# --- parse_verdict ------------------------------------------------------------------


def test_parse_pass_has_no_lever_and_clamps_confidence():
    v = parse_verdict(
        '{"passed": true, "lever": null, "reason": "matches the reference", "confidence": 1.9,'
        ' "dimensions": {"composition": true, "lighting": true}}',
        "beat-01",
    )
    assert v.passed and v.lever is None
    assert v.layer is Layer.CLIP
    assert v.confidence == 1.0
    assert v.dimensions["composition"] is True


def test_parse_reject_maps_each_scene_lever():
    for name, lever in [
        ("re_source", Lever.RE_SOURCE),
        ("switch_to_mg", Lever.SWITCH_TO_MG),
        ("human_acquire", Lever.HUMAN_ACQUIRE),
    ]:
        v = parse_verdict(f'{{"passed": false, "lever": "{name}", "reason": "x"}}', "b")
        assert v.passed is False and v.lever is lever


def test_parse_unparseable_fails_safe_to_re_source():
    # a garbled scene critic should retry the render, not escalate — the opposite of footage's default
    v = parse_verdict("no json here at all", "b")
    assert v.passed is False and v.lever is Lever.RE_SOURCE


def test_parse_unknown_lever_coerced_to_re_source():
    v = parse_verdict('{"passed": false, "lever": "redesign_grammar", "reason": "x"}', "b")
    assert v.lever is Lever.RE_SOURCE


def test_parse_score_reads_and_clamps_match():
    assert parse_score('{"match": 0.62, "passed": false}') == 0.62
    assert parse_score('{"match": 1.7}') == 1.0  # clamped
    assert parse_score("no json") == 0.0  # default on failure
    assert parse_score('{"passed": true}') == 0.0  # missing match → default


# --- build_content ------------------------------------------------------------------


def test_build_content_has_reference_then_render_images():
    blocks = build_content(
        subject="a green server tower",
        reference_images=[("image/jpeg", "UkVG"), ("image/png", "UkVHMg==")],
        render_frames=_frames(1),
    )
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 3  # 2 reference + 1 render
    assert images[0]["source"]["media_type"] == "image/jpeg"
    assert images[1]["source"]["media_type"] == "image/png"
    assert images[2]["source"]["media_type"] == "image/jpeg"  # render is jpeg
    assert any("REFERENCE" in b.get("text", "") for b in blocks)
    assert any("RENDER" in b.get("text", "") for b in blocks)


# --- critique_scene early exits (no SDK call) ---------------------------------------


def test_critique_scene_no_frames_is_re_source():
    result = asyncio.run(critique_scene(beat_id="b", subject="s", reference_images=[("image/jpeg", "x")], render_frames=[]))
    assert result.outcome == "no_frames"
    assert result.verdict.lever is Lever.RE_SOURCE


def test_critique_scene_no_reference_passes_without_gating():
    result = asyncio.run(critique_scene(beat_id="b", subject="s", reference_images=[], render_frames=_frames(1)))
    assert result.outcome == "no_reference"
    assert result.verdict.passed is True


# --- load_reference_images ----------------------------------------------------------


def test_load_reference_images_reads_supported_types(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8jpg")
    (tmp_path / "b.png").write_bytes(b"\x89PNGpng")
    (tmp_path / "notes.txt").write_text("ignore me")
    refs = load_reference_images(tmp_path)
    assert [mt for mt, _ in refs] == ["image/jpeg", "image/png"]  # txt skipped, sorted
    assert load_reference_images(tmp_path / "missing") == []


# --- make_scene_critic adapter ------------------------------------------------------


def _beat_with_clip(frames) -> BeatEntry:
    entry = BeatEntry(
        id="beat-01",
        intent=Intent(function="reconstruct", subject="a tower", evidence="", heading="1. Tower", index=0),
    )
    entry.clip = Clip(frames=tuple(frames), licence="KNOWN")
    return entry


def test_adapter_skips_when_no_clip(tmp_path):
    critic = make_scene_critic(out_dir=tmp_path)
    entry = BeatEntry(id="beat-01", intent=Intent("f", "s", "", "h", 0))
    assert asyncio.run(critic(entry)) == []


def test_adapter_skips_when_no_reference(tmp_path):
    critic = make_scene_critic(out_dir=tmp_path)
    assert asyncio.run(critic(_beat_with_clip(_frames(1)))) == []  # no refs/ dir → no gating


def test_adapter_runs_critic_when_reference_present(tmp_path, monkeypatch):
    refs = tmp_path / "beat-01" / "refs"
    refs.mkdir(parents=True)
    (refs / "target.jpg").write_bytes(b"\xff\xd8ref")

    async def fake_critique(*, beat_id, subject, reference_images, render_frames, on_message=None):
        assert reference_images and render_frames
        return SceneCritique(
            verdict=Verdict(beat=beat_id, layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE, reason="off"),
            outcome="complete", stop_reason="ok", turns=1, cost_usd=0.0,
        )

    monkeypatch.setattr(scene_critic_mod, "critique_scene", fake_critique)
    verdicts = asyncio.run(make_scene_critic(out_dir=tmp_path)(_beat_with_clip(_frames(1))))
    assert len(verdicts) == 1 and verdicts[0].lever is Lever.RE_SOURCE
