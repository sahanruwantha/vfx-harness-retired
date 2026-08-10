"""build_animation loop + motion critic parsing — fakes, no SDK/Blender."""

from __future__ import annotations

import asyncio

from agents.motion_critic import MotionCritique, parse_score, parse_verdict
from agents.scene_builder import DeskResult
from develop.ledger import BeatEntry, Intent, Layer, Realization
from develop.verdict import Lever, Verdict
from footage.inspect import FrameSample
from scene.animate import (
    AnimationResult,
    _strip_frame_numbers,
    beat_motion_brief,
    build_animation,
    make_animation_realizer,
)
from scene.harness import Iteration


# --- motion critic parsing ----------------------------------------------------------


def test_parse_verdict_pass_and_reject():
    v = parse_verdict('{"passed": true, "lever": null, "dimensions": {"start_pose": true}}', "b")
    assert v.passed and v.lever is None
    r = parse_verdict('{"passed": false, "lever": "re_source", "reason": "no inversion"}', "b")
    assert not r.passed and r.lever is Lever.RE_SOURCE and r.reason == "no inversion"


def test_parse_unparseable_is_re_source():
    assert parse_verdict("garbage", "b").lever is Lever.RE_SOURCE


def test_parse_score_reads_match():
    assert parse_score('{"match": 0.7}') == 0.7
    assert parse_score("no json") == 0.0


def test_strip_frames_are_first_middle_last():
    assert _strip_frame_numbers(36) == [1, 18, 36]
    assert _strip_frame_numbers(4) == [1, 2, 4]


# --- the loop -----------------------------------------------------------------------


class FakeBridge:
    def __init__(self, *, start_luma=0.4):
        self.start_luma = start_luma
        self.runs = 0
        self.sequences = 0
        self.saved: list[str] = []
        self.opened: list[str] = []

    def run_python(self, code):
        if '"reset"' in code or "frame_set" in code:
            return {"ok": True, "result": {"reset": True}}
        self.runs += 1
        return {"ok": True, "result": {}}

    def render(self, **p):
        return {"path": p["path"], "bytes": 100, "image_b64": "SU1H"}

    def image_stats(self, **_p):
        return {"luma_mean": self.start_luma, "luma_std": 0.2, "mean_rgb": [0.4, 0.4, 0.4], "hist8": [0.125] * 8}

    def get_scene_graph(self):
        return {"scene": {"active_camera": "cam", "engine": "EEVEE"}, "objects": [{"type": "CAMERA"}]}

    def save_blend(self, **p):
        self.saved.append(p["path"])
        return {"path": p["path"], "bytes": 10}

    def open_blend(self, **p):
        self.opened.append(p["path"])
        return {"opened": p["path"], "objects": 3}

    def render_sequence(self, **p):
        self.sequences += 1
        return {"paths": [], "count": p["end"], "dir": p["dir"]}


def _builder(notes, *, session="sess"):
    calls = []

    async def builder(*, bridge, brief, reference_images, feedback=None, resume=None, animation_frames=None,
                      fresh=True, on_message=None, context=None, lessons=None, preview_engine=None, asset_library=None):
        calls.append({"feedback": feedback, "resume": resume, "fresh": fresh, "animation_frames": animation_frames, "lessons": lessons})
        note = notes[min(len(calls) - 1, len(notes) - 1)]
        return DeskResult(
            submitted=True, note=note, session_id=session, outcome="complete",
            stop_reason="ok", turns=1, cost_usd=0.0, bpy_log=("import bpy",),
        )

    return builder, calls


def _critic(scores):
    state = {"n": 0}

    async def critic(**_kw):
        s = scores[min(state["n"], len(scores) - 1)]
        state["n"] += 1
        passed = s >= 0.99
        v = Verdict(beat="shot", layer=Layer.CLIP, passed=passed, lever=None if passed else Lever.RE_SOURCE,
                    reason="r", dimensions={"start_pose": True, "transition": s > 0.5, "end_pose": s > 0.6, "continuity": True})
        return MotionCritique(verdict=v, outcome="complete", score=s, cost_usd=0.0)

    return critic


def test_loop_passes_animation_frames_and_renders_final(tmp_path):
    builder, calls = _builder(["note0", "note1"])
    critic = _critic([0.5, 0.85])  # second pass clears accept_score
    bridge = FakeBridge()
    result = asyncio.run(build_animation(
        bridge=bridge, brief="a barrel roll", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, frames=36, builder=builder, critic=critic, max_iters=4, accept_score=0.8,
    ))
    assert result.converged and result.best.score == 0.85
    assert len(result.iterations) == 2
    assert calls[0]["animation_frames"] == 36  # the desk was told to animate
    assert calls[1]["resume"] == "sess"  # round 1 resumed the same desk session
    assert bridge.sequences == 1  # the full sequence rendered once
    assert result.final_dir is not None
    # each round snapshotted; the final render reopened the BEST round's snapshot (round 1)
    assert len(bridge.saved) == 2
    assert bridge.opened and bridge.opened[-1].endswith("round_01.blend")


def test_final_renders_from_the_best_round_not_the_last(tmp_path):
    # round 0 is the best (0.7); round 1 regresses (0.5) and never converges → ship round 0
    builder, _ = _builder(["note0", "note1"])
    critic = _critic([0.7, 0.5])
    bridge = FakeBridge()
    result = asyncio.run(build_animation(
        bridge=bridge, brief="b", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, frames=12, builder=builder, critic=critic, max_iters=2, accept_score=0.8,
    ))
    assert not result.converged and result.best.index == 0
    assert bridge.opened[-1].endswith("round_00.blend")  # reopened the best round, not the last
    assert result.final_dir is not None


def test_black_start_skips_critic(tmp_path):
    builder, calls = _builder(["dark", "fixed"])
    critic_calls = {"n": 0}

    async def counting_critic(**_kw):
        critic_calls["n"] += 1
        v = Verdict(beat="shot", layer=Layer.CLIP, passed=True, dimensions={})
        return MotionCritique(verdict=v, outcome="complete", score=0.9, cost_usd=0.0)

    bridge = FakeBridge(start_luma=0.0)  # black start
    original = bridge.image_stats

    def recover(**p):
        s = original(**p)
        bridge.start_luma = 0.5
        return s

    bridge.image_stats = recover  # type: ignore[method-assign]
    result = asyncio.run(build_animation(
        bridge=bridge, brief="b", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, frames=12, builder=builder, critic=counting_critic, max_iters=3,
    ))
    assert critic_calls["n"] == 1  # black start did not call the motion critic
    assert "BLACK" in calls[1]["feedback"]


# --- develop realizer ---------------------------------------------------------------


def _anim_beat() -> BeatEntry:
    entry = BeatEntry(id="beat-01", intent=Intent("reconstruct", "a dark server room", "[Doc 1]", "1. Server room", 0))
    entry.realization = Realization("the servers hum in the dark")
    return entry


def test_beat_motion_brief_asks_for_a_moving_shot():
    brief = beat_motion_brief("a server room", "the servers hum", "[Doc 1]")
    assert "a server room" in brief
    assert "MOVING" in brief and "camera move" in brief
    assert "the servers hum" in brief


def test_animation_realizer_builds_clip_from_sequence(tmp_path):
    final = tmp_path / "beat-01" / "final"
    final.mkdir(parents=True)
    for f in (1, 18, 36):
        (final / f"final_{f:04d}.jpg").write_bytes(b"\xff\xd8jpg")

    async def fake_build(*, bridge, brief, reference_images, subject, out_dir, frames, max_iters, resolution, samples):
        assert "MOVING" in brief  # the beat was turned into a motion brief
        it = Iteration(0, "code", str(final / "final_0001.jpg"), FrameSample("f0001", 1.0, "x"),
                       Verdict(beat="shot", layer=Layer.CLIP, passed=True), score=0.83)
        return AnimationResult(iterations=[it], frames=36, final_dir=str(final), accept_score=0.75)

    realizer = make_animation_realizer(bridge=object(), out_dir=tmp_path, frames=36, _build=fake_build)
    clip = asyncio.run(realizer(_anim_beat()))

    assert clip.licence == "KNOWN"
    assert len(clip.frames) == 3  # first/middle/last sampled from the sequence
    assert clip.render_meta["kind"] == "animation"
    assert clip.render_meta["motion_score"] == "0.83"
    assert str(clip.fetched_path).endswith("final")


def test_animation_realizer_failure_becomes_gap(tmp_path):
    async def fake_build(**_kw):
        return AnimationResult(iterations=[], frames=36, final_dir=None, accept_score=0.75)

    realizer = make_animation_realizer(bridge=object(), out_dir=tmp_path, frames=36, _build=fake_build)
    clip = asyncio.run(realizer(_anim_beat()))
    assert clip.frames == ()
    assert clip.acquisition_gap is not None and "server room" in clip.acquisition_gap
